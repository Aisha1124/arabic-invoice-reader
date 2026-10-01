import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app import review, store
from app.resolve import resolve
from app.schema import Invoice, LineItem, Reading, Resolution
from app.store import read_resolver_events
from app.validate import validate

SELLER_NAME = "شركة الخليج للمعدات الطبية"
SELLER_VAT = "315839799889833"
SHA = hashlib.sha256(b"image").hexdigest()
AUDIT_ID = "5b0f3a9e-0000-4000-8000-000000000001"


def _invoice(unit_price: str = "12.43", total: str = "46.33") -> Invoice:
    """3 x 13.43 = 40.29, with the unit price read as 12.43 (٣→٢) by default."""
    return Invoice(
        invoice_type="standard",
        seller_name=SELLER_NAME,
        seller_vat_number=SELLER_VAT,
        line_items=[
            LineItem(
                description="استشارة",
                quantity="3",
                unit_price=unit_price,
                line_total="40.29",
                vat_rate="0.15",
                vat_amount="6.04",
            )
        ],
        subtotal="40.29",
        vat_total="6.04",
        total=total,
    )


# The check outcomes validate.py produced for the default misread.
CHECKS = validate(_invoice(), {}).checks


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "app.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return tmp_path / "data" / "app.db"


def _dump(db: Path) -> str:
    with sqlite3.connect(db) as conn:
        return "\n".join(conn.iterdump())


def test_suggestion_is_queued_with_its_values() -> None:
    queue_id = review.submit(AUDIT_ID, SHA, resolve(_invoice()), CHECKS).id

    (item,) = review.pending()
    assert item.id == queue_id
    assert item.status == "suggested"
    assert item.audit_id == AUDIT_ID
    top = item.candidates[0]
    assert (top.field, top.read_value, top.value) == (
        "line_items[0].unit_price",
        Decimal("12.43"),
        Decimal("13.43"),
    )


def test_submit_logs_a_content_free_event() -> None:
    review.submit(AUDIT_ID, SHA, resolve(_invoice()), CHECKS)

    (event,) = read_resolver_events(10)
    assert event.event == "suggested"
    assert event.audit_id == AUDIT_ID
    assert event.field == "line_items[0].unit_price"
    assert event.edit == "known_substitution"
    assert event.candidates == len(resolve(_invoice()).candidates)
    assert "12.43" not in json.dumps(event.__dict__)
    assert "13.43" not in json.dumps(event.__dict__)


def test_unresolvable_is_queued_with_the_values_involved() -> None:
    two_errors = _invoice(total="99.99")
    resolution = resolve(two_errors)
    assert resolution.status == "unresolvable"

    queue_id = review.submit(AUDIT_ID, SHA, resolution, CHECKS).id

    (item,) = review.pending()
    assert item.id == queue_id
    assert item.status == "unresolvable"
    assert item.candidates == []
    involved = {r.field: r.read_value for r in item.involved}
    assert involved["line_items[0].unit_price"] == Decimal("12.43")
    assert involved["total"] == Decimal("99.99")
    (event,) = read_resolver_events(10)
    assert (event.event, event.field, event.candidates) == ("unresolvable", None, 0)


def test_unresolvable_is_closed_as_checked_manually_only() -> None:
    queue_id = review.submit(AUDIT_ID, SHA, resolve(_invoice(total="99.99")), CHECKS).id
    assert queue_id is not None

    for decision in ("accepted", "rejected"):
        with pytest.raises(ValueError, match="checked_manually"):
            review.decide(
                queue_id, decision, rank=1 if decision == "accepted" else None
            )

    review.decide(queue_id, "checked_manually")
    assert review.pending() == []
    assert read_resolver_events(1)[0].event == "checked_manually"


def test_suggestion_cannot_be_closed_as_checked_manually() -> None:
    queue_id = review.submit(AUDIT_ID, SHA, resolve(_invoice()), CHECKS).id
    assert queue_id is not None
    with pytest.raises(ValueError, match="accepted or rejected"):
        review.decide(queue_id, "checked_manually")


