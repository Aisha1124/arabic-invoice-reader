"""
Can the model VERIFY an Arabic-Indic number better than it can READ one?

    python verify_numbers.py --dry-run          # builds the questions, counts calls
    python verify_numbers.py --confirm-spend    # the paid run

Tests the confirming step of the planned resolver tool in isolation. In real use
the candidate value would come from invoice arithmetic; here the correct option
comes from the CORU transcription, and the wrong option is either the model's
own earlier misreading (hard cases) or a one-digit confusion from the n1/n2
confusion table (controls). Nothing here shows how often arithmetic would propose
the right candidate.

Questions are drawn from the 29 clean Arabic-Indic lines (read_numbers.py
--subset clean-arabic-indic) and the n1 and n2 results files. The correct option
is shown first in exactly half the questions, shuffled with a fixed seed.
"""

import argparse
import glob
import hashlib
import io
import json
import os
import random
import re
import sys
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image
from read_numbers import (
    ARABIC_INDIC,
    CACHE_DIR,
    HERE,
    NUMBER,
    REVIEW_FLAGS,
    SELECTION,
    SEPARATORS,
    _cost,
    _distance,
    _pair,
    image_tokens,
    zip_path,
)

VERSION = "v1"
SEED = 20260929
CONTROLS = 10
MAX_CALLS = 50
MAX_OUTPUT_TOKENS = 20
TO_ARABIC_INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
# Observed in the n1/n2 confusion table on these lines.
CONFUSIONS = {"٣": "٢", "٥": "٠", "٢": "٣", "٨": "٤", "٤": "٥", "٦": "٢"}

PROMPT = """This image is one line cropped from a printed receipt.
Exactly one of these two values is printed on it:
A: {a}
B: {b}
Which one does the image show? Look at each digit carefully.
Return JSON only: {{"answer": "A"}} or {{"answer": "B"}}."""


def _latest(pattern: str) -> dict[str, dict[str, Any]]:
    files = sorted(glob.glob(str(HERE / pattern)))
    if not files:
        raise FileNotFoundError(f"no results file matching {pattern} in {HERE}")
    results = json.loads(Path(files[-1]).read_text(encoding="utf-8"))["results"]
    return {r["stem"]: r for r in results}


def _clean_lines() -> list[dict[str, Any]]:
    flagged = json.loads(REVIEW_FLAGS.read_text(encoding="utf-8"))["flags"]
    lines = json.loads(SELECTION.read_text(encoding="utf-8"))
    return [
        l for l in lines if l["group"] == "arabic_indic" and l["stem"] not in flagged
    ]


def _printed(transcription: str) -> list[dict[str, Any]]:
    """Each number as printed: separator-free digits, script, decimal places."""
    out = []
    for token in NUMBER.findall(transcription):
        parts = re.split(r"[.,]", token.translate(ARABIC_INDIC))
        out.append(
            {
                "digits": "".join(parts),
                "arabic_indic": bool(re.search("[٠-٩]", token)),
                "decimals": len(parts[-1]) if len(parts) > 1 else 0,
            }
        )
    return out


def _format(digits: str, decimals: int, arabic_indic: bool) -> str:
    """Both options share this format, so layout cannot give the answer away."""
    if decimals and len(digits) > decimals:
        text = f"{digits[:-decimals]}٫{digits[-decimals:]}"
    else:
        text = digits  # no printed decimals, or too short to carry them
    if not arabic_indic:
        return text.replace("٫", ".")
    return text.translate(TO_ARABIC_INDIC)


def hard_cases(lines, runs) -> tuple[list[dict[str, Any]], Counter]:
    items, skipped = {}, Counter()
    for line in lines:
        printed = _printed(line["transcription"])
        on_line = {p["digits"] for p in printed}
        for run_name, run in runs.items():
            got = run[line["stem"]]["got"] or []
            for e, a in _pair(line["expected"], got):
                if e is None:
                    continue
                if a is None:
                    skipped["missed: no wrong reading to offer"] += 1
                    continue
                if a in on_line:
                    skipped["wrong reading also printed on the line"] += 1
                    continue
                if len(e) != len(a) and _distance(e, a) >= max(len(e), len(a)):
                    skipped[
                        "pairing not credible: different length, no digit in common"
                    ] += 1
                    print(
                        f"excluded pairing {line['stem']}: {e} vs {a}", file=sys.stderr
                    )
                    continue
                p = next(p for p in printed if p["digits"] == e)
                key = (line["stem"], e, a)
                items.setdefault(
                    key,
                    {
                        "kind": "hard",
                        "stem": line["stem"],
                        "readers": [],
                        "correct": _format(e, p["decimals"], p["arabic_indic"]),
                        "wrong": _format(a, p["decimals"], p["arabic_indic"]),
                    },
                )["readers"].append(run_name)
    for item in items.values():
        item["readers"] = sorted(set(item["readers"]))
    return list(items.values()), skipped


