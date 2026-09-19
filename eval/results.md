# Evaluation results

Full run of `eval/run_eval.py --repeats 3 --confirm-spend` on 2026-09-19.

| Setting | Value |
|---|---|
| Code | commit `2373583` |
| Model | `gpt-4o` (as set in `OPENAI_MODEL`) |
| Prompt | v3 (`app/extract.py`) |
| Temperature | 0, accepted on 90/90 calls |
| Dataset | 30 synthetic invoices, current rendering (Arabic-Indic cells in Noto Naskh 22px with `٫`) |
| Runs | 30 images × 3 repeats = 90 API calls, cache off |
| Tokens | 147,900 prompt + 65,162 completion |
| Cost | not computed (`OPENAI_PRICE_*` unset); look up gpt-4o pricing and multiply |
| Mean latency | 5.9 s per call |

Accuracy is exact match against `eval/ground_truth.json` after normalisation
(Decimal equality for money, string equality otherwise, `description` scored
against `description_ar`). Line-item fields are pooled across all lines.
Agreement is the share of images where all three runs returned the same value.
An unparseable response counts as wrong on every field.

## Headline

- **46 of 90 runs extracted every field exactly.** 12 of 30 invoices were exact in all three runs.
- **All 4 seeded ZATCA defects were caught in all 3 runs (12/12)**, each by the rule that names it and no other.
- **3 of 90 responses failed to parse** (INV-2026-1023 once, INV-2026-1025 twice): the model wrote the totals as `"SAR 11897.50"` and `"SAR 5 456.34"`. That is a gap in our normalisation (currency prefix, thousands space), not a model reading error, and it costs an entire invoice each time. Fixable on our side; not fixed in this run.
- **On Latin-numeral invoices, every monetary value in every parseable run was correct** (24 samples, 69 parseable runs, 0 numeric errors). The 96.5% in the `latin` column below is entirely the three unparseable runs.
- **Arabic-Indic numerals are the dominant failure**, and the full run corrects a claim made after the single-sample test (see "Correction" below).
- **Confidence scores are inversely calibrated at scale**: mean 0.967 on 2,483 correct fields, 1.000 on 199 incorrect fields. 199/199 errors sat above the 0.80 threshold. The 83 sub-threshold scores were all on correct fields.

## Per-field accuracy and agreement (all 90 runs)

```
field                                       accuracy           agreement
------------------------------------------------------------------------
invoice_number                       90.0%   (81/90)     93.3%   (28/30)
invoice_date                         90.0%   (81/90)     90.0%   (27/30)
invoice_timestamp                    90.0%   (81/90)     90.0%   (27/30)
invoice_type                         96.7%   (87/90)     93.3%   (28/30)
seller_name                          94.4%   (85/90)     90.0%   (27/30)
seller_vat_number                    90.0%   (81/90)     90.0%   (27/30)
buyer_name                           95.6%   (86/90)     90.0%   (27/30)
buyer_vat_number                     96.7%   (87/90)     93.3%   (28/30)
subtotal                             86.7%   (78/90)     83.3%   (25/30)
vat_total                            85.6%   (77/90)     80.0%   (24/30)
total                                86.7%   (78/90)     83.3%   (25/30)
currency                             96.7%   (87/90)     93.3%   (28/30)
line_items.count                     96.7%   (87/90)     93.3%   (28/30)
line_items[*].description            81.1% (236/291)     85.6%   (83/97)
line_items[*].quantity               97.3% (283/291)     93.8%   (91/97)
line_items[*].unit_price             85.9% (250/291)     91.8%   (89/97)
line_items[*].line_total             84.9% (247/291)     89.7%   (87/97)
line_items[*].vat_rate               97.3% (283/291)     93.8%   (91/97)
line_items[*].vat_amount             87.3% (254/291)     88.7%   (86/97)
```

## Subgroup splits

