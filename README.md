# Invoice Data Extractor
 
Feed it a PDF or image invoice, get back clean structured JSON — vendor, dates,
totals, line items, taxes — plus a verdict on whether the numbers actually add
up and whether a human should double-check it before it gets paid.
 
Setup and CLI usage: see [`USEME.md`](USEME.md). This doc is just the "how it
works and why" version.

## How it works

Five stages, each one only trusting the stage before it if the evidence checks out:

1. **Ingest** (`ingest.py`) — every page gets rasterized to an image (that's
   the extraction path, always). If the PDF has a text layer, it's also
   pulled separately — not for extraction, just so we can sanity-check things
   like "does the currency symbol on the page match what the model claims."
2. **Extract** (`extract.py`, `prompt.py`) — one vision model call per
   document, strict JSON out. A few rules keep this honest: numbers come back
   as strings (never floats, which silently mangle decimals), every field
   carries a status — `present`, `stated_absent`, `missing`, or `illegible` —
   so "the invoice says there's no due date" doesn't get flattened into the
   same thing as "we couldn't find one." The model also never does its own
   math — it reports what's printed even if it looks wrong, because a typo on
   the invoice is a signal, not something to "fix" quietly.
3. **Validate** (`validate.py`) — this is where the actual arithmetic
   happens, in `Decimal`, never float. Does qty × price = amount? Do the
   lines sum to the subtotal? Does subtotal + tax = total? Every check comes
   back `pass`, `fail`, or `skipped` — skipped isn't an error, it's what
   happens when an invoice legitimately has no line items to check.

| Check | What it recomputes |
|---|---|
| `is_invoice` | The model's own classification of the document |
| `critical_field_present` | Each field in `CRITICAL_FIELDS` actually has a value |
| `line_qty_times_price` | `qty × unit_price == amount`, per line |
| `lines_sum_to_subtotal` | `Σ line amounts == subtotal` |
| `tax_rate_times_base` | `rate × base == tax amount`, per tax line |
| `subtotal_plus_tax_equals_total` | `subtotal + Σ taxes == total_amount` |
| `currency_glyph_matches_declared` | A `€`/`£`/`₹` glyph on the page doesn't contradict the declared ISO currency |
| `invoice_date_within_window` | Invoice date isn't wildly far from the run date (±730 days) |
| `invoice_date_before_due_date` | `invoice_date <= due_date` when both exist |

4. **Route** (`route.py`) — turns all those checks into one of three
   outcomes: `clean` (ship it), `review` (a person should look), `blocked`
   (something's actually wrong, don't post it). Any failure that a human
   *can't* fix on their own (it's not an invoice, the total contradicts
   itself) blocks it. Anything just uncertain — a missing field, something
   illegible — goes to review instead of getting auto-rejected.
5. **CLI** (`cli.py`) — runs the whole pipeline over a folder, with one last
   structural sanity check before anything gets written out.
The guiding idea: a false "needs review" costs a person a couple minutes. A
false "clean" puts a wrong number into the accounting system. So the pipeline
is tuned to be paranoid in the direction that costs less.

## Output schema

One JSON document per input file. Trimmed real example (from
`output/01_maple_tech_invoice_sample.json`):

```jsonc
{
  "schema_version": "1.0",
  "doc": {
    "doc_id": "sha256:58a770be...",
    "source_file": "01_maple_tech_invoice_sample.pdf",
    "page_count": 1,
    "input_kind": "pdf_text_layer",       // pdf_text_layer | pdf_scanned | image
    "text_layer": true,
    "is_invoice": true
  },
  "fields": {
    "vendor_name":    {"value": "MAPLE TECH SOLUTIONS INC.", "status": "present", "source_text": "MAPLE TECH SOLUTIONS INC.", "page": 1},
    "invoice_number": {"value": "INV-2026-0842", "status": "present", "source_text": "INV-2026-0842", "page": 1},
    "invoice_date":   {"value": "2026-07-15", "status": "present", "source_text": "2026-07-15", "page": 1},
    "due_date":       {"value": "2026-08-14", "status": "present", "source_text": "2026-08-14", "page": 1},
    "currency":       {"value": "CAD", "status": "present", "source_text": "CAD", "page": 1},
    "subtotal":       {"value": "2450.00", "status": "present", "source_text": "$2,450.00", "page": 1},
    "total_amount":   {"value": "2816.89", "status": "present", "source_text": "$2,816.89", "page": 1}
  },
  "taxes": [
    {"label": "GST (5%)", "rate": "5", "amount": "122.50", "base": "subtotal", "source_text": "GST (5%) $122.50"}
  ],
  "line_items_status": "present",         // present | absent_by_design | extraction_failed
  "line_items": [
    {"index": 0, "sku": null, "description": "AI workstation setup and configuration",
     "qty": "1", "qty_unit": null, "unit_price": "1650.00", "amount": "1650.00"}
  ],
  "validations": [
    {"check": "subtotal_plus_tax_equals_total", "result": "pass", "scope": "document",
     "expected": "2816.89", "actual": "2816.89", "delta": "0.00", "severity": null, "note": "2 tax line(s) summed"}
  ],
  "review": {"required": false, "severity": "clean", "reasons": [], "notes": []},
  "provenance": {
    "model": "claude-haiku-4-5", "prompt_version": "p1", "attempts": 1,
    "tokens_in": 2359, "tokens_out": 761, "cost_usd": 0.012328, "latency_ms": 5380,
    "ingest_warnings": []
  }
}
```

