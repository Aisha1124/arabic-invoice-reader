"""
Recomputes every CORU figure in the project README from the evidence files in
this directory. Needs no CORU download, no API key and no network:

    python eval/coru/summarise.py

digits_only: a line counts as read correctly when its numbers, with decimal
separators removed, match CORU's transcription as a multiset.
"""

import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEPARATORS = str.maketrans("", "", ".,٫٬")
ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _load(name: str) -> dict:
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def _ratio(right: int, total: int) -> str:
    """One decimal, cut rather than rounded, so no figure is rounded up."""
    tenths = right * 1000 // total
    return f"{right} of {total} ({tenths // 10}.{tenths % 10}%)"


def _digits(numbers: list[str] | None) -> Counter[str]:
    return Counter(
        n.translate(ARABIC_INDIC).translate(SEPARATORS) for n in numbers or []
    )


def _numbers_right(lines: list[dict], answer_key: str) -> tuple[int, int]:
    right = sum(
        sum((_digits(line["expected"]) & _digits(line[answer_key])).values())
        for line in lines
    )
    return right, sum(len(line["expected"]) for line in lines)


def reading() -> list[str]:
    lines = [line for line in _load("reading_n1.json")["lines"] if not line["excluded"]]
    out = ["Reading, clean lines (reading_n1.json):"]
    for group in ("western", "arabic_indic"):
        rows = [line for line in lines if line["group"] == group]
        out.append(
            f"  {group}: {_ratio(sum(r['digits_only'] for r in rows), len(rows))}"
        )
    return out


def copy_then_convert() -> list[str]:
    lines = _load("copy_then_convert_n2.json")["lines"]
    out = [
        "Copy then convert, 29 clean Arabic-Indic lines (copy_then_convert_n2.json):"
    ]
    for run in ("n1", "n2"):
        right = sum(line[f"{run}_digits_only"] for line in lines)
        numbers = _numbers_right(lines, f"{run}_answer")
        out.append(
            f"  {run}: lines {_ratio(right, len(lines))}, numbers {_ratio(*numbers)}"
        )
    return out


def verification() -> list[str]:
    data = _load("verification.json")
    items = data["items"]
    hard = [i for i in items if i["kind"] == "hard"]
    controls = [i for i in items if i["kind"] == "control"]
    baseline = data["reading_accuracy_hard_numbers"]
    out = ["Two-choice verification (verification.json):"]
    out.append(f"  hard cases: {_ratio(sum(i['right'] for i in hard), len(hard))}")
    out.append(
        f"  reading the same numbers: {_ratio(baseline['correct'], baseline['total'])}"
    )
    out.append(
        f"  controls: {_ratio(sum(i['right'] for i in controls), len(controls))}"
    )
    for option, place in (("A", "first"), ("B", "second")):
        rows = [i for i in items if i["correct_option"] == option]
        out.append(
            f"  correct shown {place}: {_ratio(sum(i['right'] for i in rows), len(rows))}"
        )
    return out


def tesseract() -> list[str]:
    out = ["Tesseract (tesseract.json):"]
    for run in _load("tesseract.json")["runs"]:
        lines = run["lines"]
        right = sum(line["tesseract_digits_only"] for line in lines)
        numbers = _numbers_right(lines, "tesseract_numbers")
        agree = sum(
            _digits(line["tesseract_numbers"]) == _digits(line["gpt4o_n1_answer"])
            for line in lines
        )
        out.append(
            f"  {run['tesseract']} scale {run['scale']}x: lines {_ratio(right, len(lines))},"
            f" numbers {_ratio(*numbers)}, agrees with gpt-4o on {agree} lines"
        )
    return out


if __name__ == "__main__":
    print("\n".join(reading() + copy_then_convert() + verification() + tesseract()))
