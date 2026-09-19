from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.schema import ExtractionResult, Finding, Invoice, LineItem
from app.validate import validate

SELLER_VAT = "300000000000003"
BUYER_VAT = "310000000000003"


def _line(
    quantity: str, unit_price: str, vat_rate: str = "0.15", **overrides: str
) -> LineItem:
    line_total = Decimal(quantity) * Decimal(unit_price)
    values = {
        "description": "item",
        "quantity": quantity,
        "unit_price": unit_price,
        "line_total": str(line_total),
        "vat_rate": vat_rate,
        "vat_amount": str(line_total * Decimal(vat_rate)),
    }
    values.update(overrides)
    return LineItem(**values)


def _invoice(lines: list[LineItem] | None = None, **overrides: object) -> Invoice:
    if lines is None:
        lines = [_line("2", "100.00"), _line("1", "50.00")]
    subtotal = sum((line.line_total for line in lines), Decimal(0))
    vat_total = sum((line.vat_amount for line in lines), Decimal(0))
    values: dict[str, object] = {
        "invoice_number": "INV-1",
        "invoice_date": date(2026, 1, 15),
        "invoice_timestamp": datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
        "invoice_type": "standard",
        "seller_name": "Seller",
        "seller_vat_number": SELLER_VAT,
        "buyer_name": "Buyer",
        "buyer_vat_number": BUYER_VAT,
        "line_items": lines,
        "subtotal": subtotal,
        "vat_total": vat_total,
        "total": subtotal + vat_total,
    }
    values.update(overrides)
    return Invoice(**values)


def _run(
    invoice: Invoice, confidences: dict[str, float] | None = None
) -> ExtractionResult:
    return validate(invoice, confidences or {})


def _by_rule(result: ExtractionResult, rule: str) -> list[Finding]:
    return [f for f in result.findings if f.rule == rule]


# --- baseline -----------------------------------------------------------------


def test_consistent_standard_invoice_has_no_findings_and_status_ok() -> None:
    result = _run(_invoice(), {"total": 0.99, "line_items[0].line_total": 0.95})

    assert result.findings == []
    assert result.status == "ok"
    assert all(not c.needs_review for c in result.confidences)


# --- arithmetic: line_total = quantity * unit_price ---------------------------


def test_line_total_within_tolerance_passes() -> None:
    # 3 * 33.33 = 99.99; a line_total of 100.00 is exactly 0.01 away.
    line = _line("3", "33.33", line_total="100.00", vat_amount="15.00")

    result = _run(_invoice([line]))

    assert _by_rule(result, "line_total_equals_quantity_times_unit_price") == []


def test_line_total_outside_tolerance_is_error() -> None:
    line = _line("3", "33.33", line_total="100.01", vat_amount="15.00")

    result = _run(_invoice([line]))

    [finding] = _by_rule(result, "line_total_equals_quantity_times_unit_price")
    assert finding.severity == "error"
    assert finding.fields == [
        "line_items[0].quantity",
        "line_items[0].unit_price",
        "line_items[0].line_total",
    ]
    assert "99.99" in finding.message
    assert "100.01" in finding.message


# --- arithmetic: vat_amount = line_total * vat_rate ---------------------------


def test_line_vat_within_tolerance_passes() -> None:
    # 100.00 * 0.15 = 15.00; a vat_amount of 15.01 is exactly 0.01 away.
    line = _line("1", "100.00", vat_amount="15.01")

    result = _run(_invoice([line]))

    assert _by_rule(result, "vat_amount_equals_line_total_times_vat_rate") == []


def test_line_vat_outside_tolerance_is_error() -> None:
    line = _line("1", "100.00", vat_amount="14.00")

    result = _run(_invoice([line]))

    [finding] = _by_rule(result, "vat_amount_equals_line_total_times_vat_rate")
    assert finding.severity == "error"
    assert finding.fields == [
        "line_items[0].line_total",
        "line_items[0].vat_rate",
        "line_items[0].vat_amount",
    ]


# --- arithmetic: subtotal = sum(line_total) -----------------------------------


def test_subtotal_matching_line_sum_passes() -> None:
    result = _run(_invoice(subtotal=Decimal("250.01")))

    assert _by_rule(result, "subtotal_equals_sum_of_line_totals") == []


def test_subtotal_not_matching_line_sum_is_error() -> None:
    result = _run(_invoice(subtotal=Decimal("240.00")))

    [finding] = _by_rule(result, "subtotal_equals_sum_of_line_totals")
    assert finding.severity == "error"
    assert finding.fields == [
        "subtotal",
        "line_items[0].line_total",
        "line_items[1].line_total",
    ]