### Field meanings

| Key | Meaning |
|---|---|
| `schema_version` | Version of this output shape; bump it if the shape changes |
| `doc` | Ingestion metadata: hash-based `doc_id`, how the file was read, whether it's an invoice at all |
| `fields.*` | The 7 scalar fields (`vendor_name`, `invoice_number`, `invoice_date`, `due_date`, `currency`, `subtotal`, `total_amount`). Each is `{value, status, source_text, page}` |
| `fields.*.status` | `present` (found) / `stated_absent` (document explicitly says there is none) / `missing` (not found, and it doesn't say why) / `illegible` (located but unreadable) / `extraction_failed` (should be present but the model couldn't produce a valid value) |
| `taxes[]` | One entry per printed tax line (an invoice can have several, e.g. GST + QST): `label`, `rate`, `amount`, `base` (what the rate applies to), `source_text` |
| `line_items_status` | `present` / `absent_by_design` (invoice legitimately has no itemised table) / `extraction_failed` (a table is visibly there but unreadable) |
| `line_items[]` | `sku`, `description`, `qty`, `qty_unit` (e.g. `"hrs"`, split out so `qty × unit_price` still equals `amount`), `unit_price`, `amount` |
| `validations[]` | One entry per check in the table above: `check`, `result` (`pass`/`fail`/`skipped`), `scope` (`document` or e.g. `line[0]`), `expected`/`actual`/`delta` when arithmetic, `severity` (`blocking`/`warning`, only on failures), `reason` (why a check was skipped), `note` (extra context) |
| `review` | The routing decision: `required` (bool), `severity` (`clean`/`review`/`blocked`), `reasons` (why, if not clean), `notes` (benign observations, e.g. "line_items absent_by_design") |
| `provenance` | Which model ran, prompt version (for eval comparability), retry `attempts`, token usage, an estimated `cost_usd`, latency, and any ingestion-time warnings |

## Evaluation

`score.py` compares predicted output against hand-labelled files in `gold/`.
Two families of metric, because they answer different questions:

### Online metrics 

These are what you'd actually watch on a live dashboard, since they don't
require gold labels at all — every document processed contributes to them.

- **Auto-approval rate** — the fraction of documents that route to `clean`
  (`required == false`). This is the throughput number: how much of the batch
  never needs a human.
- **False-clean rate** — the fraction of documents that were auto-approved
  (`clean`) but had at least one **critical field wrong**. This is the risk
  number: how often does a bad invoice slip straight through to payment. It's
  computed here against `gold/` because that's the only place we have ground
  truth to check against; in production you'd approximate it by periodically
  auditing a sample of the `clean` bucket by hand, since by definition nothing
  else flags those documents for a second look.

Auto-approval and false-clean pull against each other — routing everything to
`review` drives false-clean to zero and auto-approval to zero too. The router
in `route.py` is tuned to keep false-clean at zero even at the cost of
auto-approval (see the asymmetry argument above).

### Offline metrics

These require `gold/` and exist to tell you whether a prompt or model change
made extraction *itself* better or worse, independent of how the router
reacts to it.

- **Field accuracy** — of every scalar field gold has a value for, across all
  documents, what fraction did we extract the *same value*? (Money compared
  as `Decimal`, dates as parsed dates, vendor/customer names with fuzzy
  matching to tolerate `"Inc."` vs `"Inc"`-style noise.)
