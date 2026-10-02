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

from app.resolve import _checks, resolve
from app.schema import Invoice, LineItem, ReviewOutcome
from app.validate import ARITHMETIC_CHECKS, validate

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


# --- step 2: verdict bar, run details, local fonts, local preview ----------------


def _verdict(body: dict) -> dict:
    return _run(f"console.log(JSON.stringify(verdict({json.dumps(body)})))")


def test_verdict_for_a_consistent_invoice_says_consistent_not_verified() -> None:
    v = _verdict(_response(_two_lines()))

    assert (v["state"], v["word"], v["reference"]) == ("passed", "OK", None)
    assert "Every arithmetic check passes" in v["sentence"]
    assert "not the same as verified" in v["sentence"]


def test_verdict_for_a_queued_misread_counts_failed_checks_and_gives_the_reference() -> (
    None
):
    v = _verdict(_response(MISREAD, _queued("suggested")))

    assert (v["state"], v["word"], v["reference"]) == (
        "failed",
        "Needs review",
        "R-0007",
    )
    assert v["sentence"].startswith("1 arithmetic check fails.")
    assert "Queued for review." in v["sentence"]


def test_verdict_for_warnings_only_is_amber_and_says_which_checks_ran() -> None:
    lumped = _two_lines(
        line_items=[
            _line("2", "100.00", "200.00", "0"),
            _line("1", "50.00", "50.00", "0"),
        ]
    )
    v = _verdict(_response(lumped))

    assert (v["state"], v["word"], v["reference"]) == ("warnings", "Warnings", None)
    assert "Every arithmetic check that ran passes" in v["sentence"]
    assert "1 compliance warning" in v["sentence"]


def test_verdict_for_a_non_arithmetic_error_says_the_resolver_has_nothing() -> None:
    v = _verdict(_response(_two_lines(invoice_date="2026-01-16")))

    assert (v["state"], v["reference"]) == ("failed", None)
    assert v["sentence"].startswith("1 error.")
    assert "only works on arithmetic" in v["sentence"]


def test_verdict_for_a_resolver_error_says_it_was_not_queued() -> None:
    v = _verdict(_response(MISREAD, ReviewOutcome(status="error")))

    assert v["reference"] is None
    assert "Not queued: the resolver failed." in v["sentence"]


def test_run_details_say_unavailable_without_an_audit_row() -> None:
    assert _run("console.log(JSON.stringify(runDetails(null)))") == (
        "Run details unavailable"
    )


def test_cached_run_details_attribute_the_latency_to_the_original_call() -> None:
    audit = {"model": "gpt-4o", "latency_ms": 7149, "cache_hit": True}
    out = _run(f"console.log(JSON.stringify(runDetails({json.dumps(audit)})))")

    assert out == "gpt-4o · from cache (original call 7.1 s)"


def test_live_run_details_keep_the_plain_latency() -> None:
    audit = {"model": "gpt-4o", "latency_ms": 7149, "cache_hit": False}
    out = _run(f"console.log(JSON.stringify(runDetails({json.dumps(audit)})))")

    assert out == "gpt-4o · 7.1 s · live call"


def test_fonts_are_vendored_and_no_font_cdn_is_called() -> None:
    assert "fonts.googleapis" not in PAGE and "fonts.gstatic" not in PAGE
    urls = re.findall(r'url\("(/static/fonts/[^"]+)"\)', PAGE)
    assert urls
    for url in urls:
        assert (ROOT / url.lstrip("/")).is_file(), url
    assert (ROOT / "static" / "fonts" / "LICENSE.txt").is_file()
    assert "SIL Open Font License" in (ROOT / "static/fonts/LICENSE.txt").read_text(
        encoding="utf-8"
    )


def test_preview_is_a_local_object_url_revoked_on_the_next_upload() -> None:
    """The preview reads the chosen file in the browser; one object URL at a time,
    and the previous one is revoked before a new one is made."""
    assert PAGE.count("URL.createObjectURL(") == 1
    assert "URL.revokeObjectURL(" in PAGE
    assert PAGE.index("URL.revokeObjectURL(") < PAGE.index("URL.createObjectURL(")
    assert "not stored" in PAGE


