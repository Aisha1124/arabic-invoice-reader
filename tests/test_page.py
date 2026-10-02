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
from decimal import Decimal
from pathlib import Path

import pytest

from app.resolve import _checks, resolve
from app.review import _check_fits
from app.schema import (
    CrossCheck,
    Invoice,
    LineItem,
    QrPayload,
    ReviewItem,
    ReviewOutcome,
)
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


def test_every_script_on_the_page_parses() -> None:
    """Only the wording block runs here; a syntax error in the other would stop the
    whole page and no other test would see it."""
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", PAGE, re.DOTALL)
    assert len(blocks) == 2
    done = subprocess.run(
        [
            "node",
            "-e",
            "for (const b of JSON.parse(require('fs').readFileSync(0, 'utf8'))) new Function(b);",
        ],
        input=json.dumps(blocks),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr


def test_wording_block_runs_without_a_page() -> None:
    assert _run(
        'console.log(JSON.stringify(plainField("line_items[0].unit_price")))'
    ) == ("Line 1 · unit price")


def test_every_validation_rule_has_its_own_sentence() -> None:
    assert len(RULES) == 16, RULES
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


# --- pipeline strip ------------------------------------------------


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


def test_strip_is_drawn_inside_the_result_only_and_nothing_waits() -> None:
    """The result is hidden on an HTTP error, so the strip is never drawn for one.
    No artificial delays: no setTimeout anywhere on the page."""
    result = PAGE.index('<div id="result" hidden>')
    assert PAGE.index('id="pipeline"') > result
    assert "setTimeout" not in PAGE
    main = PAGE[PAGE.index('<script id="wording">') :]
    main = main[main.index("</script>") :]
    assert "pipelineStages(result)" in main


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
    "seller_vat_number_matches_qr": "seller_vat_number",
    "timestamp_matches_qr": "invoice_timestamp",
    "total_matches_qr": "total",
    "vat_total_matches_qr": "vat_total",
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
    "seller_vat_number_matches_qr": ["seller_vat_number"],
    "timestamp_matches_qr": ["invoice_timestamp", "invoice_date"],
    "total_matches_qr": ["total"],
    "vat_total_matches_qr": ["vat_total"],
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

    issues = _issues(f"issuesFromFindings({json.dumps(findings)}, [])")

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

    from_findings = _issues(
        f"issuesFromFindings({json.dumps(arithmetic)}, {json.dumps(body['checks'])})"
    )
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
            "quote": None,
            "computed": None,
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
    item["cross_checks"] = []
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


# --- step 4: totals block, checks per line, flagged fields --------------------------


def _js(call: str) -> object:
    return _run(f"console.log(JSON.stringify({call}))")


def _line_checks(body: dict) -> list[dict]:
    lines = len(body["invoice"]["line_items"])
    return _js(f"lineChecks({json.dumps(body['checks'])}, {lines})")


def _totals(body: dict) -> list[dict]:
    return _js(
        f"totalsRows({json.dumps(body['invoice'])}, {json.dumps(body['checks'])})"
    )


def test_a_failed_line_check_marks_every_cell_it_reads() -> None:
    first, second = _line_checks(_response(MISREAD))

    assert first["failed"] == ["quantity", "unit_price", "line_total"]
    assert [(c["label"], c["outcome"]) for c in first["checks"]] == [
        ("Qty", "fail"),
        ("VAT", "pass"),
    ]
    assert first["checks"][0]["hover"] == (
        "Line 1: quantity × unit price does not equal the line total."
        " Quantity × unit price = 200.00"
    )
    assert first["checks"][1]["hover"] == "Line total × VAT rate = 37.5000: passes"
    assert second["failed"] == []


def test_a_skipped_line_check_marks_nothing_and_says_why() -> None:
    lumped = _two_lines(
        line_items=[
            _line("2", "100.00", "200.00", "0"),
            _line("1", "50.00", "50.00", "0"),
        ]
    )
    first = _line_checks(_response(lumped))[0]

    assert first["failed"] == []
    vat = first["checks"][1]
    assert vat["outcome"] == "not_checked"
    reason = next(c["reason"] for c in _response(lumped)["checks"] if c["reason"])
    assert vat["hover"] == f"Not checked: {reason}"


def test_totals_show_read_computed_and_signed_difference() -> None:
    rows = _totals(_response(_two_lines(total="297.50")))

    assert [(r["label"], r["outcome"]) for r in rows] == [
        ("Subtotal", "pass"),
        ("VAT total", "pass"),
        ("Total", "fail"),
    ]
    total = rows[2]
    assert (total["read"], total["computed"], total["difference"]) == (
        "297.50",
        "287.50",
        "+10.00",
    )
    assert rows[0]["difference"] == "0.00"
    assert _totals(_response(_two_lines(total="277.50")))[2]["difference"] == "-10.00"


def test_totals_without_line_items_are_not_checked_with_a_reason() -> None:
    rows = _totals(_response(_two_lines(line_items=[])))

    assert [r["outcome"] for r in rows] == ["not_checked", "not_checked", "pass"]
    assert rows[0]["computed"] is None and rows[0]["difference"] is None
    assert rows[0]["reason"]


def test_flagged_fields_keep_the_worst_severity_and_ignore_other_fields() -> None:
    findings = [
        {"rule": "a", "severity": "warning", "fields": ["seller_name", "total"]},
        {"rule": "b", "severity": "error", "fields": ["invoice_date", "seller_name"]},
        {"rule": "c", "severity": "warning", "fields": ["invoice_date"]},
    ]
    keys = ["invoice_date", "seller_name", "buyer_name"]

    assert _js(f"flaggedFields({json.dumps(findings)}, {json.dumps(keys)})") == {
        "seller_name": "error",
        "invoice_date": "error",
    }


def test_extract_page_has_no_check_grid_or_editable_values() -> None:
    extract = PAGE[
        PAGE.index('<div id="view-extract"') : PAGE.index('<div id="view-review"')
    ]
    assert 'id="checkmap"' not in extract
    assert re.findall(r"<input\b", PAGE) == ["<input"]
    assert 'type="file"' in PAGE[PAGE.index("<input") :][:60]
    assert "<th>Checks</th>" in extract
    assert '<table id="totals">' in extract
    assert ">Show all fields<" in extract
    assert 'createElement("input")' not in PAGE


def test_arithmetic_issues_carry_the_computed_value_for_details() -> None:
    body = _response(MISREAD)
    issues = _issues(
        f"issuesFromFindings({json.dumps(body['findings'])}, {json.dumps(body['checks'])})"
    )

    assert [i["computed"] for i in issues] == ["Computed 200.00 · difference +50.00"]


def test_non_arithmetic_issues_have_no_computed_value() -> None:
    body = _response(_two_lines(seller_vat_number=None))
    issues = _issues(
        f"issuesFromFindings({json.dumps(body['findings'])}, {json.dumps(body['checks'])})"
    )

    assert [i["computed"] for i in issues] == [None]


def test_a_fresh_upload_resets_the_preview_to_actual_size() -> None:
    show = PAGE[PAGE.index("function showPreview(") :]
    show = show[: show.index("\n}\n")]
    assert 'setZoom("actual")' in show


# --- step 5: review queue ---------------------------------------------------------


def _detail(item: dict) -> dict:
    return _js(f"reviewDetail({json.dumps(item)})")


def _stored(invoice: Invoice) -> dict:
    """A review item with the check outcomes the queue stores."""
    item = _item(invoice)
    item["checks"] = [
        {k: c[k] for k in ("rule", "line", "outcome", "reason")}
        for c in _response(invoice)["checks"]
    ]
    return item


def test_inbox_entry_names_the_reference_status_and_failed_checks() -> None:
    item = _item(_one_line("12.43"))
    entry = _js(f"inboxEntry({json.dumps(item)})")
    n = len(item["failed_checks"])

    assert entry["reference"] == "R-0007"
    assert (entry["status"], entry["word"]) == ("suggested", "Suggestion")
    assert entry["summary"] == f"{n} failed check{'' if n == 1 else 's'}"


def test_suggestion_detail_starts_on_the_first_candidate() -> None:
    d = _detail(_stored(_one_line("12.43")))

    assert d["heading"] == "Find paper invoice R-0007"
    assert d["chosen"] == 1
    assert d["allowed"] == ["accept", "reject"]
    first = d["candidates"][0]
    assert (first["read"], first["readArabic"]) == ("12.43", "١٢٫٤٣")
    assert (first["value"], first["arabic"]) == ("13.43", "١٣٫٤٣")
    assert d["readings"] == []
    assert d["issues"][0]["sentence"] == (
        "Line 1: quantity × unit price does not equal the line total"
    )


def test_ambiguous_detail_chooses_nothing_for_the_person() -> None:
    d = _detail(_stored(AMBIGUOUS))

    assert d["chosen"] is None
    assert d["allowed"] == ["accept", "reject"]
    assert len(d["candidates"]) > 1


def test_unresolvable_detail_shows_the_values_read_and_only_checked_manually() -> None:
    item = _stored(_one_line("12.43", total="99.99"))
    d = _detail(item)

    assert d["allowed"] == ["checked_manually"]
    assert d["candidates"] == []
    assert [r["field"] for r in d["readings"]] == [
        _js(f"plainField({json.dumps(r['field'])})") for r in item["involved"]
    ]
    assert all(r["arabic"] for r in d["readings"])
    assert "check every number on the invoice" in d["text"]


def test_legacy_item_detail_falls_back_to_stored_labels() -> None:
    item = _item(_one_line("12.43"))
    item["checks"] = None

    assert [i["sentence"] for i in _detail(item)["issues"]] == [
        _js(f"plainCheck({json.dumps(label)})") for label in item["failed_checks"]
    ]


@pytest.mark.parametrize(
    "status", ["suggested", "ambiguous", "unresolvable", "cross_check"]
)
def test_page_offers_exactly_the_decisions_the_queue_accepts(status: str) -> None:
    raw = (
        _cross_check_item()
        if status == "cross_check"
        else _stored(
            {
                "suggested": _one_line("12.43"),
                "ambiguous": AMBIGUOUS,
                "unresolvable": _one_line("12.43", total="99.99"),
            }[status]
        )
    )
    item = ReviewItem(
        **raw,
        id="x",
        created_utc="2026-10-02T00:00:00",
        audit_id="a",
        image_sha256="0" * 64,
    )
    accepted_by_queue = []
    for action, decision in [
        ("accept", "accepted"),
        ("checked_manually", "checked_manually"),
        ("reject", "rejected"),
    ]:
        try:
            _check_fits(item, decision)
        except ValueError:
            continue
        accepted_by_queue.append(action)

    assert _detail(raw)["allowed"] == accepted_by_queue


def test_shortcuts_and_decision_bodies() -> None:
    keys = ["a", "A", "m", "r", "R", "ArrowDown", "ArrowUp", "x", "Enter"]
    assert _js(f"{json.dumps(keys)}.map(shortcut)") == [
        "accept",
        "accept",
        "checked_manually",
        "reject",
        "reject",
        "next",
        "previous",
        None,
        None,
    ]
    assert _js(
        '["accept", "checked_manually", "reject"].map((a) => decisionBody(a, 2))'
    ) == [
        {"decision": "accepted", "rank": 2},
        {"decision": "checked_manually"},
        {"decision": "rejected"},
    ]


def test_review_view_has_inbox_detail_and_a_sticky_action_bar() -> None:
    review = PAGE[PAGE.index('<div id="view-review"') : PAGE.index("</main>")]
    assert 'id="inbox"' in review and 'id="detail"' in review
    for key in ("A", "M", "R"):
        assert f'aria-keyshortcuts="{key}"' in review
    assert re.search(r"\.actionbar \{ position: sticky; inset-block-end: 0;", PAGE)
    assert 'action === "reject" && !confirm(' in PAGE
    assert "checkMap" not in PAGE and 'class="checkmap"' not in PAGE


# --- QR cross-check: issues, coverage, review items ----------------------------------

QR_RULES = [
    "seller_vat_number_matches_qr",
    "timestamp_matches_qr",
    "total_matches_qr",
    "vat_total_matches_qr",
]


def _qr_for(invoice: Invoice, **overrides: object) -> QrPayload:
    values: dict[str, object] = {
        "seller_name": "s",
        "seller_vat_number": invoice.seller_vat_number,
        "timestamp": invoice.invoice_timestamp,
        "total": invoice.total,
        "vat_total": invoice.vat_total,
    }
    values.update(overrides)
    return QrPayload(**values)


DATED = {"invoice_date": "2026-01-15"}


def _qr_response(invoice: Invoice, qr: QrPayload | str) -> dict:
    body = validate(invoice, {}, qr).model_dump(mode="json")
    body["review"] = None
    return body


def _cross_check_item() -> dict:
    item = _stored(_one_line("13.43"))
    item.update(
        status="cross_check",
        reason="fields disagree with the QR code or with each other",
        failed_checks=[],
        candidates=[],
        involved=[],
        cross_checks=[
            CrossCheck(rule="total_matches_qr", fields=["total"]).model_dump(),
            CrossCheck(
                rule="invoice_date_matches_timestamp",
                fields=["invoice_date", "invoice_timestamp"],
            ).model_dump(),
        ],
    )
    return item


def test_qr_issues_say_disagrees_and_show_what_the_qr_code_says() -> None:
    invoice = _two_lines(**DATED)
    body = _qr_response(invoice, _qr_for(invoice, total=Decimal("387.50")))
    (issue,) = _issues(
        f"issuesFromFindings({json.dumps(body['findings'])}, {json.dumps(body['checks'])})"
    )

    assert issue["sentence"] == "The total disagrees with the QR code"
    assert issue["quote"] == "QR code says 387.50, the model read 287.50"
    assert issue["target"] == "total"


def _coverage(field: str, body: dict) -> str:
    return _js(
        f"fieldCoverage({json.dumps(field)}, {json.dumps(body['qr'])},"
        f" {json.dumps(body['checks'])})"
    )


def test_coverage_says_what_each_field_was_checked_against() -> None:
    invoice = _two_lines(**DATED)
    read = _qr_response(invoice, _qr_for(invoice))
    unread = _qr_response(invoice, "no QR code found")

    assert _coverage("invoice_number", read) == "Not cross-checked"
    assert _coverage("invoice_number", unread) == "Not cross-checked"
    assert _coverage("seller_name", read) == "Not cross-checked"
    assert _coverage("buyer_vat_number", read) == "Not cross-checked"
    assert _coverage("seller_vat_number", read) == "Checked by QR"
    assert _coverage("seller_vat_number", unread) == "Not cross-checked"
    assert _coverage("invoice_date", read) == "Checked by QR"
    assert _coverage("invoice_timestamp", unread) == "Not cross-checked"
    assert _coverage("total", read) == "Checked by QR and arithmetic"
    assert _coverage("total", unread) == "Checked by arithmetic"
    assert _coverage("subtotal", read) == "Checked by arithmetic"


def test_coverage_does_not_count_a_check_that_did_not_run() -> None:
    """No line items and no total: neither sum that reads the subtotal can run."""
    nothing_ran = _qr_response(
        _two_lines(line_items=[], total=None), "no QR code found"
    )
    assert _coverage("subtotal", nothing_ran) == "Not cross-checked"
    no_lines = _qr_response(_two_lines(line_items=[]), "no QR code found")
    assert _coverage("subtotal", no_lines) == "Checked by arithmetic"


def test_qr_summary_says_qr_not_read_with_the_reason() -> None:
    invoice = _two_lines(**DATED)
    summary = [
        _js(f"qrSummary({json.dumps(b['qr'])}, {json.dumps(b['findings'])})")
        for b in (
            _qr_response(invoice, "no QR code found"),
            _qr_response(invoice, _qr_for(invoice)),
            _qr_response(invoice, _qr_for(invoice, total=Decimal("1.00"))),
        )
    ]

    assert summary == [
        {"state": "not_needed", "text": "QR not read: no QR code found"},
        {"state": "passed", "text": "QR code read; it agrees with what the model read"},
        {"state": "failed", "text": "QR code read; 1 field disagrees with it"},
    ]


def test_queued_without_a_resolver_skips_resolve_and_queues() -> None:
    body = _response(_two_lines(invoice_date="2026-01-16"))
    body["review"] = ReviewOutcome(status="queued", reference="R-0009").model_dump(
        mode="json"
    )

    stages = _stages(body)
    assert [(s["stage"], s["state"]) for s in stages] == [
        ("Read", "passed"),
        ("Check", "failed"),
        ("Resolve", "not_needed"),
        ("Review", "queued"),
    ]
    assert "R-0009" in stages[3]["label"]
    assert _verdict(body)["sentence"].endswith("Queued for review.")


def test_cross_check_item_lists_the_fields_to_check_against_paper() -> None:
    d = _detail(_cross_check_item())

    assert d["allowed"] == ["checked_manually"]
    assert d["candidates"] == [] and d["readings"] == []
    assert d["fieldsToCheck"] == ["Total", "Invoice date", "Timestamp"]
    assert [i["sentence"] for i in d["issues"]] == [
        "The total disagrees with the QR code",
        "The invoice date does not match the date in the timestamp",
    ]
    assert "correct" not in json.dumps(d).lower()
    entry = _js(f"inboxEntry({json.dumps(_cross_check_item())})")
    assert (entry["word"], entry["summary"]) == ("Cross-check", "2 failed checks")


def test_legacy_item_without_cross_checks_lists_no_fields() -> None:
    item = _stored(_one_line("12.43", total="99.99"))
    item["cross_checks"] = None

    assert _detail(item)["fieldsToCheck"] == []
