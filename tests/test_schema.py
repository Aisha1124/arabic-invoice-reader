from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schema import ExtractionResult, FieldConfidence, Finding, Invoice, LineItem


def test_line_item_parses_decimal_strings_with_thousands_separator() -> None:
    item = LineItem(
        description="Consulting",
        quantity="2",
        unit_price="1,234.50",
        line_total="2,469.00",
        vat_rate="0.15",
        vat_amount="370.35",
    )

    assert item.unit_price == Decimal("1234.50")
    assert item.line_total == Decimal("2469.00")
    assert all(
        isinstance(v, Decimal)
        for v in (
            item.quantity,
            item.unit_price,
            item.line_total,
            item.vat_rate,
            item.vat_amount,
        )
    )


def test_invoice_totals_parse_decimal_strings_with_thousands_separator() -> None:
    invoice = Invoice(
        invoice_type="standard",
        line_items=[],
        subtotal="10,000.00",
        vat_total="1,500.00",
        total="11,500.00",
    )

    assert invoice.subtotal == Decimal("10000.00")
    assert invoice.vat_total == Decimal("1500.00")
    assert invoice.total == Decimal("11500.00")


def test_invoice_timestamp_parses_iso_8601_with_utc_suffix() -> None:
    invoice = Invoice(
        invoice_type="simplified",
        line_items=[],
        invoice_timestamp="2026-07-31T15:38:00Z",
    )

    assert invoice.invoice_timestamp == datetime(2026, 7, 31, 15, 38, tzinfo=UTC)


def test_invoice_with_every_optional_field_absent() -> None:
    invoice = Invoice(invoice_type="unknown", line_items=[])

    assert invoice.invoice_number is None
    assert invoice.invoice_date is None
    assert invoice.invoice_timestamp is None
    assert invoice.seller_name is None
    assert invoice.seller_vat_number is None
    assert invoice.buyer_name is None
    assert invoice.buyer_vat_number is None
    assert invoice.subtotal is None
    assert invoice.vat_total is None
    assert invoice.total is None
    assert invoice.currency == "SAR"


def test_non_numeric_amount_raises_validation_error_naming_the_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        LineItem(
            description="Consulting",
            quantity="two",
            unit_price="100.00",
            line_total="200.00",
            vat_rate="0.15",
            vat_amount="30.00",
        )

    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("quantity",)
    assert errors[0]["type"] == "decimal_parsing"


def test_float_amount_is_rejected_with_clear_message() -> None:
    with pytest.raises(ValidationError) as exc_info:
        LineItem(
            description="Consulting",
            quantity="1",
            unit_price=1234.5,
            line_total="1234.50",
            vat_rate="0.15",
            vat_amount="185.18",
        )

    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("unit_price",)
    assert "must not be a float" in errors[0]["msg"]
    assert "1234.5" in errors[0]["msg"]


def test_float_invoice_total_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Invoice(invoice_type="standard", line_items=[], total=11500.0)

    assert exc_info.value.errors()[0]["loc"] == ("total",)


def test_unknown_invoice_type_raises_validation_error() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Invoice(invoice_type="credit_note", line_items=[])

    assert exc_info.value.errors()[0]["loc"] == ("invoice_type",)


def test_malformed_date_raises_validation_error() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Invoice(invoice_type="standard", line_items=[], invoice_date="31/02/2026")

    assert exc_info.value.errors()[0]["loc"] == ("invoice_date",)


def test_confidence_outside_unit_interval_is_rejected() -> None:
    with pytest.raises(ValidationError):
        FieldConfidence(field="total", confidence=1.5, needs_review=False)


def test_extraction_result_rejects_unknown_status() -> None:
    invoice = Invoice(invoice_type="unknown", line_items=[])

    finding = Finding(
        rule="subtotal_matches_line_totals",
        severity="error",
        message="sum of line totals 100.00 does not equal subtotal 90.00",
        fields=["subtotal"],
    )

    with pytest.raises(ValidationError) as exc_info:
        ExtractionResult(
            invoice=invoice, confidences=[], findings=[finding], status="failed"
        )

    assert exc_info.value.errors()[0]["loc"] == ("status",)


def test_finding_rejects_unknown_severity() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Finding(rule="x", severity="info", message="m", fields=[])

    assert exc_info.value.errors()[0]["loc"] == ("severity",)
