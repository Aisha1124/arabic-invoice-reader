import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from openai import BadRequestError

from app import cache, extract
from app.extract import PROMPT_VERSION, ParseError, normalise_digits, parse_response

FIXTURES = Path(__file__).parent / "fixtures"
ENGLISH = (FIXTURES / "response_english.json").read_text(encoding="utf-8")
ARABIC_INDIC = (FIXTURES / "response_arabic_indic.json").read_text(encoding="utf-8")
PNG = b"\x89PNG\r\n\x1a\n" + b"not really an image"
MODEL = "test-vision-model"


@dataclass
class _Usage:
    prompt_tokens: int = 1200
    completion_tokens: int = 300


@dataclass
class _Message:
    content: str | None


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Response:
    choices: list[_Choice]
    usage: _Usage | None = field(default_factory=_Usage)


class _Completions:
    def __init__(self, content: str | None) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> _Response:
        self.calls.append(kwargs)
        return _Response(choices=[_Choice(message=_Message(content=self.content))])


class _RejectsTemperature(_Completions):
    def create(self, **kwargs: object) -> _Response:
        if "temperature" in kwargs:
            self.calls.append(kwargs)
            request = httpx2.Request(
                "POST", "https://api.openai.com/v1/chat/completions"
            )
            raise BadRequestError(
                "Unsupported value: 'temperature' does not support 0 with this model.",
                response=httpx2.Response(400, request=request),
                body=None,
            )
        return super().create(**kwargs)


class FakeClient:
    """Stands in for openai.OpenAI; records requests, never touches the network."""

    def __init__(
        self, content: str | None = ENGLISH, rejects_temperature: bool = False
    ) -> None:
        completions = _RejectsTemperature if rejects_temperature else _Completions
        self.completions = completions(content)
        self.chat = self


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("OPENAI_MODEL", MODEL)
    monkeypatch.delenv("CONFIDENCE_THRESHOLD", raising=False)
    for name in extract.PRICE_ENV:
        monkeypatch.delenv(name, raising=False)


def test_extract_english_fixture_is_clean() -> None:
    client = FakeClient()
    result, _ = extract.extract(PNG, client=client)

    assert result.status == "ok"
    assert result.findings == []
    assert result.invoice.invoice_number == "INV-2026-1000"
    assert result.invoice.total == Decimal("43113.56")
    assert isinstance(result.invoice.line_items[0].vat_amount, Decimal)
    assert {c.field for c in result.confidences} >= {
        "total",
        "line_items[0].vat_amount",
    }
    assert not any(c.needs_review for c in result.confidences)


def test_request_uses_env_model_and_json_mode() -> None:
    client = FakeClient()
    extract.extract(PNG, client=client)

    (call,) = client.completions.calls
    assert call["model"] == MODEL
    assert call["temperature"] == 0
    assert call["response_format"] == {"type": "json_object"}
    parts = call["messages"][0]["content"]
    assert parts[0]["text"] == extract.PROMPT
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_temperature_rejection_is_retried_once_without_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeClient(rejects_temperature=True)
    with caplog.at_level(logging.WARNING, logger="app.extract"):
        result, metadata = extract.extract(PNG, client=client)

    first, second = client.completions.calls
    assert first["temperature"] == 0
    assert "temperature" not in second
    assert metadata.temperature_zero is False
    assert result.status == "ok"
    assert "rejected temperature=0" in caplog.text
    record = cache.get(PNG, MODEL, PROMPT_VERSION)
    assert record is not None
    assert record["temperature_zero"] is False


def test_other_bad_requests_are_not_retried() -> None:
    class Rejects(_Completions):
        def create(self, **kwargs: object) -> _Response:
            self.calls.append(kwargs)
            request = httpx2.Request("POST", "https://api.openai.com/v1/x")
            raise BadRequestError(
                "invalid image",
                response=httpx2.Response(400, request=request),
                body=None,
            )

    client = FakeClient()
    client.completions = Rejects(ENGLISH)
    with pytest.raises(BadRequestError, match="invalid image"):
        extract.extract(PNG, client=client)
    assert len(client.completions.calls) == 1


