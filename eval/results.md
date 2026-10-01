# Evaluation results

Full run of `eval/run_eval.py --repeats 3 --confirm-spend --no-cache` on 2026-09-19.

**This supersedes the earlier full run** (commit `2373583`, same day). That run
had 3 of 90 responses fail to parse because the model wrote totals as
`"SAR 11897.50"` and `"SAR 5 456.34"` and the normaliser stripped commas only.
Each unparseable response scored zero on ~30 fields, which depressed every
"all" figure and made the `latin` column read 96.5% on money fields that were in
fact 100% correct when parsed. The normaliser now removes currency tokens and
whitespace from money fields (commit `e00ab41`); this run has 0 unparseable
responses. Nothing else changed between the two runs.

| Setting | Value |
|---|---|
| Code | commit `e55b2eb` |
| Model | `gpt-4o` (as set in `OPENAI_MODEL`) |
| Prompt | v3 (`app/extract.py`) |
| Temperature | 0, accepted on 90/90 calls |
| Dataset | 30 synthetic invoices, current rendering (Arabic-Indic cells in Noto Naskh 22px with `٫`) |
| Runs | 30 images × 3 repeats = 90 API calls, cache off |
| Unparseable responses | 0 |
| Tokens | 147,900 prompt + 65,102 completion |
| Cost | not computed (`OPENAI_PRICE_*` unset); look up gpt-4o pricing and multiply |
| Mean latency | 6.5 s per call |

Accuracy is exact match against `eval/ground_truth.json` after normalisation
(Decimal equality for money, string equality otherwise, `description` scored
against `description_ar`). Line-item fields are pooled across all lines.
Agreement is the share of images where all three runs returned the same value.

## Headline

- **51 of 90 runs extracted every field exactly.** 16 of 30 invoices were exact in all three runs.
- **All 4 seeded ZATCA defects were caught in all 3 runs (12/12)**, each by the rule that names it and no other.
- **On the 24 Latin-numeral invoices every monetary value was correct in all 72 runs.** Every one of the 97 wrong line-item amounts and every wrong total in this run is on one of the 6 Arabic-Indic invoices.
- **Arabic-Indic numerals are the dominant failure**: 41.7% on `line_total`, 55.6% on the totals, and 0/6 on invoice number, date and seller VAT for the two Arabic-only, Arabic-Indic invoices.
- **Descriptions are the second failure**: 84.9%, with 36 of 44 misses identical in all three runs.
- **Confidence scores remain inversely calibrated**: mean 0.969 on 2,609 correct fields, 1.000 on 193 incorrect. 193/193 errors sat above the 0.80 threshold; all 81 sub-threshold scores were on correct fields.
- **Arithmetic validation caught all 15 runs that contained a wrong monetary value.**

## Per-field accuracy and agreement (90 runs)

```
field                                       accuracy           agreement
------------------------------------------------------------------------
invoice_number                       93.3%   (84/90)    100.0%   (30/30)
invoice_date                         93.3%   (84/90)     96.7%   (29/30)
invoice_timestamp                    93.3%   (84/90)     96.7%   (29/30)
invoice_type                        100.0%   (90/90)    100.0%   (30/30)
seller_name                          98.9%   (89/90)     96.7%   (29/30)
seller_vat_number                    93.3%   (84/90)    100.0%   (30/30)
buyer_name                           96.7%   (87/90)    100.0%   (30/30)
buyer_vat_number                    100.0%   (90/90)    100.0%   (30/30)
subtotal                             91.1%   (82/90)     90.0%   (27/30)
vat_total                            91.1%   (82/90)     90.0%   (27/30)
total                                91.1%   (82/90)     90.0%   (27/30)
currency                            100.0%   (90/90)    100.0%   (30/30)
line_items.count                    100.0%   (90/90)    100.0%   (30/30)
line_items[*].description            84.9% (247/291)     94.8%   (92/97)
line_items[*].quantity              100.0% (291/291)    100.0%   (97/97)
line_items[*].unit_price             89.0% (259/291)     97.9%   (95/97)
line_items[*].line_total             88.0% (256/291)     96.9%   (94/97)
line_items[*].vat_rate              100.0% (291/291)    100.0%   (97/97)
line_items[*].vat_amount             89.7% (261/291)     96.9%   (94/97)
```

## Subgroup splits

