"""
Numeral-reading test on CORU OCR line crops: Arabic-Indic vs Western money amounts.

    python read_numbers.py select              # writes selection.json (no network)
    python read_numbers.py run --dry-run       # counts calls and tokens (no network)
    python read_numbers.py run --confirm-spend # the paid run

Images and transcriptions are read in memory from CORU's OCR/test.zip (38.7 MB,
huggingface.co/datasets/abdoelsayed/CORU), whose path is taken from CORU_OCR_ZIP;
nothing is extracted. The model is asked for every number on the line with digits converted
to 0-9 and separators kept as printed. Expected values come from the CORU
transcription the same way. Two scores per line:
  strict      the multiset of numbers matches exactly, separators included
  digits_only the multiset matches once '.' and ',' are removed, so a misread
              separator does not count against digit reading
Responses are cached by image SHA-256, model and prompt version; a cached line is
never sent again.
"""

import argparse
import base64
import hashlib
import io
import json
import math
import os
import re
import sys
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from PIL import Image

HERE = Path(__file__).resolve().parent
SELECTION = HERE / "selection.json"


def zip_path() -> Path:
    raw = os.environ.get("CORU_OCR_ZIP", "").strip()
    if not raw:
        raise RuntimeError(
            "CORU_OCR_ZIP is not set; point it at CORU's OCR/test.zip"
            " (huggingface.co/datasets/abdoelsayed/CORU)"
        )
    return Path(raw)


CACHE_DIR = HERE / ".cache"
MAX_OUTPUT_TOKENS = 200
PRICE_ENV = ("OPENAI_PRICE_INPUT_PER_1M_USD", "OPENAI_PRICE_OUTPUT_PER_1M_USD")
REVIEW_FLAGS = HERE / "review_flags.json"

PROMPTS = {
    # n1: the model converts digits to 0-9.
    "n1": """This image is one line cropped from a printed receipt.
Transcribe every number printed on it.
- Write each number with Western digits 0-9, converting Arabic-Indic digits.
- Keep '.' and ',' separators exactly where they are printed. Write the Arabic
  decimal separator as ','. Do not add, remove or move separators.
- Leave out signs, percent signs, currency and any letters.
- Numbers separated by a space or any other character are separate numbers.
Return JSON only: {"numbers": ["...", "..."]}. Order does not matter.""",
    # n2: the model copies glyphs as printed; Python converts them.
    "n2": """This image is one line cropped from a printed receipt.
Copy every number printed on it exactly as printed.
- Keep each digit in the script it is printed in: copy Arabic-Indic digits
  (٠١٢٣٤٥٦٧٨٩) as Arabic-Indic characters and Western digits as Western digits.
  Do not convert, translate or normalise any digit.
- Keep every separator as the character printed (for example ٫ , .).
- Leave out signs, percent signs, currency and any letters.
- Numbers separated by a space or any other character are separate numbers.
Return JSON only: {"numbers": ["...", "..."]}. Order does not matter.""",
}

