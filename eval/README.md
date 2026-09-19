# Arabic Invoice Evaluation Set (30 samples)

A reproducible evaluation set of ZATCA-format Arabic/English tax invoices with
exact ground truth. Built for measuring extraction accuracy.

## What is in here

```
samples/            30 PNG invoice images
ground_truth.json   exact field values for every sample
generate_invoices.py  the generator (regenerate or extend the set)
fonts/              Amiri and Noto Naskh Arabic (OFL licensed)
```

## What these are, honestly

**Synthetic, not scraped.** Every invoice is generated. No real company, no real
VAT number, no real customer data. This is deliberate:

- Ground truth is exact, not approximated. Accuracy numbers are defensible.
- No PII, so no privacy problem in publishing the repo or the demo.
- The defect cases are controlled, so you can prove the validator catches them.

**They are not a substitute for real-world noise.** Real invoices are photographed
at angles, crumpled, faded, and printed on thermal paper. Once extraction works on
this set, validate against a real dataset before claiming general accuracy. Two
free options:

- `abdoelsayed/CORU` on Hugging Face — 20,000 annotated Arabic/English receipts,
  PII-redacted, real retail layouts
- `humansintheloop/arabic-documents-ocr-dataset` on Kaggle — 10K images across
  12 document classes including invoices

State in your README which set each number came from. Do not blend them.

## Composition

| Property | Count |
|---|---|
| Total samples | 30 |
| Standard tax invoices (B2B) | 17 |
| Simplified tax invoices (B2C) | 13 |
| With ZATCA TLV QR code | 13 |
| Arabic-only layout (no English) | 12 |
| Arabic-Indic numerals (٠١٢٣٤٥٦٧٨٩) | 6 |
| Seeded defects | 4 |

Every sample carries scan-like degradation: slight rotation, blur, or
contrast shift. None are perfectly clean.

## Seeded defects

Four invoices contain deliberate ZATCA compliance faults. These exist so your
validation layer has real work to do and you can report a true catch rate.

| File | Defect | Why it matters |
|---|---|---|
| INV-2026-1003 | `missing_buyer_vat` | Standard invoice with no buyer TIN. The most frequent cause of clearance rejection. |
| INV-2026-1009 | `lumped_vat` | No per-line VAT breakdown. The second most common rejection cause. |
| INV-2026-1021 | `lumped_vat` | Same. |
| INV-2026-1026 | `missing_seller_vat` | No seller TIN at all. |

The `seeded_defect` field in `ground_truth.json` names the fault. Everything else
is arithmetically consistent.

**Corrected label.** The generator originally seeded `missing_buyer_vat` on
INV-2026-1014 as well, but that invoice is *simplified*, and a simplified invoice
does not carry a buyer VAT number: the omission is correct, not a defect. Its
`seeded_defect` in `ground_truth.json` has been set to `null` by hand.
`generate_invoices.py` still seeds index 14, so regenerating the set reintroduces
the wrong label; re-apply the correction if you regenerate.

## Verified properties

Checked programmatically across all 30 samples:

- `quantity × unit_price = line_total` within 0.01
- `sum(line_totals) = subtotal` within 0.02
- `sum(vat_amounts) = vat_total` within 0.02 (except seeded `lumped_vat` cases)
- `subtotal + vat_total = total` within 0.01
- Every VAT number is exactly 15 digits, starting and ending with 3
- All 13 QR codes decode from the image to valid ZATCA TLV with tags 1–5
  (Seller Name, VAT Number, Timestamp, Total, VAT Amount)

## ZATCA field reference

**Simplified tax invoice (B2C)** — QR code carries five TLV tags:
seller name, VAT number, timestamp, total including VAT, VAT amount, base64 encoded.

**Standard tax invoice (B2B)** — requires full buyer and seller identification,
both VAT numbers, and per-line VAT breakdown.

VAT rate throughout is 15%.

`invoice_timestamp` in `ground_truth.json` is naive local time (`2026-07-31T15:38:00`),
exactly as the date and time lines print it; the page shows no zone. The QR payload
carries the same instant with a `Z` suffix, as ZATCA TLV examples do, and is not scored.

## Known hard cases

These are features, not bugs. Your extractor should handle them or your accuracy
table should say it does not.

1. **Arabic-Indic invoices use the Arabic decimal separator.** A value stored as
   `5612.45` is printed as `٥٦١٢٫٤٥` (U+066B). Normalise digits and separators
   before comparing against ground truth. Numeric cells on these invoices are set
   in Noto Naskh Arabic at 22px; prose keeps the sample's own font. The first
   release printed an ASCII period in Amiri, whose period and Arabic-Indic zero
   are the same low dot, so `٦١٠.٨٤` was unreadable as 610.84 by anyone.
2. **Arabic-Indic numerals** appear in 6 samples. `٣١٠١٢٢٣٩٣٥٠٠٠٠٣` is the same
   value as `310122393500003`. See "Findings" below: current vision models
   misread these even when clearly printed.
3. **Mixed-direction text.** Invoice numbers like `INV-2026-1002` sit inside
   right-to-left lines. Extraction order matters.
4. **Arabic-only invoices** have no English label to anchor on. Twelve samples
   have no English at all.

## Findings

Things learned by running an extractor over this set. They describe the model,
not the data, and are recorded so nobody spends time "fixing" the rendering again.

**Scope.** Everything below was measured with one model (`gpt-4o`, September 2026),
on this generator's 30 invoices (the Arabic-Indic finding rests on 6 of them, and
the three-condition table on one), with `temperature=0`. It is a reproducible
observation, not a general result about vision models, and it may not hold on
other models, other fonts, or real scans. Full numbers: `results.md`.

