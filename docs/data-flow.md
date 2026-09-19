# Data flow

What this service does with an uploaded invoice, written from the code as it is
at the time of writing (`app/main.py`, `app/extract.py`, `app/cache.py`,
`app/store.py`). Where the code does not do something, this document says so.
It does not claim PDPL compliance; it describes design choices and the PDPL
concern each one addresses.

## 1. The request

`POST /extract` receives the image as the raw request body. It is read into
memory in chunks, capped at 10 MB, and rejected unless the bytes are PNG or JPEG.
The image is never written to disk by the application. It exists in process
memory for the duration of the request and is discarded when the response is
sent. Its SHA-256 is computed and used twice: as the cache key and as the
`image_sha256` column of the audit row.

## 2. What is sent to OpenAI

One HTTPS request to the OpenAI Chat Completions API per cache miss, containing:

- the **entire image**, base64-encoded, at `detail: "high"`;
- the extraction prompt (`PROMPT` in `app/extract.py`, versioned);
- the model name from `OPENAI_MODEL`, `temperature: 0`, and a JSON response
  format instruction;
- the API key, in the request header.

Nothing else: no filename, no user identity, no previous invoices, no audit data.
The request does not opt into any OpenAI storage or fine-tuning feature. What
OpenAI retains, and for how long, is governed by OpenAI's terms and is not
restated here; the reader should consult them for the account in use.

On a cache hit no request is made and nothing leaves the machine.

## 3. What is stored, where, and for how long

| Location | Contents | Contains invoice content? | Retention |
|---|---|---|---|
| `.cache/<sha256>_<model>_<prompt_version>.json` | The model's **raw response text**, plus token counts, latency and whether `temperature=0` was accepted | **Yes** | Indefinite. No TTL, no eviction. |
| `data/app.db`, table `audit_log` | One row per extraction: UUID, UTC timestamp, image SHA-256, model, prompt version, counts of extracted and flagged fields, validation findings as `{rule, severity, fields}`, latency, estimated cost, cache-hit and temperature flags | No | Indefinite. Append-only: the module contains no UPDATE or DELETE, and a test enforces that. |
| Process memory | The uploaded image, the parsed result | Yes | Until the response is sent |
| Server log (stderr) | Model name, token counts, latency, cache hits; on a parse failure, the exception message, which can quote fragments of the model's output | Can, on parse failure only | Wherever the operator sends stderr |

**`.cache/` is where the PII lives.** Each cache file is the model's JSON answer
verbatim: seller and buyer names, VAT numbers, line descriptions, amounts, dates.
It is keyed by image hash so the same invoice is never paid for twice, and it is
what the evaluation reads from. Anyone reading this document must not conclude
the system is content-free: it is content-free everywhere *except* `.cache/`,
and `.cache/` is a plain directory of JSON files with default file permissions.

Both `.cache/` and `data/` are git-ignored. The evaluation set in `eval/` is
synthetic (no real company, person or VAT number) and is the only invoice data
committed to the repository.

## 4. What is never stored: the audit log

The audit log records that an extraction happened and how it went, not what the
invoice said. Its columns are fixed by a frozen dataclass with exactly twelve
fields, none of which is a name, a number from the page, a description, or the
image. The image is represented only by its SHA-256, which cannot be reversed
into the image.

One column needed narrowing to keep this true. Validation findings carry a
human-readable `message`, and those messages quote content: the seller-VAT
format rule prints the number it received, the arithmetic rules print the
amounts that did not add up. Storing findings whole would have put VAT numbers
and totals into the log. The audit row therefore keeps only each finding's
`rule`, `severity` and `fields`; `fields` are schema paths such as
`line_items[0].vat_amount`, which name a position, not a value. A test builds a
result whose messages do quote a VAT number and a total and asserts that neither
appears anywhere in the row or in a dump of the database file.

## 5. Where data is hosted

The application runs wherever the operator starts `uvicorn`; `.cache/` and
`data/app.db` are on that machine's filesystem, in the working directory. The
code sets no region for anything. The OpenAI request goes to the SDK's default
endpoint unless the operator sets `OPENAI_BASE_URL`, which the SDK honours and
the code does not touch.

Changing where the local data lives means running the service elsewhere and
moving or discarding the two directories. Changing where the model runs means
pointing `OPENAI_BASE_URL` at a different endpoint that speaks the same API and
setting `OPENAI_MODEL` accordingly; nothing in the code assumes a provider
beyond the Chat Completions request shape. Whether a given endpoint keeps
invoice images inside a particular jurisdiction is a property of that endpoint,
not of this code.

## 6. Deletion

**No deletion endpoint exists.** The service has no API, page or command that
removes anything. Deletion is done by hand on the host:

- `rm -r .cache/` removes every stored model response. To remove one invoice's
  response, compute the image's SHA-256 and delete the file whose name begins
  with it.
- `rm data/app.db` removes the entire audit log. Individual rows cannot be
  deleted through the application by design; deleting them with `sqlite3`
  directly is possible and nothing prevents it.

Because the audit log holds no content and the image hash is one-way, deleting
`.cache/` alone removes all stored invoice content from the machine. What
OpenAI holds is outside the operator's filesystem and outside this document.

## 7. PDPL concerns and what addresses them

| Concern | Design choice | Gap |
|---|---|---|
| Data minimisation | Audit log holds counts, hashes and rule names only; images never written | `.cache/` holds full responses |
| Purpose limitation | Cached responses are used only to answer the same image again and to run the evaluation | No technical enforcement |
| Storage limitation | — | No TTL on `.cache/` or `audit_log`; retention is manual |
| Cross-border transfer | The only transfer is the image to the OpenAI endpoint; documented above; endpoint is configurable | Default endpoint is wherever OpenAI runs it |
| Right to erasure | Manual deletion described above | No endpoint; no per-subject lookup beyond image hash |
| Security of storage | `.cache/` and `data/` git-ignored; nothing written outside them | Default file permissions; no encryption at rest |
| Transparency | This document | — |
