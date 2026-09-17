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

## Known hard cases

These are features, not bugs. Your extractor should handle them or your accuracy
table should say it does not.

1. **Amiri renders decimal separators in Arabic style.** A value stored as
   `5612.45` may visually read closer to `5612٫45`. Normalise separators before
   comparing against ground truth.
2. **Arabic-Indic numerals** appear in 6 samples. `٣١٠١٢٢٣٩٣٥٠٠٠٠٣` is the same
   value as `310122393500003`.
3. **Mixed-direction text.** Invoice numbers like `INV-2026-1002` sit inside
   right-to-left lines. Extraction order matters.
4. **Arabic-only invoices** have no English label to anchor on. Twelve samples
   have no English at all.

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