# The 45 lines hand-classified as Arabic-Indic money amounts (see the session
# notes): the Arabic-Indic digits themselves are the amount, not a size or ID.
ARABIC_INDIC_AMOUNT_STEMS = [
    "07724c9f-c988-4d4c-8c17-3518f3f5dbea_line_14",
    "1e59e1d7-ad9e-4a4a-b315-7a675ab95dfd_line_13",
    "1e59e1d7-ad9e-4a4a-b315-7a675ab95dfd_line_16",
    "1e59e1d7-ad9e-4a4a-b315-7a675ab95dfd_line_19",
    "1e59e1d7-ad9e-4a4a-b315-7a675ab95dfd_line_20",
    "1e59e1d7-ad9e-4a4a-b315-7a675ab95dfd_line_23",
    "1e59e1d7-ad9e-4a4a-b315-7a675ab95dfd_line_27",
    "1f220e57-4a7e-4d85-b382-9998960ebde5_total",
    "234d352d-5051-4fec-ac5d-a5212a5b9077_line_10",
    "27a4c125-71ae-445a-8f19-42aeba0f3642_total",
    "2c85f984-9864-4c15-bffe-492d102276a8_total",
    "30ca174a-4c67-474a-ab45-70000fc283f7_line_9",
    "35058950-8985-46e5-bfef-30d37a225a09_total",
    "3aaa9f60-3fef-43d5-8a92-3f25ce86b5aa_line_4",
    "3b998526-3059-4809-8239-0436eeed1f16_line_190",
    "3e0dadc1-b903-4aad-adec-ea8ff8a7704b_total",
    "46ccec20-4679-4479-a74b-9c110002c342_line_22",
    "513ee063-3e78-4e93-bb5d-979c7a03b634_line_14",
    "513ee063-3e78-4e93-bb5d-979c7a03b634_line_18",
    "54c42162-99d2-4ee4-97f0-aab621fc651b_total",
    "5742ff8b-ebdb-4d93-b900-3b4154b71a47_line_4",
    "5a5bf090-5eb0-44bf-8ee6-5a909130e8b2_line_16",
    "5ad3bbf5-bff7-4188-a52a-b2693bb79a4f_line_18",
    "5e76093c-6633-45aa-ae80-57e9c8152a64_line_8",
    "5e76093c-6633-45aa-ae80-57e9c8152a64_total",
    "5eab8fa8-c05e-45a3-bf23-a6a84858bbfc_total",
    "61e8208f-5fec-4d6a-9070-1d01923f0216_line_7",
    "65d9f361-f71a-4391-b4cc-b25771b70047_line_16",
    "71723d56-418d-4e0b-8a5e-d91c6456f15f_line_4",
    "72cd3d0c-aa67-47cc-ab4c-cee00ad70520_line_32",
    "87f88557-53ac-4f56-9947-fe501f37b6a0_line_16",
    "8ea4e42d-d8d0-4952-be1f-441e6f38049f_line_18",
    "9ab841a4-a770-4089-b64c-d8b1365157b0_line_32",
    "a93ca0c6-0eef-40e1-8c59-5e1962793bc7_line_11",
    "a99f63ab-ec7f-4ee6-a2ea-70ba45b748a2_total",
    "af595b90-3cb0-443a-bcd1-e6b57e3d013e_line_22",
    "bd7503e4-b7fe-4459-a0f0-8f1b2eeac3a2_line_12",
    "bfdadbc6-987f-4c8f-84fe-3dce712dc37c_line_12",
    "c464e16b-3c11-446a-840d-2ae28287fdd6_line_9",
    "cd04e50d-a3f8-4591-bdf2-02bbea78c3dc_line_100",
    "da8e483e-08a2-4d42-8ee9-d93ccdd3c21a_line_14",
    "db2d2948-5099-4f9b-91ea-f2f08e47b783_line_20",
    "ddf23862-d134-4c10-8b03-ba0068c66536_line_100",
    "eae3ee5a-b08c-48bc-a9b4-02266137e764_line_20",
    "f77aad43-f49e-4d27-8c2d-1d78b951b04c_line_220",
]

# Western picks rejected on review, with the reason.
WESTERN_EXCLUDED = {
    "00b357e0-a7dd-45dc-baa8-2025484ccd3c_line_24": "item count, not money",
}

LABELS = [
    "المجموع",
    "الاجمالي",
    "الإجمالي",
    "اجمالي",
    "إجمالى",
    "الاجمالى",
    "الإجمالى",
    "اﻹجمالي",
    "الباقى",
    "الباقي",
    "المدفوع",
    "مدفوع",
    "نقدى",
    "الخصم",
    "الضريبة",
    "المضافة",
    "جنيه",
    "جنيهات",
    "الصافى",
    "الصافي",
    "دفع",
    "وفرت",
    "المستحقة",
    "مبلغ",
    "قيمة",
    "Cash",
    "VAT",
]
# Extended (Persian/Urdu) forms too: n2 asks the model to copy glyphs, and a copy in
# the wrong code block still carries the right digit value. Counted in the report.
ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "0123456789" * 2)
EXTENDED_DIGIT = re.compile("[۰-۹]")
SEPARATORS = str.maketrans("", "", ".,٫٬")
NUMBER = re.compile(r"[0-9٠-٩]+(?:[.,][0-9٠-٩]+)*")
HAS_ARABIC_INDIC = re.compile("[٠-٩]")
HAS_ARABIC_LETTER = re.compile("[ء-ي]")
# Dates, times, phone numbers and IDs: not money, keep them out of the Western set.
NOT_MONEY = re.compile(r"\d+/\d+/\d+|\d{4}-\d\d|\d{7,}|:\d\d")
RECEIPT = re.compile(r"_(line|total|date|no|merchant)")


