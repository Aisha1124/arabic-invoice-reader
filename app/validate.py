import re
from datetime import datetime
from decimal import Decimal
from typing import NamedTuple

from app.schema import (
    CheckOutcome,
    ExtractionResult,
    FieldConfidence,
    Finding,
    Invoice,
    LineItem,
    QrPayload,
    QrStatus,
)

TOLERANCE = Decimal("0.01")
# ASCII digits only: `\d` and str.isdigit() also accept Arabic-Indic digits.
SELLER_VAT_PATTERN = re.compile(r"[0-9]{15}")
# Seller VAT is the fifth TLV field; it is required on every invoice type, so
# seller_vat_number_present covers it rather than this simplified-only check.
SIMPLIFIED_QR_FIELDS = ("seller_name", "invoice_timestamp", "total", "vat_total")
# In check-map column order. The Finding(rule=...) literals below repeat these names;
# tests/test_validate.py holds the two in step.
ARITHMETIC_CHECKS = (
    "line_total_equals_quantity_times_unit_price",
    "vat_amount_equals_line_total_times_vat_rate",
    "subtotal_equals_sum_of_line_totals",
    "vat_total_equals_sum_of_vat_amounts",
    "total_equals_subtotal_plus_vat_total",
)
LINE_TOTAL, LINE_VAT, SUBTOTAL_SUM, VAT_SUM, GRAND_TOTAL = ARITHMETIC_CHECKS
# Errors that are not arithmetic: two readings that should agree do not. Nothing for
# the resolver; app/main.py queues them for a person to check against paper.
QR_RULES = frozenset(
    {
        "seller_vat_number_matches_qr",
        "timestamp_matches_qr",
        "total_matches_qr",
        "vat_total_matches_qr",
    }
)
CROSS_CHECK_RULES = QR_RULES | {"invoice_date_matches_timestamp"}
NOT_SCANNED = "no image was scanned for a QR code"
# Reasons a check did not run. They name conditions, never amounts.
LUMPED = "VAT is lumped: no line carries its own VAT amount"
NO_LINES = "no line items were read"


class Ran(NamedTuple):
    """An arithmetic check that ran: what the other cells add up to, the value read
    against it, and the finding when the two differ by more than TOLERANCE."""

    computed: Decimal
    read: Decimal
    finding: Finding | None


def _within_tolerance(expected: Decimal, actual: Decimal) -> bool:
    return abs(expected - actual) <= TOLERANCE


def _is_missing(value: object) -> bool:
    return value is None or value == ""


def _is_lumped_vat(invoice: Invoice) -> bool:
    """No per-line VAT on the page: a document defect, not a misread, so the
    per-line VAT arithmetic is skipped and vat_is_itemised_per_line reports it."""
    return (
        bool(invoice.line_items)
        and all(line.vat_amount == 0 for line in invoice.line_items)
        and invoice.vat_total is not None
        and invoice.vat_total > 0
    )


def _check_line_total(prefix: str, line: LineItem) -> Ran:
    expected = line.quantity * line.unit_price
    if _within_tolerance(expected, line.line_total):
        return Ran(expected, line.line_total, None)
    finding = Finding(
        rule="line_total_equals_quantity_times_unit_price",
        severity="error",
        message=(
            f"{prefix}: quantity {line.quantity} × unit_price {line.unit_price}"
            f" = {expected}, but line_total is {line.line_total}"
        ),
        fields=[f"{prefix}.quantity", f"{prefix}.unit_price", f"{prefix}.line_total"],
    )
    return Ran(expected, line.line_total, finding)


def _check_line_vat(prefix: str, line: LineItem) -> Ran:
    expected = line.line_total * line.vat_rate
    if _within_tolerance(expected, line.vat_amount):
        return Ran(expected, line.vat_amount, None)
    finding = Finding(
        rule="vat_amount_equals_line_total_times_vat_rate",
        severity="error",
        message=(
            f"{prefix}: line_total {line.line_total} × vat_rate {line.vat_rate}"
            f" = {expected}, but vat_amount is {line.vat_amount}"
        ),
        fields=[f"{prefix}.line_total", f"{prefix}.vat_rate", f"{prefix}.vat_amount"],
    )
    return Ran(expected, line.vat_amount, finding)