def test_use_cache_false_skips_read_but_writes() -> None:
    first = FakeClient()
    extract.extract(PNG, client=first)
    payload = json.loads(ENGLISH)
    payload["invoice"]["invoice_number"] = "INV-FRESH"
    second = FakeClient(json.dumps(payload))

    result, metadata = extract.extract(PNG, client=second, use_cache=False)

    assert len(second.completions.calls) == 1
    assert metadata.cache_hit is False
    assert result.invoice.invoice_number == "INV-FRESH"
    record = cache.get(PNG, MODEL, PROMPT_VERSION)
    assert record is not None
    assert "INV-FRESH" in record["content"]


def test_second_call_is_served_from_cache() -> None:
    client = FakeClient()
    extract.extract(PNG, client=client)
    assert cache.get(PNG, MODEL, PROMPT_VERSION) is not None

    class Exploding:
        def __getattr__(self, name: str) -> object:
            raise AssertionError("API must not be called on a cache hit")

    again, metadata = extract.extract(PNG, client=Exploding())
    assert again.invoice.total == Decimal("43113.56")
    assert len(client.completions.calls) == 1
    assert metadata.cache_hit is True


def test_metadata_on_miss_and_hit() -> None:
    _, miss = extract.extract(PNG, client=FakeClient())
    assert miss.cache_hit is False
    assert miss.model == MODEL
    assert miss.prompt_version == PROMPT_VERSION
    assert miss.prompt_tokens == 1200
    assert miss.completion_tokens == 300
    assert miss.latency_ms >= 0
    assert miss.estimated_cost_usd is None
    assert miss.temperature_zero is True

    _, hit = extract.extract(PNG, client=FakeClient())
    assert hit.cache_hit is True
    assert (hit.latency_ms, hit.prompt_tokens, hit.completion_tokens) == (
        miss.latency_ms,
        miss.prompt_tokens,
        miss.completion_tokens,
    )


def test_usage_is_cached_with_response() -> None:
    _, metadata = extract.extract(PNG, client=FakeClient())
    record = cache.get(PNG, MODEL, PROMPT_VERSION)
    assert record is not None
    assert record["content"] == ENGLISH
    assert record["prompt_tokens"] == 1200
    assert record["completion_tokens"] == 300
    assert record["latency_ms"] == metadata.latency_ms
    assert record["temperature_zero"] is True


def test_malformed_cached_record_raises() -> None:
    cache.set(PNG, MODEL, PROMPT_VERSION, {"content": ENGLISH})
    with pytest.raises(RuntimeError, match="cached response record is malformed"):
        extract.extract(PNG, client=FakeClient())


def test_cache_key_includes_model_and_prompt_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extract.extract(PNG, client=FakeClient())
    assert cache.get(PNG, "other-model", PROMPT_VERSION) is None
    assert cache.get(PNG, MODEL, "other-version") is None
    monkeypatch.setenv("OPENAI_MODEL", "other-model")
    client = FakeClient()
    extract.extract(PNG, client=client)
    assert len(client.completions.calls) == 1


def test_arabic_indic_numerals_are_normalised() -> None:
    result, _ = extract.extract(PNG, client=FakeClient(ARABIC_INDIC))
    invoice = result.invoice

    assert invoice.invoice_number == "INV-2026-1002"
    assert invoice.seller_vat_number == "310122393500003"
    assert str(invoice.invoice_date) == "2026-07-31"
    assert invoice.line_items[0].unit_price == Decimal("4157.37")
    assert invoice.line_items[0].vat_rate == Decimal("0.15")
    assert invoice.subtotal == Decimal("37416.33")
    assert invoice.total == Decimal("43028.78")
    assert result.status == "ok", [f.message for f in result.findings]


def test_free_text_fields_keep_arabic_indic_digits() -> None:
    result, _ = extract.extract(PNG, client=FakeClient(ARABIC_INDIC))
    assert result.invoice.line_items[0].description == "استشارة هندسية ٣"
    assert result.invoice.seller_name == "شركة الخليج للمعدات الطبية"


@pytest.mark.parametrize("suffix", ["Z", "+03:00", "+0300", "-05:00", ""])
def test_timezone_suffix_is_stripped_from_timestamp(suffix: str) -> None:
    payload = json.loads(ENGLISH)
    payload["invoice"]["invoice_timestamp"] = "2026-07-31T15:38:00" + suffix
    invoice, _ = parse_response(json.dumps(payload))

    assert invoice.invoice_timestamp == datetime(2026, 7, 31, 15, 38)  # noqa: DTZ001
    assert invoice.invoice_timestamp.tzinfo is None


