"""
HTTP surface only. Extraction, validation, caching, the audit log, the resolver
and the review queue live in the other modules; nothing here inspects invoice
content beyond the names of failed validation rules.

With DEMO_MODE=1 (app/demo.py) uploads are refused and visitors run saved samples
instead; the queue and log routes then use the visitor's private database, chosen
by the X-Demo-Visitor header.

Images are uploaded as the raw request body (Content-Type image/png or
image/jpeg), not multipart. Starlette's multipart parser spools large parts to
temporary files on disk, which CLAUDE.md section 9 forbids; a raw body stays in
memory.
"""

import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from openai import APIError
from pydantic import BaseModel

from app import demo, review, store
from app.extract import ParseError, extract, model_name
from app.resolve import ARITHMETIC_RULES, resolve
from app.schema import (
    CallMetadata,
    ExtractionResult,
    ExtractResponse,
    ReviewItem,
    ReviewOutcome,
    RuleFinding,
)
from app.store import audit_row, read_last, write_audit

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ACCEPTED_TYPES = {"image/png", "image/jpeg", "image/jpg"}
AUDIT_PAGE = 50
# Arithmetic failures the resolver can work on; a missing line-item table comes
# back unresolvable, which a person still needs to see.
RESOLVER_RULES = ARITHMETIC_RULES | {"totals_present_without_line_items"}

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
# Routes that read or write the queue and logs: in demo mode, the visitor's own.
VISITOR_ROUTES = ("/audit", "/reviews", "/demo/run/", "/demo/reset")
visitors = demo.Visitors()