def test_sum_checks_skipped_when_no_line_items() -> None:
    result = _run(_invoice([], subtotal=Decimal("100.00"), vat_total=Decimal("15.00")))

    assert _by_rule(result, "subtotal_equals_sum_of_line_totals") == []
    assert _by_rule(result, "vat_total_equals_sum_of_vat_amounts") == []


# --- arithmetic: vat_total = sum(vat_amount) ----------------------------------


def test_vat_total_matching_line_vat_sum_passes() -> None:
    result = _run(_invoice(vat_total=Decimal("37.49")))

    assert _by_rule(result, "vat_total_equals_sum_of_vat_amounts") == []


def test_vat_total_not_matching_line_vat_sum_is_error() -> None:
    result = _run(_invoice(vat_total=Decimal("30.00")))

    [finding] = _by_rule(result, "vat_total_equals_sum_of_vat_amounts")
    assert finding.severity == "error"
    assert finding.fields == [
        "vat_total",
        "line_items[0].vat_amount",
        "line_items[1].vat_amount",
    ]


# --- arithmetic: totals imply line items --------------------------------------


def test_invoice_with_line_items_and_totals_passes() -> None:
    result = _run(_invoice())

    assert _by_rule(result, "totals_present_without_line_items") == []


def test_no_line_items_and_no_totals_passes() -> None:
    result = _run(_invoice([], subtotal=None, vat_total=None, total=None))

    assert _by_rule(result, "totals_present_without_line_items") == []


def test_no_line_items_and_zero_totals_passes() -> None:
    result = _run(_invoice([]))

    assert _by_rule(result, "totals_present_without_line_items") == []


def test_no_line_items_with_non_zero_totals_is_error() -> None:
    result = _run(
        _invoice(
            [], subtotal=Decimal("100.00"), vat_total=None, total=Decimal("115.00")
        )
    )

    [finding] = _by_rule(result, "totals_present_without_line_items")
    assert finding.severity == "error"
    assert finding.fields == ["line_items", "subtotal", "total"]
    assert "subtotal, total" in finding.message
    assert result.status == "needs_review"


# --- arithmetic: invoice_date agrees with invoice_timestamp -------------------


def test_date_matching_timestamp_passes() -> None:
    result = _run(_invoice())

    assert _by_rule(result, "invoice_date_matches_timestamp") == []


def test_date_check_skipped_when_either_is_missing() -> None:
    without_date = _run(_invoice(invoice_date=None))
    without_timestamp = _run(_invoice(invoice_timestamp=None))

    assert _by_rule(without_date, "invoice_date_matches_timestamp") == []
    assert _by_rule(without_timestamp, "invoice_date_matches_timestamp") == []


def test_offset_timestamp_is_compared_on_its_own_date() -> None:
    # 01:00 at UTC+3 is 22:00 the previous day in UTC; the page says the 16th.
    riyadh = timezone(timedelta(hours=3))
    timestamp = datetime(2026, 1, 16, 1, 0, tzinfo=riyadh)

    local_day = _run(
        _invoice(invoice_date=date(2026, 1, 16), invoice_timestamp=timestamp)
    )
    utc_day = _run(
        _invoice(invoice_date=date(2026, 1, 15), invoice_timestamp=timestamp)
    )

    assert _by_rule(local_day, "invoice_date_matches_timestamp") == []
    assert len(_by_rule(utc_day, "invoice_date_matches_timestamp")) == 1


def test_naive_timestamp_is_compared_as_is() -> None:
    naive = datetime(2026, 1, 15, 23, 59)  # noqa: DTZ001 - naive on purpose
    result = _run(_invoice(invoice_timestamp=naive))

    assert _by_rule(result, "invoice_date_matches_timestamp") == []


def test_date_differing_from_timestamp_is_error() -> None:
    result = _run(_invoice(invoice_date=date(2026, 1, 14)))

    [finding] = _by_rule(result, "invoice_date_matches_timestamp")
    assert finding.severity == "error"
    assert finding.fields == ["invoice_date", "invoice_timestamp"]
    assert "2026-01-14" in finding.message
    assert "2026-01-15" in finding.message
    assert result.status == "needs_review"


# --- arithmetic: total = subtotal + vat_total ---------------------------------


def test_total_matching_subtotal_plus_vat_passes() -> None:
    result = _run(_invoice(total=Decimal("287.49")))

    assert _by_rule(result, "total_equals_subtotal_plus_vat_total") == []


def test_total_not_matching_subtotal_plus_vat_is_error() -> None:
    result = _run(_invoice(total=Decimal("290.00")))

    [finding] = _by_rule(result, "total_equals_subtotal_plus_vat_total")
    assert finding.severity == "error"
    assert finding.fields == ["subtotal", "vat_total", "total"]