def test_references_are_sequential_and_never_reused() -> None:
    first = review.submit(AUDIT_ID, SHA, resolve(_invoice()), CHECKS).id
    assert first is not None
    review.decide(first, "rejected")
    other_sha = hashlib.sha256(b"other image").hexdigest()
    review.submit(AUDIT_ID, other_sha, resolve(_invoice()), CHECKS)

    (item,) = review.pending()
    assert item.reference == "R-0002"


def test_resubmitting_a_pending_image_adds_nothing() -> None:
    first = review.submit(AUDIT_ID, SHA, resolve(_invoice()), CHECKS).id
    again = review.submit("another-audit-id", SHA, resolve(_invoice()), CHECKS).id

    assert again == first
    assert len(review.pending()) == 1
    assert len(read_resolver_events(10)) == 1


def test_not_needed_is_rejected() -> None:
    resolution = resolve(_invoice(unit_price="13.43"))
    assert resolution.status == "not_needed"
    with pytest.raises(ValueError, match="not_needed"):
        review.submit(AUDIT_ID, SHA, resolution, CHECKS)


def test_accept_logs_the_decision_and_deletes_the_queue_row(db: Path) -> None:
    queue_id = review.submit(AUDIT_ID, SHA, resolve(_invoice()), CHECKS).id
    assert queue_id is not None

    review.decide(queue_id, "accepted", rank=1)

    assert review.pending() == []
    decided, submitted = read_resolver_events(10)
    assert (submitted.event, decided.event) == ("suggested", "accepted")
    assert decided.queue_id == queue_id
    assert (decided.field, decided.rank, decided.edit) == (
        "line_items[0].unit_price",
        1,
        "known_substitution",
    )
    dump = _dump(db)
    assert "12.43" not in dump and "13.43" not in dump


def test_reject_logs_the_decision_without_a_candidate() -> None:
    queue_id = review.submit(AUDIT_ID, SHA, resolve(_invoice()), CHECKS).id
    assert queue_id is not None

    review.decide(queue_id, "rejected")

    decided = read_resolver_events(1)[0]
    assert decided.event == "rejected"
    assert (decided.field, decided.rank, decided.edit) == (None, None, None)
    assert review.pending() == []


def test_decision_is_final() -> None:
    queue_id = review.submit(AUDIT_ID, SHA, resolve(_invoice()), CHECKS).id
    assert queue_id is not None
    review.decide(queue_id, "rejected")
    with pytest.raises(KeyError, match="no pending review"):
        review.decide(queue_id, "accepted", rank=1)


@pytest.mark.parametrize(
    ("decision", "rank", "message"),
    [
        ("accepted", None, "accepting needs the rank"),
        ("accepted", 99, "rank 99 is not one of"),
        ("rejected", 1, "takes no rank"),
        ("maybe", None, "decision must be"),
        ("checked_manually", 1, "takes no rank"),
    ],
)
def test_invalid_decisions_change_nothing(
    decision: str, rank: int | None, message: str
) -> None:
    queue_id = review.submit(AUDIT_ID, SHA, resolve(_invoice()), CHECKS).id
    assert queue_id is not None

    with pytest.raises(ValueError, match=message):
        review.decide(queue_id, decision, rank=rank)  # type: ignore[arg-type]

    assert [item.id for item in review.pending()] == [queue_id]
    assert [e.event for e in read_resolver_events(10)] == ["suggested"]


def test_queue_holds_amounts_and_paths_only(db: Path) -> None:
    review.submit(AUDIT_ID, SHA, resolve(_invoice()), CHECKS)
    dump = _dump(db)
    assert SELLER_NAME not in dump
    assert SELLER_VAT not in dump
    assert "استشارة" not in dump


def test_submit_rejects_non_hash_identifier() -> None:
    with pytest.raises(ValueError, match="64 lowercase hex"):
        review.submit(AUDIT_ID, "not-a-hash", resolve(_invoice()), CHECKS)