app = FastAPI(title="Arabic/English Invoice Reader", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def visitor_database(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if not demo.enabled() or not request.url.path.startswith(VISITOR_ROUTES):
        return await call_next(request)
    visitor = request.headers.get("x-demo-visitor", "")
    if not demo.VISITOR_ID.fullmatch(visitor):
        return JSONResponse(
            {
                "detail": "demo mode needs an X-Demo-Visitor header of 16-64 letters,"
                " digits or dashes; the page sends one"
            },
            status_code=400,
        )
    if request.url.path == "/demo/reset":
        visitors.reset(visitor)
        return JSONResponse({"status": "reset"})
    db, lock = visitors.get(visitor)
    async with lock:
        token = store.VISITOR_DB.set(db)
        try:
            return await call_next(request)
        finally:
            store.VISITOR_DB.reset(token)


# HEAD too: hosts' health checks (Render's) send HEAD / and got 405 from GET alone.
@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def _read_image(request: Request) -> bytes:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/pdf":
        raise HTTPException(
            400, "PDF is not supported; upload a PNG or JPEG image of the invoice page"
        )
    if content_type not in ACCEPTED_TYPES:
        raise HTTPException(
            400,
            f"unsupported Content-Type {content_type or '(none)'!r};"
            " send the image bytes as the request body with Content-Type image/png"
            " or image/jpeg",
        )
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, _too_large(int(declared)))
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_UPLOAD_BYTES:
            raise HTTPException(400, _too_large(received))
        chunks.append(chunk)
    body = b"".join(chunks)
    if not body:
        raise HTTPException(400, "request body is empty; send the image bytes")
    if body.startswith(b"%PDF"):
        raise HTTPException(
            400, "PDF is not supported; upload a PNG or JPEG image of the invoice page"
        )
    return body


def _too_large(size: int) -> str:
    return (
        f"image is too large: received at least {size} bytes,"
        f" limit is {MAX_UPLOAD_BYTES} bytes (10 MB)"
    )


@app.post("/extract", response_model=ExtractResponse)
async def post_extract(request: Request) -> ExtractResponse:
    if demo.enabled():
        raise HTTPException(
            403,
            "uploads are disabled in demo mode; pick a sample, or run the app"
            " locally with your own API key for live extraction",
        )
    image = await _read_image(request)
    try:
        result, metadata = extract(image)
    except ValueError as exc:
        # extract() rejects bytes that are not PNG or JPEG; its message names only sizes.
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        # Configuration problems (OPENAI_MODEL unset, malformed cache record).
        logger.error("extraction not possible: %s", exc)
        raise HTTPException(500, "server is not configured for extraction") from exc
    except ParseError as exc:
        # exc.raw and the pydantic message can quote invoice content: keep them in the log.
        logger.error("model response could not be parsed: %s", exc)
        raise HTTPException(
            500, "the model returned a response that could not be parsed"
        ) from exc
    except APIError as exc:
        logger.error("model call failed: %s", exc)
        raise HTTPException(500, "the model call failed") from exc
    return _respond(image, result, metadata)


def _respond(
    image: bytes, result: ExtractionResult, metadata: CallMetadata
) -> ExtractResponse:
    """Audit row and review queue for one extraction, uploaded or a demo sample."""
    image_sha256 = hashlib.sha256(image).hexdigest()
    row = audit_row(image_sha256, result, metadata)
    write_audit(row)
    # Every finding reaches a person, so every needs_review invoice is queued:
    # arithmetic through the resolver, everything else (cross-checks, compliance
    # warnings) as rule names and field paths only.
    arithmetic = any(f.rule in RESOLVER_RULES for f in result.findings)
    rule_findings = [
        RuleFinding(rule=f.rule, fields=f.fields)
        for f in result.findings
        if f.rule not in RESOLVER_RULES
    ]
    queued = None
    if arithmetic or rule_findings:
        queued = _queue_for_review(
            row.id, image_sha256, result, arithmetic, rule_findings
        )
    return ExtractResponse(**dict(result), review=queued)


def _queue_for_review(
    audit_id: str,
    image_sha256: str,
    result: ExtractionResult,
    arithmetic: bool,
    rule_findings: list[RuleFinding],
) -> ReviewOutcome:
    """A resolver or queue failure must not cost the user a successful extraction:
    the invoice is already needs_review, and the response says the review was not
    queued. Only the exception type is logged, since a message could quote an amount."""
    try:
        resolution = resolve(result.invoice) if arithmetic else None
        item = review.submit(
            audit_id, image_sha256, resolution, result.checks, rule_findings
        )
    except Exception as exc:  # noqa: BLE001 - deliberate: the extraction stands regardless
        logger.error(
            "resolver failed for image %s: %s", image_sha256[:12], type(exc).__name__
        )
        return ReviewOutcome(status="error")
    return ReviewOutcome(
        status="queued",
        reference=item.reference,
        resolver_status=None if resolution is None else item.status,
    )


@app.get("/audit")
def get_audit() -> list[dict[str, object]]:
    return [asdict(row) for row in read_last(AUDIT_PAGE)]


@app.get("/reviews", response_model=list[ReviewItem])
def get_reviews() -> list[ReviewItem]:
    return review.pending()


class DecisionBody(BaseModel):
    decision: review.Decision
    rank: int | None = None


@app.post("/reviews/{queue_id}/decision")
def post_decision(queue_id: str, body: DecisionBody) -> dict[str, str]:
    """Records the decision only; no invoice value is changed."""
    try:
        item = review.decide(queue_id, body.decision, body.rank)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"reference": item.reference, "decision": body.decision}


@app.get("/demo/samples")
def get_demo_samples() -> list[dict[str, str | None]]:
    _demo_only()
    return [
        {
            "id": s.id,
            "label": s.label,
            "image": f"/static/demo/{s.image}",
            "note": s.note,
            "tooltip": s.tooltip,
        }
        for s in demo.SAMPLES
    ]


@app.post("/demo/run/{sample_id}", response_model=ExtractResponse)
def post_demo_run(sample_id: str) -> ExtractResponse:
    _demo_only()
    try:
        image, result, metadata = demo.run(sample_id)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    return _respond(image, result, metadata)


def _demo_only() -> None:
    if not demo.enabled():
        raise HTTPException(404, "demo mode is off; set DEMO_MODE=1 to use the samples")


@app.get("/health")
def get_health() -> dict[str, object]:
    if demo.enabled():
        # No key or model is read in demo mode.
        return {"status": "ok", "demo_mode": True}
    try:
        model = model_name()
    except RuntimeError:
        model = None
    return {
        "status": "ok",
        "demo_mode": False,
        "model_configured": model is not None,
        "model": model,
        "openai_key_configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
    }