def _check_subtotal(invoice: Invoice) -> Ran | str:
    if not invoice.line_items:
        return NO_LINES
    if invoice.subtotal is None:
        return "subtotal not read"
    line_sum = sum((line.line_total for line in invoice.line_items), Decimal(0))
    if _within_tolerance(line_sum, invoice.subtotal):
        return Ran(line_sum, invoice.subtotal, None)
    finding = Finding(
        rule="subtotal_equals_sum_of_line_totals",
        severity="error",
        message=(
            f"sum of line_total values is {line_sum},"
            f" but subtotal is {invoice.subtotal}"
        ),
        fields=[
            "subtotal",
            *(f"line_items[{i}].line_total" for i in range(len(invoice.line_items))),
        ],
    )
    return Ran(line_sum, invoice.subtotal, finding)


def _check_vat_sum(invoice: Invoice) -> Ran | str:
    if not invoice.line_items:
        return NO_LINES
    if _is_lumped_vat(invoice):
        return LUMPED
    if invoice.vat_total is None:
        return "vat_total not read"
    vat_sum = sum((line.vat_amount for line in invoice.line_items), Decimal(0))
    if _within_tolerance(vat_sum, invoice.vat_total):
        return Ran(vat_sum, invoice.vat_total, None)
    finding = Finding(
        rule="vat_total_equals_sum_of_vat_amounts",
        severity="error",
        message=(
            f"sum of vat_amount values is {vat_sum},"
            f" but vat_total is {invoice.vat_total}"
        ),
        fields=[
            "vat_total",
            *(f"line_items[{i}].vat_amount" for i in range(len(invoice.line_items))),
        ],
    )
    return Ran(vat_sum, invoice.vat_total, finding)


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
    Both values are wall-clock readings from the same page, so the timestamp's
    own date is compared directly; no zone conversion is applied.
    """
    if invoice.invoice_date is None or invoice.invoice_timestamp is None:
        return None
    if invoice.invoice_timestamp.date() == invoice.invoice_date:
        return None
    return Finding(
        rule="invoice_date_matches_timestamp",
        severity="error",
        message=(
            f"invoice_date is {invoice.invoice_date} but invoice_timestamp"
            f" {invoice.invoice_timestamp.isoformat()} falls on"
            f" {invoice.invoice_timestamp.date()}"
        ),
        fields=["invoice_date", "invoice_timestamp"],
    )


def _shown(value: object) -> str:
    return "nothing" if value is None else str(value)


def _check_qr_seller_vat(invoice: Invoice, qr: QrPayload) -> Finding | None:
    if invoice.seller_vat_number == qr.seller_vat_number:
        return None
    return Finding(
        rule="seller_vat_number_matches_qr",
        severity="error",
        message=(
            f"QR code says {qr.seller_vat_number},"
            f" the model read {_shown(invoice.seller_vat_number)}"
        ),
        fields=["seller_vat_number"],
    )


def _check_qr_timestamp(invoice: Invoice, qr: QrPayload) -> Finding | None:
    """To the minute, zone dropped: the page prints HH:MM with no zone, so seconds
    and a QR's Z or offset cannot be read from it."""
    said = qr.timestamp.replace(second=0, microsecond=0)
    read = invoice.invoice_timestamp
    if read is not None:
        read = read.replace(tzinfo=None, second=0, microsecond=0)
    misread = []
    if read != said:
        misread.append(("invoice_timestamp", f"timestamp {_minute(read)}"))
    if invoice.invoice_date != said.date():
        misread.append(("invoice_date", f"date {_shown(invoice.invoice_date)}"))
    if not misread:
        return None
    return Finding(
        rule="timestamp_matches_qr",
        severity="error",
        message=(
            f"QR code says {_minute(said)}, the model read"
            f" {' and '.join(text for _, text in misread)}"
        ),
        fields=[field for field, _ in misread],
    )


def _minute(value: datetime | None) -> str:
    return "nothing" if value is None else f"{value:%Y-%m-%d %H:%M}"


def _check_qr_amount(
    rule: str, field: str, read: Decimal | None, said: Decimal
) -> Finding | None:
    if read is not None and _within_tolerance(said, read):
        return None
    return Finding(
        rule=rule,
        severity="error",
        message=f"QR code says {said}, the model read {_shown(read)}",
        fields=[field],
    )


def _qr_findings(invoice: Invoice, qr: QrPayload) -> list[Finding]:
    found = (
        _check_qr_seller_vat(invoice, qr),
        _check_qr_timestamp(invoice, qr),
        _check_qr_amount(
            rule="total_matches_qr", field="total", read=invoice.total, said=qr.total
        ),
        _check_qr_amount(
            rule="vat_total_matches_qr",
            field="vat_total",
            read=invoice.vat_total,
            said=qr.vat_total,
        ),
    )
    return [f for f in found if f is not None]


