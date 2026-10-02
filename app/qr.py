"""
Reads the ZATCA QR code on an invoice image, with no model: zxing-cpp finds and
decodes the QR, and this module parses its base64 TLV payload (tags 1-5: seller
name, seller VAT number, timestamp, total incl. VAT, VAT total).

Anything short of exactly one QR holding all five tags returns the reason it was
not read; nothing is guessed or repaired. Reasons name the failure, never the
payload, so they are safe to show and log. The payload itself is never logged.
"""

import base64
import binascii
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation

import zxingcpp
from PIL import Image, UnidentifiedImageError

from app.schema import QrPayload

# Seller name, seller VAT number, timestamp, total, VAT total. Tags 6-9 (ZATCA
# phase 2: hash, signature, public key, stamp) are binary and skipped.
TAGS = (1, 2, 3, 4, 5)


def read_qr(image_bytes: bytes) -> QrPayload | str:
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except (UnidentifiedImageError, OSError):
        return "the image could not be opened to look for a QR code"
    codes = zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.QRCode)
    if not codes:
        return "no QR code found"
    if len(codes) > 1:
        return f"{len(codes)} QR codes found; cannot tell which one is the invoice's"
    return parse_payload(codes[0].text)


def parse_payload(text: str) -> QrPayload | str:
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return "the QR code is not base64 text, so not a ZATCA TLV payload"
    values = _tlv(raw)
    if isinstance(values, str):
        return values
    missing = [str(tag) for tag in TAGS if tag not in values]
    if missing:
        return f"the QR payload is missing tags {', '.join(missing)}"
    return _typed(values)


def _tlv(raw: bytes) -> dict[int, bytes] | str:
    values: dict[int, bytes] = {}
    i = 0
    while i < len(raw):
        if i + 2 > len(raw):
            return "the QR payload ends inside a tag header"
        tag, length = raw[i], raw[i + 1]
        if i + 2 + length > len(raw):
            return f"tag {tag} runs past the end of the payload"
        if tag in values:
            return f"tag {tag} appears twice in the QR payload"
        values[tag] = raw[i + 2 : i + 2 + length]
        i += 2 + length
    return values


def _typed(values: dict[int, bytes]) -> QrPayload | str:
    text: dict[int, str] = {}
    for tag in TAGS:
        try:
            text[tag] = values[tag].decode("utf-8")
        except UnicodeDecodeError:
            return f"tag {tag} is not UTF-8 text"
    try:
        # Wall clock as written: the page prints no zone, so a Z or offset is dropped.
        timestamp = datetime.fromisoformat(text[3]).replace(tzinfo=None)
    except ValueError:
        return "tag 3 is not an ISO 8601 timestamp"
    amounts: dict[int, Decimal] = {}
    for tag in (4, 5):
        try:
            amounts[tag] = Decimal(text[tag])
        except InvalidOperation:
            return f"tag {tag} is not a decimal amount"
        if not amounts[tag].is_finite():
            return f"tag {tag} is not a decimal amount"
    return QrPayload(
        seller_name=text[1],
        seller_vat_number=text[2],
        timestamp=timestamp,
        total=amounts[4],
        vat_total=amounts[5],
    )
