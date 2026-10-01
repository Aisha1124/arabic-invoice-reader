"""
The page's wording functions, run in Node. The Extract tab's findings and the
review cards must describe the same check in the same words. The functions live
in the page's <script id="wording"> block, which touches no DOM so it can run
here. Skipped where Node is not installed.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.resolve import _checks
from app.schema import Invoice, LineItem, ReviewOutcome
from app.validate import validate

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
RULES = sorted(
    set(
        re.findall(
            r'rule="(\w+)"', (ROOT / "app" / "validate.py").read_text(encoding="utf-8")
        )
    )
)
LINE_RULES = {
    "line_total_equals_quantity_times_unit_price": "line_total",
    "vat_amount_equals_line_total_times_vat_rate": "vat_amount",
}

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node is needed to run the page's script"
)


def _wording_script() -> str:
    match = re.search(r'<script id="wording">(.*?)</script>', PAGE, re.DOTALL)
    assert match, 'static/index.html has no <script id="wording"> block'
    return match.group(1)


def _run(calls: str) -> object:
    """Runs the wording block, then `calls`, which must print one JSON value."""
    done = subprocess.run(
        ["node"],
        input=_wording_script() + "\n" + calls,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _fields(rule: str, line: int) -> list[str]:
    if rule in LINE_RULES:
        return [
            f"line_items[{line}].quantity",
            f"line_items[{line}].{LINE_RULES[rule]}",
        ]
    return ["subtotal"]


def _sentences(findings: list[tuple[str, list[str]]]) -> list[str]:
    payload = json.dumps([{"rule": r, "fields": f} for r, f in findings])
    return _run(
        f"console.log(JSON.stringify({payload}.map(f => plainFinding(f.rule, f.fields))))"
    )


def test_wording_block_runs_without_a_page() -> None:
    assert _run(
        'console.log(JSON.stringify(plainField("line_items[0].unit_price")))'
    ) == ("Line 1 · unit price")


def test_every_validation_rule_has_its_own_sentence() -> None:
    assert len(RULES) == 12, RULES
    sentences = _sentences([(rule, _fields(rule, 0)) for rule in RULES])

    for rule, sentence in zip(RULES, sentences, strict=True):
        assert "_" not in sentence, (rule, sentence)
        assert sentence[0].isupper(), (rule, sentence)
    assert len(set(sentences)) == len(RULES)


def test_findings_and_review_cards_use_the_same_words() -> None:
    """Every check the resolver can name, for lines 1 and 3, against the finding
    validate.py raises for it."""
    lines = [
        LineItem(
            description="x",
            quantity="1",
            unit_price="1",
            line_total="1",
            vat_rate="0.15",
            vat_amount="0.15",
        )
    ] * 3
    invoice = Invoice(
        invoice_type="standard",
        line_items=lines,
        subtotal="3",
        vat_total="0.45",
        total="3.45",
    )
    labels = [c.label for c in _checks(invoice)]
    rule_for = {
        "quantity × unit_price = line_total": "line_total_equals_quantity_times_unit_price",
        "line_total × vat_rate = vat_amount": "vat_amount_equals_line_total_times_vat_rate",
        "sum of line totals = subtotal": "subtotal_equals_sum_of_line_totals",
        "sum of line VAT amounts = vat_total": "vat_total_equals_sum_of_vat_amounts",
        "subtotal + vat_total = total": "total_equals_subtotal_plus_vat_total",
    }
    findings = []
    for label in labels:
        line = re.match(r"line_items\[(\d+)\]: (.*)", label)
        rule = rule_for[line.group(2) if line else label]
        findings.append((rule, _fields(rule, int(line.group(1)) if line else 0)))

    from_cards = _run(
        f"console.log(JSON.stringify({json.dumps(labels)}.map(plainCheck)))"
    )

    assert from_cards == _sentences(findings)
    assert "Line 3: quantity × unit price does not equal the line total" in from_cards


def test_extract_tab_headlines_the_sentence_and_keeps_the_rule() -> None:
    script = PAGE[PAGE.index('<script id="wording">') :]
    main = script[script.index("</script>") :]
    assert "plainFinding(f.rule, f.fields)" in main
    assert "f.rule" in main.replace("plainFinding(f.rule, f.fields)", "")


# --- pipeline strip and check map ------------------------------------------------


def _line(quantity: str, unit_price: str, line_total: str, vat: str) -> LineItem:
    return LineItem(
        description="x",
        quantity=quantity,
        unit_price=unit_price,
        line_total=line_total,
        vat_rate="0.15",
        vat_amount=vat,
    )


def _two_lines(**overrides: object) -> Invoice:
    """2 x 100 = 200 and 1 x 50 = 50; VAT 30 + 7.50; seller VAT present."""
    values: dict[str, object] = {
        "invoice_type": "simplified",
        "seller_name": "s",
        "seller_vat_number": "300000000000003",
        "invoice_timestamp": "2026-01-15T10:30:00",
        "line_items": [
            _line("2", "100.00", "200.00", "30.00"),
            _line("1", "50.00", "50.00", "7.50"),
        ],
        "subtotal": "250.00",
        "vat_total": "37.50",
        "total": "287.50",
    }
    values.update(overrides)
    return Invoice(**values)


def _response(invoice: Invoice, review: ReviewOutcome | None = None) -> dict:
    """The /extract body: validate.py's result plus what /extract did about review."""
    body = validate(invoice, {}).model_dump(mode="json")
    body["review"] = review.model_dump(mode="json") if review else None
    return body


