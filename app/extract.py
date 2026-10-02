import base64
import json
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from openai import BadRequestError, OpenAI
from pydantic import ValidationError

from app import cache
from app.qr import read_qr
from app.schema import CallMetadata, ExtractionResult, Invoice
from app.validate import validate

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v3"
PROMPT = """You are extracting a Saudi tax invoice into structured JSON for ZATCA validation.

The invoice may be in Arabic, English, or both. Arabic text is right-to-left; \
mixed-direction lines are common (an invoice number like INV-2026-1002 inside an \
Arabic sentence, or Latin brand names in Arabic descriptions). Read each value as it \
appears on the page, not as its surrounding line direction suggests. Many invoices have \
no English labels at all: rely on the Arabic labels \
(رقم الفاتورة, التاريخ, الرقم الضريبي, البائع, المشتري, المجموع, ضريبة القيمة المضافة, الإجمالي).

You are a transcriber, not a reader. Copy text character by character. Do not translate, \
paraphrase, correct, or substitute a more plausible word. If a word looks misspelled on \
the page, copy the misspelling. Return text in logical reading order, not visual order: \
an Arabic line containing Latin tokens must come back in the order a reader speaks it, \
not left to right across the page. When the invoice prints both Arabic and English for a \
name or a description, always return the Arabic.

Numbers may be printed in Arabic-Indic digits (٠١٢٣٤٥٦٧٨٩) with the Arabic decimal \
separator (٫). Transcribe them exactly as printed; do not convert them. Never guess a \
digit you cannot read: leave the field null and give it a low confidence.

Return one JSON object with exactly two keys:

"invoice": an object with these keys. Use null when a value is absent from the page.
  invoice_number: string
  invoice_date: string, YYYY-MM-DD
  invoice_timestamp: string, YYYY-MM-DDTHH:MM:SS from the printed date and time, \
with no timezone suffix (the page does not show one)
  invoice_type: "standard" (tax invoice, buyer VAT number shown), \
"simplified" (simplified tax invoice, retail), or "unknown"
  seller_name: string, the Arabic name when one is printed
  seller_vat_number: string, 15 digits as printed
  buyer_name: string, the Arabic name when one is printed
  buyer_vat_number: string, 15 digits as printed
  line_items: array of objects with description (transcribed exactly, Arabic when \
printed in both languages), quantity, unit_price, line_total, \
vat_rate (a fraction such as "0.15", not a percentage), vat_amount. \
If the table has no per-line VAT column, set vat_amount to "0" on every line, not null.
  subtotal: total excluding VAT
  vat_total: total VAT
  total: total including VAT
  currency: ISO 4217 code, "SAR" unless the page says otherwise
All monetary values, quantities and rates are strings exactly as printed, never numbers.

"confidence": an object mapping every extracted field path to a number from 0 to 1, \
your confidence that the value is transcribed exactly. Include every key of "invoice" \
and every line item field as line_items[i].field, for example "line_items[0].vat_amount". \
Include fields you set to null."""

