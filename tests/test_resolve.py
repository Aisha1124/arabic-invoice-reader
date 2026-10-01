from decimal import Decimal

import pytest

from app.resolve import DIGIT_CONFUSIONS, KNOWN_PAIRS, classify_edit, resolve
from app.schema import Invoice, LineItem, Resolution

# A correct invoice: every arithmetic check passes.
#   line 0: 3 x 13.43 = 40.29, VAT 6.04
#   line 1: 2 x 25.00 = 50.00, VAT 7.50
#   line 2: 1 x 84.00 = 84.00, VAT 12.60
#   subtotal 174.29 + VAT 26.14 = total 200.43
LINES = [
    ("3", "13.43", "40.29", "0.15", "6.04"),
    ("2", "25.00", "50.00", "0.15", "7.50"),
    ("1", "84.00", "84.00", "0.15", "12.60"),
]
TOTALS = {"subtotal": "174.29", "vat_total": "26.14", "total": "200.43"}
LINE_FIELDS = ("quantity", "unit_price", "line_total", "vat_rate", "vat_amount")


def _invoice(lines: list[tuple[str, ...]] = LINES, **totals: str | None) -> Invoice:
    return Invoice(
        invoice_type="standard",
        seller_name="شركة الخليج",
        seller_vat_number="300000000000003",
        buyer_name="Buyer",
        buyer_vat_number="310000000000003",
        line_items=[
            LineItem(description=f"item {i}", **dict(zip(LINE_FIELDS, values)))
            for i, values in enumerate(lines)
        ],
        **{**TOTALS, **totals},
    )


def _misread(path: str, value: str) -> Invoice:
    """The correct invoice with one cell replaced by what the model read."""
    if path.startswith("line_items["):
        index, name = int(path[len("line_items[")]), path.split(".")[1]
        lines = [list(line) for line in LINES]
        lines[index][LINE_FIELDS.index(name)] = value
        return _invoice([tuple(line) for line in lines])
    return _invoice(**{path: value})


def test_valid_invoice_needs_no_resolution() -> None:
    resolution = resolve(_invoice())
    assert resolution.status == "not_needed"
    assert resolution.candidates == []
    assert resolution.failed_checks == []


# One known confusion injected per case: (cell, what the model read, true value, edit).
INJECTED = [
    ("line_items[0].unit_price", "12.43", "13.43", "known_substitution"),  # ٣→٢
    ("line_items[1].quantity", "3", "2", "known_substitution"),  # ٢→٣
    ("line_items[2].unit_price", "48.00", "84.00", "adjacent_swap"),  # ٨٤→٤٨
    ("line_items[0].line_total", "4.29", "40.29", "digit_dropped"),
    ("line_items[2].vat_amount", "12.20", "12.60", "known_substitution"),  # ٦→٢
    ("line_items[1].vat_rate", "0.10", "0.15", "known_substitution"),  # ٥→٠
    ("subtotal", "175.29", "174.29", "known_substitution"),  # ٤→٥
    ("vat_total", "266.14", "26.14", "digit_added"),
    ("total", "205.43", "200.43", "known_substitution"),  # ٠→٥
]


@pytest.mark.parametrize(("path", "read", "true", "edit"), INJECTED)
def test_injected_confusion_is_located_and_ranked_first(
    path: str, read: str, true: str, edit: str
) -> None:
    resolution = resolve(_misread(path, read))

    assert resolution.status == "suggested", resolution.reason
    top = resolution.candidates[0]
    assert top.field == path
    assert top.read_value == Decimal(read)
    assert top.value == Decimal(true)
    assert top.edit == edit
    assert top.rank == 1


def test_line_total_error_names_every_check_it_breaks() -> None:
    resolution = resolve(_misread("line_items[0].line_total", "4.29"))
    assert resolution.failed_checks == [
        "line_items[0]: quantity × unit_price = line_total",
        "line_items[0]: line_total × vat_rate = vat_amount",
        "sum of line totals = subtotal",
    ]


def test_fractional_quantity_is_a_valid_candidate() -> None:
    """Weighed goods: 0.270 kg x 49.75 = 13.43. Read as 0.370 (٢→٣)."""
    lines = [("0.370", "49.75", "13.43", "0.15", "2.01")]
    invoice = _invoice(lines, subtotal="13.43", vat_total="2.01", total="15.44")

    resolution = resolve(invoice)

    assert resolution.status == "suggested", resolution.reason
    top = resolution.candidates[0]
    assert (top.field, top.value) == ("line_items[0].quantity", Decimal("0.270"))


def test_confusion_type_outranks_whole_number_preference() -> None:
    """Read 2 where 2.5 is printed: 2.500 is one ٥↔٠ step from 2.000, so it beats a
    whole-number preference that would favour a weaker unit-price candidate."""
    lines = [("2", "10.00", "25.00", "0.15", "3.75")]
    invoice = _invoice(lines, subtotal="25.00", vat_total="3.75", total="28.75")

    top = resolve(invoice).candidates[0]

    assert (top.field, top.value, top.edit) == (
        "line_items[0].quantity",
        Decimal("2.500"),
        "known_substitution",
    )


