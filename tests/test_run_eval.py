import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app import cache, extract
from app.schema import CallMetadata, Invoice, LineItem
from eval.load_data import (
    GOLDEN_COLUMNS,
    GoldenRow,
    GroundTruthMeta,
    Sample,
    load_golden,
)
from eval.run_eval import (
    Run,
    accuracy,
    agreement,
    calibration,
    confidence_threshold,
    defect_catch,
    dump_runs,
    flatten,
    golden_accuracy,
    golden_record,
    golden_report,
    main,
    qr_outcome,
    qr_report,
    report,
    subgroup_table,
    unverified_fields,
    write_run_record,
)

META = CallMetadata(
    model="m",
    prompt_version="v",
    latency_ms=1,
    prompt_tokens=10,
    completion_tokens=5,
    estimated_cost_usd=None,
    cache_hit=False,
    temperature_zero=True,
)


def _invoice(**overrides: object) -> Invoice:
    values: dict[str, object] = {
        "invoice_number": "INV-1",
        "invoice_date": date(2026, 1, 15),
        "invoice_timestamp": datetime(2026, 1, 15, 10, 30),  # noqa: DTZ001
        "invoice_type": "standard",
        "seller_name": "Seller",
        "seller_vat_number": "300000000000003",
        "buyer_name": "Buyer",
        "buyer_vat_number": "310000000000003",
        "line_items": [
            LineItem(
                description="a",
                quantity="2",
                unit_price="100.00",
                line_total="200.00",
                vat_rate="0.15",
                vat_amount="30.00",
            )
        ],
        "subtotal": "200.00",
        "vat_total": "30.00",
        "total": "230.00",
    }
    values.update(overrides)
    return Invoice(**values)


def _sample(file: str = "a.png", defect: str | None = None) -> Sample:
    return Sample(
        image_path=Path(file),
        invoice=_invoice(),
        meta=GroundTruthMeta(
            file=file,
            seller_name_en="S",
            buyer_name_en="B",
            seller_city="C",
            qr_base64=None,
            seeded_defect=defect,
            numerals="latin",
            language="bilingual",
        ),
    )


def _run(
    invoice: Invoice | None,
    rules: list[str] | None = None,
    confidences: dict[str, float] | None = None,
) -> Run:
    return Run(
        metadata=META,
        fields=flatten(invoice) if invoice else None,
        rules=rules or [],
        confidences=confidences or {},
    )


def test_flatten_pools_line_items_by_index() -> None:
    flat = flatten(_invoice())
    assert flat["total"] == Decimal("230.00")
    assert flat["line_items.count"] == 1
    assert flat["line_items[0].unit_price"] == Decimal("100.00")
    assert "line_items" not in flat


def test_accuracy_counts_each_run_and_pools_line_fields() -> None:
    sample = _sample()
    wrong_total = _invoice(total="231.00")
    runs = {"a.png": [_run(_invoice()), _run(wrong_total)]}

    acc = accuracy([sample], runs)

    assert acc["total"] == [True, False]
    assert acc["line_items[*].unit_price"] == [True, True]
    assert acc["line_items.count"] == [True, True]


def test_accuracy_treats_unparseable_run_as_all_wrong() -> None:
    acc = accuracy([_sample()], {"a.png": [_run(None)]})
    assert all(flags == [False] for flags in acc.values())


def test_agreement_is_per_sample_across_runs() -> None:
    runs = {"a.png": [_run(_invoice()), _run(_invoice(total="231.00"))]}
    agree = agreement([_sample()], runs)
    assert agree["total"] == [False]
    assert agree["subtotal"] == [True]


def test_defect_catch_reports_rule_hits_per_run() -> None:
    sample = _sample(defect="lumped_vat")
    runs = {"a.png": [_run(_invoice(), ["vat_is_itemised_per_line"]), _run(_invoice())]}
    (line,) = defect_catch([sample], runs)
    assert "lumped_vat" in line
    assert line.endswith("caught 1/2")


def test_calibration_splits_scores_by_correctness() -> None:
    scores = {"total": 1.0, "subtotal": 0.5, "line_items[0].unit_price": 0.9}
    wrong_total = _invoice(total="231.00")
    runs = {"a.png": [_run(wrong_total, confidences=scores)]}

    lines = calibration([_sample()], runs, threshold=0.8)

    text = "\n".join(lines)
    assert "mean confidence on correct fields:   0.700 (n=2)" in text
    assert "mean confidence on incorrect fields: 1.000 (n=1)" in text
    assert "incorrect fields scored >= threshold: 1/1" in text
    assert "correct fields scored < threshold:    1/2" in text
    # 12 top-level + 6 line fields = 18 scorable paths, 3 scored; count is never scored.
    assert "fields with no score from the model:  15" in text


