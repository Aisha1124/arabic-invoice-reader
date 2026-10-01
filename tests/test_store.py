import hashlib
import json
import re
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app import store
from app.schema import CallMetadata, ExtractionResult, Invoice, LineItem
from app.store import AuditRow, audit_row, read_last, write_audit
from app.validate import validate

SELLER_NAME = "شركة الخليج للمعدات الطبية"
SELLER_VAT = (
    "3158397998898"  # 13 digits: triggers the format warning, quoted in its message
)
BUYER_NAME = "Al Amanah Trading Est"
IMAGE = b"\x89PNG\r\n\x1a\nfake image bytes"
SHA = hashlib.sha256(IMAGE).hexdigest()


def _metadata(**overrides: object) -> CallMetadata:
    values: dict[str, object] = {
        "model": "test-model",
        "prompt_version": "v3",
        "latency_ms": 1234,
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "estimated_cost_usd": Decimal("0.0125"),
        "cache_hit": False,
        "temperature_zero": True,
    }
    values.update(overrides)
    return CallMetadata(**values)


def _result() -> ExtractionResult:
    """A result whose findings quote content: a bad VAT number and a wrong total."""
    invoice = Invoice(
        invoice_number="INV-2026-1000",
        invoice_date=date(2026, 7, 31),
        invoice_timestamp=datetime(2026, 7, 31, 15, 38),  # noqa: DTZ001
        invoice_type="standard",
        seller_name=SELLER_NAME,
        seller_vat_number=SELLER_VAT,
        buyer_name=BUYER_NAME,
        buyer_vat_number=None,
        line_items=[
            LineItem(
                description="استشارة هندسية",
                quantity="9",
                unit_price="4157.37",
                line_total="37416.33",
                vat_rate="0.15",
                vat_amount="5612.45",
            )
        ],
        subtotal="37416.33",
        vat_total="5612.45",
        total="99999.99",
    )
    return validate(invoice, {"total": 1.0, "seller_vat_number": 1.0})


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "app.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return tmp_path / "data" / "app.db"


def test_audit_row_counts_and_findings() -> None:
    result = _result()
    assert {f.rule for f in result.findings} == {
        "total_equals_subtotal_plus_vat_total",
        "seller_vat_number_is_15_digits",
        "standard_invoice_has_buyer_vat_number",
    }

    row = audit_row(SHA, result, _metadata())

    # 12 top-level fields minus the null buyer_vat_number, plus 6 per line item.
    assert row.fields_extracted == 11 + 6
    # subtotal, vat_total, total, seller_vat_number, buyer_vat_number
    assert row.fields_flagged == 5
    findings = json.loads(row.validation_findings)
    assert {f["rule"] for f in findings} == {f.rule for f in result.findings}
    assert all(set(f) == {"rule", "severity", "fields"} for f in findings)
    assert row.estimated_cost_usd == "0.0125"
    assert row.model == "test-model"
    assert row.prompt_version == "v3"
    assert row.latency_ms == 1234
    assert row.temperature_zero is True
    assert row.cache_hit is False
    assert re.fullmatch(r"[0-9a-f-]{36}", row.id)
    assert row.timestamp_utc.endswith("+00:00")


def test_audit_row_carries_no_invoice_content() -> None:
    result = _result()
    # The finding messages do quote content; that is exactly what must not be stored.
    assert any(SELLER_VAT in f.message for f in result.findings)
    assert any("99999.99" in f.message for f in result.findings)

    serialised = json.dumps(
        audit_row(SHA, result, _metadata()).__dict__, ensure_ascii=False
    )

    for secret in (
        SELLER_NAME,
        SELLER_VAT,
        BUYER_NAME,
        "INV-2026-1000",
        "استشارة",
        "4157.37",
        "37416.33",
        "5612.45",
        "99999.99",
        "2026-07-31",
    ):
        assert secret not in serialised, secret


def test_audit_row_rejects_non_hash_identifier() -> None:
    with pytest.raises(ValueError, match="64 lowercase hex"):
        audit_row("INV-2026-1000.png", _result(), _metadata())
    with pytest.raises(ValueError, match="64 lowercase hex"):
        audit_row(SHA.upper(), _result(), _metadata())


def test_write_then_read_back(db: Path) -> None:
    # Timestamps are set explicitly: two rows written in the same instant would
    # otherwise be ordered by their random ids.
    first = replace(
        audit_row(SHA, _result(), _metadata()),
        timestamp_utc="2026-09-19T10:00:00.000001+00:00",
    )
    second = replace(
        audit_row(SHA, _result(), _metadata(cache_hit=True, estimated_cost_usd=None)),
        timestamp_utc="2026-09-19T10:00:00.000002+00:00",
    )
    write_audit(first)
    write_audit(second)

    rows = read_last(10)

    assert db.is_file()
    assert [r.id for r in rows] == [second.id, first.id]
    assert rows[0] == second
    assert rows[1] == first
    assert rows[0].cache_hit is True
    assert rows[0].estimated_cost_usd is None
    assert read_last(1) == [second]


def test_read_last_rejects_zero() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        read_last(0)


def test_database_has_only_the_audit_columns(db: Path) -> None:
    import sqlite3

    write_audit(audit_row(SHA, _result(), _metadata()))
    with sqlite3.connect(db) as conn:
        columns = [c[1] for c in conn.execute("PRAGMA table_info(audit_log)")]
        dump = "\n".join(conn.iterdump())
    assert columns == list(store.COLUMNS)
    assert SELLER_VAT not in dump
    assert SELLER_NAME not in dump


def test_store_module_has_no_update_or_delete() -> None:
    source = Path(store.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b(UPDATE|DELETE|DROP|TRUNCATE)\b", source, re.IGNORECASE)


def test_missing_postgres_driver_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    monkeypatch.setitem(__import__("sys").modules, "psycopg", None)
    with pytest.raises(RuntimeError, match="psycopg package is not installed"):
        read_last(1)


def test_audit_row_is_frozen() -> None:
    row = audit_row(SHA, _result(), _metadata())
    with pytest.raises(AttributeError):
        row.model = "other"  # type: ignore[misc]
    assert isinstance(row, AuditRow)


def test_audit_row_carries_no_computed_check_values() -> None:
    result = _result()
    grand = next(c for c in result.checks if c.rule.startswith("total_equals"))
    assert (grand.computed, grand.difference) == (
        Decimal("43028.78"),
        Decimal("56971.21"),
    )

    serialised = json.dumps(audit_row(SHA, result, _metadata()).__dict__)

    for amount in ("43028.78", "56971.21", "5612.4495"):
        assert amount not in serialised, amount
    assert "computed" not in serialised and "difference" not in serialised