def test_count_line_prefers_whole_quantity_at_equal_rank() -> None:
    """Read unit price 12.00 where 30.00 is printed; quantity 1 is a count. Both
    cells have a candidate with no known confusion; the fractional quantity 2.500
    ranks below the unit price because the line reads as a count."""
    lines = [("1", "12.00", "30.00", "0.15", "4.50")]
    invoice = _invoice(lines, subtotal="30.00", vat_total="4.50", total="34.50")

    resolution = resolve(invoice)

    assert resolution.status == "suggested", resolution.reason
    top = resolution.candidates[0]
    assert (top.field, top.value) == ("line_items[0].unit_price", Decimal("30.00"))


def test_quantity_or_price_with_equal_evidence_is_ambiguous() -> None:
    """2 x 5.00 = 10.00 read with quantity 5: quantity 2 or unit price 2.00 both fit,
    and neither is a known confusion of 5. The resolver must not pick one."""
    lines = [("5", "5.00", "10.00", "0.15", "1.50")]
    invoice = _invoice(lines, subtotal="10.00", vat_total="1.50", total="11.50")

    resolution = resolve(invoice)

    assert resolution.status == "ambiguous"
    fields = {c.field for c in resolution.candidates}
    assert {"line_items[0].quantity", "line_items[0].unit_price"} <= fields
    assert "cannot choose" in resolution.reason


def test_two_misread_cells_are_unresolvable() -> None:
    invoice = _misread("line_items[0].unit_price", "12.43")
    invoice = invoice.model_copy(update={"total": Decimal("205.43")})

    resolution = resolve(invoice)

    assert resolution.status == "unresolvable"
    assert resolution.candidates == []
    assert "no single cell" in resolution.reason
    assert resolution.failed_checks == [
        "line_items[0]: quantity × unit_price = line_total",
        "subtotal + vat_total = total",
    ]


def test_cell_whose_checks_cannot_all_pass_is_unresolvable() -> None:
    """Line total read as 4.29 on an invoice whose subtotal and total were also
    printed wrong: quantity x unit price needs 40.29, the subtotal needs 46.00."""
    invoice = _misread("line_items[0].line_total", "4.29").model_copy(
        update={"subtotal": Decimal("180.00"), "total": Decimal("206.14")}
    )

    resolution = resolve(invoice)

    assert resolution.status == "unresolvable"
    assert resolution.candidates == []
    assert "no value" in resolution.reason


def test_totals_without_line_items_are_unresolvable() -> None:
    invoice = _invoice([], subtotal="100.00", vat_total="15.00", total="115.00")
    resolution = resolve(invoice)
    assert resolution.status == "unresolvable"
    assert "line-item table" in resolution.reason


def test_lumped_vat_invoice_skips_per_line_vat_checks() -> None:
    lines = [(q, u, t, r, "0") for q, u, t, r, _ in LINES]
    lines[0] = ("3", "13.43", "4.29", "0.15", "0")
    invoice = _invoice(lines)

    resolution = resolve(invoice)

    assert resolution.status == "suggested", resolution.reason
    assert resolution.candidates[0].value == Decimal("40.29")
    assert not any("vat_rate" in check for check in resolution.failed_checks)


def test_candidates_are_plausible_values() -> None:
    for path, read, _, _ in INJECTED:
        for candidate in resolve(_misread(path, read)).candidates:
            places = -candidate.value.as_tuple().exponent
            limit = 3 if candidate.field.endswith(".quantity") else 2
            assert places <= limit, candidate
            assert candidate.value >= 0


def test_resolver_is_deterministic_and_never_changes_the_invoice() -> None:
    invoice = _misread("line_items[0].unit_price", "12.43")
    before = invoice.model_dump()

    first, second = resolve(invoice), resolve(invoice)

    assert first == second
    assert invoice.model_dump() == before
    assert isinstance(first, Resolution)


def test_reason_and_checks_quote_no_amounts() -> None:
    """They go into the content-free resolver log."""
    for path, read, true, _ in INJECTED:
        resolution = resolve(_misread(path, read))
        text = resolution.reason + " ".join(resolution.failed_checks)
        assert read not in text and true not in text, path


@pytest.mark.parametrize(
    ("read", "candidate", "scale", "edit"),
    [
        ("12.43", "13.43", 2, "known_substitution"),
        ("12.43", "17.43", 2, "other_substitution"),  # ٢↔٧ seen once: not known
        ("48.00", "84.00", 2, "adjacent_swap"),
        ("1420.00", "142.00", 2, "digit_added"),
        ("10.85", "102.85", 2, "digit_dropped"),
        ("12.43", "31.43", 2, "other"),
        ("3", "2", 3, "known_substitution"),
    ],
)
def test_classify_edit(read: str, candidate: str, scale: int, edit: str) -> None:
    assert classify_edit(Decimal(read), Decimal(candidate), scale) == edit


def test_known_pairs_are_those_seen_at_least_twice() -> None:
    assert frozenset("23") in KNOWN_PAIRS
    assert frozenset("05") in KNOWN_PAIRS
    assert frozenset("48") in KNOWN_PAIRS
    assert frozenset("27") not in KNOWN_PAIRS
    assert DIGIT_CONFUSIONS[("3", "2")] == 7