def test_normalise_digits_touches_numeric_fields_only() -> None:
    assert normalise_digits(
        {
            "seller_name": "متجر ٣",
            "total": "١٬٠٠٠٫٥٠",
            "subtotal": None,
            "line_items": [{"description": "صنف ٧", "quantity": "٢"}, "junk"],
        }
    ) == {
        "seller_name": "متجر ٣",
        "total": "1,000.50",
        "subtotal": None,
        "line_items": [{"description": "صنف ٧", "quantity": "2"}, "junk"],
    }


def test_json_numbers_are_parsed_as_decimal() -> None:
    payload = json.loads(ENGLISH)
    payload["invoice"]["total"] = 43113.56
    payload["invoice"]["line_items"][0]["vat_amount"] = 5612.45
    invoice, _ = parse_response(json.dumps(payload))
    assert invoice.total == Decimal("43113.56")
    assert invoice.line_items[0].vat_amount == Decimal("5612.45")


def test_low_confidence_is_recorded_without_a_finding() -> None:
    payload = json.loads(ENGLISH)
    payload["confidence"]["seller_vat_number"] = 0.5
    result, _ = extract.extract(PNG, client=FakeClient(json.dumps(payload)))

    assert result.status == "ok"
    assert result.findings == []
    by_field = {c.field: c for c in result.confidences}
    assert by_field["seller_vat_number"].confidence == 0.5
    assert by_field["seller_vat_number"].needs_review is False


def test_missing_model_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_MODEL")
    with pytest.raises(RuntimeError, match="OPENAI_MODEL is not set"):
        extract.extract(PNG, client=FakeClient())


def test_unsupported_image_raises() -> None:
    with pytest.raises(ValueError, match="expected PNG or JPEG"):
        extract.extract(b"GIF89a....", client=FakeClient())


MINIMAL_INVOICE = '{"invoice": {"invoice_type": "standard", "line_items": []}, '


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("{not json", "not valid JSON"),
        ('{"invoice": {}}', "keys 'invoice' and 'confidence'"),
        ('{"invoice": [], "confidence": {}}', "'invoice' must be an object"),
        (
            '{"invoice": {"invoice_type": "standard"}, "confidence": {}}',
            "Invoice schema",
        ),
        (MINIMAL_INVOICE + '"confidence": [0.5]}', "'confidence' must be an object"),
        (MINIMAL_INVOICE + '"confidence": {"total": "high"}}', "must be a number"),
        (MINIMAL_INVOICE + '"confidence": {"total": 1.5}}', "between 0 and 1"),
    ],
)
def test_malformed_response_raises_parse_error_with_raw(
    content: str, match: str
) -> None:
    with pytest.raises(ParseError, match=match) as info:
        extract.extract(PNG, client=FakeClient(content))
    assert info.value.raw == content


def test_empty_model_content_raises_parse_error() -> None:
    with pytest.raises(ParseError, match="no message content"):
        extract.extract(PNG, client=FakeClient(None))


def test_malformed_response_is_still_cached() -> None:
    with pytest.raises(ParseError):
        extract.extract(PNG, client=FakeClient("{not json"))
    cached = cache.get(PNG, MODEL, PROMPT_VERSION)
    assert cached is not None
    assert cached["content"] == "{not json"


def test_usage_logged_without_prices(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.extract"):
        extract.extract(PNG, client=FakeClient())
    assert "cache_hit=False temperature_zero=True" in caplog.text
    assert "prompt_tokens=1200 completion_tokens=300" in caplog.text
    assert "estimated_cost=unknown" in caplog.text


def test_usage_logged_with_prices(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENAI_PRICE_INPUT_PER_1M_USD", "2.50")
    monkeypatch.setenv("OPENAI_PRICE_OUTPUT_PER_1M_USD", "10.00")
    with caplog.at_level(logging.INFO, logger="app.extract"):
        _, metadata = extract.extract(PNG, client=FakeClient())
    # 1200 * 2.50 / 1e6 + 300 * 10.00 / 1e6
    assert metadata.estimated_cost_usd == Decimal("0.006")
    assert "estimated_cost=$0.006000" in caplog.text