def test_ambiguous_resolution_is_queued() -> None:
    ambiguous = Invoice(
        invoice_type="standard",
        line_items=[
            LineItem(
                description="x",
                quantity="5",
                unit_price="5.00",
                line_total="10.00",
                vat_rate="0.15",
                vat_amount="1.50",
            )
        ],
        subtotal="10.00",
        vat_total="1.50",
        total="11.50",
    )
    resolution: Resolution = resolve(ambiguous)
    assert resolution.status == "ambiguous"
    review.submit(AUDIT_ID, SHA, resolution, CHECKS)
    (item,) = review.pending()
    assert item.status == "ambiguous"
    assert read_resolver_events(1)[0].field is None


def test_values_as_read_are_in_invoice_order() -> None:
    """Line by line in column order, then subtotal, VAT total, total; line 11
    sorts after line 3 (by number, not as text)."""
    scrambled = [
        "total",
        "line_items[1].vat_amount",
        "subtotal",
        "line_items[0].line_total",
        "line_items[10].quantity",
        "line_items[1].quantity",
        "vat_total",
        "line_items[0].unit_price",
        "line_items[0].vat_rate",
        "line_items[2].line_total",
    ]
    resolution = Resolution(
        status="unresolvable",
        reason="test",
        failed_checks=["subtotal + vat_total = total"],
        candidates=[],
        involved=[
            Reading(field=field, read_value=Decimal(n))
            for n, field in enumerate(scrambled)
        ],
    )

    review.submit(AUDIT_ID, SHA, resolution, CHECKS)

    (item,) = review.pending()
    assert [r.field for r in item.involved] == [
        "line_items[0].unit_price",
        "line_items[0].line_total",
        "line_items[0].vat_rate",
        "line_items[1].quantity",
        "line_items[1].vat_amount",
        "line_items[2].line_total",
        "line_items[10].quantity",
        "subtotal",
        "vat_total",
        "total",
    ]
    by_field = {r.field: r.read_value for r in item.involved}
    assert by_field["total"] == Decimal(0)  # values travel with their fields


# --- check outcomes on review cards ---------------------------------------------


def test_check_outcomes_are_stored_and_returned() -> None:
    review.submit(AUDIT_ID, SHA, resolve(_invoice()), CHECKS)

    (item,) = review.pending()
    assert item.checks == CHECKS
    by_cell = {(c.rule, c.line): c.outcome for c in item.checks}
    assert by_cell[("line_total_equals_quantity_times_unit_price", 0)] == "fail"
    assert by_cell[("subtotal_equals_sum_of_line_totals", None)] == "pass"


def test_submit_returns_the_item_and_a_resubmit_returns_the_pending_one() -> None:
    first = review.submit(AUDIT_ID, SHA, resolve(_invoice()), CHECKS)
    again = review.submit("another-audit-id", SHA, resolve(_invoice()), CHECKS)

    assert (first.reference, first.status) == ("R-0001", "suggested")
    assert again == first


def test_stored_check_outcomes_hold_no_amounts(db: Path) -> None:
    review.submit(AUDIT_ID, SHA, resolve(_invoice()), CHECKS)
    with sqlite3.connect(db) as conn:
        (stored,) = conn.execute("SELECT checks FROM review_queue").fetchone()

    assert {key for check in json.loads(stored) for key in check} == {
        "rule",
        "line",
        "outcome",
        "reason",
    }
    assert "12.43" not in stored and "40.29" not in stored


def test_queue_created_before_check_outcomes_is_migrated(db: Path) -> None:
    """A review_queue made by the previous version has no checks column. Its pending
    rows come back with checks None: not recorded, never invented."""
    legacy = review.CREATE_QUEUE.replace(",\n    checks         TEXT", "")
    assert legacy != review.CREATE_QUEUE and "\n    checks " not in legacy
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute(legacy)
        conn.execute(
            "INSERT INTO review_queue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "old",
                "R-0900",
                "2026-09-30T00:00:00+00:00",
                AUDIT_ID,
                SHA,
                "unresolvable",
                "r",
                "[]",
                "[]",
                "[]",
            ),
        )
    conn.close()

    (old,) = review.pending()
    assert old.checks is None

    other_sha = hashlib.sha256(b"other image").hexdigest()
    review.submit(AUDIT_ID, other_sha, resolve(_invoice()), CHECKS)
    assert [item.checks for item in review.pending()] == [None, CHECKS]
