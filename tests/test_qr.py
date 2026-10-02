"""
ZATCA QR decoding, no model. tests/fixtures/zatca_qr.png is a synthetic QR made
once with the `qrcode` package: base64 of TLV tags 1-5 holding seller
"شركة الاختبار", VAT 300000000000003, 2026-06-16T14:23:00Z, total 115.00, VAT 15.00.
"""

import base64
import io
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

from app.qr import parse_payload, read_qr
from app.schema import QrPayload

FIXTURE = Path(__file__).parent / "fixtures" / "zatca_qr.png"
SAMPLES = Path(__file__).resolve().parent.parent / "eval" / "samples"
EXPECTED = QrPayload(
    seller_name="شركة الاختبار",
    seller_vat_number="300000000000003",
    timestamp=datetime(2026, 6, 16, 14, 23),  # noqa: DTZ001 - wall clock, no zone
    total=Decimal("115.00"),
    vat_total=Decimal("15.00"),
)
VALUES = ("شركة الاختبار", "300000000000003", "2026-06-16", "115.00", "15.00")


def _tlv(*fields: tuple[int, bytes]) -> str:
    raw = b"".join(bytes([tag, len(value)]) + value for tag, value in fields)
    return base64.b64encode(raw).decode("ascii")


def _five(**replace: bytes) -> list[tuple[int, bytes]]:
    values = {
        1: "شركة الاختبار".encode(),
        2: b"300000000000003",
        3: b"2026-06-16T14:23:00Z",
        4: b"115.00",
        5: b"15.00",
    }
    for tag, value in replace.items():
        values[int(tag[1:])] = value
    return list(values.items())


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _assert_not_read(result: QrPayload | str, reason: str) -> None:
    assert isinstance(result, str), result
    assert reason in result
    for value in VALUES:
        assert value not in result, "a not-read reason must not quote the payload"


# --- images -----------------------------------------------------------------------


def test_reads_the_five_values_from_a_qr_image() -> None:
    assert read_qr(FIXTURE.read_bytes()) == EXPECTED


def test_image_without_a_qr_is_not_read() -> None:
    blank = _png(Image.new("L", (400, 300), 255))
    _assert_not_read(read_qr(blank), "no QR code found")


def test_two_qr_codes_are_not_read_rather_than_guessed() -> None:
    qr = Image.open(FIXTURE)
    page = Image.new("L", (qr.width * 2, qr.height), 255)
    page.paste(qr, (0, 0))
    page.paste(qr, (qr.width, 0))
    _assert_not_read(read_qr(_png(page)), "2 QR codes found")


def test_bytes_that_are_not_an_image_are_not_read() -> None:
    _assert_not_read(read_qr(b"\x89PNG\r\n\x1a\nnot really"), "could not be opened")


# --- payload ----------------------------------------------------------------------


def test_payload_parses_tags_one_to_five() -> None:
    assert parse_payload(_tlv(*_five())) == EXPECTED


@pytest.mark.parametrize(
    ("stamp", "wall_clock"),
    [
        (b"2026-06-16T14:23:00Z", datetime(2026, 6, 16, 14, 23)),  # noqa: DTZ001
        (b"2026-06-16T14:23:00+03:00", datetime(2026, 6, 16, 14, 23)),  # noqa: DTZ001
        (b"2026-06-16T14:23:00", datetime(2026, 6, 16, 14, 23)),  # noqa: DTZ001
    ],
)
def test_timestamp_keeps_the_wall_clock_and_drops_the_zone(
    stamp: bytes, wall_clock: datetime
) -> None:
    payload = parse_payload(_tlv(*_five(t3=stamp)))
    assert isinstance(payload, QrPayload)
    assert payload.timestamp == wall_clock
    assert payload.timestamp.tzinfo is None


def test_phase_two_tags_after_five_are_skipped() -> None:
    """Tags 6-9 (hash, signature, key, stamp) are binary and not compared."""
    fields = [*_five(), (6, b"\xff\x00\x81"), (8, bytes(range(40)))]
    assert parse_payload(_tlv(*fields)) == EXPECTED


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("https://example.com/invoice", "not base64"),
        (_tlv(*_five()[:3]), "missing tags 4, 5"),
        (_tlv(*_five(), (2, b"300000000000003")), "tag 2 appears twice"),
        (_tlv(*_five(t1=b"\xff\xfe")), "tag 1 is not UTF-8 text"),
        (_tlv(*_five(t3=b"16/06/2026 14:23")), "tag 3 is not an ISO 8601 timestamp"),
        (_tlv(*_five(t4=b"115,00")), "tag 4 is not a decimal amount"),
        (_tlv(*_five(t5=b"")), "tag 5 is not a decimal amount"),
    ],
)
def test_malformed_payloads_are_not_read(text: str, reason: str) -> None:
    _assert_not_read(parse_payload(text), reason)


def test_truncated_payload_is_not_read() -> None:
    raw = base64.b64decode(_tlv(*_five()))
    cut = base64.b64encode(raw[:-3]).decode("ascii")
    _assert_not_read(parse_payload(cut), "tag 5 runs past the end of the payload")


def test_tag_header_cut_off_is_not_read() -> None:
    raw = base64.b64decode(_tlv(*_five())) + b"\x06"
    cut = base64.b64encode(raw).decode("ascii")
    _assert_not_read(parse_payload(cut), "ends inside a tag header")


# --- the eval set: the decode-rate check, kept --------------------------------------


@pytest.mark.skipif(
    not SAMPLES.is_dir(), reason="eval/samples/ is gitignored; generate it to run this"
)
def test_every_eval_qr_decodes_to_its_ground_truth_payload_and_no_other_finds_one() -> (
    None
):
    truth = json.loads((SAMPLES.parent / "ground_truth.json").read_text("utf-8"))
    for record in truth:
        result = read_qr((SAMPLES / record["file"]).read_bytes())
        if record["qr_base64"]:
            assert result == parse_payload(record["qr_base64"]), record["file"]
        else:
            assert result == "no QR code found", record["file"]