def transcription(archive: zipfile.ZipFile, stem: str) -> str:
    return " ".join(json.loads(archive.read(f"test/{stem}.txt").decode("utf-8-sig")))


def expected_numbers(text: str) -> list[str]:
    return [n.translate(ARABIC_INDIC) for n in NUMBER.findall(text)]


def kind(text: str) -> str:
    return "labelled" if any(label in text for label in LABELS) else "item_row"


def all_zero(text: str) -> bool:
    return all(set(n) <= {"0", ".", ","} for n in expected_numbers(text))


def _western_candidates(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    out = []
    for name in sorted(n for n in archive.namelist() if n.endswith(".txt")):
        stem = name[len("test/") : -len(".txt")]
        text = transcription(archive, stem)
        if (
            stem.startswith("0 (")  # passport machine-readable zones
            or stem in WESTERN_EXCLUDED
            or HAS_ARABIC_INDIC.search(text)
            or not HAS_ARABIC_LETTER.search(text)
            or NOT_MONEY.search(text)
        ):
            continue
        numbers = expected_numbers(text)
        decimals = [n for n in numbers if re.search(r"[.,]\d\d$", n)]
        if (
            kind(text) == "labelled"
            and decimals
            or kind(text) == "item_row"
            and len(numbers) >= 3
            and len(decimals) >= 2
        ):
            out.append((stem, text))
    return out


def select(archive: zipfile.ZipFile) -> list[dict[str, Any]]:
    """Western lines matched to the Arabic-Indic set on kind and on zero-only count,
    at most two per receipt, first in filename order."""
    arabic = [(s, transcription(archive, s)) for s in ARABIC_INDIC_AMOUNT_STEMS]
    need = Counter((kind(t), all_zero(t)) for _, t in arabic)
    per_receipt: Counter[str] = Counter()
    western = []
    for stem, text in _western_candidates(archive):
        key = (kind(text), all_zero(text))
        receipt = RECEIPT.split(stem)[0]
        if need[key] > 0 and per_receipt[receipt] < 2:
            western.append((stem, text))
            need[key] -= 1
            per_receipt[receipt] += 1
    if sum(need.values()):
        raise RuntimeError(f"not enough Western candidates; still needed: {dict(need)}")
    return [
        {
            "group": group,
            "stem": s,
            "kind": kind(t),
            "transcription": t,
            "expected": expected_numbers(t),
        }
        for group, lines in (("arabic_indic", arabic), ("western", western))
        for s, t in lines
    ]


def image_tokens(width: int, height: int) -> tuple[int, int]:
    """(estimate, upper bound) from OpenAI's published high-detail tile rule: fit in
    2048x2048, shortest side to 768, 170 tokens per 512px tile plus 85. The docs do
    not say whether small images are scaled up, so the estimate assumes not and the
    upper bound assumes so."""

    def tiles(w: float, h: float, upscale: bool) -> int:
        scale = min(1.0, 2048 / max(w, h))
        w, h = w * scale, h * scale
        short = min(w, h)
        if short > 768 or upscale:
            w, h = w * 768 / short, h * 768 / short
            scale = min(1.0, 2048 / max(w, h))
            w, h = w * scale, h * scale
        return 85 + 170 * math.ceil(w / 512) * math.ceil(h / 512)

    return tiles(width, height, False), tiles(width, height, True)


def _cache_path(image: bytes, model: str, version: str) -> Path:
    digest = hashlib.sha256(image).hexdigest()
    return CACHE_DIR / f"{digest}_{model}_{version}.json"


def _prices() -> tuple[Decimal, Decimal] | None:
    raw = [os.environ.get(name, "").strip() for name in PRICE_ENV]
    if not all(raw):
        return None
    return Decimal(raw[0]), Decimal(raw[1])


def _cost(prompt_tokens: int, completion_tokens: int) -> str:
    prices = _prices()
    if prices is None:
        return f"unknown (set {' and '.join(PRICE_ENV)})"
    usd = (prompt_tokens * prices[0] + completion_tokens * prices[1]) / 1_000_000
    return f"${usd:.4f}"


def dry_run(
    archive: zipfile.ZipFile, lines: list[dict[str, Any]], model: str, version: str
) -> None:
    # Rough: ~3 characters per token for this prompt, plus message overhead.
    text_tokens = len(PROMPTS[version]) // 3 + 20
    uncached = estimate = upper = 0
    for line in lines:
        image = archive.read(f"test/{line['stem']}.jpg")
        if _cache_path(image, model, version).exists():
            continue
        uncached += 1
        est, up = image_tokens(*Image.open(io.BytesIO(image)).size)
        estimate += est + text_tokens
        upper += up + text_tokens
    output = uncached * MAX_OUTPUT_TOKENS
    print(f"model={model} prompt_version={version}")
    print(
        f"lines={len(lines)} cached={len(lines) - uncached} planned_api_calls={uncached}"
    )
    print(f"input tokens: estimate {estimate}, upper bound {upper}")
    print(f"output tokens: at most {output} ({MAX_OUTPUT_TOKENS} per call cap)")
    print(
        f"cost: estimate {_cost(estimate, output)}, upper bound {_cost(upper, output)}"
    )
    print("dry run: no API call made")


def _call(client: Any, model: str, image: bytes, prompt: str) -> dict[str, Any]:
    started = time.perf_counter()
    url = f"data:image/jpeg;base64,{base64.b64encode(image).decode('ascii')}"
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=MAX_OUTPUT_TOKENS,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
                ],
            }
        ],
    )
    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError("model returned no message content")
    return {
        "content": content,
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


def _read(record: dict[str, Any]) -> list[str] | None:
    """None when the answer is not the requested shape; scored as wrong."""
    try:
        numbers = json.loads(record["content"])["numbers"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    if not isinstance(numbers, list) or not all(isinstance(n, str) for n in numbers):
        return None
    return [n.strip().translate(ARABIC_INDIC) for n in numbers]


def _strip(numbers: list[str]) -> Counter[str]:
    return Counter(n.translate(SEPARATORS) for n in numbers)


def score(line: dict[str, Any], got: list[str] | None) -> dict[str, Any]:
    expected = line["expected"]
    if got is None:
        return {"strict": False, "digits_only": False, "found": 0, "got": None}
    found = sum((Counter(expected) & Counter(got)).values())
    return {
        "strict": Counter(expected) == Counter(got),
        "digits_only": _strip(expected) == _strip(got),
        "found": found,
        "got": got,
    }


def report(results: list[dict[str, Any]]) -> list[str]:
    out = [f"{'group':<14}{'lines':>6}{'strict':>16}{'digits_only':>16}{'numbers':>18}"]
    for group in sorted({r["group"] for r in results}):
        rows = [r for r in results if r["group"] == group]
        n = len(rows)
        strict = sum(r["strict"] for r in rows)
        digits = sum(r["digits_only"] for r in rows)
        found = sum(r["found"] for r in rows)
        total = sum(len(r["expected"]) for r in rows)
        out.append(
            f"{group:<14}{n:>6}{f'{strict}/{n} {100 * strict / n:.0f}%':>16}"
            f"{f'{digits}/{n} {100 * digits / n:.0f}%':>16}"
            f"{f'{found}/{total} {100 * found / total:.0f}%':>18}"
        )
    return out


def _distance(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1]


def _adjacent_swap(e: str, a: str) -> bool:
    diff = [i for i, (x, y) in enumerate(zip(e, a)) if x != y]
    return (
        len(e) == len(a)
        and len(diff) == 2
        and diff[1] == diff[0] + 1
        and e[diff[0]] == a[diff[1]]
        and e[diff[1]] == a[diff[0]]
    )


def _pair(expected: list[str], got: list[str]) -> list[tuple[str | None, str | None]]:
    """Separator-free numbers: exact matches removed, then leftovers paired closest
    first by edit distance; what is left over is missed or extra."""
    exp = [e.translate(SEPARATORS) for e in expected]
    ans = [g.translate(SEPARATORS) for g in got]
    for e in list(exp):
        if e in ans:
            exp.remove(e)
            ans.remove(e)
    pairs: list[tuple[str | None, str | None]] = []
    while exp and ans:
        e, a = min(((e, a) for e in exp for a in ans), key=lambda p: _distance(*p))
        pairs.append((e, a))
        exp.remove(e)
        ans.remove(a)
    return pairs + [(e, None) for e in exp] + [(None, a) for a in ans]


def confusions(results: list[dict[str, Any]]) -> list[str]:
    """Misreads shown in Arabic-Indic glyphs: single-digit swaps (٣→٢) counted per
    digit; reversals, digits added or dropped, and missed or extra numbers whole."""
    to_ai = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
    digit: Counter[str] = Counter()
    whole: Counter[str] = Counter()
    for r in results:
        for e, a in _pair(r["expected"], r["got"] or []):
            ea, aa = (e or "").translate(to_ai), (a or "").translate(to_ai)
            if e is None:
                whole[f"extra      {aa}"] += 1
            elif a is None:
                whole[f"missed     {ea}"] += 1
            elif e[::-1] == a:
                whole[f"reversed   {ea}→{aa}"] += 1
            elif _adjacent_swap(e, a):
                whole[f"swapped    {ea}→{aa}"] += 1
            elif len(e) == len(a):
                digit.update(f"{x}→{y}" for x, y in zip(ea, aa) if x != y)
            else:
                whole[f"length     {ea}→{aa}"] += 1
    out = ["digit substitutions (same-length numbers):"]
    out += [f"  {k}  x{v}" for k, v in digit.most_common()] or ["  none"]
    out += ["whole-number errors:"]
    out += [f"  {k}  x{v}" for k, v in sorted(whole.items())] or ["  none"]
    return out


def run(
    archive: zipfile.ZipFile, lines: list[dict[str, Any]], model: str, version: str
) -> None:
    client = None
    CACHE_DIR.mkdir(exist_ok=True)
    results, spent_in, spent_out, live, extended = [], 0, 0, 0, 0
    for line in lines:
        image = archive.read(f"test/{line['stem']}.jpg")
        path = _cache_path(image, model, version)
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
        else:
            if client is None:
                from openai import OpenAI  # only a cache miss needs the SDK and a key

                client = OpenAI()
            record = _call(client, model, image, PROMPTS[version])
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            live += 1
            spent_in += record["prompt_tokens"]
            spent_out += record["completion_tokens"]
            print(
                f"{line['stem']}: prompt_tokens={record['prompt_tokens']}"
                f" completion_tokens={record['completion_tokens']}"
                f" cost={_cost(record['prompt_tokens'], record['completion_tokens'])}",
                file=sys.stderr,
            )
        extended += bool(EXTENDED_DIGIT.search(record["content"]))
        results.append({**line, **score(line, _read(record)), "raw": record["content"]})
    stamp = datetime.now(UTC)
    out = HERE / f"results_{version}_{stamp:%Y%m%dT%H%M%SZ}.json"
    out.write_text(
        json.dumps(
            {
                "timestamp_utc": stamp.isoformat(),
                "model": model,
                "prompt_version": version,
                "results": results,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print("\n".join(report(results)))
    print("\n".join(confusions([r for r in results if r["group"] == "arabic_indic"])))
    print(f"answers using Extended Arabic-Indic (U+06F0-06F9) digits: {extended}")
    print(
        f"live_calls={live} prompt_tokens={spent_in} completion_tokens={spent_out}"
        f" spent={_cost(spent_in, spent_out)}"
    )
    print(f"results: {out}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("select", "run"))
    parser.add_argument("--prompt-version", choices=sorted(PROMPTS), default="n1")
    parser.add_argument(
        "--subset",
        choices=("all", "clean-arabic-indic"),
        default="all",
        help="clean-arabic-indic: Arabic-Indic lines not flagged in review_flags.json",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--confirm-spend", action="store_true")
    args = parser.parse_args()
    archive = zipfile.ZipFile(zip_path())
    if args.command == "select":
        lines = select(archive)
        SELECTION.write_text(json.dumps(lines, ensure_ascii=False, indent=1), "utf-8")
        print(f"wrote {len(lines)} lines to {SELECTION}")
        return 0
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not model:
        parser.error("OPENAI_MODEL is not set")
    lines = json.loads(SELECTION.read_text(encoding="utf-8"))
    if args.subset == "clean-arabic-indic":
        flagged = json.loads(REVIEW_FLAGS.read_text(encoding="utf-8"))["flags"]
        lines = [
            l
            for l in lines
            if l["group"] == "arabic_indic" and l["stem"] not in flagged
        ]
    if args.dry_run:
        dry_run(archive, lines, model, args.prompt_version)
        return 0
    uncached = sum(
        not _cache_path(
            archive.read(f"test/{l['stem']}.jpg"), model, args.prompt_version
        ).exists()
        for l in lines
    )
    if uncached and not args.confirm_spend:
        parser.error(
            f"{uncached} lines are not cached; --dry-run, or --confirm-spend to pay"
        )
    run(archive, lines, model, args.prompt_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
