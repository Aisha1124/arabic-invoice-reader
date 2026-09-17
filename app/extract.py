import base64
import json
import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Any

from openai import OpenAI
from pydantic import ValidationError

from app import cache
from app.schema import ExtractionResult, Invoice
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
    content = response.choices[0].message.content
    if content is None:
        raise ParseError("model returned no message content", raw="")
    usage = response.usage
    return {
        "content": content,
        "prompt_tokens": usage.prompt_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
    }


def _log_usage(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    cost = _estimated_cost_usd(prompt_tokens, completion_tokens)
    cost_text = (
        f"${cost:.6f}"
        if cost is not None
        else f"unknown (set {' and '.join(PRICE_ENV)})"
    )
    logger.info(
        "model=%s prompt_tokens=%d completion_tokens=%d estimated_cost=%s",
        model,
        prompt_tokens,
        completion_tokens,
        cost_text,
    )


def normalise_digits(value: Any) -> Any:
    """Recursively map Arabic-Indic digits and separators to ASCII in every string."""
    if isinstance(value, str):
        return value.translate(ARABIC_INDIC)
    if isinstance(value, list):
        return [normalise_digits(v) for v in value]
    if isinstance(value, dict):
        return {k: normalise_digits(v) for k, v in value.items()}
    return value


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
    payload = normalise_digits(payload)
    try:
        invoice = Invoice.model_validate(payload["invoice"])
    except ValidationError as exc:
        raise ParseError(
            f"model response does not match the Invoice schema: {exc}", raw=content
        ) from exc
    return invoice, _parse_confidences(payload["confidence"], content)


def extract(image_bytes: bytes, client: OpenAI | None = None) -> ExtractionResult:
    """
    `client` is only constructed on a cache miss, so cached images need no API key.
    Responses are cached before parsing: a malformed answer is still a paid answer,
    and re-running must not silently spend again (delete the cache file to retry).
    """
    model = model_name()
    response = cache.get(image_bytes, model, PROMPT_VERSION)
    if response is None:
        response = _call_model(client or OpenAI(), model, image_bytes)
        cache.set(image_bytes, model, PROMPT_VERSION, response)
        _log_usage(model, response["prompt_tokens"], response["completion_tokens"])
    else:
        logger.info("model=%s cache hit, no API call", model)
    invoice, confidences = parse_response(response["content"])
    return validate(invoice, confidences, _confidence_threshold())
