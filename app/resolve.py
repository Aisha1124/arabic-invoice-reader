"""
Deterministic resolver for failed invoice arithmetic: which cell was most likely
misread, and which values would make the invoice add up. Pure Python, no model
call. It only suggests: the invoice is never changed, and every suggestion goes
to a person through app/review.py.

It assumes one misread cell. Each arithmetic check involves a fixed set of cells,
so one misread breaks exactly the checks that contain it, and the set of failed
checks points back to the cell. Quantity and unit price share their only check, so
both are tried. If no cell matches the failed checks, more than one value is wrong
and the resolver says so instead of guessing.
"""

from collections.abc import Callable, Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import NamedTuple

from app.schema import Candidate, Edit, Invoice, Reading, Resolution
from app.validate import ARITHMETIC_CHECKS, TOLERANCE, _is_lumped_vat, validate

# gpt-4o misreading Arabic-Indic receipt crops, (printed digit, digit read) -> count,
# over two prompt versions on 29 clean CORU OCR test-split lines (2026-09-29). One
# model and a small set: a ranking prior, not a measured error rate.
DIGIT_CONFUSIONS: dict[tuple[str, str], int] = {
    ("3", "2"): 7,
    ("5", "0"): 5,
    ("2", "3"): 3,
    ("3", "4"): 2,
    ("6", "2"): 2,
    ("8", "4"): 2,
    ("4", "5"): 2,
    ("8", "1"): 1,
    ("2", "7"): 1,
    ("5", "6"): 1,
    ("0", "1"): 1,
    ("1", "4"): 1,
    ("4", "0"): 1,
    ("2", "0"): 1,
}
# Same runs, whole-number errors. Kept for provenance; ranking treats all three as known.
STRUCTURAL_CONFUSIONS = {"digit_added": 14, "digit_dropped": 8, "adjacent_swap": 2}
# A digit pair is a known confusion when seen at least this often, in either direction.
KNOWN_MIN_COUNT = 2


def _known_pairs() -> frozenset[frozenset[str]]:
    counts: dict[frozenset[str], int] = {}
    for pair, count in DIGIT_CONFUSIONS.items():
        counts[frozenset(pair)] = counts.get(frozenset(pair), 0) + count
    return frozenset(p for p, n in counts.items() if n >= KNOWN_MIN_COUNT)


KNOWN_PAIRS = _known_pairs()
TIER: dict[str, int] = {
    "known_substitution": 0,
    "adjacent_swap": 0,
    "digit_added": 0,
    "digit_dropped": 0,
    "other_substitution": 1,
    "other": 2,
}
ARITHMETIC_RULES = frozenset(ARITHMETIC_CHECKS)
LINE_CELLS = ("quantity", "unit_price", "line_total", "vat_rate", "vat_amount")
TOTAL_CELLS = ("subtotal", "vat_total", "total")
AMOUNT_STEP = Decimal("0.01")  # amounts: at most 2 decimals
QUANTITY_STEP = Decimal("0.001")  # weighed goods: up to 3 decimals (0.270 kg)
MAX_CANDIDATES = 5
# Wider than this and the arithmetic does not narrow the cell down.
MAX_GRID = 1000


class Check(NamedTuple):
    label: str  # names fields, never amounts: it is logged
    cells: tuple[str, ...]
    residual: Callable[
        [dict[str, Decimal]], Decimal
    ]  # expected - actual, as validate.py


class Option(NamedTuple):
    cell: str
    read: Decimal
    value: Decimal
    edit: Edit
    fractional_count: bool  # a fractional quantity on a line that reads as a count
    distance: Decimal  # from the middle of the range the arithmetic allows


def _values(invoice: Invoice) -> dict[str, Decimal]:
    values = {
        name: getattr(invoice, name)
        for name in TOTAL_CELLS
        if getattr(invoice, name) is not None
    }
    for i, line in enumerate(invoice.line_items):
        for name in LINE_CELLS:
            values[f"line_items[{i}].{name}"] = getattr(line, name)
    return values


def _line_checks(i: int, check_vat: bool) -> list[Check]:
    q, u, t, r, v = (f"line_items[{i}].{name}" for name in LINE_CELLS)
    checks = [
        Check(
            f"line_items[{i}]: quantity × unit_price = line_total",
            (q, u, t),
            lambda x: x[q] * x[u] - x[t],
        )
    ]
    if check_vat:
        checks.append(
            Check(
                f"line_items[{i}]: line_total × vat_rate = vat_amount",
                (t, r, v),
                lambda x: x[t] * x[r] - x[v],
            )
        )
    return checks