def test_invalid_threshold_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "high")
    with pytest.raises(RuntimeError, match="CONFIDENCE_THRESHOLD"):
        confidence_threshold()
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "1.5")
    with pytest.raises(RuntimeError, match="between 0 and 1"):
        confidence_threshold()


def test_calibration_with_no_errors_reports_na() -> None:
    lines = calibration([_sample()], {"a.png": [_run(_invoice())]}, threshold=0.8)
    assert "mean confidence on incorrect fields: n/a (n=0)" in "\n".join(lines)


def test_report_includes_agreement_only_with_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONFIDENCE_THRESHOLD", raising=False)
    runs = {"a.png": [_run(_invoice())]}
    single = report([_sample()], runs, repeats=1)
    assert "confidence calibration (threshold 0.80)" in single
    assert "agreement" not in single
    assert "temperature_zero=1/1" in single
    assert "prompt_tokens=10 completion_tokens=5" in single
    double = report([_sample()], {"a.png": [_run(_invoice()), _run(_invoice())]}, 2)
    assert "agreement" in double


def test_subgroup_table_splits_by_language_and_numerals() -> None:
    bilingual = _sample("a.png")
    arabic_only = _sample("b.png")
    arabic_only.meta.language = "arabic_only"
    arabic_only.meta.numerals = "arabic_indic"
    runs = {"a.png": [_run(_invoice())], "b.png": [_run(_invoice(total="1.00"))]}

    lines = subgroup_table([bilingual, arabic_only], runs)

    assert "all (n=2)" in lines[0]
    assert "arabic_only (n=1)" in lines[0]
    total_row = next(line for line in lines if line.startswith("total"))
    assert total_row.split() == ["total", "50.0%", "0.0%", "100.0%", "0.0%", "100.0%"]


def test_dump_runs_writes_json_with_string_fields(tmp_path: Path) -> None:
    path = tmp_path / "runs.json"
    dump_runs(path, {"a.png": [_run(_invoice(), ["r"], {"total": 1.0})]})

    data = json.loads(path.read_text(encoding="utf-8"))
    (run,) = data["a.png"]
    assert run["fields"]["total"] == "230.00"
    assert run["rules"] == ["r"]
    assert run["confidences"] == {"total": 1.0}
    assert run["metadata"]["model"] == "m"


def test_main_refuses_large_uncached_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "m")
    samples = [_sample(f"{i}.png") for i in range(3)]
    monkeypatch.setattr("eval.run_eval.load_samples", lambda: samples)

    assert main(["--repeats", "17"]) == 2
    assert "51 API calls" in capsys.readouterr().err


def test_main_runs_from_cache_without_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "m")
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    image = tmp_path / "a.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nx")
    sample = _sample()
    sample.image_path = image
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "response_english.json").read_text(
            encoding="utf-8"
        )
    )
    cache.set(
        image.read_bytes(),
        "m",
        extract.PROMPT_VERSION,
        {
            "content": json.dumps(fixture),
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "latency_ms": 1,
            "temperature_zero": True,
        },
    )
    monkeypatch.setattr("eval.run_eval.load_samples", lambda: [sample])

    assert main([]) == 0
    out = capsys.readouterr().out
    assert "cache_hits=1" in out
    assert "invoice_type" in out


def _golden_row(file: str = "a.png", numerals: str = "latin") -> GoldenRow:
    """Matches _invoice(): one line, 2 x 100.00 = 200.00, total 230.00."""
    expected = {
        "subtotal": "200.00",
        "vat_total": "30.00",
        "total": "230.00",
        "line_items.count": "1",
        "line_items[0].quantity": "2",
        "line_items[0].unit_price": "100.00",
        "line_items[0].line_total": "200.00",
    }
    return GoldenRow(
        file=file,
        image_path=Path(file),
        numerals=numerals,
        expected={k: Decimal(v) for k, v in expected.items()},
    )


def test_unverified_fields_are_everything_outside_the_csv() -> None:
    assert unverified_fields() == [
        "invoice_number",
        "invoice_date",
        "invoice_timestamp",
        "invoice_type",
        "seller_name",
        "seller_vat_number",
        "buyer_name",
        "buyer_vat_number",
        "currency",
        "line_items[*].description",
        "line_items[*].vat_rate",
        "line_items[*].vat_amount",
    ]