def test_preview_opens_at_actual_size_and_enlarges_in_a_dialog() -> None:
    """At fit width a full invoice is too small to read in the column. The dialog
    keeps the not-stored note and hands focus back to the image when it closes."""
    assert 'class="frame actual"' in PAGE
    assert 'id="zoom-actual" aria-pressed="true"' in PAGE
    assert 'id="zoom-fit" aria-pressed="false"' in PAGE
    dialog = PAGE[PAGE.index('<dialog id="image-dialog"') : PAGE.index("</dialog>")]
    assert "not stored" in dialog
    assert 'imageDialog.addEventListener("close", () =>' in PAGE
    assert 'getElementById("enlarge").focus()' in PAGE


# --- step 3: issues list and resolver panel -----------------------------------------

# Where "Show in table" lands for each rule: the cell the check is about.
TARGETS = {
    "line_total_equals_quantity_times_unit_price": "line_items[0].line_total",
    "vat_amount_equals_line_total_times_vat_rate": "line_items[0].vat_amount",
    "subtotal_equals_sum_of_line_totals": "subtotal",
    "vat_total_equals_sum_of_vat_amounts": "vat_total",
    "total_equals_subtotal_plus_vat_total": "total",
    "totals_present_without_line_items": "subtotal",
    "invoice_date_matches_timestamp": "invoice_date",
    "seller_vat_number_present": "seller_vat_number",
    "seller_vat_number_is_15_digits": "seller_vat_number",
    "standard_invoice_has_buyer_vat_number": "buyer_vat_number",
    "vat_is_itemised_per_line": "vat_total",
    "simplified_invoice_has_qr_fields": "seller_name",
}
# The fields validate.py puts on each rule's finding, in its order.
FINDING_FIELDS = {
    "line_total_equals_quantity_times_unit_price": [
        "line_items[0].quantity",
        "line_items[0].unit_price",
        "line_items[0].line_total",
    ],
    "vat_amount_equals_line_total_times_vat_rate": [
        "line_items[0].line_total",
        "line_items[0].vat_rate",
        "line_items[0].vat_amount",
    ],
    "subtotal_equals_sum_of_line_totals": ["subtotal", "line_items[0].line_total"],
    "vat_total_equals_sum_of_vat_amounts": ["vat_total", "line_items[0].vat_amount"],
    "total_equals_subtotal_plus_vat_total": ["subtotal", "vat_total", "total"],
    "totals_present_without_line_items": ["line_items", "subtotal", "total"],
    "invoice_date_matches_timestamp": ["invoice_date", "invoice_timestamp"],
    "seller_vat_number_present": ["seller_vat_number"],
    "seller_vat_number_is_15_digits": ["seller_vat_number"],
    "standard_invoice_has_buyer_vat_number": ["buyer_vat_number"],
    "vat_is_itemised_per_line": ["vat_total", "line_items[0].vat_amount"],
    "simplified_invoice_has_qr_fields": ["seller_name", "total"],
}


def _issues(call: str) -> list[dict]:
    return _run(f"console.log(JSON.stringify({call}))")


def test_finding_fields_cover_every_rule() -> None:
    assert sorted(FINDING_FIELDS) == RULES == sorted(TARGETS)


def test_every_rule_becomes_one_plain_issue_with_a_table_target() -> None:
    findings = [
        {
            "rule": r,
            "severity": "error",
            "message": f"m-{r}",
            "fields": FINDING_FIELDS[r],
        }
        for r in RULES
    ]

    issues = _issues(f"issuesFromFindings({json.dumps(findings)})")

    assert len(issues) == len(RULES)
    for rule, issue in zip(RULES, issues, strict=True):
        assert issue["rule"] == rule
        assert issue["target"] == TARGETS[rule], rule
        assert issue["message"] == f"m-{rule}"
        assert "_" not in issue["sentence"], issue


