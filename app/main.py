"""
HTTP surface only. Extraction, validation, caching and the audit log live in the
other modules; nothing here inspects invoice content.

Images are uploaded as the raw request body (Content-Type image/png or
image/jpeg), not multipart. Starlette's multipart parser spools large parts to
temporary files on disk, which CLAUDE.md section 9 forbids; a raw body stays in
memory.
"""

import hashlib
import logging
import os
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Request
from openai import APIError

from app.extract import ParseError, extract, model_name
from app.schema import ExtractionResult
from app.store import audit_row, read_last, write_audit

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ACCEPTED_TYPES = {"image/png", "image/jpeg", "image/jpg"}
AUDIT_PAGE = 50

app = FastAPI(title="Arabic/English Invoice Reader", docs_url=None, redoc_url=None)


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


@app.post("/extract", response_model=ExtractionResult)
async def post_extract(request: Request) -> ExtractionResult:
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
    write_audit(audit_row(hashlib.sha256(image).hexdigest(), result, metadata))
    return result


@app.get("/audit")
def get_audit() -> list[dict[str, object]]:
    return [asdict(row) for row in read_last(AUDIT_PAGE)]


@app.get("/health")
def get_health() -> dict[str, object]:
    try:
        model = model_name()
    except RuntimeError:
        model = None
    return {
        "status": "ok",
        "model_configured": model is not None,
        "model": model,
        "openai_key_configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
    }
