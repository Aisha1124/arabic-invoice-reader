# CLAUDE.md

Project constitution. Read this fully before writing any code. Re-read before each new task.

---

## 1. What this project is

An Arabic/English invoice extraction service. It takes an invoice image, returns validated structured JSON aligned to ZATCA tax-invoice fields, flags low-confidence fields for human review, and logs every extraction for audit.

This is a portfolio-grade production artifact, not a demo. The thing being proven is engineering discipline: measured accuracy, handled failure, auditable output.

**Not in scope.** This is an extraction and validation layer. It is NOT a certified ZATCA e-invoicing clearance solution, it does not submit to the Fatoora platform, and it must never claim to. Any README or UI text implying certification is a bug.

---

## 2. Hard rules

These are not suggestions. Violating any of them means the task is not done.

1. **Never invent a fact.** If you do not know a model name, an API signature, a field requirement, or a library behaviour, stop and say so. Do not guess and do not write plausible-looking placeholder values presented as real.
2. **No unnecessary code.** No abstraction layers for one implementation. No config systems for three settings. No "future-proofing." If a function has one caller and five lines, inline it.
3. **No dead code.** No commented-out blocks, no unused imports, no functions nothing calls, no `TODO` left behind without an open issue reference.
4. **Every error path is handled explicitly.** No bare `except:`. No silently swallowed exceptions. If something can fail, decide what happens and write it down.
5. **Run the code before claiming it works.** Execute it. Read the actual output. If a test exists, run it. "This should work" is not acceptable.
6. **Fix the cause, not the symptom.** Do not wrap a failing call in a retry to make an error go away. Find out why it failed.
7. **One task at a time.** Do not build ahead. Do not implement Phase 3 while asked for Phase 1.
8. **Ask before adding a dependency.** Every new package must be justified in one sentence.

---

## 3. Cost discipline

The only spend on this project is the OpenAI API key. Treat it as scarce.

- **Cache every model response by image SHA-256.** Cache directory: `.cache/`. Never call the API twice for the same image.
- Evaluation runs must read from cache by default. A flag `--no-cache` forces a fresh run.
- Never loop the API over a full dataset. The working set is 30 images, fixed.
- Log the token count and estimated cost of every call. Print a total at the end of any batch run.
- Before any batch operation that will exceed 50 API calls, stop and ask.

---

## 4. Stack

Fixed. Do not substitute.

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| API | FastAPI |
| Validation | Pydantic v2 |
| Model | OpenAI vision, model name from `OPENAI_MODEL` env var |
| Storage | SQLite locally (`data/app.db`); Postgres via `DATABASE_URL` if set |
| Tests | pytest |
| Formatting | ruff (format + lint) |
| Frontend | One plain HTML page. No React, no framework, no build step. Fonts are vendored in `static/fonts/`; no font CDN or other third-party request. |

**Model name:** never hardcode it. Read `OPENAI_MODEL` from environment. If unset, fail with a clear message telling the user to set it. Do not assume which vision models exist — the user sets this.

---

## 5. Repository layout

Create exactly this. Nothing else at the top level.

```
arabic-invoice-reader/
├── CLAUDE.md
├── README.md
├── .env.example
├── .gitignore
├── requirements.txt       # what the app runs
├── requirements-eval.txt  # generator only (pillow, arabic-reshaper, python-bidi, qrcode)
├── ruff.toml          # excludes vendored eval/generate_invoices.py and *.md
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI app, routes only
│   ├── schema.py        # Pydantic models
│   ├── extract.py       # model call + response parsing
│   ├── validate.py      # business rules, confidence gating
│   ├── store.py         # database + audit log
│   ├── cache.py         # SHA-256 response cache
│   ├── resolve.py       # arithmetic resolver: localise, suggest, rank (no model call)
│   └── review.py        # review queue: a person accepts or rejects each suggestion
├── static/
│   ├── index.html       # upload page
│   └── fonts/           # IBM Plex Sans Arabic woff2 (400, 500, 600) + LICENSE.txt (OFL 1.1), from @ibm/plex-sans-arabic 1.1.0
├── eval/
│   ├── samples/               # 30 PNG invoices (gitignored)
│   ├── ground_truth.json      # exact field values
│   ├── golden_set.csv         # numeric values verified by hand from the images
│   ├── runs/                  # timestamped golden-set run records (committed)
│   ├── README.md              # dataset documentation
│   ├── generate_invoices.py   # regenerates the set
│   ├── results.md             # full evaluation output and analysis
│   ├── fonts/                 # Arabic fonts for the generator
│   ├── load_data.py           # reads and validates the set
│   ├── run_eval.py            # accuracy measurement
│   └── coru/                  # CORU numeral-reading evidence: scores only, no images or text
├── tests/
│   ├── __init__.py      # makes the repo root importable under pytest
│   ├── fixtures/        # recorded model responses
│   ├── test_schema.py
│   ├── test_validate.py
│   ├── test_extract.py
│   ├── test_cache.py
│   ├── test_resolve.py
│   ├── test_review.py
│   ├── test_main.py     # HTTP endpoints, via FastAPI's TestClient (httpx)
│   ├── test_store.py
│   ├── test_load_data.py
│   └── test_run_eval.py
└── docs/
    └── data-flow.md     # PDPL data-flow note
```

