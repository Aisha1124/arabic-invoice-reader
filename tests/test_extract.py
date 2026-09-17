import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

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


class FakeClient:
    """Stands in for openai.OpenAI; records requests, never touches the network."""

    def __init__(self, content: str | None = ENGLISH) -> None:
        self.completions = _Completions(content)
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
    result = extract.extract(PNG, client=client)

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
    assert call["response_format"] == {"type": "json_object"}
    parts = call["messages"][0]["content"]
    assert parts[0]["text"] == extract.PROMPT
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_second_call_is_served_from_cache() -> None:
    client = FakeClient()
    extract.extract(PNG, client=client)
    assert cache.get(PNG, MODEL, PROMPT_VERSION) is not None

    class Exploding:
        def __getattr__(self, name: str) -> object:
            raise AssertionError("API must not be called on a cache hit")

    again = extract.extract(PNG, client=Exploding())
    assert again.invoice.total == Decimal("43113.56")
    assert len(client.completions.calls) == 1


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
    result = extract.extract(PNG, client=FakeClient(ARABIC_INDIC))
    invoice = result.invoice

    assert invoice.invoice_number == "INV-2026-1002"
    assert invoice.seller_vat_number == "310122393500003"
    assert str(invoice.invoice_date) == "2026-07-31"
    assert invoice.line_items[0].unit_price == Decimal("4157.37")
    assert invoice.line_items[0].vat_rate == Decimal("0.15")
    assert invoice.subtotal == Decimal("37416.33")
    assert invoice.total == Decimal("43028.78")
    assert invoice.line_items[0].description == "استشارة هندسية 3"
    assert result.status == "ok", [f.message for f in result.findings]


def test_normalise_digits_leaves_non_strings_alone() -> None:
    assert normalise_digits({"a": ["١", 2, None, Decimal(3)]}) == {
        "a": ["1", 2, None, Decimal(3)]
    }


def test_json_numbers_are_parsed_as_decimal() -> None:
    payload = json.loads(ENGLISH)
    payload["invoice"]["total"] = 43113.56
    payload["invoice"]["line_items"][0]["vat_amount"] = 5612.45
    invoice, _ = parse_response(json.dumps(payload))
    assert invoice.total == Decimal("43113.56")
    assert invoice.line_items[0].vat_amount == Decimal("5612.45")


def test_low_confidence_marks_field_for_review() -> None:
    payload = json.loads(ENGLISH)
    payload["confidence"]["seller_vat_number"] = 0.5
    result = extract.extract(PNG, client=FakeClient(json.dumps(payload)))

    assert result.status == "needs_review"
    (finding,) = result.findings
    assert finding.rule == "confidence_below_threshold"
    assert finding.fields == ["seller_vat_number"]
    flagged = {c.field for c in result.confidences if c.needs_review}
    assert flagged == {"seller_vat_number"}


def test_missing_model_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_MODEL")
    with pytest.raises(RuntimeError, match="OPENAI_MODEL is not set"):
        extract.extract(PNG, client=FakeClient())


def test_invalid_threshold_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "high")
    with pytest.raises(RuntimeError, match="CONFIDENCE_THRESHOLD"):
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
    assert "prompt_tokens=1200 completion_tokens=300" in caplog.text
    assert "estimated_cost=unknown" in caplog.text


def test_usage_logged_with_prices(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENAI_PRICE_INPUT_PER_1M_USD", "2.50")
    monkeypatch.setenv("OPENAI_PRICE_OUTPUT_PER_1M_USD", "10.00")
    with caplog.at_level(logging.INFO, logger="app.extract"):
        extract.extract(PNG, client=FakeClient())
    # 1200 * 2.50 / 1e6 + 300 * 10.00 / 1e6
    assert "estimated_cost=$0.006000" in caplog.text
