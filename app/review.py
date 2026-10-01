"""
Review queue for resolver results. A person decides each one: accepts a ranked
candidate or rejects them all; an unresolvable invoice is closed as checked
manually. Nothing here changes an invoice value: accepting records the decision,
it does not apply it.

The queue holds what a reviewer needs to decide: field paths, the values read
and the candidate values. It never holds names, VAT numbers, descriptions or
images; a Resolution carries none. The row is removed in the same transaction
that logs the decision, so amounts leave the database once a person has decided.
Undecided rows stay until then. The decision log (resolver_events in
app/store.py) is append-only and holds no amounts.

Each item gets a short reference (R-0042) from a counter that never reuses a
number, so it can be written on the paper invoice: the reviewer never sees the
image, which is not stored.

This module removes rows and app/store.py must not, which is why the queue does
not live there.
"""

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from app import store
from app.resolve import LINE_CELLS, TOTAL_CELLS
from app.schema import CheckOutcome, Resolution, ReviewItem

Decision = Literal["accepted", "rejected", "checked_manually"]

CREATE_QUEUE = """
CREATE TABLE IF NOT EXISTS review_queue (
    id             TEXT PRIMARY KEY,
    reference      TEXT NOT NULL UNIQUE,
    created_utc    TEXT NOT NULL,
    audit_id       TEXT NOT NULL,
    image_sha256   TEXT NOT NULL,
    status         TEXT NOT NULL,
    reason         TEXT NOT NULL,
    failed_checks  TEXT NOT NULL,
    candidates     TEXT NOT NULL,
    involved       TEXT NOT NULL,
    checks         TEXT
)
"""
# Queues made before check outcomes were stored lack the column; their rows keep NULL.
ADD_CHECKS = "ALTER TABLE review_queue ADD COLUMN checks TEXT"
# SQLite only: AUTOINCREMENT never reuses a number, even after rows are removed.
CREATE_COUNTER = (
    "CREATE TABLE IF NOT EXISTS review_counter (n INTEGER PRIMARY KEY AUTOINCREMENT)"
)
NEXT_NUMBER = "INSERT INTO review_counter DEFAULT VALUES RETURNING n"
QUEUE_COLUMNS = tuple(ReviewItem.model_fields)
JSON_COLUMNS = ("failed_checks", "candidates", "involved", "checks")
INSERT_QUEUE = f"INSERT INTO review_queue ({', '.join(QUEUE_COLUMNS)}) VALUES ({', '.join('?' * len(QUEUE_COLUMNS))})"
SELECT = f"SELECT {', '.join(QUEUE_COLUMNS)} FROM review_queue"
SELECT_PENDING = f"{SELECT} ORDER BY created_utc, id"
SELECT_ONE = f"{SELECT} WHERE id = ?"
SELECT_BY_IMAGE = f"{SELECT} WHERE image_sha256 = ?"
REMOVE_ONE = "DELETE FROM review_queue WHERE id = ?"
LINE_FIELD = re.compile(r"line_items\[(\d+)\]\.(\w+)")


def _create(conn: Any) -> None:
    conn.execute(CREATE_QUEUE)
    conn.execute(CREATE_COUNTER)
    # cursor.description is DB-API, so this needs no SQLite-only PRAGMA.
    columns = {
        d[0] for d in conn.execute("SELECT * FROM review_queue LIMIT 0").description
    }
    if "checks" not in columns:
        conn.execute(ADD_CHECKS)


def _to_db(item: ReviewItem) -> tuple[Any, ...]:
    data = item.model_dump(mode="json")
    return tuple(
        json.dumps(data[c], ensure_ascii=False) if c in JSON_COLUMNS else data[c]
        for c in QUEUE_COLUMNS
    )


def _invoice_order(field: str) -> tuple[int, int, int, str]:
    """Reading order for "values as read": line by line in column order, then the
    totals. The resolver lists cells in failed-check order, which is not."""
    line = LINE_FIELD.fullmatch(field)
    if line and line.group(2) in LINE_CELLS:
        return (0, int(line.group(1)), LINE_CELLS.index(line.group(2)), "")
    if field in TOTAL_CELLS:
        return (1, 0, TOTAL_CELLS.index(field), "")
    return (2, 0, 0, field)


def _from_db(values: tuple[Any, ...]) -> ReviewItem:
    record = dict(zip(QUEUE_COLUMNS, values, strict=True))
    for column in JSON_COLUMNS:
        if record[column] is not None:
            record[column] = json.loads(record[column])
    record["involved"].sort(key=lambda reading: _invoice_order(reading["field"]))
    return ReviewItem.model_validate(record)