def reading_accuracy(lines, runs, hard: list[dict[str, Any]]) -> tuple[int, int]:
    """Over every printed occurrence of the hard numbers, in each run: how many
    were read right. Misses count as wrong."""
    wanted = {
        (h["stem"], h["correct"].translate(ARABIC_INDIC).translate(SEPARATORS))
        for h in hard
    }
    correct = total = 0
    for line in lines:
        digits = [x.translate(SEPARATORS) for x in line["expected"]]
        for run in runs.values():
            wrong = Counter(
                e
                for e, _ in _pair(line["expected"], run[line["stem"]]["got"] or [])
                if e
            )
            for e in {d for d in digits if (line["stem"], d) in wanted}:
                total += digits.count(e)
                correct += digits.count(e) - wrong[e]
    return correct, total


def controls(
    lines, runs, hard_stems: set[str], rng: random.Random
) -> list[dict[str, Any]]:
    """Numbers both runs read right; one confusable digit swapped for the wrong option."""
    pool = []
    for line in lines:
        printed = [p for p in _printed(line["transcription"]) if p["arabic_indic"]]
        on_line = {p["digits"] for p in printed}
        expected = {x.translate(SEPARATORS) for x in line["expected"]}
        right_in_all = set.intersection(
            *(
                expected
                - {
                    e
                    for e, _ in _pair(line["expected"], r[line["stem"]]["got"] or [])
                    if e
                }
                for r in runs.values()
            )
        )
        for p in printed:
            ai = p["digits"].translate(TO_ARABIC_INDIC)
            spots = [i for i, c in enumerate(ai) if c in CONFUSIONS]
            if p["digits"] not in right_in_all or not spots:
                continue
            i = rng.choice(spots)
            wrong = (ai[:i] + CONFUSIONS[ai[i]] + ai[i + 1 :]).translate(ARABIC_INDIC)
            if wrong in on_line:
                continue
            pool.append(
                {
                    "kind": "control",
                    "stem": line["stem"],
                    "readers": [],
                    "correct": _format(p["digits"], p["decimals"], True),
                    "wrong": _format(wrong, p["decimals"], True),
                    "substitution": f"{ai[i]}→{CONFUSIONS[ai[i]]}",
                }
            )
    # Prefer lines without hard cases, then others, one control per line first.
    rng.shuffle(pool)
    pool.sort(key=lambda c: c["stem"] in hard_stems)
    chosen, seen = [], set()
    for c in pool:
        if c["stem"] not in seen:
            chosen.append(c)
            seen.add(c["stem"])
    for c in pool:
        if len(chosen) >= CONTROLS:
            break
        if c not in chosen:
            chosen.append(c)
    return chosen[:CONTROLS]


