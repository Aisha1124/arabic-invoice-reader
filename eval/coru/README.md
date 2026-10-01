# CORU numeral-reading evidence

The evidence behind the project README's finding that gpt-4o reads Arabic-Indic
digits far less reliably than Western digits, and behind what was tried to fix it.

## Source data

Line crops and transcriptions come from **CORU** (published on Hugging Face as
[`abdoelsayed/CORU`](https://huggingface.co/datasets/abdoelsayed/CORU); the dataset
card calls it ReceiptSense), `OCR/test.zip`:

> Abdelrahman Abdallah, Mahmoud Abdalla, Mahmoud SalahEldin Kasem, Mohamed Mahmoud,
> Ibrahim Abdelhalim, Mohamed Elkasaby, Yasser Elbendary, Adam Jatowt.
> *ReceiptSense: Beyond Traditional OCR - A Dataset for Receipt Understanding.* 2024.
> [arXiv:2406.04493](https://arxiv.org/abs/2406.04493)

CORU is released under the **MIT licence**, as stated on its Hugging Face page. This
directory holds no CORU images and no transcription text: only each line's id (the
CORU file name without extension), the numbers on the line in Western digits, the
readers' answers and the scores.

The receipts are Egyptian retail receipts (prices in EGP, 14% VAT), not Saudi tax
invoices.

## Files

| File | What it holds |
|---|---|
| `review_flags.json` | The 25 crops excluded after looking at every image, with the reason category |
| `reading_n1.json` | All 90 lines: group, exclusion, expected numbers, gpt-4o's answer, digits_only score |
| `copy_then_convert_n2.json` | The 29 clean Arabic-Indic lines: answers and scores for both prompt versions |
| `verification.json` | The 42 two-choice questions: options, which was correct, the answer, and the reading baseline |
| `tesseract.json` | Tesseract's numbers and scores on the 29 lines, at three crop sizes |
| `summarise.py` | Recomputes every figure in the project README from the files above |
| `read_numbers.py`, `verify_numbers.py`, `tesseract_numbers.py` | The scripts that produced them |

Check the figures, with no download and no API key:

```bash
.venv/bin/python eval/coru/summarise.py
```

## Re-running the experiments

This makes paid API calls and needs CORU's `OCR/test.zip` (38.7 MB), Pillow
(`requirements-eval.txt`), and for the Tesseract comparison `tesseract-ocr` with
the Arabic model (`tesseract-ocr-ara`).

```bash
export CORU_OCR_ZIP=/path/to/test.zip OPENAI_MODEL=gpt-4o OPENAI_API_KEY=...
cd eval/coru
python read_numbers.py select                        # rebuilds the 90-line selection, no network
python read_numbers.py run --dry-run                 # counts calls and tokens first
python read_numbers.py run --confirm-spend           # 90 calls
python read_numbers.py run --prompt-version n2 --subset clean-arabic-indic --confirm-spend   # 29 calls
python verify_numbers.py --dry-run                   # then --confirm-spend: 42 calls
python tesseract_numbers.py                          # local, no API
```

The run files these write (`selection.json`, `results_*.json`, `verify_v1_*.json`,
`tess_x*.json`, `.cache/`) contain transcription text or model responses and are
git-ignored. Model output varies between runs, so a re-run will not reproduce every
answer exactly.

## Method notes

- The 45 Arabic-Indic lines were every line in the test split whose Arabic-Indic
  digits are a money amount, classified by hand from the transcription text. The 45
  Western lines were matched on kind (labelled total or item row) and zero-only
  count, at most two per receipt.
- Every crop was then checked by eye. Handwritten, blurred, faint or badly cut
  crops, and crops containing part of another receipt line, were excluded
  (`review_flags.json`), leaving 29 Arabic-Indic and 36 Western lines. No
  replacement Arabic-Indic lines existed in this split.
- The answer key is CORU's transcription. Seven Arabic-Indic transcriptions were
  compared with the image digit by digit and all seven were right; the rest were
  not checked that way.
- Scores ignore decimal separators (digits_only): the Arabic-Indic receipts print
  `٫`, which CORU transcribes as `.` or `,`.