def test_golden_accuracy_splits_by_numerals() -> None:
    rows = [_golden_row("a.png"), _golden_row("b.png", "arabic_indic")]
    runs = {"a.png": _run(_invoice()), "b.png": _run(_invoice(total="203.00"))}

    table = golden_accuracy(rows, runs)

    assert table["all"]["total"] == [True, False]
    assert table["latin"]["total"] == [True]
    assert table["arabic_indic"]["total"] == [False]
    assert table["arabic_indic"]["line_items[*].unit_price"] == [True]
    assert set(table["all"]).isdisjoint(unverified_fields())


def test_golden_accuracy_scores_a_missed_line_and_unparseable_run_as_wrong() -> None:
    row = _golden_row()
    row.expected["line_items.count"] = Decimal(2)
    row.expected["line_items[1].quantity"] = Decimal(1)

    table = golden_accuracy([row], {"a.png": _run(_invoice())})
    assert table["all"]["line_items[*].quantity"] == [True, False]
    assert table["all"]["line_items.count"] == [False]

    table = golden_accuracy([_golden_row()], {"a.png": _run(None)})
    assert all(flags == [False] for flags in table["all"].values())


def test_golden_record_separates_spend_from_cached_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "m")
    golden = tmp_path / "golden.csv"
    golden.write_text("x", encoding="utf-8")
    cached = META.model_copy(
        update={
            "cache_hit": True,
            "estimated_cost_usd": Decimal("0.01"),
            "latency_ms": 900,
        }
    )
    live = META.model_copy(
        update={"estimated_cost_usd": Decimal("0.02"), "latency_ms": 100}
    )
    rows = [_golden_row("a.png"), _golden_row("b.png", "arabic_indic")]
    extra_line = _invoice(line_items=[_invoice().line_items[0]] * 2)
    runs = {
        "a.png": Run(metadata=cached, fields=flatten(_invoice())),
        "b.png": Run(metadata=live, fields=flatten(extra_line)),
    }

    record = golden_record(rows, runs, golden, datetime(2026, 9, 28, tzinfo=UTC))

    totals = record["totals"]
    assert totals["invoices"] == {"all": 2, "latin": 1, "arabic_indic": 1}
    assert totals["live_calls"] == 1
    assert totals["cache_hits"] == 1
    assert totals["spent_this_run_usd"] == "0.02"
    assert totals["original_calls_cost_usd"] == "0.03"
    assert totals["original_call_latency_ms"] == {"mean": 500, "max": 900}
    assert totals["lines_extra_not_scored"] == 1
    assert totals["lines_missed"] == 0
    assert record["accuracy"]["arabic_indic"]["total"] == {"correct": 1, "total": 1}
    (first, _) = record["invoices"]
    assert first["fields"]["total"] == {
        "expected": "230.00",
        "extracted": "230.00",
        "match": True,
    }
    assert record["model"] == "m"
    assert "seller_name" not in first["fields"]


def test_golden_report_marks_unverified_fields_without_a_figure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "m")
    golden = tmp_path / "golden.csv"
    golden.write_text("x", encoding="utf-8")
    runs = {"a.png": _run(_invoice(total="1.00"))}
    record = golden_record([_golden_row()], runs, golden, datetime.now(UTC))

    text = golden_report(record)

    total_row = next(line for line in text.splitlines() if line.startswith("total"))
    assert total_row.split() == ["total", "0.0%", "(0/1)", "0.0%", "(0/1)", "n/a"]
    seller = next(line for line in text.splitlines() if "seller_name" in line)
    assert seller.split() == ["seller_name", "UNVERIFIED"]
    assert "arabic_indic (n=0)" in text
    assert "spent_this_run_usd=unknown" in text


def test_write_run_record_never_overwrites(tmp_path: Path) -> None:
    started = datetime(2026, 9, 28, 10, 15, 30, tzinfo=UTC)
    path = write_run_record({"a": 1}, tmp_path / "runs", started)
    assert path.name == "20260928T101530Z.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    with pytest.raises(FileExistsError):
        write_run_record({"a": 2}, tmp_path / "runs", started)


def test_main_golden_rejects_flags_that_would_bypass_the_cache() -> None:
    for flags in (["--no-cache"], ["--repeats", "2"], ["--dump", "x.json"]):
        with pytest.raises(SystemExit) as exc:
            main(["--golden", "g.csv", *flags])
        assert exc.value.code == 2