def build() -> tuple[list[dict[str, Any]], Counter, tuple[int, int]]:
    rng = random.Random(SEED)
    lines = _clean_lines()
    runs = {"n1": _latest("results_n1_*.json"), "n2": _latest("results_n2_*.json")}
    hard, skipped = hard_cases(lines, runs)
    items = hard + controls(lines, runs, {h["stem"] for h in hard}, rng)
    first = [True] * (len(items) // 2) + [False] * (len(items) - len(items) // 2)
    rng.shuffle(first)
    for item, correct_first in zip(items, first):
        item["correct_option"] = "A" if correct_first else "B"
        a, b = (
            (item["correct"], item["wrong"])
            if correct_first
            else (item["wrong"], item["correct"])
        )
        item["prompt"] = PROMPT.format(a=a, b=b)
    return items, skipped, reading_accuracy(lines, runs, hard)


def _cache_path(image: bytes, prompt: str, model: str) -> Path:
    key = hashlib.sha256(image + prompt.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{key}_{model}_verify_{VERSION}.json"


def _call(client: Any, model: str, image: bytes, prompt: str) -> dict[str, Any]:
    import base64

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


def _answer(content: str) -> str | None:
    try:
        answer = json.loads(content).get("answer")
    except (json.JSONDecodeError, AttributeError):
        return None
    return answer if answer in ("A", "B") else None


def _rate(rows: list[dict[str, Any]]) -> str:
    ok = sum(r["right"] for r in rows)
    return f"{ok}/{len(rows)} ({100 * ok / len(rows):.0f}%)" if rows else "n/a"


def report(items: list[dict[str, Any]], reading: tuple[int, int]) -> list[str]:
    hard = [i for i in items if i["kind"] == "hard"]
    out = [
        f"hard cases (model misread in n1 or n2): {_rate(hard)}",
        f"controls (read right in n1 and n2):     {_rate([i for i in items if i['kind'] == 'control'])}",
        f"correct option shown first (A):          {_rate([i for i in items if i['correct_option'] == 'A'])}",
        f"correct option shown second (B):         {_rate([i for i in items if i['correct_option'] == 'B'])}",
        (
            f"answered A: {sum(i['answer'] == 'A' for i in items)}  answered B:"
            f" {sum(i['answer'] == 'B' for i in items)}"
            f"  invalid: {sum(i['answer'] is None for i in items)}"
        ),
    ]
    out.append(
        f"reading accuracy on the same hard numbers (n1+n2, every printed occurrence):"
        f" {reading[0]}/{reading[1]} ({100 * reading[0] / reading[1]:.0f}%)"
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--confirm-spend", action="store_true")
    args = parser.parse_args()
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not model:
        parser.error("OPENAI_MODEL is not set")
    archive = zipfile.ZipFile(zip_path())
    items, skipped, reading = build()
    images = {i["stem"]: archive.read(f"test/{i['stem']}.jpg") for i in items}
    uncached = [
        i
        for i in items
        if not _cache_path(images[i["stem"]], i["prompt"], model).exists()
    ]
    if len(uncached) > MAX_CALLS:
        parser.error(f"{len(uncached)} calls planned, limit {MAX_CALLS}")
    if args.dry_run:
        return dry_run(items, skipped, uncached, images, model, reading)
    return run(items, images, model, reading)


def dry_run(items, skipped, uncached, images, model, reading) -> int:
    kinds = Counter(i["kind"] for i in items)
    print(f"model={model} version={VERSION} seed={SEED}")
    print(
        f"questions: hard={kinds['hard']} controls={kinds['control']} total={len(items)}"
    )
    print(f"hard numbers excluded: {dict(skipped)}")
    print(
        f"correct option first: {sum(i['correct_option'] == 'A' for i in items)}"
        f"  second: {sum(i['correct_option'] == 'B' for i in items)}"
    )
    print(f"lines used: {len({i['stem'] for i in items})}")
    print(f"reading accuracy on the hard numbers (n1+n2): {reading[0]}/{reading[1]}")
    text = (
        max(len(i["prompt"]) for i in items) // 2 + 20
    )  # Arabic-Indic digits tokenise densely
    est = up = 0
    for i in uncached:
        e, u = image_tokens(*Image.open(io.BytesIO(images[i["stem"]])).size)
        est, up = est + e + text, up + u + text
    out_tokens = len(uncached) * MAX_OUTPUT_TOKENS
    print(f"planned_api_calls={len(uncached)} (limit {MAX_CALLS})")
    print(
        f"input tokens: estimate {est}, upper bound {up}; output at most {out_tokens}"
    )
    print(f"cost: estimate {_cost(est, out_tokens)}")
    print("\nquestions:")
    for n, i in enumerate(items):
        print(
            f"  {n:02d} {i['kind']:<7} correct={i['correct']:<10} wrong={i['wrong']:<10}"
            f" shown_first={'correct' if i['correct_option'] == 'A' else 'wrong'}"
            f" {i.get('substitution') or 'misread by ' + '+'.join(i['readers'])}"
        )
    print("\ndry run: no API call made")
    return 0


def run(items, images, model, reading) -> int:
    client = None
    live = spent_in = spent_out = 0
    for i in items:
        path = _cache_path(images[i["stem"]], i["prompt"], model)
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
        else:
            if client is None:
                from openai import OpenAI

                client = OpenAI()
            record = _call(client, model, images[i["stem"]], i["prompt"])
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            live, spent_in, spent_out = (
                live + 1,
                spent_in + record["prompt_tokens"],
                spent_out + record["completion_tokens"],
            )
        i["answer"] = _answer(record["content"])
        i["right"] = i["answer"] == i["correct_option"]
    stamp = datetime.now(UTC)
    out = HERE / f"verify_{VERSION}_{stamp:%Y%m%dT%H%M%SZ}.json"
    out.write_text(
        json.dumps(
            {
                "timestamp_utc": stamp.isoformat(),
                "model": model,
                "version": VERSION,
                "seed": SEED,
                "note": "The correct option comes from CORU. In real use it would come from invoice"
                " arithmetic; this tests only the confirming step in isolation. Both options"
                " put the decimal separator where it is printed, so a wrong option may carry a"
                " separator the model's original reading did not.",
                "reading_accuracy_hard_numbers": {
                    "correct": reading[0],
                    "total": reading[1],
                },
                "items": items,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print("\n".join(report(items, reading)))
    print(
        f"live_calls={live} prompt_tokens={spent_in} completion_tokens={spent_out}"
        f" spent={_cost(spent_in, spent_out)}"
    )
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
