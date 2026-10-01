# Arabic/English Invoice Reader

Reads an image of a Saudi tax invoice into structured fields with gpt-4o, checks
the numbers against each other and against ZATCA field rules, and sends invoices
whose arithmetic fails to a person for review; it never corrects a value itself.

## The finding

On real receipt photos, gpt-4o read Western digits far more reliably than
Arabic-Indic digits (٠١٢٣٤٥٦٧٨٩):

| Digits on the line | Lines read correctly |
|---|---|
| Western | 34 of 36 (94.4%) |
| Arabic-Indic | 9 of 29 (31.0%) |

Test set: single-line crops of real receipts from the CORU dataset
(`abdoelsayed/CORU`, OCR test split, MIT licence), checked by eye to remove
handwritten, blurred or badly cut crops. "Correct" means every digit on the line
matches CORU's transcription, ignoring decimal separators. The Arabic-Indic set
is small (29 lines, 6 of them from one receipt), so treat this as a direction,
not a precise rate.

The misreads repeat: ٣ read as ٢, ٥ as ٠, ٨٤ as ٤٨, a digit added or dropped.

Every figure in this section and the next is recomputed from the evidence files
by `python eval/coru/summarise.py`, which needs no download and no API key. Data,
attribution and method: [`eval/coru/README.md`](eval/coru/README.md).

## What I tried that didn't fix it

All on the same 29 Arabic-Indic lines, except the first.