def _sum_check(label: str, parts: list[str], total: str) -> Check:
    return Check(label, (*parts, total), lambda x: sum(x[p] for p in parts) - x[total])


def _checks(invoice: Invoice) -> list[Check]:
    """The arithmetic validate.py checks, skipped under the same conditions."""
    check_vat = not _is_lumped_vat(invoice)
    n = range(len(invoice.line_items))
    out = [c for i in n for c in _line_checks(i, check_vat)]
    if invoice.subtotal is not None:
        totals = [f"line_items[{i}].line_total" for i in n]
        out.append(_sum_check("sum of line totals = subtotal", totals, "subtotal"))
    if check_vat and invoice.vat_total is not None:
        vats = [f"line_items[{i}].vat_amount" for i in n]
        out.append(_sum_check("sum of line VAT amounts = vat_total", vats, "vat_total"))
    if None not in (invoice.subtotal, invoice.vat_total, invoice.total):
        out.append(
            Check(
                "subtotal + vat_total = total",
                TOTAL_CELLS,
                lambda x: x["subtotal"] + x["vat_total"] - x["total"],
            )
        )
    return out


def _digits(value: Decimal, scale: int) -> str:
    return format(abs(value), f".{scale}f").replace(".", "")


def _one_deletion(longer: str, shorter: str) -> bool:
    return len(longer) == len(shorter) + 1 and any(
        longer[:i] + longer[i + 1 :] == shorter for i in range(len(longer))
    )


def classify_edit(read: Decimal, candidate: Decimal, scale: int) -> Edit:
    """How the candidate differs from what was read, comparing digit strings at a
    fixed scale so 142.00 against 1420.00 is one added digit."""
    r, c = _digits(read, scale), _digits(candidate, scale)
    if len(r) == len(c):
        diff = [i for i, (x, y) in enumerate(zip(r, c)) if x != y]
        if len(diff) == 1:
            pair = frozenset((r[diff[0]], c[diff[0]]))
            return "known_substitution" if pair in KNOWN_PAIRS else "other_substitution"
        if (
            len(diff) == 2
            and diff[1] == diff[0] + 1
            and (r[diff[0]], r[diff[1]]) == (c[diff[1]], c[diff[0]])
        ):
            return "adjacent_swap"
        return "other"
    if _one_deletion(r, c):
        return "digit_added"
    if _one_deletion(c, r):
        return "digit_dropped"
    return "other"


def _range(
    check: Check, cell: str, values: dict[str, Decimal]
) -> tuple[Decimal, Decimal] | None:
    """Every check is linear in any one of its cells, so two evaluations give the
    range of values that keep |residual| within the tolerance."""
    at_zero = check.residual({**values, cell: Decimal(0)})
    slope = check.residual({**values, cell: Decimal(1)}) - at_zero
    if slope == 0:
        return None
    ends = ((-TOLERANCE - at_zero) / slope, (TOLERANCE - at_zero) / slope)
    return min(ends), max(ends)


def _grid(low: Decimal, high: Decimal, step: Decimal) -> list[Decimal] | None:
    first = int((low / step).to_integral_value(ROUND_CEILING))
    last = int((high / step).to_integral_value(ROUND_FLOOR))
    if last - first + 1 > MAX_GRID:
        return None
    return [k * step for k in range(max(first, 0), last + 1)]


def _with(invoice: Invoice, cell: str, value: Decimal) -> Invoice:
    if not cell.startswith("line_items["):
        return invoice.model_copy(update={cell: value})
    index, name = int(cell[len("line_items[") : cell.index("]")]), cell.split(".")[1]
    lines = list(invoice.line_items)
    lines[index] = lines[index].model_copy(update={name: value})
    return invoice.model_copy(update={"line_items": lines})


def _passes(invoice: Invoice, cell: str, value: Decimal) -> bool:
    """The final word is validate.py's own, on a copy with the cell replaced."""
    findings = validate(_with(invoice, cell, value), {}).findings
    return not any(f.rule in ARITHMETIC_RULES for f in findings)


