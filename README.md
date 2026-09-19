# Arabic/English Invoice Reader

Takes an image of a Saudi tax invoice, returns validated structured JSON aligned
to ZATCA fields, flags fields that fail arithmetic checks for human review, and
logs every extraction for audit without storing what the invoice said.

## Results

30 generated invoices, one model (`gpt-4o`), three repeats each at
`temperature=0`, 90 API calls, **0 unparseable responses**. Exact match against
ground truth. Full tables, method and per-cause breakdown: [`eval/results.md`](eval/results.md).

| Field | Accuracy (90 runs) | Latin numerals (24 invoices, 72 runs) | Arabic-Indic numerals (6 invoices, 18 runs) |
|---|---|---|---|
| invoice_number | 93.3% | 100% | 66.7% |
| invoice_date / invoice_timestamp | 93.3% | 100% | 66.7% |
| invoice_type | 100% | 100% | 100% |
| seller_name | 98.9% | 98.6% | 100% |
| seller_vat_number | 93.3% | 100% | 66.7% |
| buyer_name | 96.7% | 95.8% | 100% |
| buyer_vat_number | 100% | 100% | 100% |
| subtotal / vat_total / total | 91.1% | 100% | 55.6% |
| line item description | 84.9% | 88.7% | 70.0% |
| line item quantity | 100% | 100% | 100% |
| line item unit_price | 89.0% | 100% | 46.7% |
| line item line_total | 88.0% | 100% | 41.7% |
| line item vat_rate | 100% | 100% | 100% |
| line item vat_amount | 89.7% | 100% | 50.0% |

- **Every remaining numeric error falls on the six Arabic-Indic invoices.** On
  the 24 Latin-numeral invoices, every monetary value was correct in all 72 runs.
- **Seeded ZATCA defects caught: 12/12** (4 defects × 3 runs), each by the rule
  that names it and no other.
- 51 of 90 runs were exact on every field; 16 of 30 invoices were exact in all
  three runs. Agreement across repeats was 96–100% on every field except the
  totals (90%) and descriptions (94.8%).
- Every run containing a wrong monetary value (15 of 90) carried an
  `error`-severity arithmetic finding.

**Scope.** These numbers come from 30 synthetic invoices produced by
`eval/generate_invoices.py`, one model, three repeats, on 2026-09-19. Nothing
here has been validated against real-world scanned receipts: no photographs, no
thermal paper, no handwriting, no real layouts. Running the PII-redacted
CORU receipt set (`abdoelsayed/CORU` on Hugging Face, see `eval/README.md`) would add
the thing this evaluation cannot: real retail layouts and real scan noise, at a
scale where the Arabic-Indic and description findings below could be confirmed
or overturned. That has not been done.

## Architecture

```
POST /extract (raw PNG/JPEG body, ≤10 MB, in memory only)
  → app/extract.py   SHA-256 → .cache/ lookup → OpenAI vision call (temperature 0)
                     → digit/separator/currency normalisation → Invoice (Decimal money)
  → app/validate.py  arithmetic rules (error) + ZATCA structural rules (warning)
                     → ExtractionResult{invoice, findings, status}
  → app/store.py     one append-only audit row: hash, counts, rule names, tokens, latency
  → JSON response (200 for both "ok" and "needs_review")
```

`static/index.html` is a plain upload page: fields in a table, cells named in an
arithmetic finding highlighted and editable, findings listed, Arabic rendered
right-to-left. `GET /audit` returns the last 50 audit rows; `GET /health` reports
whether a model is configured.

## What it does not do

- **Not a ZATCA-certified e-invoicing solution.** It does not generate or sign
  XML, does not produce QR codes, and does not submit anything to the Fatoora
  platform. It reads invoices and checks them against a subset of the rules.
- **No deletion endpoint.** Stored data is removed by hand; see below.
- **Postgres is not supported.** A `DATABASE_URL` branch exists in `app/store.py`
  but has never been executed and its driver is deliberately not a dependency.
  SQLite at `data/app.db` is the store.
- **`.cache/` holds invoice content.** Every model response is cached verbatim by
  image hash — names, VAT numbers, amounts — indefinitely, so the same image is
  never paid for twice. The audit log holds none of that; the cache holds all of
  it. [`docs/data-flow.md`](docs/data-flow.md) says exactly what is sent, stored,
  and how to delete it, and does not claim PDPL compliance.
- **No confidence gating.** Scores are requested and recorded but nothing acts on
  them. See Findings.

## Setup

Python 3.11+. Verified from a clean clone.

```bash
git clone <this repo> && cd <this repo>
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # set OPENAI_API_KEY and OPENAI_MODEL
.venv/bin/python -m pytest  # 143 passed, 1 skipped (needs the sample images)
```

The app does not read `.env` itself. Export it, then start the server:

```bash
set -a; . ./.env; set +a
.venv/bin/uvicorn app.main:app --port 8000
# open http://127.0.0.1:8000/
```

The 30 sample images are not committed. To regenerate them byte-for-byte
(the set is seeded; this was checked against the evaluated images):

```bash
.venv/bin/pip install -r requirements-eval.txt   # generator only
cd eval && ../.venv/bin/python generate_invoices.py && mv out/samples samples && rm -r out && cd ..
.venv/bin/python -m eval.load_data       # prints the dataset summary
```

Pillow must be built with libraqm or Arabic renders unjoined; `eval/README.md`
has the check. Then:

```bash
.venv/bin/python -m eval.run_eval                       # from cache, no API calls
.venv/bin/python -m eval.run_eval --repeats 3 --confirm-spend --no-cache   # 90 calls
```

Cost is printed only if `OPENAI_PRICE_INPUT_PER_1M_USD` and
`OPENAI_PRICE_OUTPUT_PER_1M_USD` are set; prices are never hardcoded.

## Findings

Measured on this set and this model; see the scope note above. Details and
evidence in [`eval/README.md`](eval/README.md#findings).

**1. Multi-digit Arabic-Indic numbers are read unreliably; single digits, dates
in Latin digits, and Latin-numeral amounts are fine.** Line totals on the six
Arabic-Indic invoices were 41.7% correct; on the two that are Arabic-only, the
invoice number, date and 15-digit VAT number were wrong in 6 of 6 runs
(`٢٠٢٦`→2023, `١٠٠٣`→1002, digits dropped). Single-digit quantities were 100%.
The rendering was made progressively clearer (Amiri → Noto Naskh, ASCII period →
`٫`, 19px → 22px) and accuracy did not improve; at `temperature=0` the wrong
values are the same wrong values every run. Bilingual invoices hide the problem
on any field that is also printed in Latin digits.

**2. The model's confidence scores are inversely calibrated, so the gate was
removed.** Mean confidence was 0.969 on the 2,609 correct fields and 1.000 on
the 193 incorrect ones. 193 of 193 errors sat above the 0.80 threshold; all 81
sub-threshold scores were on correct fields (mostly `0.0` on correctly-null
fields). Nothing in the app acts on confidence now; `needs_review` comes from
findings alone. Arithmetic validation is what catches misreads: 15 of 15 runs
with a wrong amount carried an `error` finding. The scores are still requested
and recorded so the measurement can be repeated (`run_eval.py` prints it).

**3. One error class reaches the output with no flag at all.** INV-2026-1017 is a
simplified invoice with no buyer; in all three runs the model returned the
seller's city as `buyer_name` at confidence 1.0. Arithmetic cannot see a name,
confidence was 1.0, and no structural rule applies because the field is
legitimately absent. The same is true of the description substitutions
(`كيلو`→`جاز`, `سندويتش`→`سندوتش`, 36 of 44 misses identical across all three
runs). Validation by consistency cannot catch a plausible value invented for a
field with nothing to check it against. "No findings" means internally
consistent, not verified.

## What broke and how I fixed it

### A confounded control (methodology error, not a code bug)

After a single-invoice test (INV-2026-1002, three repeated runs under three
renderings) I wrote in `eval/README.md` that gpt-4o's Arabic-Indic failures were
"specific to multi-digit decimal amounts", because on that page the invoice
number, date, time and 15-digit VAT numbers were read correctly while the table
amounts were not. That looked like an internal control ruling out the script and
the font.

It was confounded. INV-2026-1002 is bilingual: its English block prints the
invoice number, date and VAT numbers again in Latin digits, so the model had a
Latin copy of every header field and no Latin copy of any amount. The full
evaluation (`eval/results.md`) exposed it: on the two Arabic-only, Arabic-Indic
invoices the invoice number, date and seller VAT were wrong in 6 of 6 runs.

The finding was rewritten to what the data supports — multi-digit Arabic-Indic
numbers are read unreliably, single digits are fine, and a bilingual layout masks
the problem on any field that is also printed in Latin — and the README now names
the confound. The lesson is procedural: a control has to vary only the thing
being tested, and one sample cannot establish that it did.

### The timestamp was never on the page

Ground truth carried `invoice_timestamp` but the generator only wrote it into the
QR payload, never as text. The model correctly returned `null` three times out of
three and I initially reported that as an extraction failure. The generator now
prints a time line; ground truth was unchanged, images changed.

### Invented timezones

With the time printed, the model appended `+03:00` or `Z` — neither is on the
page, and the generator's own `Z` was equally invented. Timestamps are now naive
throughout; `extract.py` strips any suffix the model adds.

### The decimal separator was an ASCII period

`num()` transliterated digits to Arabic-Indic but left the `.`, which Amiri draws
as the same low dot as `٠`. `٦١٠.٨٤` was unreadable as 610.84 by anyone. Fixed to
`٫` and Noto Naskh for numeric cells. This removed the separator/zero
transposition and exposed the digit-deletion failure underneath it as a model
limitation rather than a rendering artefact.

### A mislabelled seeded defect

The generator seeded `missing_buyer_vat` on index 14 without checking the
invoice was standard; it rolled simplified, where a missing buyer VAT is correct.
The label was corrected and the generator now only applies that defect to
standard invoices. Honest defect count: 4, not 5.

### Currency tokens in money strings

3 of 90 responses failed to parse because totals came back as `"SAR 11897.50"`
and `"SAR 5 456.34"`, zeroing ~30 fields each and distorting every accuracy
figure. The normaliser now strips currency tokens and whitespace from money
fields — and only money fields, because applying it to timestamps as first
specified would have corrupted them.

### Two places the literal spec was wrong

Section 8 said "JSON array of findings" while section 9 forbade content in the
audit log; `Finding.message` quotes VAT numbers and amounts. Only
`{rule, severity, fields}` is stored. And the money-normalisation instruction
named a field set that included the timestamp. Both are recorded in
`CLAUDE.md` §14 as cases where the narrower reading was the right one.