- **Status accuracy** — of the same fields, what fraction did we assign the
  *same status* (`present`/`missing`/`stated_absent`/...), independent of
  whether the value matched? Catches a model that gets the value right but
  mislabels *why* a field is empty, or vice versa.
- **Line exact precision** — of the line items *we produced* (after greedy
  matching each predicted line to a gold line by SKU or fuzzy description),
  what fraction are byte-for-byte correct on `qty`, `unit_price`, `amount`,
  and `qty_unit`?
- **Line exact recall** — of the line items *gold says should exist*, what
  fraction did we produce an exact match for? Precision answers "is what we
  extracted trustworthy"; recall answers "did we extract everything".

(`score.py` also reports `document_perfect_rate` — all critical fields
correct on a whole document — and `router_agreement` — did our severity match
gold's expected severity exactly, not just clean-vs-not. Both are in the
tables below but aren't separately defined per the metrics above since they're
composites of the same underlying checks.)

## Model comparison

### Accuracy

Three models have actually been run end-to-end against the 3-document gold
set (`score_logs/`):

| Model              | Provider | Field acc. | Status acc. | Doc perfect | Line precision | Line recall | Auto-approval | False-clean | Router agreement |
|--------------------|---|---|-------------|---|---|---|---|---|---|
| `claude-sonnet-5`  | Anthropic | 100% | 100%        | 100% | 100% | 100% | 100% | 0% | 100% |
| `claude-haiku-4-5` | Anthropic | 100% | 100%        | 100% | 100% | 100% | 100% | 0% | 100% |
| `gpt-5.6-luna`     | OpenAI | 100% | 100%        | 100% | 100% | 100% | 100% | 0% | 100% |
| `gpt-4o-mini`      | OpenAI | 100% | 100%        | 100% | 100% | 100% | 100% | 0% | 100% |


### Estimated cost per invoice

All three tested models are vision calls over a single rasterised page. Per
the ingestion settings in `ingest.py` (200 DPI, downscaled to ≤1568px), a
US-letter page renders at ~1212×1568px, which is where the ≈2,530 image-token
figure below comes from. Add the fixed prompt overhead and expected output,
and the per-invoice token budget used for every row in this table is:

- **Input:** ~2,530 image tokens + ~1,200 prompt tokens = **~3,730 tokens in**
- **Output:** **~800 tokens out**

| Model | Provider | $/M in | $/M out | Cost / invoice | Cost / 1,000 invoices |
|---|---|---|---------|----------------|-----------------------|
| `claude-haiku-4-5` | Anthropic | $1.00 | $5.00   | $0.0077        | $7.76                 |
| `claude-sonnet-5` | Anthropic | $2.00 | $10.00  | $0.0155        | $15.52                |
| `gpt-5.6-luna` | OpenAI | $0.20 | $1.80   | $0.0018        | $1.80                 |
| `gpt-4o-mini` | OpenAI | $0.15 | $0.60   | $0.004266      | $4.26                 |

### Verdict

Every model tested above posts a perfect score on every metric — which is a
statement about the eval, not about the models. At n=3, a hand-picked set of
clean, well-formed invoices, any competent model is expected to extract them
correctly; the accuracy table confirms the pipeline works end-to-end across
providers, it does not separate a good model from a great one. There's no
defensible way to pick a "winner" out of this table as it stands.

Getting a real answer means, in order:

1. **A bigger, harder, more representative gold set.** Not more of the same
   clean samples — invoices that look like actual production traffic: scans
   with real illegibility, multi-currency, unusual layouts, ambiguous or
   missing line-item detail, the edge cases the current 3 samples were
   picked to each demonstrate one of, but many more of them and many more
   variations.
2. **Only then do the accuracy metrics mean anything.** Once models actually
   disagree on field accuracy, status accuracy, line precision/recall,
   auto-approval, and false-clean rate, rank by **false-clean rate first** —
   it's the one metric that's a direct production-risk number (a wrong
   invoice paid automatically), not a quality-of-life number, so a model with
   *any* nonzero false-clean rate on a real dataset should lose to one
   without it regardless of everything else.
3. **Then break remaining ties on latency and cost.** The cost table above is
   the input for that, but latency isn't aggregated by `score.py` today —
   it's only recorded per document in `provenance.latency_ms`. Worth adding
   as an aggregate once step 1 exists, since a marginally more accurate model
   that's meaningfully slower or several times the cost may not be the right
   tradeoff for this workload's actual volume and turnaround requirements.