def _options(
    invoice: Invoice, all_checks: list[Check], cell: str
) -> list[Option] | None:
    """Plausible values for one cell; None when the arithmetic allows too many."""
    values = _values(invoice)
    ranges = [_range(c, cell, values) for c in all_checks if cell in c.cells]
    if not ranges or None in ranges:
        return []
    low = max(r[0] for r in ranges if r)
    high = min(r[1] for r in ranges if r)
    quantity = cell.endswith(".quantity")
    grid = (
        _grid(low, high, QUANTITY_STEP if quantity else AMOUNT_STEP)
        if low <= high
        else []
    )
    if grid is None:
        return None
    read, middle = values[cell], (low + high) / 2
    count_line = quantity and read == read.to_integral_value()
    return [
        Option(
            cell,
            read,
            value,
            classify_edit(read, value, 3 if quantity else 2),
            count_line and value != value.to_integral_value(),
            abs(value - middle),
        )
        for value in grid
        if not (quantity and value == 0) and _passes(invoice, cell, value)
    ]


def _rank_key(option: Option) -> tuple[object, ...]:
    """Confusion type first; the whole-number preference only breaks ties, so a
    known confusion to a fractional quantity (2 read for 2.5) still wins."""
    return (
        TIER[option.edit],
        option.fractional_count,
        option.distance,
        option.cell,
        option.value,
    )


def _tied(a: Option, b: Option) -> bool:
    """Equal evidence: same confusion tier and whole-number standing, and either
    different cells (their distances are in different units) or equal distance."""
    same_rank = (TIER[a.edit], a.fractional_count) == (TIER[b.edit], b.fractional_count)
    return same_rank and (a.cell != b.cell or a.distance == b.distance)


def _result(
    status: str, reason: str, failed: list[Check], options: Sequence[Option] = ()
) -> Resolution:
    return Resolution(
        status=status,
        reason=reason,
        failed_checks=[c.label for c in failed],
        candidates=[
            Candidate(
                field=o.cell, read_value=o.read, value=o.value, edit=o.edit, rank=n
            )
            for n, o in enumerate(options[:MAX_CANDIDATES], start=1)
        ],
    )


def _suspects(all_checks: list[Check], failed: list[Check]) -> list[str]:
    """Cells that take part in exactly the failed checks and no others."""
    membership: dict[str, set[str]] = {}
    for check in all_checks:
        for cell in check.cells:
            membership.setdefault(cell, set()).add(check.label)
    failed_labels = {c.label for c in failed}
    return [cell for cell, labels in membership.items() if labels == failed_labels]


def resolve(invoice: Invoice) -> Resolution:
    if not invoice.line_items:
        if any(getattr(invoice, name) not in (None, 0) for name in TOTAL_CELLS):
            return _result(
                "unresolvable",
                "the line-item table is missing, so there is no arithmetic to localise a misread",
                [],
            )
        return _result("not_needed", "no line items and no totals to check", [])
    all_checks = _checks(invoice)
    values = _values(invoice)
    failed = [c for c in all_checks if abs(c.residual(values)) > TOLERANCE]
    if not failed:
        return _result("not_needed", "every arithmetic check passes", [])
    involved = list(dict.fromkeys(cell for c in failed for cell in c.cells))
    readings = [Reading(field=cell, read_value=values[cell]) for cell in involved]
    return _localise(invoice, all_checks, failed).model_copy(
        update={"involved": readings}
    )


def _localise(
    invoice: Invoice, all_checks: list[Check], failed: list[Check]
) -> Resolution:
    suspects = _suspects(all_checks, failed)
    if not suspects:
        return _result(
            "unresolvable",
            "no single cell takes part in exactly the failed checks;"
            " more than one value is probably misread",
            failed,
        )
    return _rank(invoice, all_checks, failed, suspects)


def _rank(
    invoice: Invoice, all_checks: list[Check], failed: list[Check], suspects: list[str]
) -> Resolution:
    found = {cell: _options(invoice, all_checks, cell) for cell in suspects}
    options = sorted(
        (o for opts in found.values() if opts for o in opts), key=_rank_key
    )
    cells = ", ".join(suspects)
    if not options:
        wide = [cell for cell, opts in found.items() if opts is None]
        reason = (
            f"too many values fit {', '.join(wide)}; the arithmetic does not narrow it down"
            if wide
            else f"no value for {cells} satisfies all of its checks;"
            " more than one value is probably misread"
        )
        return _result("unresolvable", reason, failed)
    if len(options) > 1 and _tied(options[0], options[1]):
        between = ", ".join(sorted({options[0].cell, options[1].cell}))
        return _result(
            "ambiguous",
            f"the arithmetic fits more than one reading and the confusion table"
            f" cannot choose between the top candidates for {between}",
            failed,
            options,
        )
    return _result(
        "suggested",
        f"the failed checks point to {cells}; top candidate ranked by confusion type",
        failed,
        options,
    )
