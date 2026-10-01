"""
Does Tesseract make different mistakes from gpt-4o on Arabic-Indic numbers?

    python tesseract_numbers.py [--scale N]

Runs the tesseract CLI (Arabic model, --psm 7: one text line) on the 29 clean
Arabic-Indic lines, read in memory from CORU's OCR test.zip, and compares its
numbers with gpt-4o's cached n1 answers. No API call. Scores are digits_only
against the CORU transcription, as in read_numbers.py.
"""

import argparse
import glob
import io
import json
import re
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image
from read_numbers import (
    ARABIC_INDIC,
    HERE,
    REVIEW_FLAGS,
    SELECTION,
    SEPARATORS,
    zip_path,
)

LANG = "ara"
PSM = "7"
# Tesseract may emit the Arabic decimal and thousands separators as well.
NUMBER = re.compile(r"[0-9٠-٩۰-۹]+(?:[.,٫٬][0-9٠-٩۰-۹]+)*")


def tesseract(image: bytes) -> str:
    try:
        done = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", LANG, "--psm", PSM],
            input=image,
            capture_output=True,
            check=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "tesseract is not installed (apt: tesseract-ocr tesseract-ocr-ara)"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"tesseract failed ({exc.returncode}): {exc.stderr.decode(errors='replace')}"
        ) from exc
    return done.stdout.decode("utf-8").strip()


def prepare(image: bytes, scale: int) -> bytes:
    """Tesseract does poorly on small text; the crops are ~40px tall."""
    if scale == 1:
        return image
    im = Image.open(io.BytesIO(image)).convert("L")
    im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def digits(numbers: list[str]) -> Counter[str]:
    return Counter(n.translate(ARABIC_INDIC).translate(SEPARATORS) for n in numbers)


def classify(rows: list[dict[str, Any]]) -> list[str]:
    n = len(rows)
    both = [r for r in rows if r["tess_right"] and r["gpt_right"]]
    neither = [r for r in rows if not r["tess_right"] and not r["gpt_right"]]
    agree = [r for r in rows if r["agree"]]
    disagree = [r for r in rows if not r["agree"]]
    same_mistake = [r for r in neither if r["agree"]]
    gpt_wrong = [r for r in rows if not r["gpt_right"]]
    pct = lambda a, b: f"{a}/{b} ({100 * a / b:.0f}%)" if b else f"{a}/0"
    return [
        f"tesseract digits_only:   {pct(sum(r['tess_right'] for r in rows), n)}",
        f"gpt-4o n1 digits_only:   {pct(sum(r['gpt_right'] for r in rows), n)}",
        (
            f"both right: {len(both)}  both wrong: {len(neither)}"
            f"  only tesseract right: {sum(r['tess_right'] and not r['gpt_right'] for r in rows)}"
            f"  only gpt-4o right: {sum(r['gpt_right'] and not r['tess_right'] for r in rows)}"
        ),
        f"both wrong with the SAME answer: {pct(len(same_mistake), len(neither))}",
        f"readers agree: {len(agree)} lines; agreed answer correct: {pct(sum(r['gpt_right'] for r in agree), len(agree))}",
        f"readers disagree: {len(disagree)} lines; gpt-4o wrong on {pct(sum(not r['gpt_right'] for r in disagree), len(disagree))}",
        f"gpt-4o errors flagged by disagreement: {pct(sum(not r['agree'] for r in gpt_wrong), len(gpt_wrong))}",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scale", type=int, default=1, help="upscale factor before OCR"
    )
    scale = parser.parse_args().scale
    archive = zipfile.ZipFile(zip_path())
    flagged = json.loads(REVIEW_FLAGS.read_text(encoding="utf-8"))["flags"]
    lines = [
        l
        for l in json.loads(SELECTION.read_text(encoding="utf-8"))
        if l["group"] == "arabic_indic" and l["stem"] not in flagged
    ]
    n1 = {
        r["stem"]: r
        for r in json.loads(
            Path(max(glob.glob(str(HERE / "results_n1_*.json")))).read_text(
                encoding="utf-8"
            )
        )["results"]
    }
    rows = []
    for line in lines:
        text = tesseract(prepare(archive.read(f"test/{line['stem']}.jpg"), scale))
        tess = NUMBER.findall(text)
        gpt = n1[line["stem"]]["got"] or []
        expected = digits(line["expected"])
        rows.append(
            {
                "stem": line["stem"],
                "transcription": line["transcription"],
                "tesseract_text": text,
                "expected": line["expected"],
                "tesseract": tess,
                "gpt": gpt,
                "tess_right": digits(tess) == expected,
                "gpt_right": digits(gpt) == expected,
                "agree": digits(tess) == digits(gpt),
            }
        )
    version = subprocess.run(
        ["tesseract", "--version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    stamp = datetime.now(UTC)
    out = HERE / f"tess_x{scale}_{stamp:%Y%m%dT%H%M%SZ}.json"
    out.write_text(
        json.dumps(
            {
                "timestamp_utc": stamp.isoformat(),
                "tesseract": version,
                "lang": LANG,
                "psm": PSM,
                "scale": scale,
                "rows": rows,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(version)
    print("\n".join(classify(rows)))
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