## Alternate approaches to explore

The current design (rasterise → single VLM call → deterministic validation)
is one point in a larger space. Three worth prototyping against it:

1. **OCR → text LLM**, instead of feeding page images directly to a vision
   model: run the page through an OCR engine (Tesseract, or a cloud OCR API)
   to get text + bounding boxes, then have a *text-only* LLM extract the
   schema from that. Usually cheaper per call and lets you cache/reuse OCR
   output across prompt iterations, but layout information (which number is
   under which column header) has to be reconstructed from OCR's reading
   order or box coordinates instead of being read directly off the image —
   exactly the kind of table-binding problem `pdfplumber`'s `layout=True`
   extraction already fights with today. Worth it mainly if per-page cost at
   volume dominates over accuracy on tabular layouts.

2. **Purpose-built document AI** (Azure AI Document Intelligence, Google
   Document AI, AWS Textract): these ship a pretrained "invoice" model that
   already knows the common fields and returns them with real bounding boxes
   and per-field confidence scores — something this pipeline currently has no
   equivalent for (the model's own `source_text` string is a much weaker
   signal than an actual box). Worth prototyping specifically for the
   confidence scores, since they'd let `route.py` make graduated
   review-vs-clean decisions instead of the current binary
   present/missing/illegible signal. The tradeoff is flexibility: extending
   the schema (a new tax type, a non-invoice document class) means retraining
   or waiting on the vendor, versus editing `prompt.py`.

3. **Text path and vision path in parallel, then diff.** Run the same
   document through both a text-layer-only extraction (with `pdfplumber`'s
   already-extracted text, when a usable layer exists) and the current
   vision extraction, then compare the two at the field level. Where they
   agree, confidence is much higher than either path alone; where they
   disagree, that disagreement *is* the review signal — replacing the current
   provenance check (which was cut for being too narrow: it could only ever
   fail loudly on hallucination, never confirm correctness) with something
   that produces a genuine agree/disagree verdict per field. Only applies to
   `pdf_text_layer` documents, not scans or images, so it would sit alongside
   the vision-only path rather than replace it.

## Things I'd explore if I had more time

1. **End-to-end testing of the application.** There's no automated test suite
   today — correctness has been checked by eyeballing a 3-document smoke test
   through `score.py`. I'd add unit tests for the sharp edges the code itself
   already calls out (`money.py`'s locale-ambiguous number parsing, the
   greedy line-matching and fuzzy-name matching in `score.py`, the Decimal
   tolerances in `validate.py`), plus an integration test that runs `cli.py`
   against a fixture directory with the provider call mocked, so the ingest →
   extract → validate → route wiring is checked on every change without
   spending real API calls.

2. **A real UI instead of a CLI.** `review_queue.json` is meant for a human,
   but today that human reads raw JSON. I'd build a minimal reviewer view:
   the page image next to the extracted fields, the `focus` entries from
   `route.review_item()` highlighted directly on the fields they concern, and
   an approve/correct action — which would also be the natural way to grow
   the gold set over time instead of hand-writing it.

3. **Explore the alternate approaches discussed above** — OCR → text-LLM, a
   purpose-built document AI vendor, and especially the parallel
   text-path/vision-path disagreement check, since it's the most direct
   replacement for the provenance check that got cut for being too narrow
   (it could only ever fail loudly on a hallucination, never actually confirm
   a field was read correctly).

4. **Other gaps found while building this, in rough priority order:**
   - **Retry/backoff on transient provider errors.** `extract.py` only
     retries a malformed-JSON reply; a rate limit or a transient 5xx from
     either provider currently fails the whole document outright.
   - **Aggregate cost and latency in `score.py`,** not just per-document in
     `provenance` — needed before the Verdict's cost/latency tie-break above
     is actually usable on a real run rather than by hand.
   - **Concurrency in `cli.py`.** Invoices are processed one at a time,
     sequentially; any real batch volume would want the API calls
     parallelized, with the per-file `try/except` already in place making
     that a reasonably safe change.
   - **A durable store instead of a folder of JSON files.** `doc_id` (the
     sha256 hash) is already a natural dedup key, but nothing is backed by a
     database — re-running, auditing, and querying "everything blocked last
     week" all mean grepping files by hand today.
   - **Per-field confidence scores, not just the four-state status enum.**
     Ties directly into the document-AI alternative above, and would let
     `route.py` make a graduated review-vs-clean decision instead of today's
     binary present/missing/illegible signal.

