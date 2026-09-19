"""
Measures per-field extraction accuracy against eval/ground_truth.json.
Run from the repo root: `python -m eval.run_eval [--repeats N] [--no-cache] [--files ...]`.

Responses come from .cache/ unless --no-cache is given. With --repeats N > 1 every
image is extracted N times with the cache disabled, and the report adds per-field
agreement across runs: the model is not guaranteed to be deterministic, and one
pass hides that.
"""

import argparse
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app import cache
from app.extract import (
    PROMPT_VERSION,
    ParseError,
    confidence_threshold,
    extract,
    model_name,
)
from app.schema import CallMetadata, Invoice
from eval.load_data import Sample, load_samples

MAX_UNCONFIRMED_CALLS = 50
DEFECT_RULES = {
    "missing_buyer_vat": "standard_invoice_has_buyer_vat_number",
    "lumped_vat": "vat_is_itemised_per_line",
    "missing_seller_vat": "seller_vat_number_present",
}
LINE_INDEX = re.compile(r"line_items\[\d+\]")


@dataclass
class Run:
    metadata: CallMetadata | None  # None only if the call itself returned no content
    fields: dict[str, Any] | None  # None when the response could not be parsed
    rules: list[str] = field(default_factory=list)
    confidences: dict[str, float] = field(default_factory=dict)


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


def run_sample(sample: Sample, repeats: int, use_cache: bool) -> list[Run]:
    image = sample.image_path.read_bytes()
    runs: list[Run] = []
    for _ in range(repeats):
        try:
            result, metadata = extract(image, use_cache=use_cache)
        except ParseError as exc:
            print(f"{sample.meta.file}: unparseable response: {exc}", file=sys.stderr)
            runs.append(Run(metadata=exc.metadata, fields=None))
            continue
        runs.append(
            Run(
                metadata=metadata,
                fields=flatten(result.invoice),
                rules=[f.rule for f in result.findings],
                confidences={c.field: c.confidence for c in result.confidences},
            )
        )
    return runs


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


def _pct(flags: list[bool]) -> str:
    fraction = f"({sum(flags)}/{len(flags)})"
    return f"{100 * sum(flags) / len(flags):6.1f}% {fraction:>9}"


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
    out += [
        "",
        f"seeded defects caught ({len(all_runs)} runs):",
        *defect_catch(samples, runs),
    ]
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


def _total_cost(metas: list[CallMetadata]) -> str:
    costs = [m.estimated_cost_usd for m in metas]
    if any(c is None for c in costs):
        return "unknown (set OPENAI_PRICE_INPUT_PER_1M_USD and OPENAI_PRICE_OUTPUT_PER_1M_USD)"
    return f"{sum(costs, Decimal(0)):.4f}"


def _planned_calls(samples: list[Sample], repeats: int, use_cache: bool) -> int:
    if not use_cache:
        return len(samples) * repeats
    model = model_name()
    return sum(
        cache.get(s.image_path.read_bytes(), model, PROMPT_VERSION) is None
        for s in samples
    )


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    samples = load_samples()
    if args.files:
        known = {s.meta.file: s for s in samples}
        unknown = [f for f in args.files if f not in known]
        if unknown:
            parser.error(f"not in ground truth: {', '.join(unknown)}")
        samples = [known[f] for f in args.files]
    use_cache = not args.no_cache and args.repeats == 1
    planned = _planned_calls(samples, args.repeats, use_cache)
    if planned > MAX_UNCONFIRMED_CALLS and not args.confirm_spend:
        print(
            f"this run would make {planned} API calls (limit {MAX_UNCONFIRMED_CALLS} without --confirm-spend); stopping",
            file=sys.stderr,
        )
        return 2
    print(
        f"planned API calls: {planned} (cache {'on' if use_cache else 'off'})",
        file=sys.stderr,
    )
    runs = {s.meta.file: run_sample(s, args.repeats, use_cache) for s in samples}
    print(report(samples, runs, args.repeats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
