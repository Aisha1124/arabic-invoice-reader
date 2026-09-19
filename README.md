# Arabic/English Invoice Reader

Full README to follow. The section below is kept current as the build goes.

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