---

## 6. The data schema

This is the contract. Everything else serves it.

```python
class LineItem(BaseModel):
    description: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal
    vat_rate: Decimal          # e.g. 0.15
    vat_amount: Decimal

class Invoice(BaseModel):
    invoice_number: str | None
    invoice_date: date | None
    invoice_timestamp: datetime | None   # ZATCA TLV tag 3; naive ISO 8601, as printed
    invoice_type: Literal["standard", "simplified", "unknown"]
    seller_name: str | None
    seller_vat_number: str | None    # 15-digit KSA TIN when present
    buyer_name: str | None
    buyer_vat_number: str | None
    line_items: list[LineItem]
    subtotal: Decimal | None          # total excluding VAT
    vat_total: Decimal | None
    total: Decimal | None             # total including VAT
    currency: str                      # default "SAR"
```

**All monetary values use `Decimal`, never `float`.** Float arithmetic on money is a correctness bug.

Every field carries a confidence score, held in a parallel structure:

```python
class FieldConfidence(BaseModel):
    field: str
    confidence: float          # 0.0 to 1.0
    needs_review: bool
```

---

## 7. Validation rules

`validate.py` implements these. Each returns a named, human-readable finding — never a bare boolean.

**Arithmetic**
- Every line: `quantity * unit_price` equals `line_total` within 0.01
- Every line: `line_total * vat_rate` equals `vat_amount` within 0.01
- `sum(line_totals)` equals `subtotal` within 0.01
- `sum(vat_amounts)` equals `vat_total` within 0.01
- `subtotal + vat_total` equals `total` within 0.01
- If `line_items` is empty but `subtotal`, `vat_total` or `total` is non-zero, flag it. The line-item table was missed; this is an extraction failure. The three sum checks above are skipped in that case.
- If every line has `vat_amount == 0` and `vat_total > 0`, the invoice is lumped-VAT. That is a document defect, not a misread, so the two per-line VAT checks (`line_total * vat_rate` and `sum(vat_amounts)`) are skipped and `vat_is_itemised_per_line` carries the finding as a warning.
- If both `invoice_date` and `invoice_timestamp` are present, the timestamp's date must equal `invoice_date`. Both come from the same document, so a mismatch means one was misread. Timestamps are naive wall-clock values: the page shows no zone, so `extract.py` strips any suffix the model appends and the comparison is naive to naive.

**ZATCA structural**
- Seller VAT number must be present on every invoice type; ZATCA requires it on standard and simplified invoices alike. Missing → `seller_vat_number_present`, warning.
- Seller VAT number, when present, is exactly 15 digits. Separate rule from presence.
- If `invoice_type == "standard"`, a missing `buyer_vat_number` is flagged. This is the most common real-world clearance rejection.
- If every line shares one lumped VAT figure rather than per-line VAT, flag it. This is the second most common rejection.
- Simplified invoices: seller name, timestamp, total, and VAT amount must all be present. With the seller VAT number above, these are the five TLV QR fields.

**Confidence scores**
- The prompt asks for a per-field confidence score and `FieldConfidence` records it. Nothing gates on it. The threshold gate was removed after being measured as inversely calibrated on this project's eval set: over 102 scored fields on three samples, mean confidence was 0.977 on correct fields and 1.000 on incorrect ones, all 15 wrong values scored 1.0, and the only sub-threshold scores were on two correctly-null fields (false alarms). See `eval/README.md` "Findings". Arithmetic validation caught every one of those misreads.
- The scores stay in the output and `eval/run_eval.py` keeps the calibration metric, so the finding remains reproducible. `CONFIDENCE_THRESHOLD` (default 0.80, from env) is used only by that report.
- Any failed arithmetic check forces `needs_review = true` on the fields involved
- Any finding of either severity sets `"status": "needs_review"`. Severity says whether we read the invoice wrong; status says whether a human should look. A warning means the invoice itself is non-compliant and will be rejected at clearance, so the answer is yes for both.
- An invoice with `"status": "needs_review"` returns HTTP 200 — it is not an error, it is a queue