# Invoices print wall-clock time with no zone. Any suffix the model appends
# (usually Z or +03:00) is invented, so it is removed before parsing.
TIMEZONE_SUFFIX = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")
ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
# Amounts sometimes come back as printed: "SAR 11897.50", "5 456.34 ريال". The
# currency token and any whitespace thousands separator are removed before parsing.
# Not applied to invoice_number or the timestamp, where whitespace is meaningful.
MONEY_FIELDS = frozenset(
    {
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
CURRENCY_TOKEN = re.compile(
    r"^\s*(?:SAR|ر\.س\.?|ريال)\s*|\s*(?:SAR|ر\.س\.?|ريال)\s*$", re.IGNORECASE
)
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
        # Set by extract() once the call has been paid for, so callers can still
        # account for the tokens of an answer that could not be parsed.
        self.metadata: CallMetadata | None = None


def model_name() -> str:
    name = os.environ.get("OPENAI_MODEL", "").strip()
    if not name:
        raise RuntimeError(
            "OPENAI_MODEL is not set. Set it to the OpenAI vision model you want to use"
            " (see .env.example); this project does not assume which models exist."
        )
    return name


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


def _request(
    client: OpenAI, model: str, image_bytes: bytes, temperature_zero: bool
) -> Any:
    params: dict[str, Any] = {"temperature": 0} if temperature_zero else {}
    return client.chat.completions.create(
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
        **params,
    )


def _call_model(client: OpenAI, model: str, image_bytes: bytes) -> dict[str, Any]:
    """
    temperature=0 is requested for repeatability. Some models reject the parameter
    outright (HTTP 400 naming it); that one case is retried without it and recorded
    as temperature_zero=False so the eval can say which runs were deterministic.
    """
    started = time.perf_counter()
    temperature_zero = True
    try:
        response = _request(client, model, image_bytes, temperature_zero=True)
    except BadRequestError as exc:
        if "temperature" not in str(exc):
            raise
        logger.warning("model=%s rejected temperature=0: %s", model, exc)
        temperature_zero = False
        response = _request(client, model, image_bytes, temperature_zero=False)
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
        "temperature_zero": temperature_zero,
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
        temperature_zero = bool(record["temperature_zero"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"cached response record is malformed ({exc!r}); delete it from"
            f" {cache.CACHE_DIR} to re-extract"
        ) from exc
    cost = _estimated_cost_usd(prompt_tokens, completion_tokens)
    logger.info(
        "model=%s cache_hit=%s temperature_zero=%s latency_ms=%d prompt_tokens=%d"
        " completion_tokens=%d estimated_cost=%s",
        model,
        cache_hit,
        temperature_zero,
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
        temperature_zero=temperature_zero,
    )


def normalise_digits(invoice: dict[str, Any]) -> dict[str, Any]:
    """Map Arabic-Indic digits and separators to ASCII in NUMERIC_FIELDS only, and
    drop currency tokens and whitespace from MONEY_FIELDS."""

    def fix(key: str, value: Any) -> Any:
        if key in MONEY_FIELDS and isinstance(value, str):
            value = CURRENCY_TOKEN.sub("", value.translate(ARABIC_INDIC))
            return "".join(value.split())
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


def strip_timezone(invoice: dict[str, Any]) -> dict[str, Any]:
    timestamp = invoice.get("invoice_timestamp")
    if isinstance(timestamp, str):
        invoice = {**invoice, "invoice_timestamp": TIMEZONE_SUFFIX.sub("", timestamp)}
    return invoice


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
        invoice = Invoice.model_validate(strip_timezone(normalise_digits(raw_invoice)))
    except ValidationError as exc:
        raise ParseError(
            f"model response does not match the Invoice schema: {exc}", raw=content
        ) from exc
    return invoice, _parse_confidences(payload["confidence"], content)


def extract(
    image_bytes: bytes, client: OpenAI | None = None, use_cache: bool = True
) -> tuple[ExtractionResult, CallMetadata]:
    """
    `client` is only constructed on a cache miss, so cached images need no API key.
    Responses are cached before parsing: a malformed answer is still a paid answer,
    and re-running must not silently spend again (delete the cache file to retry).
    `use_cache=False` skips the read but still writes, so the latest answer is kept.
    The invoice's QR is read from the same bytes, with no model, on every call.
    """
    model = model_name()
    record = cache.get(image_bytes, model, PROMPT_VERSION) if use_cache else None
    cache_hit = record is not None
    if record is None:
        record = _call_model(client or OpenAI(), model, image_bytes)
        cache.set(image_bytes, model, PROMPT_VERSION, record)
    return from_record(image_bytes, model, record, cache_hit)


def from_record(
    image_bytes: bytes, model: str, record: dict[str, Any], cache_hit: bool = True
) -> tuple[ExtractionResult, CallMetadata]:
    """Everything after the model call: a cached or saved answer (app/demo.py) runs
    the same path as a fresh one, with no client and no API key."""
    metadata = _metadata(model, record, cache_hit)
    try:
        invoice, confidences = parse_response(record["content"])
    except ParseError as exc:
        exc.metadata = metadata
        raise
    return validate(invoice, confidences, read_qr(image_bytes)), metadata