def test_main_golden_uses_cache_and_writes_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "m")
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("eval.run_eval.RUNS_DIR", tmp_path / "runs")

    def no_live_calls(*_: object) -> None:
        raise AssertionError("a cached invoice was sent to the API")

    monkeypatch.setattr(extract, "_call_model", no_live_calls)
    (tmp_path / "samples").mkdir()
    image = tmp_path / "samples" / "a.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nx")
    fixture = (Path(__file__).parent / "fixtures" / "response_english.json").read_text(
        encoding="utf-8"
    )
    record = {
        "content": fixture,
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "latency_ms": 7,
        "temperature_zero": True,
    }
    cache.set(image.read_bytes(), "m", extract.PROMPT_VERSION, record)
    golden = tmp_path / "golden.csv"
    cells = {c: "" for c in GOLDEN_COLUMNS} | {
        "file": "a.png",
        "numerals": "latin",
        "subtotal": "1",
        "vat_total": "1",
        "total": "1",
        "line_count": "1",
        "line1_quantity": "1",
        "line1_unit_price": "1",
        "line1_line_total": "1",
    }
    golden.write_text(
        ",".join(GOLDEN_COLUMNS) + "\n" + ",".join(cells.values()) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "eval.run_eval.load_golden",
        lambda path: load_golden(path, tmp_path / "samples"),
    )

    assert main(["--golden", str(golden)]) == 0

    out = capsys.readouterr().out
    assert "live_calls=0 cache_hits=1" in out
    (path,) = (tmp_path / "runs").iterdir()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["totals"]["original_call_latency_ms"]["max"] == 7
    assert written["invoices"][0]["metadata"]["cache_hit"] is True


# --- QR cross-check measurement ------------------------------------------------------


def _qr_sample(qr: bool = True, numerals: str = "arabic_indic") -> Sample:
    sample = _sample()
    sample.meta.qr_base64 = "AQ==" if qr else None
    sample.meta.numerals = numerals
    return sample


def _qr_run(
    read: bool = True, flagged: list[str] | None = None, **misread: object
) -> Run:
    run = _run(_invoice(**misread))
    run.qr_read = read
    run.qr_flagged = flagged or []
    return run


def test_qr_outcome_names_why_a_misread_was_missed() -> None:
    wrong_date = {"invoice_date": date(2023, 1, 15)}

    assert (
        qr_outcome(
            _qr_sample(),
            _qr_run(flagged=["invoice_date"], **wrong_date),
            "invoice_date",
        )
        == "caught"
    )
    assert qr_outcome(
        _qr_sample(qr=False), _qr_run(read=False, **wrong_date), "invoice_date"
    ) == ("missed: no QR on the invoice")
    assert qr_outcome(
        _qr_sample(), _qr_run(read=False, **wrong_date), "invoice_date"
    ) == ("missed: QR not read")
    assert qr_outcome(_qr_sample(), _qr_run(**wrong_date), "invoice_date") == (
        "missed: QR read but raised nothing"
    )
    assert qr_outcome(
        _qr_sample(), _qr_run(invoice_number="INV-2"), "invoice_number"
    ) == ("missed: not in the QR")


def test_qr_outcome_counts_a_flag_on_a_correct_read_as_a_false_alarm() -> None:
    assert (
        qr_outcome(_qr_sample(), _qr_run(flagged=["total"]), "total") == "false alarm"
    )
    assert qr_outcome(_qr_sample(), _qr_run(), "total") is None


def test_qr_report_counts_per_numeral_group_from_the_runs() -> None:
    caught = _qr_sample()
    caught.meta.file = "caught.png"
    no_qr = _qr_sample(qr=False)
    no_qr.meta.file = "no_qr.png"
    latin = _qr_sample(numerals="latin")
    latin.meta.file = "latin.png"
    runs = {
        "caught.png": [_qr_run(flagged=["total"], total=Decimal("1.00"))],
        "no_qr.png": [_qr_run(read=False, invoice_number="INV-9")],
        "latin.png": [_qr_run()],
    }

    lines = qr_report([caught, no_qr, latin], runs)

    assert (
        "  arabic_indic: QR on 1 of 2 invoices, read in 1 of 2 runs;"
        " 2 header misreads, 1 caught, 1 missed, 0 false alarms"
    ) in lines
    assert "    caught.png  total: caught" in lines
    assert "    no_qr.png  invoice_number: missed: not in the QR" in lines
    assert (
        "  latin: QR on 1 of 1 invoices, read in 1 of 1 runs;"
        " 0 header misreads, 0 caught, 0 missed, 0 false alarms"
    ) in lines
