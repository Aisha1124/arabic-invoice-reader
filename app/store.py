"""
Append-only audit log: one row per extraction, never updated, never deleted.

The row records that an extraction happened and how it went, never what the
invoice said. Nothing in this module accepts an Invoice, an image, or a model
response; see `audit_row` for what is derived from the result and how.
"""

import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.schema import CallMetadata, ExtractionResult

DB_PATH = Path("data/app.db")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS audit_log (
    id                  TEXT PRIMARY KEY,
    timestamp_utc       TEXT NOT NULL,
    image_sha256        TEXT NOT NULL,
    model               TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    fields_extracted    INTEGER NOT NULL,
    fields_flagged      INTEGER NOT NULL,
    validation_findings TEXT NOT NULL,
    latency_ms          INTEGER NOT NULL,
    estimated_cost_usd  TEXT,
    temperature_zero    BOOLEAN NOT NULL,
    cache_hit           BOOLEAN NOT NULL
)
"""
COLUMNS = (
    "id",
    "timestamp_utc",
    "image_sha256",
    "model",
    "prompt_version",
    "fields_extracted",
    "fields_flagged",
    "validation_findings",
    "latency_ms",
    "estimated_cost_usd",
    "temperature_zero",
    "cache_hit",
)
INSERT = f"INSERT INTO audit_log ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})"
SELECT_LAST = f"SELECT {', '.join(COLUMNS)} FROM audit_log ORDER BY timestamp_utc DESC, id DESC LIMIT ?"


@dataclass(frozen=True)
class AuditRow:
    id: str
    timestamp_utc: str
    image_sha256: str
    model: str
    prompt_version: str
    fields_extracted: int
    fields_flagged: int
    validation_findings: str  # JSON array of {rule, severity, fields}
    latency_ms: int
    estimated_cost_usd: str | None
    temperature_zero: bool
    cache_hit: bool


def _count_extracted(result: ExtractionResult) -> int:
    """Non-null fields, counted without reading their values into anything."""
    invoice = result.invoice
    top_level = sum(
        getattr(invoice, name) is not None
        for name in type(invoice).model_fields
        if name != "line_items"
    )
    per_line = sum(len(type(line).model_fields) for line in invoice.line_items)
    return top_level + per_line


def audit_row(
    image_sha256: str, result: ExtractionResult, metadata: CallMetadata
) -> AuditRow:
    """
    Derives the row from the result. Finding messages are deliberately dropped:
    they quote amounts and VAT numbers. Only rule names, severities and field
    paths (e.g. "line_items[0].vat_amount") are kept, and those name positions
    in the schema, not content.
    """
    if not SHA256_HEX.fullmatch(image_sha256):
        raise ValueError(
            f"image_sha256 must be 64 lowercase hex characters, received {image_sha256!r}"
        )
    findings = [
        {"rule": f.rule, "severity": f.severity, "fields": f.fields}
        for f in result.findings
    ]
    flagged = {field for f in result.findings for field in f.fields}
    cost = metadata.estimated_cost_usd
    return AuditRow(
        id=str(uuid.uuid4()),
        timestamp_utc=datetime.now(UTC).isoformat(timespec="microseconds"),
        image_sha256=image_sha256,
        model=metadata.model,
        prompt_version=metadata.prompt_version,
        fields_extracted=_count_extracted(result),
        fields_flagged=len(flagged),
        validation_findings=json.dumps(findings),
        latency_ms=metadata.latency_ms,
        estimated_cost_usd=str(cost) if cost is not None else None,
        temperature_zero=metadata.temperature_zero,
        cache_hit=metadata.cache_hit,
    )


@contextmanager
def _connection() -> Iterator[tuple[Any, str]]:
    """Yields (connection, placeholder). Postgres when DATABASE_URL is set, else SQLite."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "DATABASE_URL is set but the psycopg package is not installed;"
                " install it or unset DATABASE_URL to use SQLite"
            ) from exc
        with psycopg.connect(url) as conn:
            yield conn, "%s"
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:
            yield conn, "?"
    finally:
        conn.close()


def write_audit(row: AuditRow) -> None:
    with _connection() as (conn, placeholder):
        conn.execute(CREATE_TABLE)
        conn.execute(INSERT.replace("?", placeholder), tuple(asdict(row).values()))


def read_last(n: int) -> list[AuditRow]:
    if n < 1:
        raise ValueError(f"n must be at least 1, received {n}")
    with _connection() as (conn, placeholder):
        conn.execute(CREATE_TABLE)
        rows = conn.execute(SELECT_LAST.replace("?", placeholder), (n,)).fetchall()
    return [_from_db(values) for values in rows]


def _from_db(values: tuple[Any, ...]) -> AuditRow:
    record = dict(zip(COLUMNS, values, strict=True))
    # SQLite stores BOOLEAN as 0/1; Postgres returns real booleans. Normalise both.
    record["temperature_zero"] = bool(record["temperature_zero"])
    record["cache_hit"] = bool(record["cache_hit"])
    return AuditRow(**record)
