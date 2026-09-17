"""
Reads eval/ground_truth.json into Invoice models plus the metadata the eval needs.
Run from the repo root as `python -m eval.load_data` to print the dataset summary.

Descriptions are scored against `description_ar`: the generator renders only the
Arabic description in the line-item table, on bilingual invoices too, so
`description_en` never appears on the page and is deliberately not mapped.
"""

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.schema import Invoice

EVAL_DIR = Path(__file__).resolve().parent

LINE_FIELDS = frozenset(
    {
        "description_ar",
        "description_en",
        "quantity",
        "unit_price",
        "line_total",
        "vat_rate",
        "vat_amount",
    }
)


class GroundTruthMeta(BaseModel):
    file: str
    seller_name_en: str
    buyer_name_en: str | None
    seller_city: str
    qr_base64: str | None
    seeded_defect: (
        Literal["missing_buyer_vat", "lumped_vat", "missing_seller_vat"] | None
    )
    numerals: Literal["latin", "arabic_indic"]
    language: Literal["bilingual", "arabic_only"]


class Sample(BaseModel):
    image_path: Path
    invoice: Invoice
    meta: GroundTruthMeta


def _check_keys(record: dict[str, Any], expected: frozenset[str], where: str) -> None:
    unknown = set(record) - expected
    missing = expected - set(record)
    if unknown or missing:
        raise ValueError(
            f"{where}: ground-truth fields do not map onto the schema;"
            f" unknown={sorted(unknown)} missing={sorted(missing)}"
        )


def _map_line(line: dict[str, Any], where: str, lumped_vat: bool) -> dict[str, Any]:
    _check_keys(line, LINE_FIELDS, where)
    vat_amount = line["vat_amount"]
    # Lumped-VAT invoices print no per-line VAT; the schema requires a value and
    # validate.py treats all-zero line VAT with a non-zero vat_total as lumped.
    if vat_amount == "":
        if not lumped_vat:
            raise ValueError(
                f"{where}: vat_amount is empty but seeded_defect is not lumped_vat"
            )
        vat_amount = Decimal(0)
    return {
        "description": line["description_ar"],
        "quantity": line["quantity"],
        "unit_price": line["unit_price"],
        "line_total": line["line_total"],
        "vat_rate": line["vat_rate"],
        "vat_amount": vat_amount,
    }


def _map_record(record: dict[str, Any], samples_dir: Path) -> Sample:
    where = record.get("file", "<record without file>")
    invoice_fields = frozenset(Invoice.model_fields)
    meta_fields = frozenset(GroundTruthMeta.model_fields)
    _check_keys(record, invoice_fields | meta_fields, where)
    meta = GroundTruthMeta.model_validate({k: record[k] for k in meta_fields})
    lumped = meta.seeded_defect == "lumped_vat"
    values = {k: record[k] for k in invoice_fields - {"line_items"}}
    values["line_items"] = [
        _map_line(line, f"{where} line_items[{i}]", lumped)
        for i, line in enumerate(record["line_items"])
    ]
    try:
        invoice = Invoice.model_validate(values)
    except ValidationError as exc:
        raise ValueError(
            f"{where}: ground truth does not satisfy the Invoice schema: {exc}"
        ) from exc
    return Sample(image_path=samples_dir / meta.file, invoice=invoice, meta=meta)


def load_samples(eval_dir: Path = EVAL_DIR) -> list[Sample]:
    truth_path = eval_dir / "ground_truth.json"
    samples_dir = eval_dir / "samples"
    records = json.loads(truth_path.read_text(encoding="utf-8"), parse_float=Decimal)
    if not isinstance(records, list):
        raise TypeError(
            f"{truth_path}: expected a JSON array, received {type(records).__name__}"
        )
    samples = [_map_record(record, samples_dir) for record in records]
    missing = [s.meta.file for s in samples if not s.image_path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"{len(missing)} of {len(samples)} ground-truth images are missing from"
            f" {samples_dir}: {', '.join(missing)}"
        )
    return samples


def summary(samples: list[Sample]) -> str:
    lines = [f"samples: {len(samples)}"]
    by_type = Counter(s.invoice.invoice_type for s in samples)
    lines.append(
        "by invoice_type: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
    )
    defects = Counter(s.meta.seeded_defect for s in samples if s.meta.seeded_defect)
    lines.append(
        f"seeded defects: {sum(defects.values())}"
        + (
            " (" + ", ".join(f"{k}={v}" for k, v in sorted(defects.items())) + ")"
            if defects
            else ""
        )
    )
    lines.append("populated fields:")
    for name in Invoice.model_fields:
        populated = sum(
            1 for s in samples if getattr(s.invoice, name) not in (None, "")
        )
        lines.append(f"  {name:<18}{populated:>3}/{len(samples)}")
    return "\n".join(lines)


if __name__ == "__main__":
    # Run from the repo root as `python -m eval.load_data` so `app` is importable.
    print(summary(load_samples()))
