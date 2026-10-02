"""
Demo mode (DEMO_MODE=1): saved model output on synthetic invoices, for a public
deployment with no API key. Nothing reads OPENAI_API_KEY or OPENAI_MODEL, no
model client is made, and uploads are refused (app/main.py). A visitor picks a
sample, which runs through the real pipeline from the model's saved answer on:
parsing, QR decode, validation, audit row, resolver and review queue.

The samples are in static/demo/: invoices from eval/generate_invoices.py, whose
names are invented and whose VAT numbers are random, and gpt-4o's answers to
them copied from .cache/. "Single misread" is INV-2026-1010's answer with one
digit changed on purpose (quantity ٣ read as ٢, the confusion the resolver tests
inject), so the resolver has one misread to suggest values for.

Each visitor gets a private in-memory SQLite database, chosen by an id the page
makes when it loads and sends in the X-Demo-Visitor header. Not a cookie, so it
keeps working when the page is embedded in another site's iframe, where browsers
often block cookies. Reloading the page or pressing Reset starts an empty one, and
nothing is written to data/app.db. At most MAX_VISITORS databases are kept; the
least recently used is closed first, so one still in use by a request could be
closed only if that many other visitors arrived during the request.
"""

import asyncio
import json
import os
import re
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import NamedTuple

from app.extract import PROMPT_VERSION, from_record
from app.schema import CallMetadata, ExtractionResult

DEMO_DIR = Path(__file__).resolve().parent.parent / "static" / "demo"
MAX_VISITORS = 200
VISITOR_ID = re.compile(r"[A-Za-z0-9-]{16,64}")
ON = ("1", "true", "yes", "on")
OFF = ("", "0", "false", "no", "off")


class Sample(NamedTuple):
    id: str
    label: str
    image: str  # file name in static/demo/
    response: str  # saved model response in static/demo/
    note: str | None = None


SAMPLES = (
    Sample("clean", "Clean invoice", "INV-2026-1000.png", "INV-2026-1000.json"),
    Sample("many-misreads", "Many misreads", "INV-2026-1002.png", "INV-2026-1002.json"),
    Sample("warnings-only", "Warnings only", "INV-2026-1009.png", "INV-2026-1009.json"),
    Sample(
        "qr-disagrees", "QR code disagrees", "INV-2026-1012.png", "INV-2026-1012.json"
    ),
    Sample(
        "single-misread",
        "Single misread",
        "INV-2026-1010.png",
        "single-misread.json",
        note="One value altered on purpose to show how suggestions work.",
    ),
)


def enabled() -> bool:
    value = os.environ.get("DEMO_MODE", "").strip().lower()
    if value in ON:
        return True
    if value in OFF:
        return False
    raise RuntimeError(
        f"DEMO_MODE must be one of {', '.join(ON)} or {', '.join(OFF[1:])},"
        f" received {value!r}"
    )


def run(sample_id: str) -> tuple[bytes, ExtractionResult, CallMetadata]:
    """The image's bytes and the pipeline's result on its saved model answer."""
    sample = next((s for s in SAMPLES if s.id == sample_id), None)
    if sample is None:
        raise KeyError(
            f"no demo sample {sample_id!r}; choose one of {[s.id for s in SAMPLES]}"
        )
    image = (DEMO_DIR / sample.image).read_bytes()
    saved = json.loads((DEMO_DIR / sample.response).read_text(encoding="utf-8"))
    if saved["prompt_version"] != PROMPT_VERSION:
        raise RuntimeError(
            f"{sample.response} was saved with prompt {saved['prompt_version']},"
            f" the app uses {PROMPT_VERSION}; refresh the demo fixtures"
        )
    result, metadata = from_record(image, saved["model"], saved["record"])
    return image, result, metadata


class Visitors:
    """One in-memory database and one lock per visitor, least recently used first
    out. The lock makes a visitor's requests take turns on their connection."""

    def __init__(self, limit: int = MAX_VISITORS) -> None:
        self._limit = limit
        self._open: OrderedDict[str, tuple[sqlite3.Connection, asyncio.Lock]] = (
            OrderedDict()
        )
        self._guard = threading.Lock()

    def get(self, visitor: str) -> tuple[sqlite3.Connection, asyncio.Lock]:
        with self._guard:
            entry = self._open.pop(visitor, None)
            if entry is None:
                entry = (
                    sqlite3.connect(":memory:", check_same_thread=False),
                    asyncio.Lock(),
                )
            self._open[visitor] = entry
            while len(self._open) > self._limit:
                _, (oldest, _) = self._open.popitem(last=False)
                oldest.close()
            return entry

    def reset(self, visitor: str) -> None:
        with self._guard:
            entry = self._open.pop(visitor, None)
        if entry is not None:
            entry[0].close()

    def __len__(self) -> int:
        return len(self._open)
