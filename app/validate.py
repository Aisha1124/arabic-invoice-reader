import re
from datetime import UTC
from decimal import Decimal

from app.schema import ExtractionResult, FieldConfidence, Finding, Invoice, LineItem

TOLERANCE = Decimal("0.01")
# ASCII digits only: `\d` and str.isdigit() also accept Arabic-Indic digits.
SELLER_VAT_PATTERN = re.compile(r"[0-9]{15}")
SIMPLIFIED_QR_FIELDS = (
    "seller_name",
    "seller_vat_number",
    "invoice_timestamp",
    "total",
    "vat_total",
)


def _within_tolerance(expected: Decimal, actual: Decimal) -> bool:
    return abs(expected - actual) <= TOLERANCE


def _is_missing(value: object) -> bool:
    return value is None or value == ""


def _check_line_arithmetic(index: int, line: LineItem) -> list[Finding]:
    prefix = f"line_items[{index}]"
    findings: list[Finding] = []
    expected_total = line.quantity * line.unit_price
    if not _within_tolerance(expected_total, line.line_total):
        findings.append(
            Finding(
                rule="line_total_equals_quantity_times_unit_price",
                severity="error",
                message=(
                    f"{prefix}: quantity {line.quantity} × unit_price {line.unit_price}"
                    f" = {expected_total}, but line_total is {line.line_total}"
                ),
                fields=[
                    f"{prefix}.quantity",
                    f"{prefix}.unit_price",
                    f"{prefix}.line_total",
                ],
            )
        )
    expected_vat = line.line_total * line.vat_rate
    if not _within_tolerance(expected_vat, line.vat_amount):
        findings.append(
            Finding(
                rule="vat_amount_equals_line_total_times_vat_rate",
                severity="error",
                message=(
                    f"{prefix}: line_total {line.line_total} × vat_rate {line.vat_rate}"
                    f" = {expected_vat}, but vat_amount is {line.vat_amount}"
                ),
                fields=[
                    f"{prefix}.line_total",
                    f"{prefix}.vat_rate",
                    f"{prefix}.vat_amount",
                ],
            )
        )
    return findings


def _check_sums_against_lines(invoice: Invoice) -> list[Finding]:
    if not invoice.line_items:
        return []
    line_fields = [f"line_items[{i}]" for i in range(len(invoice.line_items))]
    findings: list[Finding] = []
    line_sum = sum((line.line_total for line in invoice.line_items), Decimal(0))
    if invoice.subtotal is not None and not _within_tolerance(
        line_sum, invoice.subtotal
    ):
        findings.append(
            Finding(
                rule="subtotal_equals_sum_of_line_totals",
                severity="error",
                message=(
                    f"sum of line_total values is {line_sum},"
                    f" but subtotal is {invoice.subtotal}"
                ),
                fields=["subtotal", *(f"{f}.line_total" for f in line_fields)],
            )
        )
    vat_sum = sum((line.vat_amount for line in invoice.line_items), Decimal(0))
    if invoice.vat_total is not None and not _within_tolerance(
        vat_sum, invoice.vat_total
    ):
        findings.append(
            Finding(
                rule="vat_total_equals_sum_of_vat_amounts",
                severity="error",
                message=(
                    f"sum of vat_amount values is {vat_sum},"
                    f" but vat_total is {invoice.vat_total}"
                ),
                fields=["vat_total", *(f"{f}.vat_amount" for f in line_fields)],
            )
        )
    return findings


def _check_totals_have_line_items(invoice: Invoice) -> Finding | None:
    if invoice.line_items:
        return None
    present = [
        name
        for name in ("subtotal", "vat_total", "total")
        if getattr(invoice, name) not in (None, 0)
    ]
    if not present:
        return None
    return Finding(
        rule="totals_present_without_line_items",
        severity="error",
        message=(
            "no line items were extracted but non-zero totals are present"
            f" ({', '.join(present)}); the line-item table was missed"
        ),
        fields=["line_items", *present],
    )


def _check_date_matches_timestamp(invoice: Invoice) -> Finding | None:
    """
    Compares invoice_date with the UTC date of invoice_timestamp. A non-UTC
    timestamp near midnight can legitimately differ by a day; we choose not to
    handle timezones here. A naive timestamp is compared as-is.
    """
    if invoice.invoice_date is None or invoice.invoice_timestamp is None:
        return None
    timestamp = invoice.invoice_timestamp
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(UTC)
    if timestamp.date() == invoice.invoice_date:
        return None
    return Finding(
        rule="invoice_date_matches_timestamp",
        severity="error",
        message=(
            f"invoice_date is {invoice.invoice_date} but invoice_timestamp"
            f" {invoice.invoice_timestamp.isoformat()} falls on {timestamp.date()} UTC"
        ),
        fields=["invoice_date", "invoice_timestamp"],
    )


