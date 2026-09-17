import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.validate import validate
from eval.load_data import EVAL_DIR, load_samples, summary

RECORD: dict[str, Any] = {
    "invoice_number": "INV-2026-1000",
    "invoice_date": "2026-07-31",
    "invoice_timestamp": "2026-07-31T15:38:00Z",
    "invoice_type": "standard",
    "seller_name": "شركة الخليج",
    "seller_name_en": "Gulf Co",
    "seller_vat_number": "315839799889833",
    "seller_city": "جدة",
    "buyer_name": "مؤسسة الأمانة",
    "buyer_name_en": "Al Amanah",
    "buyer_vat_number": "326487668627023",
    "currency": "SAR",
    "line_items": [
        {
            "description_ar": "استشارة هندسية",
            "description_en": "Engineering Consultancy",
            "quantity": "9",
            "unit_price": "4157.37",
            "line_total": "37416.33",
            "vat_rate": "0.15",
            "vat_amount": "5612.45",
        }
    ],
    "subtotal": "37416.33",
    "vat_total": "5612.45",
    "total": "43028.78",
    "qr_base64": None,
    "seeded_defect": None,
    "numerals": "latin",
    "language": "bilingual",
    "file": "INV-2026-1000.png",
}


def _write_set(root: Path, records: list[dict[str, Any]], images: bool = True) -> Path:
    (root / "samples").mkdir()
    (root / "ground_truth.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8"
    )
    if images:
        for record in records:
            (root / "samples" / record["file"]).write_bytes(b"png")
    return root


def test_maps_record_onto_invoice_and_meta(tmp_path: Path) -> None:
    (sample,) = load_samples(_write_set(tmp_path, [RECORD]))

    assert sample.image_path == tmp_path / "samples" / "INV-2026-1000.png"
    assert sample.invoice.seller_name == "شركة الخليج"
    assert sample.invoice.total == Decimal("43028.78")
    assert isinstance(sample.invoice.line_items[0].unit_price, Decimal)
    assert sample.invoice.line_items[0].description == "استشارة هندسية"
    assert sample.meta.seller_name_en == "Gulf Co"
    assert sample.meta.numerals == "latin"
    assert sample.meta.language == "bilingual"
    assert sample.meta.seeded_defect is None


def test_missing_image_is_reported_by_name(tmp_path: Path) -> None:
    _write_set(tmp_path, [RECORD], images=False)
    with pytest.raises(FileNotFoundError, match="INV-2026-1000.png"):
        load_samples(tmp_path)


def test_unknown_top_level_field_raises(tmp_path: Path) -> None:
    record = {**RECORD, "seller_iban": "SA00"}
    _write_set(tmp_path, [record])
    with pytest.raises(ValueError, match=r"unknown=\['seller_iban'\]"):
        load_samples(tmp_path)


def test_missing_top_level_field_raises(tmp_path: Path) -> None:
    record = {k: v for k, v in RECORD.items() if k != "numerals"}
    _write_set(tmp_path, [record])
    with pytest.raises(ValueError, match=r"missing=\['numerals'\]"):
        load_samples(tmp_path)


def test_unknown_line_field_raises(tmp_path: Path) -> None:
    line = {**RECORD["line_items"][0], "discount": "0"}
    record = {**RECORD, "line_items": [line]}
    _write_set(tmp_path, [record])
    with pytest.raises(ValueError, match=r"line_items\[0\].*unknown=\['discount'\]"):
        load_samples(tmp_path)


def test_lumped_vat_maps_empty_line_vat_to_zero(tmp_path: Path) -> None:
    line = {**RECORD["line_items"][0], "vat_amount": ""}
    record = {**RECORD, "line_items": [line], "seeded_defect": "lumped_vat"}
    (sample,) = load_samples(_write_set(tmp_path, [record]))
    assert sample.invoice.line_items[0].vat_amount == Decimal(0)
    assert sample.meta.seeded_defect == "lumped_vat"


def test_empty_line_vat_without_lumped_defect_raises(tmp_path: Path) -> None:
    line = {**RECORD["line_items"][0], "vat_amount": ""}
    record = {**RECORD, "line_items": [line]}
    _write_set(tmp_path, [record])
    with pytest.raises(ValueError, match="vat_amount is empty"):
        load_samples(tmp_path)


def test_invalid_money_names_the_file(tmp_path: Path) -> None:
    record = {**RECORD, "total": "forty"}
    _write_set(tmp_path, [record])
    with pytest.raises(ValueError, match="INV-2026-1000.png.*Invoice schema"):
        load_samples(tmp_path)


def test_summary_counts(tmp_path: Path) -> None:
    simplified = {
        **RECORD,
        "file": "INV-2026-1001.png",
        "invoice_type": "simplified",
        "buyer_name": None,
        "buyer_name_en": None,
        "buyer_vat_number": None,
        "seeded_defect": "missing_seller_vat",
        "seller_vat_number": None,
    }
    text = summary(load_samples(_write_set(tmp_path, [RECORD, simplified])))
    assert "samples: 2" in text
    assert "by invoice_type: simplified=1, standard=1" in text
    assert "seeded defects: 1 (missing_seller_vat=1)" in text
    assert "buyer_vat_number    1/2" in text
    assert "seller_vat_number   1/2" in text


@pytest.mark.skipif(
    not (EVAL_DIR / "samples").is_dir(), reason="eval/samples is gitignored"
)
def test_real_set_matches_readme_composition() -> None:
    samples = load_samples()
    assert len(samples) == 30
    assert sum(s.invoice.invoice_type == "standard" for s in samples) == 17
    assert sum(s.meta.language == "arabic_only" for s in samples) == 12
    assert sum(s.meta.numerals == "arabic_indic" for s in samples) == 6
    assert sum(s.meta.seeded_defect is not None for s in samples) == 5
    # Ground truth without a seeded defect must be arithmetically clean at our tolerance.
    for sample in samples:
        if sample.meta.seeded_defect is None:
            result = validate(sample.invoice, {}, 0.8)
            assert result.findings == [], sample.meta.file