def _check_grand_total(invoice: Invoice) -> Ran | str:
    if invoice.subtotal is None or invoice.vat_total is None or invoice.total is None:
        missing = [
            n for n in ("subtotal", "vat_total", "total") if getattr(invoice, n) is None
        ]
        return f"{', '.join(missing)} not read"
    expected = invoice.subtotal + invoice.vat_total
    if _within_tolerance(expected, invoice.total):
        return Ran(expected, invoice.total, None)
    finding = Finding(
        rule="total_equals_subtotal_plus_vat_total",
        severity="error",
        message=(
            f"subtotal {invoice.subtotal} + vat_total {invoice.vat_total}"
            f" = {expected}, but total is {invoice.total}"
        ),
        fields=["subtotal", "vat_total", "total"],
    )
    return Ran(expected, invoice.total, finding)


def _check_seller_vat_present(invoice: Invoice) -> Finding | None:
    if not _is_missing(invoice.seller_vat_number):
        return None
    return Finding(
        rule="seller_vat_number_present",
        severity="warning",
        message=(
            "seller_vat_number is missing; ZATCA requires it on standard and"
            " simplified invoices alike"
        ),
        fields=["seller_vat_number"],
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
    if _is_lumped_vat(invoice):
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


def _arithmetic(invoice: Invoice) -> tuple[list[Finding], list[CheckOutcome]]:
    """Every arithmetic check, in check-map order. Each check returns what it ran
    (Ran) or the reason it could not run; a check that did not run is recorded as
    not_checked, never left out where it would read as a pass."""
    lumped = LUMPED if _is_lumped_vat(invoice) else None
    ran: list[tuple[str, int | None, Ran | str]] = []
    for i, line in enumerate(invoice.line_items):
        prefix = f"line_items[{i}]"
        ran.append((LINE_TOTAL, i, _check_line_total(prefix, line)))
        ran.append((LINE_VAT, i, lumped or _check_line_vat(prefix, line)))
    ran.append((SUBTOTAL_SUM, None, _check_subtotal(invoice)))
    ran.append((VAT_SUM, None, _check_vat_sum(invoice)))
    ran.append((GRAND_TOTAL, None, _check_grand_total(invoice)))
    findings = [r.finding for _, _, r in ran if isinstance(r, Ran) and r.finding]
    return findings, [_outcome(rule, line, result) for rule, line, result in ran]


def _outcome(rule: str, line: int | None, result: Ran | str) -> CheckOutcome:
    if isinstance(result, str):
        return CheckOutcome(rule=rule, line=line, outcome="not_checked", reason=result)
    return CheckOutcome(
        rule=rule,
        line=line,
        outcome="fail" if result.finding else "pass",
        computed=result.computed,
        difference=result.read - result.computed,
    )


def validate(
    invoice: Invoice, confidences: dict[str, float], qr: QrPayload | str = NOT_SCANNED
) -> ExtractionResult:
    """
    `confidences` maps field paths (e.g. "total", "line_items[0].vat_amount") to the
    model's self-reported scores. They are recorded, never acted on: measured on this
    project's eval set they were inversely calibrated (CLAUDE.md section 7), so
    needs_review comes from findings alone. Any finding of either severity queues the
    invoice: a warning means the invoice itself is non-compliant, and a human must
    still see that.

    `qr` is the invoice's decoded QR (app/qr.py), or the reason it was not read; a
    QR that was not read raises no finding, since there is nothing to compare.
    """
    findings, checks = _arithmetic(invoice)
    whole_invoice = (
        _check_totals_have_line_items(invoice),
        _check_date_matches_timestamp(invoice),
        _check_seller_vat_present(invoice),
        _check_seller_vat_format(invoice),
        _check_buyer_vat_present(invoice),
        _check_per_line_vat(invoice),
        _check_simplified_qr_fields(invoice),
    )
    findings.extend(f for f in whole_invoice if f is not None)
    if isinstance(qr, QrPayload):
        findings.extend(_qr_findings(invoice, qr))
    error_fields = {
        field for f in findings if f.severity == "error" for field in f.fields
    }
    return ExtractionResult(
        invoice=invoice,
        confidences=[
            FieldConfidence(
                field=field, confidence=score, needs_review=field in error_fields
            )
            for field, score in confidences.items()
        ],
        findings=findings,
        status="needs_review" if findings else "ok",
        checks=checks,
        qr=(
            QrStatus(status="read")
            if isinstance(qr, QrPayload)
            else QrStatus(status="not_read", reason=qr)
        ),
    )