**Severity mapping**

Every finding carries `severity: "error" | "warning"`.

- `"error"`: any arithmetic check that fails. The numbers do not add up, so the extraction is wrong. Forces `needs_review = true` on the fields involved.
- `"warning"`: ZATCA structural checks. The extraction may be correct and the invoice itself is non-compliant. Flagged for the reviewer, but does not by itself mean the extraction failed.

These are different failures. An arithmetic error means we read the invoice wrong. A structural finding means we read it right and the invoice has a compliance problem. Conflating them would make the eval numbers meaningless — we could not tell extraction failures from real ZATCA defects in the source documents.

---

## 8. Audit log

Every extraction writes one immutable row. Append-only. No updates, no deletes.

| Column | Meaning |
|---|---|
| `id` | UUID |
| `timestamp_utc` | when |
| `image_sha256` | which file, without storing the file |
| `model` | which model version produced this |
| `prompt_version` | which prompt version produced this |
| `fields_extracted` | count |
| `fields_flagged` | count |
| `validation_findings` | JSON array of `{rule, severity, fields}` — never `message` |
| `latency_ms` | duration |
| `estimated_cost_usd` | spend |

**Resolver log.** `store.py` also holds `resolver_events`, append-only like the audit log: one row per resolver outcome (`suggested`, `ambiguous`, `unresolvable`) and per review decision (`accepted`, `rejected`, `checked_manually`), with the audit row id, the review reference (`R-0042`), failed check names, the top or accepted field path, its confusion type and rank, and the candidate count. Never amounts. The amounts a reviewer needs live in `review.py`'s `review_queue`, which is the only table rows are ever removed from (see section 9).

**Never write invoice content, names, VAT numbers, or images into the audit log.** The log records that an extraction happened and how it went, not what was in it. This is the PDPL-safe design and it is the point.

`Finding.message` is excluded from `validation_findings` because it quotes content: the seller-VAT format rule prints the received number, the arithmetic rules print the amounts. `rule`, `severity` and `fields` name positions in the schema, not values, so they are safe to keep.

---

## 9. Privacy rules

- Uploaded images are processed in memory and discarded. Never written to disk outside `.cache/` (hash-keyed, gitignored).
- `review_queue` (in `data/app.db`) stores amounts and field paths for a pending review: the values read in the failed checks and the candidate values, plus a short reference (`R-0042`) the user writes on the paper invoice, and the outcome of each arithmetic check (`{rule, line, outcome, reason}`: positions and pass/fail/not-checked, no amounts) for the review detail's issues list. Never names, VAT numbers, descriptions or images. `GET /reviews` returns these amounts and the app has no authentication: anyone who can reach the server can read the queue. The row is removed in the same transaction that logs the decision, so amounts leave the database once a person has decided. Undecided rows stay until then.
- `.cache/` and `data/` are in `.gitignore`. No sample invoice with real data ever enters git.
- Use only the PII-redacted variants of public datasets.
- `docs/data-flow.md` states plainly: what is sent to OpenAI, what is stored, what is not stored, where it is hosted, and how deletion works. Write what the code actually does. If the code does not implement deletion, the document says so.

---

## 10. Testing

- `tests/test_validate.py` covers every rule in section 7, with a passing case and a failing case each. These need no API key.
- `tests/test_schema.py` covers Decimal parsing, missing optional fields, and malformed input.
- `tests/test_extract.py` uses a recorded fixture response. It must not call the API.
- Tests must run offline. A test suite requiring a paid API call is a broken test suite.
- Run `pytest` before declaring any task complete. Paste the actual output.

---

## 11. Code style

- Type hints on every function signature.
- Docstrings only where the reason is not obvious from the name. No docstring that restates the function name.
- Functions under 40 lines. If longer, it does more than one thing.
- No comments explaining what a line does. Comments explain why, and only when why is not obvious.
- `ruff format` and `ruff check` clean before any task is called done.
- Errors raised must say what failed, what was expected, and what was received.

---

## 12. Definition of done

A task is complete only when all of these hold:

1. Code runs. You executed it and read the output.
2. `pytest` passes. You ran it and pasted the result.
3. `ruff check` is clean.
4. No dead code, no unused imports, no leftover debug prints.
5. Anything you were unsure about is stated explicitly, not guessed.

If any of these fail, say which one and why. Do not report success.