def test_total_check_skipped_when_a_component_is_missing() -> None:
    result = _run(_invoice(vat_total=None))

    assert _by_rule(result, "total_equals_subtotal_plus_vat_total") == []


# --- ZATCA: seller VAT is 15 digits -------------------------------------------


def test_fifteen_digit_seller_vat_passes() -> None:
    result = _run(_invoice(seller_vat_number=SELLER_VAT))

    assert _by_rule(result, "seller_vat_number_is_15_digits") == []


def test_absent_seller_vat_is_not_a_format_finding() -> None:
    result = _run(_invoice(seller_vat_number=None))

    assert _by_rule(result, "seller_vat_number_is_15_digits") == []


def test_fourteen_digit_seller_vat_is_warning() -> None:
    result = _run(_invoice(seller_vat_number="30000000000000"))

    [finding] = _by_rule(result, "seller_vat_number_is_15_digits")
    assert finding.severity == "warning"
    assert finding.fields == ["seller_vat_number"]
    assert "14 characters" in finding.message


def test_seller_vat_with_non_ascii_digits_is_warning() -> None:
    result = _run(_invoice(seller_vat_number="٣٠٠٠٠٠٠٠٠٠٠٠٠٠٣"))

    assert len(_by_rule(result, "seller_vat_number_is_15_digits")) == 1


# --- ZATCA: seller VAT number present on every invoice type --------------------


def test_present_seller_vat_passes() -> None:
    result = _run(_invoice(seller_vat_number=SELLER_VAT))

    assert _by_rule(result, "seller_vat_number_present") == []


@pytest.mark.parametrize("invoice_type", ["standard", "simplified", "unknown"])
def test_missing_seller_vat_is_warning_on_every_invoice_type(
    invoice_type: str,
) -> None:
    result = _run(_invoice(invoice_type=invoice_type, seller_vat_number=None))

    [finding] = _by_rule(result, "seller_vat_number_present")
    assert finding.severity == "warning"
    assert finding.fields == ["seller_vat_number"]
    assert _by_rule(result, "seller_vat_number_is_15_digits") == []


def test_empty_string_seller_vat_is_warning() -> None:
    result = _run(_invoice(seller_vat_number=""))

    assert len(_by_rule(result, "seller_vat_number_present")) == 1


# --- ZATCA: standard invoice has buyer VAT ------------------------------------


def test_standard_invoice_with_buyer_vat_passes() -> None:
    result = _run(_invoice(invoice_type="standard", buyer_vat_number=BUYER_VAT))

    assert _by_rule(result, "standard_invoice_has_buyer_vat_number") == []


def test_simplified_invoice_without_buyer_vat_is_not_flagged() -> None:
    result = _run(_invoice(invoice_type="simplified", buyer_vat_number=None))

    assert _by_rule(result, "standard_invoice_has_buyer_vat_number") == []


def test_standard_invoice_without_buyer_vat_is_warning() -> None:
    result = _run(_invoice(invoice_type="standard", buyer_vat_number=None))

    [finding] = _by_rule(result, "standard_invoice_has_buyer_vat_number")
    assert finding.severity == "warning"
    assert finding.fields == ["buyer_vat_number"]


def test_standard_invoice_with_empty_string_buyer_vat_is_warning() -> None:
    result = _run(_invoice(invoice_type="standard", buyer_vat_number=""))

    assert len(_by_rule(result, "standard_invoice_has_buyer_vat_number")) == 1


# --- ZATCA: VAT itemised per line ---------------------------------------------


def test_per_line_vat_passes() -> None:
    result = _run(_invoice([_line("2", "100.00"), _line("1", "50.00")]))

    assert _by_rule(result, "vat_is_itemised_per_line") == []


def test_identical_lines_with_correct_vat_are_not_flagged_as_lumped() -> None:
    result = _run(_invoice([_line("1", "100.00"), _line("1", "100.00")]))

    assert _by_rule(result, "vat_is_itemised_per_line") == []


def test_zero_rated_invoice_is_not_flagged_as_lumped() -> None:
    lines = [_line("1", "100.00", vat_rate="0"), _line("1", "50.00", vat_rate="0")]

    result = _run(_invoice(lines))

    assert _by_rule(result, "vat_is_itemised_per_line") == []


def test_lines_with_zero_vat_but_positive_vat_total_is_warning() -> None:
    lines = [_line("1", "100.00", vat_amount="0"), _line("1", "50.00", vat_amount="0")]

    result = _run(_invoice(lines, vat_total=Decimal("22.50")))

    [finding] = _by_rule(result, "vat_is_itemised_per_line")
    assert finding.severity == "warning"
    assert finding.fields == [
        "vat_total",
        "line_items[0].vat_amount",
        "line_items[1].vat_amount",
    ]


