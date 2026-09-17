import base64
import json
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from openai import OpenAI
from pydantic import ValidationError

from app import cache
from app.schema import CallMetadata, ExtractionResult, Invoice
from app.validate import validate

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
PROMPT = """You are extracting a Saudi tax invoice into structured JSON for ZATCA validation.

The invoice may be in Arabic, English, or both. Arabic text is right-to-left; \
mixed-direction lines are common (an invoice number like INV-2026-1002 inside an \
Arabic sentence, or Latin brand names in Arabic descriptions). Read each value as it \
appears on the page, not as its surrounding line direction suggests. Many invoices have \
no English labels at all: rely on the Arabic labels \
(رقم الفاتورة, التاريخ, الرقم الضريبي, البائع, المشتري, المجموع, ضريبة القيمة المضافة, الإجمالي).

Numbers may be printed in Arabic-Indic digits (٠١٢٣٤٥٦٧٨٩) with the Arabic decimal \
separator (٫). Transcribe them exactly as printed; do not convert them. Never guess a \
digit you cannot read: leave the field null and give it a low confidence.

Return one JSON object with exactly two keys:

"invoice": an object with these keys. Use null when a value is absent from the page.
  invoice_number: string
  invoice_date: string, YYYY-MM-DD
  invoice_timestamp: string, ISO 8601 with timezone (the ZATCA QR timestamp)
  invoice_type: "standard" (tax invoice, buyer VAT number shown), \
"simplified" (simplified tax invoice, retail), or "unknown"
  seller_name: string
  seller_vat_number: string, 15 digits as printed
  buyer_name: string
  buyer_vat_number: string, 15 digits as printed
  line_items: array of objects with description, quantity, unit_price, line_total, \
vat_rate (a fraction such as "0.15", not a percentage), vat_amount
  subtotal: total excluding VAT
  vat_total: total VAT
  total: total including VAT
  currency: ISO 4217 code, "SAR" unless the page says otherwise
All monetary values, quantities and rates are strings exactly as printed, never numbers.

"confidence": an object mapping every extracted field path to a number from 0 to 1, \
your confidence that the value is transcribed exactly. Include every key of "invoice" \
and every line item field as line_items[i].field, for example "line_items[0].vat_amount". \
Include fields you set to null."""

ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
# Free text (description, names) is left verbatim: changing its digits alters content.
NUMERIC_FIELDS = frozenset(
    {
        "invoice_number",
        "invoice_date",
        "invoice_timestamp",
        "seller_vat_number",
        "buyer_vat_number",
        "quantity",
        "unit_price",
        "line_total",
        "vat_rate",
        "vat_amount",
        "subtotal",
        "vat_total",
        "total",
    }
)
PRICE_ENV = ("OPENAI_PRICE_INPUT_PER_1M_USD", "OPENAI_PRICE_OUTPUT_PER_1M_USD")