---

## 13. When stuck

Say so directly. State what you tried, what happened, and what you need.

Never: silently simplify the task, fake a result, stub something and describe it as working, or continue past an error hoping it resolves later.

---

## 14. Known limitations

- Line-item descriptions are scored against `description_ar`. The generator renders only the Arabic description in the table, on bilingual invoices too, so `description_en` never appears on the page. `eval/load_data.py` drops it deliberately.
- Lumped-VAT invoices have no per-line VAT, but `LineItem.vat_amount` is required. Convention: `0`. Prompt v2 instructs the model to emit `"0"`, `eval/load_data.py` maps the empty ground-truth value to `0`, and `validate.py` treats all-zero line VAT with a non-zero `vat_total` as lumped.
- Model output is not guaranteed to be deterministic. `extract.py` requests `temperature=0` and records in `CallMetadata.temperature_zero` whether the model accepted it, but the same image has produced materially different field errors on consecutive runs. Single-pass eval numbers carry run-to-run variance; `eval/run_eval.py --repeats N` reports per-field agreement across runs alongside accuracy, and any published figure should say which it is.
- gpt-4o misreads Arabic-Indic amounts in table cells even when clearly printed (Noto Naskh, 22px, `٫` separator), with the same wrong values on repeated runs and confidence 1.0. Details in `eval/README.md` "Findings". The arithmetic checks catch it; the confidence scores do not. Report the 6 Arabic-Indic samples separately.
- The prompt still asks for per-field confidence scores that nothing acts on. That is roughly 400 output tokens per call (the `confidence` object is about a third of each response) spent so the inverse-calibration finding stays verifiable with the shipped code. Deliberate trade-off; drop it if the finding is ever retired.
- Currency-token and whitespace stripping applies to `MONEY_FIELDS`, not all of `NUMERIC_FIELDS`. The instruction said the latter; taken literally it would have turned `2026-01-31 19:10:00` into an unparseable timestamp and mangled invoice numbers containing spaces. Recorded as a case where the literal instruction would have introduced a bug and the narrower reading was right.
- `validation_findings` in the audit log stores `{rule, severity, fields}` only. Section 8 originally said "JSON array of findings", which taken literally includes `Finding.message` — and messages quote VAT numbers and amounts, violating section 9. Second case, after `MONEY_FIELDS`, where the literal spec was wrong and the narrower reading was right.
- The Postgres branch in `app/store.py` (`DATABASE_URL` set) is written but has never been executed: `psycopg` is deliberately not a dependency, and the branch raises a clear error naming it. SQLite is the supported store. Do not claim Postgres support in the README.
- The resolver assumes one misread cell. Two or more misreads usually match no single cell's checks and come back `unresolvable`; it does not try pairs. Candidates are non-negative, amounts at 2 decimals, quantities at up to 3.
- The resolver's confusion table (`app/resolve.py`) comes from gpt-4o on 29 CORU receipt lines, one model, two prompt versions. It orders candidates; it is not an error rate and has not been measured on this project's invoices or any other model.
- Accepting a suggestion records the decision; nothing applies the value to an invoice.
- On real gpt-4o output the one-misread assumption rarely holds for Arabic-Indic invoices. Run on the cached answers for the 6 Arabic-Indic eval samples (2026-09-30): 5 failed arithmetic, and all 5 came back `unresolvable`, with 4 to 11 misread cells each. The resolver said so instead of guessing, and the reviewer gets the values involved, but it suggested nothing. Its suggestions are proven only on single injected misreads (`tests/test_resolve.py`).
- `/extract` runs the resolver after the audit row is written, only when a finding is an arithmetic rule. A resolver or queue failure is logged by exception type and does not fail the extraction. Re-uploading an image that already has a pending review queues nothing new. The response's `review` field says which happened: `null` (nothing to resolve), `queued` with the reference and resolver status (the existing item's on a re-upload), or `error`.
- The review queue's `checks` column was added after the queue existed. `app/review.py` adds it with `ALTER TABLE` when missing; rows queued before then have `checks = null` and their review detail lists the failed checks from the resolver's stored labels instead.
- The review counter uses SQLite `AUTOINCREMENT` and `INSERT … RETURNING`; like the rest of the Postgres branch, it has not been run against Postgres and its syntax would need changing there.
- Append-only is enforced in application code, not at the database level. `app/store.py` contains no UPDATE or DELETE and a test scans the source for them; nothing stops a user with the SQLite file from deleting rows. A `BEFORE DELETE` trigger was deliberately not added because defining it would put `DELETE` in the module.
