import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app import cache, extract
from app.schema import CallMetadata, Invoice, LineItem
from eval.load_data import GroundTruthMeta, Sample
from eval.run_eval import (
    Run,
    accuracy,
    agreement,
    calibration,
    confidence_threshold,
    defect_catch,
    dump_runs,
    flatten,
    main,
    report,
    subgroup_table,
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