```
field                                 all (n=30)  arabic_only (n=12)    bilingual (n=18)  arabic_indic (n=6)        latin (n=24)
--------------------------------------------------------------------------------------------------------------------------------
invoice_number                             90.0%               77.8%               98.1%               66.7%               95.8%
invoice_date                               90.0%               77.8%               98.1%               66.7%               95.8%
invoice_timestamp                          90.0%               77.8%               98.1%               66.7%               95.8%
invoice_type                               96.7%               94.4%               98.1%              100.0%               95.8%
seller_name                                94.4%               88.9%               98.1%              100.0%               93.1%
seller_vat_number                          90.0%               77.8%               98.1%               66.7%               95.8%
buyer_name                                 95.6%               94.4%               96.3%              100.0%               94.4%
buyer_vat_number                           96.7%               94.4%               98.1%              100.0%               95.8%
subtotal                                   86.7%               88.9%               85.2%               50.0%               95.8%
vat_total                                  85.6%               86.1%               85.2%               44.4%               95.8%
total                                      86.7%               88.9%               85.2%               50.0%               95.8%
currency                                   96.7%               94.4%               98.1%              100.0%               95.8%
line_items.count                           96.7%               94.4%               98.1%              100.0%               95.8%
line_items[*].description                  81.1%               83.8%               79.6%               66.7%               84.8%
line_items[*].quantity                     97.3%               96.2%               97.8%              100.0%               96.5%
line_items[*].unit_price                   85.9%               84.8%               86.6%               45.0%               96.5%
line_items[*].line_total                   84.9%               84.8%               84.9%               40.0%               96.5%
line_items[*].vat_rate                     97.3%               96.2%               97.8%              100.0%               96.5%
line_items[*].vat_amount                   87.3%               87.6%               87.1%               51.7%               96.5%
```

Same table with the 3 unparseable runs excluded (87 runs), to separate parsing
from reading:

```
invoice_number                93.1% (81/87)      subtotal                      89.7% (78/87)
invoice_date                  93.1% (81/87)      vat_total                     88.5% (77/87)
invoice_timestamp             93.1% (81/87)      total                         89.7% (78/87)
invoice_type                 100.0% (87/87)      line_items[*].description     83.4% (236/283)
seller_name                   97.7% (85/87)      line_items[*].quantity       100.0% (283/283)
seller_vat_number             93.1% (81/87)      line_items[*].unit_price      88.3% (250/283)
buyer_name                    98.9% (86/87)      line_items[*].line_total      87.3% (247/283)
buyer_vat_number             100.0% (87/87)      line_items[*].vat_rate       100.0% (283/283)
currency / line_items.count  100.0%              line_items[*].vat_amount      89.8% (254/283)
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
  mean confidence on correct fields:   0.967 (n=2483)
  mean confidence on incorrect fields: 1.000 (n=199)
  incorrect fields scored >= threshold: 199/199
  correct fields scored < threshold:    83/2483
  fields with no score from the model:  60
```

The 60 unscored fields are the 3 unparseable runs (20 scorable paths each).
Confidence never predicted an error. What caught the numeric errors was
arithmetic validation: all 15 parseable runs containing a wrong monetary value
carried at least one `error`-severity arithmetic finding (15/15). 24 of the 90
runs had a finding of some severity.

## Summary line

```
samples=30 repeats=3 runs=90 unparseable=3 cache_hits=0 temperature_zero=90/90
prompt_tokens=147900 completion_tokens=65162 estimated_cost_usd=unknown (set OPENAI_PRICE_INPUT_PER_1M_USD and OPENAI_PRICE_OUTPUT_PER_1M_USD)
```

## What failed, by cause

### 1. Arabic-Indic numerals (6 samples, 18 runs) — model limitation

Every wrong `subtotal`, `vat_total`, `total`, `unit_price`, `line_total`
and `vat_amount` in the whole run is on one of the six Arabic-Indic invoices, or
in an unparseable run. Splitting those six by layout:

| | bilingual (1002, 1010, 1011, 1020) | arabic_only (1003, 1012) |
|---|---|---|
| invoice_number | 12/12 | **0/6** |
| invoice_date, invoice_timestamp | 12/12 | **0/6** |
| seller_vat_number | 12/12 | **0/6** |
| line_items[*].quantity | 42/42 | 18/18 |
| line_items[*].unit_price | 21/42 | 6/18 |
| line_items[*].line_total | 18/42 | 6/18 |
| subtotal | 5/12 | 4/6 |

On the two Arabic-only, Arabic-Indic invoices the model returned the wrong
invoice number (`INV-2026-1003` → `INV-2026-1002`, `INV-2026-1012` →
`INV-2026-1002`), the wrong year (`٢٠٢٦` → `2023`) and a 15-digit VAT
number with digits dropped (`300670227773013` → `31027773013070`,
`30072773013`, `301072773013`) in **all six runs**. Single-digit quantities
were still 100%.

**Correction to `eval/README.md`.** The single-sample test on INV-2026-1002
concluded the failure was "specific to multi-digit decimal amounts", because the
invoice number, date and VAT numbers on that page were read correctly. That
control was confounded: INV-2026-1002 is bilingual, and its English block prints
the same values in Latin digits. The full run shows the header fields fail just as
hard when there is no Latin duplicate to read from. The accurate statement is:
**this model reads multi-digit Arabic-Indic numbers unreliably; single digits are
fine; bilingual layouts mask the problem for any field that is also printed in
Latin digits.** The README is updated to say this.

### 2. Unparseable responses (2 samples, 3 runs) — our normalisation gap

`"SAR 11897.50"`, `"SAR 5 456.34"`: currency code and a thousands space inside
a money string. `schema.py` strips commas only. Both invoices are Latin-numeral;
the underlying digits were right. Each failure zeroes ~30 fields, which is why
`invoice_number` is 90.0% and not 93.1% overall.

### 3. Descriptions (83.4% of parseable line items) — mostly systematic

Every miss, with how many of the 3 runs it appeared in:

| Kind | Examples | Runs |
|---|---|---|
| Spelling "corrected" to a common variant | `سندويتش` → `سندوتش` (4 samples) | 12 |
| Word substituted by a plausible word | `كيلو`→`جاز`/`كبير`, `شهرية`→`كهربية`, `محمول`→`عمول`, `حبر`→`جهاز`, `شاورما`→`هاورما`, `كرسي`→`كوبي` | 16 |
| Latin digit inside Arabic text re-scripted | `27 بوصة` → `٢٧ بوصة`, `5 متر` → `٥ متر` (mostly on Arabic-Indic invoices; once on a Latin one) | 9 |
| Punctuation around `A4` | `ورق تصوير A4 - علبة` → `ورق تصوير - A4 - علبة` / `- A4 علبة` | 7 |
| Visual (LTR) order instead of logical | `ورق تصوير A4 - علبة` → `علبة - A4 ورق تصوير` (INV-2026-1010) | 3 |

Prompt v3's "transcribe, do not correct" instruction removed some of this on the
three-sample test but not at scale: 33 of 47 description misses occurred in all
three runs, i.e. the model makes the same substitution deterministically.

### 4. Other

- INV-2026-1008 `seller_name`: `شركة جدة للأغذية` → `شركة جدة الأغذية` (a letter dropped) in 2/3 runs.
- INV-2026-1017 `buyer_name`: the seller's city returned as the buyer name in 1/3 runs (a simplified invoice with no buyer).
- No run misread `invoice_type`, `currency`, `vat_rate`, or the number of line items.

## Agreement

Agreement is 88–94% on most fields and 80–83% on the totals. Almost all
disagreement is on the Arabic-Indic invoices and the two unparseable-in-some-runs
invoices; on the other 22 invoices the three runs were nearly always identical.
`temperature=0` makes the misreads repeatable but not correct.

## Reproducing

```
set -a; . ./.env; set +a
.venv/bin/python -m eval.run_eval --repeats 3 --confirm-spend --dump runs.json
```

A single pass from cache (`python -m eval.run_eval`) re-reads the last of the
three responses per image and makes no API calls.