### Multi-digit decimal amounts in Arabic-Indic numerals are misread

INV-2026-1002 was extracted three times under each of three renderings of the
numeric table cells. Ground truth, layout and every other pixel were identical
across conditions.

| Rendering of numeric cells | unit_price | line_total | vat_amount | agreement across 3 runs |
|---|---|---|---|---|
| Amiri 19px, ASCII `.` separator (first release) | 8/15 | 8/15 | 8/15 | 60% / 80% / 60% |
| Noto Naskh 19px, `٫` separator | 6/15 | 3/15 | 6/15 | 100% / 100% / 60% |
| Noto Naskh 22px, `٫` separator (current) | 6/15 | 3/15 | 6/15 | 80% / 100% / 100% |

`subtotal`, `vat_total` and `total` were wrong in 25 of 27 runs. Typical returns
for the current rendering, identical in all three runs: `٧٧٫٥٨`→75.8,
`٦٢٠٫٦٤`→606.4, `٦٩٫٠٥`→69.5, `٦١٠٫٨٤`→61.84, `٢٤٤٣٫٣٦`→247.36,
`٤٣١٩٫١٥`→4219.15. Mostly single-digit deletions, sometimes a substitution.

Three things follow from the table:

1. **Making the rendering clearer did not help; accuracy fell.** The first
   release's ASCII period is drawn by Amiri as the same low dot as `٠`, and the
   errors in that condition had a separator/zero-swap signature (`٦١٠٫٨٤`→61.084).
   Fixing the separator and font removed that signature and made the cells plainly
   legible to a human, and line_total accuracy went from 8/15 to 3/15.
2. **At `temperature=0` the wrong values are stable.** Agreement rose to 100% on
   most cells once the rendering was unambiguous: the model returns the same wrong
   number every time. This is systematic, not noise.
3. **The apparent control was confounded.** In the same runs, single-digit
   quantities were 45/45, the date, time and invoice number 9/9 each, and the two
   15-digit VAT numbers 17/18, which at first suggested the failure was specific
   to multi-digit *decimal* amounts. The full evaluation (`results.md`) showed
   otherwise: INV-2026-1002 is bilingual, and its English block prints the same
   header values in Latin digits. On the two Arabic-only, Arabic-Indic invoices
   (INV-2026-1003, INV-2026-1012) the invoice number, date and 15-digit VAT
   number were wrong in 6 of 6 runs — a dropped digit, `٢٠٢٦` read as 2023,
   `١٠٠٣` read as 1002. Single-digit quantities stayed 100%. The accurate
   statement is: **multi-digit Arabic-Indic numbers are read unreliably; single
   digits are fine; a bilingual layout hides the problem on any field that is
   also printed in Latin digits.**

Latin-numeral invoices had every monetary value correct in every parseable run
of the full evaluation (24 samples, 69 runs). Report the 6 Arabic-Indic samples
separately from the other 24; pooling them hides this.

### The model's confidence scores do not predict its errors

Measured by `run_eval.py`'s calibration report over the full evaluation
(30 invoices × 3 runs, 2,682 scored fields):

```
mean confidence on correct fields:   0.967 (n=2483)
mean confidence on incorrect fields: 1.000 (n=199)
incorrect fields scored >= threshold: 199/199
correct fields scored < threshold:    83/2483
```

Every wrong value carried a score of 1.0. Every sub-threshold score was on a
correct field, almost all of them 0.0 on fields correctly returned as `null`.
On this evidence the confidence score flags nullness, not risk. What actually
catches the misreads is arithmetic validation: all 15 runs containing a wrong
monetary value carried an `error`-severity finding. The score is still requested
and recorded so this measurement can be repeated.

### A hallucinated field that nothing in this architecture can catch

INV-2026-1017 is a simplified invoice with no buyer. In all three runs of the
full evaluation the model returned the seller's city, `المدينة المنورة`, as
`buyer_name`, with confidence 1.0. Ground truth is `null`.

Every safeguard in this pipeline misses it, for a reason each:

- **Arithmetic validation** only sees numbers; a name is not in any sum.
- **Confidence gating** (were it still on) would pass it: the score was 1.0.
- **Structural rules** do not apply: a simplified invoice legitimately has no
  buyer, `buyer_vat_number` was correctly `null`, and nothing requires
  `buyer_name` to be null when the VAT number is.

This is the one error class observed in this repo that reaches the output with
no flag at all: a plausible value invented for an absent field. It is a limit of
validation-by-consistency, not a bug to fix. Catching it would need a second,
independent read (another model or a second pass) or a human. Reviewers should
know that "no findings" means "internally consistent", not "verified".

### A malformed confidence object

In one run of 90 (INV-2026-1025, run 1) the model returned a `confidence` object
whose keys matched none of the field paths the prompt specifies. The invoice
fields in that run were all correct. `run_eval.py` reports such fields as "no
score from the model" and excludes them from the calibration means. One
occurrence; recorded so it is not mistaken for a scoring bug if it recurs.

## Regenerating

```bash
pip install pillow arabic-reshaper python-bidi qrcode
python generate_invoices.py
```

Seed is fixed at `20260916`, so the set is reproducible. Change `SEED` for a
different set, or edit `DEFECTS` to seed different faults.

Pillow must be built with libraqm for Arabic shaping. Check with:

```python
from PIL import features; print(features.check("raqm"))
```

If this returns `False`, Arabic letters will render disconnected and unreadable.
Install `libraqm0`.

## Licensing

Generated content is yours to use freely. Fonts are Amiri and Noto Naskh Arabic,
both under the SIL Open Font License, included in `fonts/`.