def _queued(resolver_status: str) -> ReviewOutcome:
    return ReviewOutcome(
        status="queued", reference="R-0007", resolver_status=resolver_status
    )


MISREAD = _two_lines(
    line_items=[
        _line("2", "100.00", "250.00", "37.50"),
        _line("1", "50.00", "50.00", "7.50"),
    ],
    subtotal="300.00",
    vat_total="45.00",
    total="345.00",
)


def _stages(body: dict) -> list[dict]:
    return _run(f"console.log(JSON.stringify(pipelineStages({json.dumps(body)})))")


def _states(body: dict) -> list[tuple[str, str]]:
    return [(s["stage"], s["state"]) for s in _stages(body)]


def test_clean_invoice_passes_read_and_check_and_needs_nothing_else() -> None:
    assert _states(_response(_two_lines())) == [
        ("Read", "passed"),
        ("Check", "passed"),
        ("Resolve", "not_needed"),
        ("Review", "not_needed"),
    ]


def test_suggested_misread_resolves_and_is_queued_with_its_reference() -> None:
    stages = _stages(_response(MISREAD, _queued("suggested")))

    assert [(s["stage"], s["state"]) for s in stages] == [
        ("Read", "passed"),
        ("Check", "failed"),
        ("Resolve", "passed"),
        ("Review", "queued"),
    ]
    assert "R-0007" in stages[3]["label"]


@pytest.mark.parametrize("resolver_status", ["ambiguous", "unresolvable"])
def test_resolver_without_one_answer_is_a_failed_resolve(resolver_status: str) -> None:
    stages = _stages(_response(MISREAD, _queued(resolver_status)))

    assert stages[2]["state"] == "failed"
    assert resolver_status in stages[2]["label"].lower()
    assert stages[3]["state"] == "queued"


def test_resolver_error_fails_resolve_and_review() -> None:
    body = _response(MISREAD, ReviewOutcome(status="error"))

    assert [state for _, state in _states(body)] == [
        "passed",
        "failed",
        "failed",
        "failed",
    ]


def test_warnings_only_is_amber_not_red_and_queues_nothing() -> None:
    body = _response(_two_lines(seller_vat_number=None))
    assert {f["severity"] for f in body["findings"]} == {"warning"}

    stages = _stages(body)

    assert [s["state"] for s in stages] == [
        "passed",
        "warnings",
        "not_needed",
        "not_needed",
    ]
    assert "warning" in stages[1]["label"]


def test_non_arithmetic_error_is_red_but_needs_no_resolver() -> None:
    """A date that disagrees with the timestamp is a misread (severity error),
    so Check is red; the resolver only works on arithmetic."""
    body = _response(_two_lines(invoice_date="2026-01-16"))
    assert [f["rule"] for f in body["findings"]] == ["invoice_date_matches_timestamp"]

    assert [state for _, state in _states(body)] == [
        "passed",
        "failed",
        "not_needed",
        "not_needed",
    ]


def _map(checks: list[dict]) -> dict:
    return _run(f"console.log(JSON.stringify(checkMap({json.dumps(checks)})))")


def _grid(checks: list[dict]) -> list[list[str]]:
    return [
        [row["label"], *(c["outcome"] if c else "-" for c in row["cells"])]
        for row in _map(checks)["rows"]
    ]


def test_check_map_has_a_row_per_line_and_a_totals_row() -> None:
    checks = _response(MISREAD)["checks"]

    assert _grid(checks) == [
        ["Line 1", "fail", "pass", "-", "-", "-"],
        ["Line 2", "pass", "pass", "-", "-", "-"],
        ["Totals", "-", "-", "pass", "pass", "pass"],
    ]
    assert len(_map(checks)["columns"]) == 5


def test_check_map_shows_skipped_checks_as_not_checked_with_the_reason() -> None:
    lumped = _two_lines(
        line_items=[
            _line("2", "100.00", "200.00", "0"),
            _line("1", "50.00", "50.00", "0"),
        ]
    )
    checks = _response(lumped)["checks"]

    grid = _grid(checks)
    assert grid[0] == ["Line 1", "pass", "not_checked", "-", "-", "-"]
    assert grid[2] == ["Totals", "-", "-", "pass", "not_checked", "pass"]
    cell = _map(checks)["rows"][0]["cells"][1]
    assert cell["reason"] == next(
        c["reason"] for c in checks if c["outcome"] == "not_checked"
    )


def test_check_map_without_line_items_is_the_totals_row_only() -> None:
    checks = _response(_two_lines(line_items=[]))["checks"]

    assert _grid(checks) == [["Totals", "-", "-", "not_checked", "not_checked", "pass"]]


def test_strip_is_drawn_inside_the_result_only_and_nothing_waits() -> None:
    """The result is hidden on an HTTP error, so the strip is never drawn for one.
    No artificial delays: no setTimeout anywhere on the page."""
    result = PAGE.index('<div id="result" hidden>')
    assert PAGE.index('id="pipeline"') > result
    assert "setTimeout" not in PAGE
    main = PAGE[PAGE.index('<script id="wording">') :]
    main = main[main.index("</script>") :]
    assert "pipelineStages(result)" in main
    assert "checkMap(" in main
