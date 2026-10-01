import csv
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.validate import validate
from eval.load_data import (
    EVAL_DIR,
    GOLDEN_COLUMNS,
    GOLDEN_PATH,
    load_golden,
    load_samples,
    summary,
)

RECORD: dict[str, Any] = {
    "invoice_number": "INV-2026-1000",
    "invoice_date": "2026-07-31",
    "invoice_timestamp": "2026-07-31T15:38:00",
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
    assert sum(s.meta.seeded_defect is not None for s in samples) == 4
    # Ground truth without a seeded defect must validate clean; every seeded
    # defect must be caught by exactly the rule that names it, and nothing else.
    expected_rule = {
        "missing_buyer_vat": "standard_invoice_has_buyer_vat_number",
        "lumped_vat": "vat_is_itemised_per_line",
        "missing_seller_vat": "seller_vat_number_present",
    }
    for sample in samples:
        rules = [f.rule for f in validate(sample.invoice, {}).findings]
        defect = sample.meta.seeded_defect
        assert rules == ([expected_rule[defect]] if defect else []), sample.meta.file


GOLDEN_ROW = {
    "file": "INV-2026-1000.png",
    "numerals": "latin",
    "subtotal": "37489.97",
    "vat_total": "5623.51",
    "total": "43113.48",
    "line_count": "2",
    "line1_quantity": "9",
    "line1_unit_price": "4157.37",
    "line1_line_total": "37416.33",
    "line2_quantity": "4",
    "line2_unit_price": "18.43",
    "line2_line_total": "73.72",
}


def _write_golden(
    root: Path, rows: list[dict[str, str]], header: tuple[str, ...] = GOLDEN_COLUMNS
) -> Path:
    (root / "samples").mkdir(exist_ok=True)
    path = root / "golden.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, restval="")
        writer.writeheader()
        writer.writerows(rows)
    for row in rows:
        (root / "samples" / row["file"]).write_bytes(b"png")
    return path


def _load(root: Path, rows: list[dict[str, str]]) -> None:
    load_golden(_write_golden(root, rows), root / "samples")


def test_golden_row_maps_onto_flatten_paths(tmp_path: Path) -> None:
    path = _write_golden(tmp_path, [GOLDEN_ROW])
    (row,) = load_golden(path, tmp_path / "samples")

    assert row.image_path == tmp_path / "samples" / "INV-2026-1000.png"
    assert row.numerals == "latin"
    assert row.expected == {
        "subtotal": Decimal("37489.97"),
        "vat_total": Decimal("5623.51"),
        "total": Decimal("43113.48"),
        "line_items.count": Decimal(2),
        "line_items[0].quantity": Decimal(9),
        "line_items[0].unit_price": Decimal("4157.37"),
        "line_items[0].line_total": Decimal("37416.33"),
        "line_items[1].quantity": Decimal(4),
        "line_items[1].unit_price": Decimal("18.43"),
        "line_items[1].line_total": Decimal("73.72"),
    }


def test_golden_accepts_excel_bom_and_padded_cells(tmp_path: Path) -> None:
    path = _write_golden(tmp_path, [{**GOLDEN_ROW, "total": " 43113.48 "}])
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    (row,) = load_golden(path, tmp_path / "samples")
    assert row.expected["total"] == Decimal("43113.48")


@pytest.mark.parametrize("cell", ["٤٣١١٣٫٤٨", "43,113.48", "43113.48 SAR", "", "1e3"])
def test_golden_rejects_non_western_or_formatted_numbers(
    tmp_path: Path, cell: str
) -> None:
    with pytest.raises(ValueError, match=r"line 2 total: expected a number"):
        _load(tmp_path, [{**GOLDEN_ROW, "total": cell}])


def test_golden_rejects_blank_line_cell_within_line_count(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="line2_unit_price: expected a number"):
        _load(tmp_path, [{**GOLDEN_ROW, "line2_unit_price": ""}])


def test_golden_rejects_value_past_line_count(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="line3_quantity: line_count is 2"):
        _load(tmp_path, [{**GOLDEN_ROW, "line3_quantity": "1"}])


@pytest.mark.parametrize("cell", ["0", "7", "", "٢", "2.0"])
def test_golden_rejects_bad_line_count(tmp_path: Path, cell: str) -> None:
    with pytest.raises(ValueError, match="line_count: expected a whole number"):
        _load(tmp_path, [{**GOLDEN_ROW, "line_count": cell}])


def test_golden_rejects_unknown_numerals(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"(?s)line 2:.*numerals.*western"):
        _load(tmp_path, [{**GOLDEN_ROW, "numerals": "western"}])


def test_golden_rejects_wrong_header(tmp_path: Path) -> None:
    path = _write_golden(tmp_path, [], header=GOLDEN_COLUMNS[:-1])
    with pytest.raises(ValueError, match="header does not match"):
        load_golden(path, tmp_path / "samples")


def test_golden_rejects_short_row(tmp_path: Path) -> None:
    path = _write_golden(tmp_path, [GOLDEN_ROW])
    text = path.read_text(encoding="utf-8").rstrip("\n")
    path.write_text(text[: text.rindex(",")] + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2: expected 24 cells"):
        load_golden(path, tmp_path / "samples")


def test_golden_rejects_empty_set(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no rows below the header"):
        _load(tmp_path, [])


def test_golden_rejects_duplicate_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="more than once: INV-2026-1000.png"):
        _load(tmp_path, [GOLDEN_ROW, GOLDEN_ROW])


def test_golden_missing_image_is_reported_by_name(tmp_path: Path) -> None:
    path = _write_golden(tmp_path, [GOLDEN_ROW])
    (tmp_path / "samples" / "INV-2026-1000.png").unlink()
    with pytest.raises(FileNotFoundError, match="INV-2026-1000.png"):
        load_golden(path, tmp_path / "samples")


def test_shipped_golden_set_lists_every_sample_with_its_numerals() -> None:
    with GOLDEN_PATH.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames or ()) == GOLDEN_COLUMNS
        listed = {row["file"]: row["numerals"] for row in reader}
    truth = json.loads((EVAL_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    assert listed == {r["file"]: r["numerals"] for r in truth}