def test_issues_from_checks_match_issues_from_findings() -> None:
    """A review item's issues come from its stored checks, the extract screen's
    from findings; the same failure must read the same and point at the same cell."""
    body = _response(MISREAD)
    arithmetic = [f for f in body["findings"] if f["rule"] in ARITHMETIC_CHECKS]

    from_findings = _issues(f"issuesFromFindings({json.dumps(arithmetic)})")
    from_checks = _issues(f"issuesFromChecks({json.dumps(body['checks'])}, [])")

    def key(i: dict) -> tuple:
        return (i["sentence"], i["target"], i["severity"])

    assert [key(i) for i in from_checks] == [key(i) for i in from_findings]
    assert from_checks[0]["sentence"] == (
        "Line 1: quantity × unit price does not equal the line total"
    )


def test_legacy_review_items_fall_back_to_their_stored_labels() -> None:
    issues = _issues('issuesFromChecks(null, ["sum of line totals = subtotal"])')

    assert issues == [
        {
            "severity": "error",
            "sentence": "The line totals do not add up to the subtotal",
            "rule": "sum of line totals = subtotal",
            "message": None,
            "target": None,
        }
    ]


def _one_line(unit_price: str, total: str = "46.33") -> Invoice:
    """3 x 13.43 = 40.29; unit_price 12.43 is the ٣→٢ misread."""
    return Invoice(
        invoice_type="standard",
        line_items=[_line("3", unit_price, "40.29", "6.04")],
        subtotal="40.29",
        vat_total="6.04",
        total=total,
    )


def _item(invoice: Invoice) -> dict:
    """A review item as GET /reviews returns it, from the real resolver."""
    item = resolve(invoice).model_dump(mode="json")
    item["reference"] = "R-0007"
    return item


AMBIGUOUS = Invoice(
    invoice_type="standard",
    line_items=[_line("5", "5.00", "10.00", "1.50")],
    subtotal="10.00",
    vat_total="1.50",
    total="11.50",
)


def _panel(review: ReviewOutcome | None, item: dict | None) -> dict | None:
    r = json.dumps(review.model_dump(mode="json") if review else None)
    return _run(f"console.log(JSON.stringify(resolverPanel({r}, {json.dumps(item)})))")


def test_resolver_panel_states() -> None:
    suggested = _item(_one_line("12.43"))
    ambiguous = _item(AMBIGUOUS)
    unresolvable = _item(_one_line("12.43", total="99.99"))
    assert [i["status"] for i in (suggested, ambiguous, unresolvable)] == [
        "suggested",
        "ambiguous",
        "unresolvable",
    ]

    assert _panel(None, None) is None

    s = _panel(_queued("suggested"), suggested)
    assert s["tone"] == "queued"
    first = s["candidates"][0]
    assert first["label"] == "Consistent with checks"
    assert first["first"] is True
    assert (first["field"], first["read"], first["value"]) == (
        "Line 1 · unit price",
        "12.43",
        "13.43",
    )
    assert first["arabic"] == "١٣٫٤٣"

    a = _panel(_queued("ambiguous"), ambiguous)
    assert a["tone"] == "neutral"
    assert len(a["candidates"]) > 1
    assert not any(c["first"] for c in a["candidates"])

    u = _panel(_queued("unresolvable"), unresolvable)
    assert (u["tone"], u["candidates"]) == ("neutral", [])
    assert "expected" in u["text"]

    gone = _panel(_queued("suggested"), None)
    assert gone["tone"] == "neutral" and "R-0007" in gone["title"]

    err = _panel(ReviewOutcome(status="error"), None)
    assert err["tone"] == "failed"

    for panel in (s, a, u, gone, err):
        assert "correct" not in json.dumps(panel).lower(), panel


def test_page_never_calls_a_candidate_correct() -> None:
    assert "Consistent with checks" in PAGE
    assert not re.search(r"\bcorrect(ion|ed)?\b", PAGE, re.IGNORECASE)
    assert "Show in table" in PAGE