def _check_grand_total(invoice: Invoice) -> Finding | None:
    if invoice.subtotal is None or invoice.vat_total is None or invoice.total is None:
        return None
    expected = invoice.subtotal + invoice.vat_total
    if _within_tolerance(expected, invoice.total):
        return None
    return Finding(
        rule="total_equals_subtotal_plus_vat_total",
        severity="error",
        message=(
            f"subtotal {invoice.subtotal} + vat_total {invoice.vat_total}"
            f" = {expected}, but total is {invoice.total}"
        ),
        fields=["subtotal", "vat_total", "total"],
    )


def _check_seller_vat_format(invoice: Invoice) -> Finding | None:
    vat = invoice.seller_vat_number
    if _is_missing(vat) or SELLER_VAT_PATTERN.fullmatch(vat):
        return None
    return Finding(
        rule="seller_vat_number_is_15_digits",
        severity="warning",
        message=(
            f"seller_vat_number must be exactly 15 ASCII digits,"
            f" received {vat!r} ({len(vat)} characters)"
        ),
        fields=["seller_vat_number"],
    )


def _check_buyer_vat_present(invoice: Invoice) -> Finding | None:
    if invoice.invoice_type != "standard" or not _is_missing(invoice.buyer_vat_number):
        return None
    return Finding(
        rule="standard_invoice_has_buyer_vat_number",
        severity="warning",
        message=(
            "invoice_type is standard but buyer_vat_number is missing;"
            " this is the most common ZATCA clearance rejection"
        ),
        fields=["buyer_vat_number"],
    )


def _check_per_line_vat(invoice: Invoice) -> Finding | None:
    lines = invoice.line_items
    if not lines:
        return None
    vat_fields = [f"line_items[{i}].vat_amount" for i in range(len(lines))]
    all_zero = all(line.vat_amount == 0 for line in lines)
    if all_zero and invoice.vat_total is not None and invoice.vat_total > 0:
        return Finding(
            rule="vat_is_itemised_per_line",
            severity="warning",
            message=(
                f"vat_total is {invoice.vat_total} but no line item carries"
                " its own vat_amount; ZATCA requires per-line VAT"
            ),
            fields=["vat_total", *vat_fields],
        )
    if len(lines) < 2:
        return None
    shared = lines[0].vat_amount
    identical = all(line.vat_amount == shared for line in lines)
    matches_own_line = all(
        _within_tolerance(line.line_total * line.vat_rate, line.vat_amount)
        for line in lines
    )
    if not identical or matches_own_line:
        return None
    return Finding(
        rule="vat_is_itemised_per_line",
        severity="warning",
        message=(
            f"every line item carries the same vat_amount {shared}, which does not"
            " match its own line_total × vat_rate; VAT appears lumped, not per-line"
        ),
        fields=vat_fields,
    )


def _check_simplified_qr_fields(invoice: Invoice) -> Finding | None:
    if invoice.invoice_type != "simplified":
        return None
    missing = [f for f in SIMPLIFIED_QR_FIELDS if _is_missing(getattr(invoice, f))]
    if not missing:
        return None
    return Finding(
        rule="simplified_invoice_has_qr_fields",
        severity="warning",
        message=("simplified invoice is missing TLV QR fields: " + ", ".join(missing)),
        fields=missing,
    )


def _gate_confidences(
    confidences: dict[str, float], threshold: float, error_fields: set[str]
) -> tuple[list[FieldConfidence], list[Finding]]:
    gated: list[FieldConfidence] = []
    findings: list[Finding] = []
    for field, score in confidences.items():
        below = score < threshold
        if below:
            findings.append(
                Finding(
                    rule="confidence_below_threshold",
                    severity="warning",
                    message=(
                        f"{field}: confidence {score:.2f} is below"
                        f" CONFIDENCE_THRESHOLD {threshold:.2f}"
                    ),
                    fields=[field],
                )
            )
        gated.append(
            FieldConfidence(
                field=field,
                confidence=score,
                needs_review=below or field in error_fields,
            )
        )
    return gated, findings


def validate(
    invoice: Invoice, confidences: dict[str, float], threshold: float
) -> ExtractionResult:
    """
    `confidences` maps field paths (e.g. "total", "line_items[0].vat_amount") to scores.
    Any finding of either severity queues the invoice: a warning means the invoice
    itself is non-compliant, and a human must still see that.
    """
    findings: list[Finding] = []
    for index, line in enumerate(invoice.line_items):
        findings.extend(_check_line_arithmetic(index, line))
    findings.extend(_check_sums_against_lines(invoice))
    whole_invoice = (
        _check_totals_have_line_items(invoice),
        _check_grand_total(invoice),
        _check_date_matches_timestamp(invoice),
        _check_seller_vat_format(invoice),
        _check_buyer_vat_present(invoice),
        _check_per_line_vat(invoice),
        _check_simplified_qr_fields(invoice),
    )
    findings.extend(f for f in whole_invoice if f is not None)
    error_fields = {
        field for f in findings if f.severity == "error" for field in f.fields
    }
    gated, low_confidence = _gate_confidences(confidences, threshold, error_fields)
    findings.extend(low_confidence)
    return ExtractionResult(
        invoice=invoice,
        confidences=gated,
        findings=findings,
        status="needs_review" if findings else "ok",
    )