class ParseError(Exception):
    """The model answered, but not in the shape the prompt asked for."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


def model_name() -> str:
    name = os.environ.get("OPENAI_MODEL", "").strip()
    if not name:
        raise RuntimeError(
            "OPENAI_MODEL is not set. Set it to the OpenAI vision model you want to use"
            " (see .env.example); this project does not assume which models exist."
        )
    return name


def _confidence_threshold() -> float:
    raw = os.environ.get("CONFIDENCE_THRESHOLD", "0.80")
    try:
        threshold = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"CONFIDENCE_THRESHOLD must be a number between 0 and 1, received {raw!r}"
        ) from exc
    if not 0.0 <= threshold <= 1.0:
        raise RuntimeError(
            f"CONFIDENCE_THRESHOLD must be between 0 and 1, received {threshold}"
        )
    return threshold


def _data_url(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif image_bytes.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    else:
        raise ValueError(
            f"unsupported image: expected PNG or JPEG, received {len(image_bytes)} bytes"
            f" starting {image_bytes[:8]!r}"
        )
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def _estimated_cost_usd(prompt_tokens: int, completion_tokens: int) -> Decimal | None:
    """Pricing is not hardcoded: it changes per model and over time, so it comes from env."""
    prices = [os.environ.get(name, "").strip() for name in PRICE_ENV]
    if not all(prices):
        return None
    try:
        per_input, per_output = (Decimal(p) for p in prices)
    except InvalidOperation as exc:
        raise RuntimeError(
            f"{' and '.join(PRICE_ENV)} must be decimal USD amounts, received {prices}"
        ) from exc
    return (
        Decimal(prompt_tokens) * per_input + Decimal(completion_tokens) * per_output
    ) / Decimal(1_000_000)


def _call_model(client: OpenAI, model: str, image_bytes: bytes) -> dict[str, Any]:
    started = time.perf_counter()
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": _data_url(image_bytes), "detail": "high"},
                    },
                ],
            }
        ],
    )
    latency_ms = round((time.perf_counter() - started) * 1000)
    content = response.choices[0].message.content
    if content is None:
        raise ParseError("model returned no message content", raw="")
    usage = response.usage
    return {
        "content": content,
        "prompt_tokens": usage.prompt_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
        "latency_ms": latency_ms,
    }


def _metadata(model: str, record: dict[str, Any], cache_hit: bool) -> CallMetadata:
    """
    On a cache hit the tokens, latency and cost describe the call that produced the
    cached answer; `cache_hit` says nothing was spent this time.
    """
    try:
        latency_ms = int(record["latency_ms"])
        prompt_tokens = int(record["prompt_tokens"])
        completion_tokens = int(record["completion_tokens"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"cached response record is malformed ({exc!r}); delete it from"
            f" {cache.CACHE_DIR} to re-extract"
        ) from exc
    cost = _estimated_cost_usd(prompt_tokens, completion_tokens)
    logger.info(
        "model=%s cache_hit=%s latency_ms=%d prompt_tokens=%d completion_tokens=%d"
        " estimated_cost=%s",
        model,
        cache_hit,
        latency_ms,
        prompt_tokens,
        completion_tokens,
        f"${cost:.6f}"
        if cost is not None
        else f"unknown (set {' and '.join(PRICE_ENV)})",
    )
    return CallMetadata(
        model=model,
        prompt_version=PROMPT_VERSION,
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost_usd=cost,
        cache_hit=cache_hit,
    )


def normalise_digits(invoice: dict[str, Any]) -> dict[str, Any]:
    """Map Arabic-Indic digits and separators to ASCII in NUMERIC_FIELDS only."""

    def fix(key: str, value: Any) -> Any:
        if key in NUMERIC_FIELDS and isinstance(value, str):
            return value.translate(ARABIC_INDIC)
        if key == "line_items" and isinstance(value, list):
            return [
                {k: fix(k, v) for k, v in line.items()}
                if isinstance(line, dict)
                else line
                for line in value
            ]
        return value

    return {k: fix(k, v) for k, v in invoice.items()}


def _parse_confidences(raw: Any, content: str) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise ParseError(
            f"'confidence' must be an object, received {type(raw).__name__}",
            raw=content,
        )
    scores: dict[str, float] = {}
    for field, score in raw.items():
        if isinstance(score, bool) or not isinstance(score, int | Decimal):
            raise ParseError(
                f"confidence for {field!r} must be a number, received {score!r}",
                raw=content,
            )
        if not 0 <= score <= 1:
            raise ParseError(
                f"confidence for {field!r} must be between 0 and 1, received {score}",
                raw=content,
            )
        scores[field] = float(score)
    return scores


def parse_response(content: str) -> tuple[Invoice, dict[str, float]]:
    """Money must stay exact: floats are rejected by the schema, so parse them as Decimal."""
    try:
        payload = json.loads(content, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise ParseError(
            f"model response is not valid JSON: {exc}", raw=content
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {"invoice", "confidence"}:
        raise ParseError(
            "model response must be an object with keys 'invoice' and 'confidence',"
            f" received {sorted(payload) if isinstance(payload, dict) else payload!r}",
            raw=content,
        )
    raw_invoice = payload["invoice"]
    if not isinstance(raw_invoice, dict):
        raise ParseError(
            f"'invoice' must be an object, received {type(raw_invoice).__name__}",
            raw=content,
        )
    try:
        invoice = Invoice.model_validate(normalise_digits(raw_invoice))
    except ValidationError as exc:
        raise ParseError(
            f"model response does not match the Invoice schema: {exc}", raw=content
        ) from exc
    return invoice, _parse_confidences(payload["confidence"], content)


def extract(
    image_bytes: bytes, client: OpenAI | None = None
) -> tuple[ExtractionResult, CallMetadata]:
    """
    `client` is only constructed on a cache miss, so cached images need no API key.
    Responses are cached before parsing: a malformed answer is still a paid answer,
    and re-running must not silently spend again (delete the cache file to retry).
    """
    model = model_name()
    record = cache.get(image_bytes, model, PROMPT_VERSION)
    cache_hit = record is not None
    if record is None:
        record = _call_model(client or OpenAI(), model, image_bytes)
        cache.set(image_bytes, model, PROMPT_VERSION, record)
    metadata = _metadata(model, record, cache_hit)
    invoice, confidences = parse_response(record["content"])
    return validate(invoice, confidences, _confidence_threshold()), metadata