def submit(
    audit_id: str,
    image_sha256: str,
    resolution: Resolution,
    checks: list[CheckOutcome],
) -> ReviewItem:
    """Queues the result and logs it; returns the queued item. An image that already
    has a pending review keeps it, and that item is returned: nothing new is queued
    or logged."""
    if resolution.status == "not_needed":
        raise ValueError(
            "resolution status is not_needed: there is nothing to review or log"
        )
    if not store.SHA256_HEX.fullmatch(image_sha256):
        raise ValueError(
            f"image_sha256 must be 64 lowercase hex characters, received {image_sha256!r}"
        )
    with store.connection() as (conn, placeholder):
        _create(conn)
        existing = conn.execute(
            SELECT_BY_IMAGE.replace("?", placeholder), (image_sha256,)
        ).fetchone()
        if existing is not None:
            return _from_db(existing)
        number = conn.execute(NEXT_NUMBER).fetchone()[0]
        item = ReviewItem(
            id=str(uuid.uuid4()),
            reference=f"R-{number:04d}",
            created_utc=datetime.now(UTC).isoformat(timespec="microseconds"),
            audit_id=audit_id,
            image_sha256=image_sha256,
            **resolution.model_dump(include={"status", "reason", "failed_checks"}),
            candidates=resolution.candidates,
            involved=resolution.involved,
            checks=checks,
        )
        conn.execute(INSERT_QUEUE.replace("?", placeholder), _to_db(item))
        store.insert_resolver_event(conn, placeholder, _submitted_event(item))
    return item


def _submitted_event(item: ReviewItem) -> store.ResolverEvent:
    top = item.candidates[0] if item.status == "suggested" else None
    return store.resolver_event(
        item.audit_id,
        item.id,
        item.reference,
        item.status,
        item.failed_checks,
        field=top.field if top else None,
        edit=top.edit if top else None,
        candidates=len(item.candidates),
    )


def pending() -> list[ReviewItem]:
    with store.connection() as (conn, _):
        _create(conn)
        rows = conn.execute(SELECT_PENDING).fetchall()
    return [_from_db(row) for row in rows]


def _check_shape(decision: str, rank: int | None) -> None:
    if decision not in ("accepted", "rejected", "checked_manually"):
        raise ValueError(
            "decision must be 'accepted', 'rejected' or 'checked_manually',"
            f" received {decision!r}"
        )
    if decision == "accepted" and rank is None:
        raise ValueError("accepting needs the rank of the chosen candidate")
    if decision != "accepted" and rank is not None:
        raise ValueError(f"{decision} takes no rank, received rank {rank}")


def _check_fits(item: ReviewItem, decision: str) -> None:
    """Unresolvable items have nothing to accept or reject; the others have a
    suggestion a person must accept or reject."""
    if item.status == "unresolvable" and decision != "checked_manually":
        raise ValueError(
            f"{item.reference} is unresolvable: the only decision is checked_manually,"
            f" received {decision!r}"
        )
    if item.status != "unresolvable" and decision == "checked_manually":
        raise ValueError(
            f"{item.reference} has candidates: the decision must be accepted or rejected"
        )


def decide(queue_id: str, decision: Decision, rank: int | None = None) -> ReviewItem:
    """Logs the decision and removes the queue row, in one transaction. Returns the
    item as it was, so the caller can report its reference."""
    _check_shape(decision, rank)
    with store.connection() as (conn, placeholder):
        _create(conn)
        row = conn.execute(SELECT_ONE.replace("?", placeholder), (queue_id,)).fetchone()
        if row is None:
            raise KeyError(
                f"no pending review {queue_id!r}: it does not exist or was already decided"
            )
        item = _from_db(row)
        _check_fits(item, decision)
        chosen = next((c for c in item.candidates if c.rank == rank), None)
        if decision == "accepted" and chosen is None:
            ranks = [c.rank for c in item.candidates]
            raise ValueError(f"rank {rank} is not one of the candidate ranks {ranks}")
        event = store.resolver_event(
            item.audit_id,
            item.id,
            item.reference,
            decision,
            item.failed_checks,
            field=chosen.field if chosen else None,
            edit=chosen.edit if chosen else None,
            rank=chosen.rank if chosen else None,
            candidates=len(item.candidates),
        )
        store.insert_resolver_event(conn, placeholder, event)
        conn.execute(REMOVE_ONE.replace("?", placeholder), (queue_id,))
    return item