def test_correctly_extracted_lumped_vat_invoice_yields_only_the_warning() -> None:
    lines = [_line("1", "100.00", vat_amount="0"), _line("1", "50.00", vat_amount="0")]

    result = _run(
        _invoice(lines, vat_total=Decimal("22.50"), total=Decimal("172.50")),
        {"line_items[0].vat_amount": 0.99, "vat_total": 0.99},
    )

    [finding] = result.findings
    assert finding.rule == "vat_is_itemised_per_line"
    assert finding.severity == "warning"
    assert result.status == "needs_review"
    # A warning queues the invoice but does not force needs_review on fields.
    assert not any(c.needs_review for c in result.confidences)


def test_lumped_vat_does_not_skip_line_total_arithmetic() -> None:
    lines = [
        _line("1", "100.00", vat_amount="0", line_total="999.00"),
        _line("1", "50.00", vat_amount="0"),
    ]

    result = _run(_invoice(lines, vat_total=Decimal("22.50")))

    assert len(_by_rule(result, "line_total_equals_quantity_times_unit_price")) == 1
    assert _by_rule(result, "vat_amount_equals_line_total_times_vat_rate") == []
    assert _by_rule(result, "vat_total_equals_sum_of_vat_amounts") == []


def test_every_line_carrying_the_invoice_vat_figure_is_warning() -> None:
    lines = [
        _line("1", "100.00", vat_amount="22.50"),
        _line("1", "50.00", vat_amount="22.50"),
    ]

    result = _run(_invoice(lines))

    [finding] = _by_rule(result, "vat_is_itemised_per_line")
    assert finding.severity == "warning"
    assert finding.fields == ["line_items[0].vat_amount", "line_items[1].vat_amount"]
    assert "22.50" in finding.message


def test_single_line_is_never_flagged_as_lumped() -> None:
    result = _run(_invoice([_line("1", "100.00", vat_amount="99.00")]))

    assert _by_rule(result, "vat_is_itemised_per_line") == []


# --- ZATCA: simplified invoice carries the five TLV QR fields -----------------


def test_simplified_invoice_with_all_qr_fields_passes() -> None:
    result = _run(_invoice(invoice_type="simplified"))

    assert _by_rule(result, "simplified_invoice_has_qr_fields") == []


def test_standard_invoice_missing_qr_fields_is_not_flagged_by_this_rule() -> None:
    result = _run(_invoice(invoice_type="standard", seller_name=None))

    assert _by_rule(result, "simplified_invoice_has_qr_fields") == []


def test_simplified_invoice_missing_qr_fields_is_warning() -> None:
    result = _run(
        _invoice(
            invoice_type="simplified",
            seller_vat_number=None,
            invoice_timestamp=None,
            total=None,
        )
    )

    [finding] = _by_rule(result, "simplified_invoice_has_qr_fields")
    assert finding.severity == "warning"
    assert finding.fields == ["invoice_timestamp", "total"]
    assert "invoice_timestamp, total" in finding.message
    assert len(_by_rule(result, "seller_vat_number_present")) == 1


def test_simplified_invoice_with_date_but_no_timestamp_is_warning() -> None:
    result = _run(_invoice(invoice_type="simplified", invoice_timestamp=None))

    [finding] = _by_rule(result, "simplified_invoice_has_qr_fields")
    assert finding.fields == ["invoice_timestamp"]


# --- confidence gating --------------------------------------------------------


def test_low_confidence_is_recorded_but_never_gates() -> None:
    result = _run(_invoice(), {"total": 0.0, "seller_name": 0.99})

    by_field = {c.field: c for c in result.confidences}
    assert by_field["total"].confidence == 0.0
    assert by_field["total"].needs_review is False
    assert by_field["seller_name"].needs_review is False
    assert result.findings == []
    assert result.status == "ok"


def test_arithmetic_error_forces_needs_review_despite_high_confidence() -> None:
    result = _run(_invoice(total=Decimal("290.00")), {"total": 0.99, "subtotal": 0.99})

    by_field = {c.field: c for c in result.confidences}
    assert by_field["total"].needs_review is True
    assert by_field["subtotal"].needs_review is True
    assert result.status == "needs_review"


def test_arithmetic_error_sets_needs_review_status_without_confidence_entries() -> None:
    result = _run(_invoice(total=Decimal("290.00")), {})

    assert result.status == "needs_review"


def test_structural_warning_alone_sets_needs_review_status() -> None:
    result = _run(_invoice(buyer_vat_number=None), {"total": 0.99})

    assert [f.severity for f in result.findings] == ["warning"]
    assert all(not c.needs_review for c in result.confidences)
    assert result.status == "needs_review"
