"""
Measures per-field extraction accuracy against eval/ground_truth.json.
Run from the repo root: `python -m eval.run_eval [--repeats N] [--no-cache] [--files ...]`.

Responses come from .cache/ unless --no-cache is given. With --repeats N > 1 every
image is extracted N times with the cache disabled, and the report adds per-field
agreement across runs: the model is not guaranteed to be deterministic, and one
pass hides that.

`--golden eval/golden_set.csv` scores only the hand-verified numeric fields in that
CSV, split by numeral type, and writes a run record to eval/runs/. It always reads
from the cache: an invoice already cached is never sent to the API again. Every
field outside the CSV is reported as UNVERIFIED, with no accuracy figure.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app import cache
from app.extract import PROMPT_VERSION, ParseError, extract, model_name
from app.schema import CallMetadata, Invoice, LineItem
from app.validate import QR_RULES
from eval.load_data import (
    EVAL_DIR,
    GOLDEN_LINE_FIELDS,
    GOLDEN_TOTALS,
    GoldenRow,
    Sample,
    load_golden,
    load_samples,
)

MAX_UNCONFIRMED_CALLS = 50
DEFECT_RULES = {
    "missing_buyer_vat": "standard_invoice_has_buyer_vat_number",
    "lumped_vat": "vat_is_itemised_per_line",
    "missing_seller_vat": "seller_vat_number_present",
}
LINE_INDEX = re.compile(r"line_items\[\d+\]")
RUNS_DIR = EVAL_DIR / "runs"
COST_UNKNOWN = (
    "unknown (set OPENAI_PRICE_INPUT_PER_1M_USD and OPENAI_PRICE_OUTPUT_PER_1M_USD)"
)
GOLDEN_GROUPS = ("all", "latin", "arabic_indic")
# Header fields scored for the QR cross-check. The invoice number is in no ZATCA QR,
# so it is listed to show that its misreads are always missed.
QR_SCORED = (
    "invoice_number",
    "invoice_date",
    "invoice_timestamp",
    "seller_vat_number",
    "total",
    "vat_total",
)
QR_FIELDS = frozenset(QR_SCORED[1:])


@dataclass
class Run:
    metadata: CallMetadata | None  # None only if the call itself returned no content
    fields: dict[str, Any] | None  # None when the response could not be parsed
    rules: list[str] = field(default_factory=list)
    confidences: dict[str, float] = field(default_factory=dict)
    qr_read: bool = False
    qr_flagged: list[str] = field(default_factory=list)  # fields a QR finding names


def flatten(invoice: Invoice) -> dict[str, Any]:
    values = {
        name: getattr(invoice, name)
        for name in Invoice.model_fields
        if name != "line_items"
    }
    values["line_items.count"] = len(invoice.line_items)
    for i, line in enumerate(invoice.line_items):
        for name, value in line.model_dump().items():
            values[f"line_items[{i}].{name}"] = value
    return values


def _group(path: str) -> str:
    """Per-line paths are pooled: line_items[3].vat_amount -> line_items[*].vat_amount."""
    return LINE_INDEX.sub("line_items[*]", path)


def _extract_once(image: bytes, file: str, use_cache: bool) -> Run:
    try:
        result, metadata = extract(image, use_cache=use_cache)
    except ParseError as exc:
        print(f"{file}: unparseable response: {exc}", file=sys.stderr)
        return Run(metadata=exc.metadata, fields=None)
    return Run(
        metadata=metadata,
        fields=flatten(result.invoice),
        rules=[f.rule for f in result.findings],
        confidences={c.field: c.confidence for c in result.confidences},
        qr_read=result.qr.status == "read",
        qr_flagged=sorted(
            {path for f in result.findings if f.rule in QR_RULES for path in f.fields}
        ),
    )


def run_sample(sample: Sample, repeats: int, use_cache: bool) -> list[Run]:
    image = sample.image_path.read_bytes()
    return [_extract_once(image, sample.meta.file, use_cache) for _ in range(repeats)]


def accuracy(
    samples: list[Sample], runs: dict[str, list[Run]]
) -> dict[str, list[bool]]:
    """Per pooled field: one bool per (sample, run) — did it match ground truth."""
    hits: dict[str, list[bool]] = {}
    for sample in samples:
        expected = flatten(sample.invoice)
        for run in runs[sample.meta.file]:
            for path, value in expected.items():
                got = run.fields.get(path) if run.fields is not None else None
                hits.setdefault(_group(path), []).append(got == value)
    return hits


def agreement(
    samples: list[Sample], runs: dict[str, list[Run]]
) -> dict[str, list[bool]]:
    """Per pooled field: one bool per sample — did every run return the same value."""
    same: dict[str, list[bool]] = {}
    for sample in samples:
        outputs = [r.fields for r in runs[sample.meta.file]]
        for path in flatten(sample.invoice):
            values = [str(o.get(path)) if o is not None else None for o in outputs]
            same.setdefault(_group(path), []).append(len(set(values)) == 1)
    return same


def qr_outcome(sample: Sample, run: Run, path: str) -> str | None:
    """What the QR cross-check did about one header field; None when the field was
    read right and nothing was raised."""
    misread = run.fields.get(path) != flatten(sample.invoice)[path]
    flagged = path in run.qr_flagged
    if not misread:
        return "false alarm" if flagged else None
    if flagged:
        return "caught"
    if path not in QR_FIELDS:
        return "missed: not in the QR"
    if sample.meta.qr_base64 is None:
        return "missed: no QR on the invoice"
    if not run.qr_read:
        return "missed: QR not read"
    return "missed: QR read but raised nothing"


def qr_report(samples: list[Sample], runs: dict[str, list[Run]]) -> list[str]:
    out = ["QR cross-check, header fields against ground truth:"]
    for group in ("arabic_indic", "latin"):
        subset = [s for s in samples if s.meta.numerals == group]
        if subset:
            out += _qr_group(group, subset, runs)
    return out


def _qr_group(
    group: str, subset: list[Sample], runs: dict[str, list[Run]]
) -> list[str]:
    details: list[str] = []
    counts: Counter[str] = Counter()
    for sample in subset:
        for run in runs[sample.meta.file]:
            if run.fields is None:
                continue
            for path in QR_SCORED:
                outcome = qr_outcome(sample, run, path)
                if outcome is not None:
                    counts[outcome.split(":")[0]] += 1
                    details.append(f"    {sample.meta.file}  {path}: {outcome}")
    group_runs = [r for s in subset for r in runs[s.meta.file]]
    with_qr = sum(s.meta.qr_base64 is not None for s in subset)
    misreads = counts["caught"] + counts["missed"]
    summary = (
        f"  {group}: QR on {with_qr} of {len(subset)} invoices,"
        f" read in {sum(r.qr_read for r in group_runs)} of {len(group_runs)} runs;"
        f" {misreads} header misreads, {counts['caught']} caught,"
        f" {counts['missed']} missed, {counts['false alarm']} false alarms"
    )
    return [summary, *details]


def defect_catch(samples: list[Sample], runs: dict[str, list[Run]]) -> list[str]:
    lines = []
    for sample in samples:
        defect = sample.meta.seeded_defect
        if defect is None:
            continue
        caught = sum(DEFECT_RULES[defect] in r.rules for r in runs[sample.meta.file])
        lines.append(
            f"  {sample.meta.file}  {defect:<20} caught {caught}/{len(runs[sample.meta.file])}"
        )
    return lines


def confidence_threshold() -> float:
    """Only the calibration report uses this; nothing in the app gates on it."""
    raw = os.environ.get("CONFIDENCE_THRESHOLD", "0.80")
    try:
        threshold = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"CONFIDENCE_THRESHOLD must be a number between 0 and 1, received {raw!r}"
        ) from exc
    if not 0.0 <= threshold <= 1.0:
        raise RuntimeError(
            f"CONFIDENCE_THRESHOLD must be between 0 and 1, received {threshold}"
        )
    return threshold


def calibration(
    samples: list[Sample], runs: dict[str, list[Run]], threshold: float
) -> list[str]:
    """
    Does the model's confidence predict its errors? Compares the mean score on
    correct fields with the mean on incorrect ones, and counts wrong fields that
    the threshold would have waved through. Fields the model gave no score are
    reported but excluded from the means.
    """
    correct: list[float] = []
    incorrect: list[float] = []
    unscored = 0
    for sample in samples:
        for path, value in flatten(sample.invoice).items():
            if path == "line_items.count":
                continue
            for run in runs[sample.meta.file]:
                if run.fields is None:
                    continue
                score = run.confidences.get(path)
                if score is None:
                    unscored += 1
                    continue
                (correct if run.fields.get(path) == value else incorrect).append(score)
    missed = sum(s >= threshold for s in incorrect)
    false_alarms = sum(s < threshold for s in correct)
    return [
        f"confidence calibration (threshold {threshold:.2f}):",
        f"  mean confidence on correct fields:   {_mean(correct)} (n={len(correct)})",
        f"  mean confidence on incorrect fields: {_mean(incorrect)} (n={len(incorrect)})",
        f"  incorrect fields scored >= threshold: {missed}/{len(incorrect)}",
        f"  correct fields scored < threshold:    {false_alarms}/{len(correct)}",
        f"  fields with no score from the model:  {unscored}",
    ]


def _mean(scores: list[float]) -> str:
    return f"{sum(scores) / len(scores):.3f}" if scores else "n/a"


SUBGROUPS = (
    ("arabic_only", lambda s: s.meta.language == "arabic_only"),
    ("bilingual", lambda s: s.meta.language == "bilingual"),
    ("arabic_indic", lambda s: s.meta.numerals == "arabic_indic"),
    ("latin", lambda s: s.meta.numerals == "latin"),
)


def subgroup_table(samples: list[Sample], runs: dict[str, list[Run]]) -> list[str]:
    """Per-field accuracy split by layout language and numeral style."""
    columns = [("all", samples)] + [
        (name, [s for s in samples if keep(s)]) for name, keep in SUBGROUPS
    ]
    columns = [(name, subset) for name, subset in columns if subset]
    tables = {name: accuracy(subset, runs) for name, subset in columns}
    header = f"{'field':<28}" + "".join(
        f"{name} (n={len(subset)})".rjust(20) for name, subset in columns
    )
    out = [header, "-" * len(header)]
    for path in tables["all"]:
        row = f"{path:<28}"
        for name, _ in columns:
            flags = tables[name][path]
            row += f"{100 * sum(flags) / len(flags):.1f}%".rjust(20)
        out.append(row)
    return out


def dump_runs(path: Path, runs: dict[str, list[Run]]) -> None:
    """Raw per-run output, so later cuts of the numbers need no new API calls."""
    serialisable = {
        file: [
            {
                **asdict(run),
                "metadata": run.metadata.model_dump(mode="json")
                if run.metadata
                else None,
                "fields": {k: str(v) for k, v in run.fields.items()}
                if run.fields is not None
                else None,
            }
            for run in file_runs
        ]
        for file, file_runs in runs.items()
    }
    path.write_text(json.dumps(serialisable, ensure_ascii=False, indent=1), "utf-8")


def _pct(flags: list[bool]) -> str:
    return _ratio(sum(flags), len(flags))


def _ratio(correct: int, total: int) -> str:
    fraction = f"({correct}/{total})"
    return f"{100 * correct / total:6.1f}% {fraction:>9}"


def report(samples: list[Sample], runs: dict[str, list[Run]], repeats: int) -> str:
    acc = accuracy(samples, runs)
    agree = agreement(samples, runs) if repeats > 1 else {}
    header = f"{'field':<32}{'accuracy':>20}" + (f"{'agreement':>20}" if agree else "")
    out = [header, "-" * len(header)]
    for name in acc:
        row = f"{name:<32}{_pct(acc[name]):>20}"
        if agree:
            row += f"{_pct(agree[name]):>20}"
        out.append(row)
    all_runs = [r for rs in runs.values() for r in rs]
    out += ["", *subgroup_table(samples, runs)]
    out += [
        "",
        f"seeded defects caught ({len(all_runs)} runs):",
        *defect_catch(samples, runs),
    ]
    out += ["", *qr_report(samples, runs)]
    out += ["", *calibration(samples, runs, confidence_threshold())]
    out += ["", *_summary(samples, all_runs, repeats)]
    return "\n".join(out)


def _summary(samples: list[Sample], all_runs: list[Run], repeats: int) -> list[str]:
    metas = [r.metadata for r in all_runs if r.metadata is not None]
    unparseable = sum(r.fields is None for r in all_runs)
    return [
        (
            f"samples={len(samples)} repeats={repeats} runs={len(all_runs)}"
            f" unparseable={unparseable}"
            f" cache_hits={sum(m.cache_hit for m in metas)}"
            f" temperature_zero={sum(m.temperature_zero for m in metas)}/{len(metas)}"
        ),
        (
            f"prompt_tokens={sum(m.prompt_tokens for m in metas)}"
            f" completion_tokens={sum(m.completion_tokens for m in metas)}"
            f" estimated_cost_usd={_total_cost(metas)}"
        ),
    ]


def _cost_sum(metas: list[CallMetadata]) -> Decimal | None:
    costs = [m.estimated_cost_usd for m in metas]
    if any(c is None for c in costs):
        return None
    return sum(costs, Decimal(0))


def _total_cost(metas: list[CallMetadata]) -> str:
    total = _cost_sum(metas)
    if total is None:
        return COST_UNKNOWN
    return f"{total:.4f}"


def unverified_fields() -> list[str]:
    """Pooled paths the golden set does not cover; they get no accuracy figure."""
    verified = {
        *GOLDEN_TOTALS,
        "line_items.count",
        *(f"line_items[*].{name}" for name in GOLDEN_LINE_FIELDS),
    }
    paths = [name for name in Invoice.model_fields if name != "line_items"]
    paths += [f"line_items[*].{name}" for name in LineItem.model_fields]
    return [path for path in paths if path not in verified]


def golden_matches(row: GoldenRow, run: Run) -> dict[str, bool]:
    """Lines are matched by position; a line the model missed reads as None."""
    fields = run.fields or {}
    return {path: fields.get(path) == value for path, value in row.expected.items()}


def golden_accuracy(
    rows: list[GoldenRow], runs: dict[str, Run]
) -> dict[str, dict[str, list[bool]]]:
    """Per numeral group, per pooled field: one bool per invoice."""
    table: dict[str, dict[str, list[bool]]] = {group: {} for group in GOLDEN_GROUPS}
    for row in rows:
        for path, hit in golden_matches(row, runs[row.file]).items():
            for group in ("all", row.numerals):
                table[group].setdefault(_group(path), []).append(hit)
    return table


def _golden_invoice(row: GoldenRow, run: Run) -> dict[str, Any]:
    fields = run.fields or {}
    matches = golden_matches(row, run)
    return {
        "file": row.file,
        "numerals": row.numerals,
        "parsed": run.fields is not None,
        "expected_line_count": int(row.expected["line_items.count"]),
        "extracted_line_count": fields.get("line_items.count"),
        "metadata": run.metadata.model_dump(mode="json") if run.metadata else None,
        "fields": {
            path: {
                "expected": str(value),
                "extracted": None if fields.get(path) is None else str(fields[path]),
                "match": matches[path],
            }
            for path, value in row.expected.items()
        },
    }


def _golden_totals(invoices: list[dict[str, Any]], metas: list[CallMetadata]) -> dict:
    """On a cache hit, cost and latency describe the call that produced the answer."""
    live = [m for m in metas if not m.cache_hit]
    spent, original = _cost_sum(live), _cost_sum(metas)
    latencies = [m.latency_ms for m in metas]
    gaps = [
        (i["extracted_line_count"] or 0) - i["expected_line_count"] for i in invoices
    ]
    return {
        "invoices": {"all": len(invoices), **Counter(i["numerals"] for i in invoices)},
        "unparseable": sum(not i["parsed"] for i in invoices),
        "live_calls": len(live),
        "cache_hits": len(metas) - len(live),
        "spent_this_run_usd": None if spent is None else str(spent),
        "original_calls_cost_usd": None if original is None else str(original),
        "original_call_latency_ms": {
            "mean": round(sum(latencies) / len(latencies)) if latencies else None,
            "max": max(latencies, default=None),
        },
        "lines_missed": -sum(g for g in gaps if g < 0),
        "lines_extra_not_scored": sum(g for g in gaps if g > 0),
    }


def golden_record(
    rows: list[GoldenRow], runs: dict[str, Run], golden: Path, started: datetime
) -> dict[str, Any]:
    invoices = [_golden_invoice(row, runs[row.file]) for row in rows]
    metas = [r.metadata for r in runs.values() if r.metadata is not None]
    accuracy_table = golden_accuracy(rows, runs)
    return {
        "timestamp_utc": started.isoformat(),
        "golden_set": golden.as_posix(),
        "golden_set_sha256": hashlib.sha256(golden.read_bytes()).hexdigest(),
        "model": model_name(),
        "prompt_version": PROMPT_VERSION,
        "accuracy": {
            group: {
                path: {"correct": sum(flags), "total": len(flags)}
                for path, flags in fields.items()
            }
            for group, fields in accuracy_table.items()
        },
        "unverified": unverified_fields(),
        "totals": _golden_totals(invoices, metas),
        "invoices": invoices,
    }


def write_run_record(record: dict[str, Any], runs_dir: Path, started: datetime) -> Path:
    runs_dir.mkdir(exist_ok=True)
    path = runs_dir / f"{started:%Y%m%dT%H%M%SZ}.json"
    # "x": a record is never overwritten; a clash raises FileExistsError.
    with path.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    return path


def golden_report(record: dict[str, Any]) -> str:
    counts = record["totals"]["invoices"]
    header = f"{'field':<28}" + "".join(
        f"{group} (n={counts.get(group, 0)})".rjust(20) for group in GOLDEN_GROUPS
    )
    out = [header, "-" * len(header)]
    for path in record["accuracy"]["all"]:
        cells = [record["accuracy"][group].get(path) for group in GOLDEN_GROUPS]
        out.append(
            f"{path:<28}"
            + "".join(
                (_ratio(c["correct"], c["total"]) if c else "n/a").rjust(20)
                for c in cells
            )
        )
    out += ["", "not in the golden set (no accuracy figure):"]
    out += [f"  {path:<28}UNVERIFIED" for path in record["unverified"]]
    totals = record["totals"]
    latency = totals["original_call_latency_ms"]
    out += [
        "",
        (
            f"model={record['model']} prompt_version={record['prompt_version']}"
            f" unparseable={totals['unparseable']} live_calls={totals['live_calls']}"
            f" cache_hits={totals['cache_hits']}"
        ),
        (
            f"lines_missed={totals['lines_missed']}"
            f" lines_extra_not_scored={totals['lines_extra_not_scored']}"
        ),
        f"original_call_latency_ms mean={latency['mean']} max={latency['max']}",
        f"spent_this_run_usd={totals['spent_this_run_usd'] or COST_UNKNOWN}",
        f"original_calls_cost_usd={totals['original_calls_cost_usd'] or COST_UNKNOWN}",
    ]
    return "\n".join(out)


def _planned_calls(images: list[Path], repeats: int, use_cache: bool) -> int:
    if not use_cache:
        return len(images) * repeats
    model = model_name()
    return sum(
        cache.get(image.read_bytes(), model, PROMPT_VERSION) is None for image in images
    )


def _over_budget(planned: int, confirmed: bool) -> bool:
    if planned > MAX_UNCONFIRMED_CALLS and not confirmed:
        print(
            f"this run would make {planned} API calls (limit {MAX_UNCONFIRMED_CALLS}"
            " without --confirm-spend); stopping",
            file=sys.stderr,
        )
        return True
    return False


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repeats", type=int, default=1, help="runs per image (>1 disables cache)"
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="call the API even when cached"
    )
    parser.add_argument(
        "--files", nargs="+", metavar="FILE", help="subset of sample filenames"
    )
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help=f"allow more than {MAX_UNCONFIRMED_CALLS} API calls",
    )
    parser.add_argument(
        "--dump", type=Path, metavar="PATH", help="write raw per-run results as JSON"
    )
    parser.add_argument(
        "--golden",
        type=Path,
        metavar="CSV",
        help="score the hand-verified golden set and write a record to eval/runs/",
    )
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.golden and (args.no_cache or args.repeats != 1 or args.dump):
        parser.error(
            "--golden always reads the cache and writes its own run record;"
            " it cannot be combined with --no-cache, --repeats or --dump"
        )
    if args.files:
        _check_files(parser, args.files, args.golden)
    return args


def _check_files(
    parser: argparse.ArgumentParser, files: list[str], golden: Path | None
) -> None:
    if golden:
        known = {r.file for r in load_golden(golden)}
    else:
        known = {s.meta.file for s in load_samples()}
    unknown = [f for f in files if f not in known]
    if unknown:
        parser.error(f"not in {golden or 'ground truth'}: {', '.join(unknown)}")


def run_golden(golden: Path, files: list[str] | None, confirmed: bool) -> int:
    rows = load_golden(golden)
    if files:
        rows = [r for r in rows if r.file in files]
    planned = _planned_calls([r.image_path for r in rows], 1, use_cache=True)
    if _over_budget(planned, confirmed):
        return 2
    print(f"planned API calls: {planned} (cache on)", file=sys.stderr)
    started = datetime.now(UTC)
    runs = {
        r.file: _extract_once(r.image_path.read_bytes(), r.file, True) for r in rows
    }
    record = golden_record(rows, runs, golden, started)
    path = write_run_record(record, RUNS_DIR, started)
    print(golden_report(record))
    print(f"run record: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.golden:
        return run_golden(args.golden, args.files, args.confirm_spend)
    samples = load_samples()
    if args.files:
        by_file = {s.meta.file: s for s in samples}
        samples = [by_file[f] for f in args.files]
    use_cache = not args.no_cache and args.repeats == 1
    images = [s.image_path for s in samples]
    planned = _planned_calls(images, args.repeats, use_cache)
    if _over_budget(planned, args.confirm_spend):
        return 2
    print(
        f"planned API calls: {planned} (cache {'on' if use_cache else 'off'})",
        file=sys.stderr,
    )
    runs = {s.meta.file: run_sample(s, args.repeats, use_cache) for s in samples}
    if args.dump:
        dump_runs(args.dump, runs)
    print(report(samples, runs, args.repeats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