- **Reading again** (synthetic-invoice result). At temperature 0 the model gives
  the same wrong answer each time: on a generated invoice (INV-2026-1002) read
  three times, 3 of 15 line totals were correct, and values such as
  ٦٢٠٫٦٤ → 606.4 were identical in all three runs
  ([`eval/README.md`](eval/README.md#multi-digit-decimal-amounts-in-arabic-indic-numerals-are-misread)).
- **Copy the digits, convert in code.** Asking the model to copy Arabic-Indic
  digits as printed and converting them in Python: 11 of 29 lines (37.9%)
  against 9 of 29 (31.0%), but the same 30 of 57 numbers (52.6%) correct either
  way. No real improvement.
- **Two-choice verification.** Showing the crop with the right value and the
  model's own wrong reading, asking which is printed: right 10 of 32 times
  (31.2%). Better than its reading of those same numbers (6 of 56, 10.7%) but
  worse than a coin flip (50%). It confirms its own misreading. On numbers it
  had read correctly it was right 10 of 10 times, and it showed no preference
  for the first option (right 10 of 21 times in each position).
- **A second reader (Tesseract 5.3.4, Arabic model).** 0 of 29 lines correct;
  6 of 57 numbers as-is, 4 of 57 and 3 of 57 with the crop enlarged 3× and 4×.
  It never agreed with gpt-4o, so disagreement flagged every line, right or
  wrong.

## What I built instead

Arithmetic is the one reliable signal: on this project's synthetic invoices,
every run with a wrong amount also failed an arithmetic check
([`eval/results.md`](eval/results.md)).

- **Resolver** (`app/resolve.py`, plain Python, no model call). When an
  invoice's numbers don't add up, it works out which single cell would explain
  the failed checks, lists the values that cell would need, and ranks them by
  how they differ from what the model read, using the confusions above. If no
  single cell explains the failures, or two candidates are equally likely, it
  says so and suggests nothing.
- **Review queue and page** (`app/review.py`, "Review queue" tab). Every
  invoice whose arithmetic fails gets a short reference such as `R-0042` to
  write on the paper invoice. A person accepts a candidate, rejects them all,
  or marks an unresolvable invoice as checked manually. Nothing is ever
  written back to the invoice: accepting only records the decision.

```
POST /extract (raw PNG/JPEG body, ≤10 MB, in memory only)
  → app/extract.py   SHA-256 → .cache/ lookup → OpenAI vision call (temperature 0)
                     → digit/separator/currency normalisation → Invoice (Decimal money)
  → app/validate.py  arithmetic rules (error) + ZATCA structural rules (warning)
  → app/store.py     one append-only audit row: hash, counts, rule names, tokens, latency
  → if an arithmetic rule failed:
      app/resolve.py which single cell explains the failed checks, and ranked candidates
      app/review.py  review_queue row (amounts and field paths only) + reference R-0042
      app/store.py   append-only resolver event (no amounts)
  → JSON response (200 for both "ok" and "needs_review"), with `checks` (every arithmetic
    check: pass, fail or not checked, and why) and `review` (null, queued with its
    reference, or error); the page draws these as the pipeline strip and check map

GET  /reviews                       pending reviews
POST /reviews/{id}/decision         accepted (with rank) | rejected | checked_manually
```

## How it behaves on real model output

Run on gpt-4o's cached answers for the 6 synthetic invoices printed in
Arabic-Indic digits ([`eval/results.md`](eval/results.md#resolver-on-real-model-output),
with the command that reproduces it):

| Invoice | Arithmetic | Misread numeric cells | Of those, in failed checks | Resolver |
|---|---|---|---|---|
| INV-2026-1002 | failed | 13 | 11 | unresolvable |
| INV-2026-1003 | failed | 8 | 5 | unresolvable |
| INV-2026-1010 | passed | 0 | – | not needed |
| INV-2026-1011 | failed | 5 | 4 | unresolvable |
| INV-2026-1012 | failed | 6 | 5 | unresolvable |
| INV-2026-1020 | failed | 10 | 8 | unresolvable |

Every invoice with a misread failed arithmetic and went to review. With 5 to
13 misread cells each, the arithmetic cannot pin down the values, and the
resolver said so each time. **It made no wrong suggestions and no successful
ones.** A reviewer gets the reference, the failed checks and the values in them,
and is told to check every number on the invoice against the paper.

The resolver's suggestions work only when a single cell is misread; that is
proven by tests, not yet by real data.

## Tests

249 tests, all passing, all offline (no API calls). They include:

- nine invoices with one known confusion injected (٣→٢, ٨٤→٤٨, a dropped
  digit, …), one per kind of cell, each checked for the right cell and the
  right value ranked first;
- invoices the resolver must refuse: two misread cells, a cell whose checks
  can't all be satisfied, a missing line-item table;
- the review endpoints: decisions, the rule that an unresolvable invoice can
  only be marked checked manually, repeat uploads, and that no names or VAT
  numbers reach the queue.

To check the tests themselves, I broke the resolver four ways: ranking by whole
numbers before confusion type, removing ambiguity detection, allowing only
whole-number quantities, and ignoring the confusion table. Each break made 1 or
2 resolver tests fail. (This was a one-off check, not part of the suite.)

## Limitations

- **Arithmetic catches the invoice, not every wrong cell.** Some misreads sit
  outside the failed checks because the wrong values still add up. On the five
  failing invoices above, 9 of 42 misread values were outside every failed
  check (on INV-2026-1002, 2 of 13), so they are not among the values a review
  card lists. The card says so.
- **Small test sets.** 29 Arabic-Indic lines, 32 verification questions, and
  6 Arabic-Indic synthetic invoices. The confusion table behind the ranking
  comes from those same 29 lines.
- **Synthetic invoices.** The end-to-end evaluation uses 30 generated Saudi
  invoices, not scans. The CORU lines are real but Egyptian retail receipts
  (EGP, 14% VAT), not ZATCA invoices, and CORU's transcriptions were used as
  the answer key; I compared 7 of them with the image digit by digit, and all 7
  were right.
- **One model.** Everything was measured on gpt-4o, mostly with single runs at
  temperature 0.
- **No login.** The review page and `GET /reviews` show pending amounts to
  anyone who can reach the server. The queue never holds names, VAT numbers,
  descriptions or images, and a row is deleted when decided, but run this only
  where that exposure is acceptable.
- **Not a ZATCA e-invoicing solution.** It reads and checks invoices. It does
  not generate or sign XML, produce QR codes, or submit anything to Fatoora.
- **`.cache/` keeps every model response verbatim**, including names and VAT
  numbers, so the same image is never paid for twice. See
  [`docs/data-flow.md`](docs/data-flow.md).
- **Postgres is not supported.** SQLite at `data/app.db` is the store.

Also measured on the synthetic set, and not repeated here: per-field accuracy
([`eval/results.md`](eval/results.md#per-field-accuracy-and-agreement-90-runs)),
and the finding that the model's confidence scores do not predict its errors
([`eval/README.md`](eval/README.md#the-models-confidence-scores-do-not-predict-its-errors)).

## How to run it

Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env              # set OPENAI_API_KEY and OPENAI_MODEL
.venv/bin/python -m pytest        # 249 passed here, with the sample images present
```

The app does not read `.env` itself:

```bash
set -a; . ./.env; set +a
.venv/bin/uvicorn app.main:app --port 8000
# open http://127.0.0.1:8000/ : "Extract" and "Review queue" tabs
```

The synthetic evaluation set, how to regenerate it and how to re-run the
evaluation: [`eval/README.md`](eval/README.md) and
[`eval/results.md`](eval/results.md). The CORU experiments:
[`eval/coru/README.md`](eval/coru/README.md).