```
field                                 all (n=30)  arabic_only (n=12)    bilingual (n=18)  arabic_indic (n=6)        latin (n=24)
--------------------------------------------------------------------------------------------------------------------------------
invoice_number                             93.3%               83.3%              100.0%               66.7%              100.0%
invoice_date                               93.3%               83.3%              100.0%               66.7%              100.0%
invoice_timestamp                          93.3%               83.3%              100.0%               66.7%              100.0%
invoice_type                              100.0%              100.0%              100.0%              100.0%              100.0%
seller_name                                98.9%               97.2%              100.0%              100.0%               98.6%
seller_vat_number                          93.3%               83.3%              100.0%               66.7%              100.0%
buyer_name                                 96.7%              100.0%               94.4%              100.0%               95.8%
buyer_vat_number                          100.0%              100.0%              100.0%              100.0%              100.0%
subtotal                                   91.1%               94.4%               88.9%               55.6%              100.0%
vat_total                                  91.1%               91.7%               90.7%               55.6%              100.0%
total                                      91.1%               94.4%               88.9%               55.6%              100.0%
currency                                  100.0%              100.0%              100.0%              100.0%              100.0%
line_items.count                          100.0%              100.0%              100.0%              100.0%              100.0%
line_items[*].description                  84.9%               88.6%               82.8%               70.0%               88.7%
line_items[*].quantity                    100.0%              100.0%              100.0%              100.0%              100.0%
line_items[*].unit_price                   89.0%               88.6%               89.2%               46.7%              100.0%
line_items[*].line_total                   88.0%               88.6%               87.6%               41.7%              100.0%
line_items[*].vat_rate                    100.0%              100.0%              100.0%              100.0%              100.0%
line_items[*].vat_amount                   89.7%               90.5%               89.2%               50.0%              100.0%
```

## Seeded defects

```
seeded defects caught (90 runs):
  INV-2026-1003.png  missing_buyer_vat    caught 3/3
  INV-2026-1009.png  lumped_vat           caught 3/3
  INV-2026-1021.png  lumped_vat           caught 3/3
  INV-2026-1026.png  missing_seller_vat   caught 3/3
```

## Confidence calibration

```
confidence calibration (threshold 0.80):
  mean confidence on correct fields:   0.969 (n=2609)
  mean confidence on incorrect fields: 1.000 (n=193)
  incorrect fields scored >= threshold: 193/193
  correct fields scored < threshold:    81/2609
  fields with no score from the model:  24
```

The 24 unscored fields are one run of INV-2026-1025 in which the model returned
a `confidence` object whose keys matched none of the field paths; the invoice
itself was extracted correctly in that run.

## Summary line

```
samples=30 repeats=3 runs=90 unparseable=0 cache_hits=0 temperature_zero=90/90
prompt_tokens=147900 completion_tokens=65102 estimated_cost_usd=unknown (set OPENAI_PRICE_INPUT_PER_1M_USD and OPENAI_PRICE_OUTPUT_PER_1M_USD)
```

## What failed, by cause

### 1. Arabic-Indic numerals (6 samples, 18 runs) — model limitation

All 97 wrong line-item amounts and all 24 wrong totals are on the six Arabic-Indic
invoices. Splitting those six by layout:

| | bilingual (1002, 1010, 1011, 1020) | arabic_only (1003, 1012) |
|---|---|---|
| invoice_number | 12/12 | **0/6** |
| invoice_date, invoice_timestamp | 12/12 | **0/6** |
| seller_vat_number | 12/12 | **0/6** |
| line_items[*].quantity | 42/42 | 18/18 |
| line_items[*].unit_price | 22/42 | 6/18 |
| line_items[*].line_total | 19/42 | 6/18 |
| subtotal | 6/12 | 4/6 |

On the two Arabic-only, Arabic-Indic invoices the model returned the wrong invoice
number (`INV-2026-1003` → `INV-2026-1002`, `INV-2026-1012` → `INV-2026-1002`),
the wrong year (`٢٠٢٦` → `2023`) and a 15-digit VAT number with digits dropped
(`300670227773013` → `301072773013`, `396374143699743` → `39623741439974`)
in all six runs. Single-digit quantities were 100%. The bilingual four read every
header field correctly because the English block prints the same values in Latin
digits; their table amounts, which have no Latin copy, fail too (unit_price
22/42, versus 6/18 on the Arabic-only pair). The failure is therefore multi-digit
Arabic-Indic numbers wherever no Latin copy exists, not table cells specifically —
see `README.md` "Findings" for the correction history.

Agreement on these fields is now 97–98%: the wrong values are the same wrong
values every time.

### 2. Descriptions (84.9%) — systematic transcription errors

44 misses across 291 line items, 36 of them in all three runs:

| Kind | Examples | Runs |
|---|---|---|
| Word substituted by a plausible word | `كيلو`→`جاز`/`كبير`, `شهرية`→`كهربية`, `محمول`→`عمول`, `طازج`→`فواكه`, `حبر`→`جهاز`, `كرسي`→`كوبي` | 15 |
| Spelling "corrected" to a common variant | `سندويتش` → `سندوتش` (3 samples, 4 lines) | 12 |
| Latin digit inside Arabic text re-scripted | `27 بوصة` → `٢٧ بوصة`, `5 متر` → `٥ متر` (3 Arabic-Indic invoices, 1 Latin) | 11 |
| Hyphen moved around `A4` | `ورق تصوير A4 - علبة` → `ورق تصوير - A4 علبة` | 6 |

No visual-order (bidi) reversal occurred in this run; the one seen previously
(INV-2026-1010) did not recur. Prompt v3's "transcribe, do not correct" instruction
did not stop the substitutions.

### 3. Other

- INV-2026-1017 `buyer_name`: the seller's city (`المدينة المنورة`) returned as
  the buyer name in 3/3 runs, on a simplified invoice with no buyer. `buyer_vat_number`
  was correctly null, so no rule fires; this is a hallucinated field with confidence 1.0.
- INV-2026-1008 `seller_name`: one letter dropped (`للأغذية` → `الأغذية`) in 1/3 runs.
- No run misread `invoice_type`, `currency`, `vat_rate`, `buyer_vat_number`, any
  quantity, or the number of line items.

## Agreement

96–100% on every field except the totals (90%) and descriptions (94.8%).
Disagreement is confined to the Arabic-Indic invoices plus three single-run
slips (INV-2026-1006 `فواكه`, INV-2026-1008 seller name, INV-2026-1026 two
descriptions). `temperature=0` makes the output repeatable; on Arabic-Indic
numerals it makes it repeatably wrong.

## Resolver on real model output

Run 2026-09-30 on gpt-4o's cached answers for the 6 Arabic-Indic samples: the
last of the three evaluation responses per image, no API calls. "Misread" counts
every numeric field that differs from ground truth; "in failed checks" counts
those that sit in at least one failed arithmetic check, which are the values a
review card lists.

| Sample | Arithmetic | Resolver | Candidates | Misread | In failed checks | Misread outside the failed checks |
|---|---|---|---|---|---|---|
| INV-2026-1002 | needs_review | unresolvable | 0 | 13 | 11 | line_items[0].unit_price, line_items[4].unit_price |
| INV-2026-1003 | needs_review | unresolvable | 0 | 8 | 5 | line_items[1–3].unit_price |
| INV-2026-1010 | ok | not_needed | 0 | 0 | 0 | – |
| INV-2026-1011 | needs_review | unresolvable | 0 | 5 | 4 | line_items[2].unit_price |
| INV-2026-1012 | needs_review | unresolvable | 0 | 6 | 5 | total |
| INV-2026-1020 | needs_review | unresolvable | 0 | 10 | 8 | line_items[0].unit_price, line_items[2].unit_price |

Every sample with a misread failed arithmetic, and every one came back
unresolvable: the resolver assumes one misread cell and these have 5 to 13. It
made no suggestions, so none were wrong and none were right. Of the 42 misread
values, 9 sat outside every failed check (8 of them unit prices): arithmetic
flags the invoice, not every wrong cell.

Reproduce from the repo root (needs `.cache/`; with no cache entry, `extract`
would call the API):

```bash
OPENAI_MODEL=gpt-4o .venv/bin/python - <<'PY'
from decimal import Decimal
from app.extract import extract
from app.resolve import resolve
from eval.load_data import load_samples
from eval.run_eval import flatten

for s in load_samples():
    if s.meta.numerals != "arabic_indic":
        continue
    result, meta = extract(s.image_path.read_bytes())  # cache only; a miss would call the API
    assert meta.cache_hit, s.meta.file
    got, truth = flatten(result.invoice), flatten(s.invoice)
    wrong = {k for k, v in truth.items() if isinstance(v, Decimal) and got.get(k) != v}
    r = resolve(result.invoice)
    in_checks = wrong & {x.field for x in r.involved}
    print(s.meta.file, result.status, r.status, f"candidates={len(r.candidates)}",
          f"misread={len(wrong)}", f"in_failed_checks={len(in_checks)}",
          f"outside={sorted(wrong - in_checks)}")
PY
```

## Reproducing

```
set -a; . ./.env; set +a
.venv/bin/python -m eval.run_eval --repeats 3 --confirm-spend --no-cache --dump runs.json
```

A single pass from cache (`python -m eval.run_eval`) re-reads the last of the
three responses per image and makes no API calls.
