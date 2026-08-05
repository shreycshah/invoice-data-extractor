# Invoice Data Extractor : Setup & Usage

Pipeline: ingest a PDF/image invoice -> extract structured fields via a vision
model -> validate the arithmetic deterministically -> route to clean /
review / blocked.

## 1. Clone the project

```bash
git clone <repo-url>
cd invoice-data-extractor
```

## 2. Install dependencies

Python 3.11+. A virtual environment is recommended:

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

## 3. Set your API key

Extraction calls a vision model from either Anthropic or OpenAI — whichever
one the `--model` you pass belongs to (`claude-...` -> Anthropic, `gpt-...` /
`o1-...` / `o3-...` / `o4-...` -> OpenAI). Create a `.env` file in the project
root with the key(s) for whichever provider(s) you plan to use:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

`.env` is already in `.gitignore` — it will not be committed.

Optional overrides (also via `.env` or the shell environment):

| Variable            | Purpose                                          | Default          |
|----------------------|---------------------------------------------------|-------------------|
| `INVOICE_MODEL`      | default model when `--model` is not passed        | `claude-sonnet-5` |
| `INVOICE_COST_IN`    | $ per 1M input tokens, for the cost estimate       | `2.00`            |
| `INVOICE_COST_OUT`   | $ per 1M output tokens, for the cost estimate      | `10.00`           |

## 4. Run the extractor (`cli.py`)

Run from the project root, with `src/` on the Python path:

```bash
PYTHONPATH=src python3 src/cli.py <input_dir> <output_dir> [--model MODEL]
```

`<input_dir>` can be a directory of invoices or a single file. Supported
types: `.pdf`, `.png`, `.jpg`/`.jpeg`, `.webp`, `.gif`, `.tif`/`.tiff`.

Example, using the bundled samples:

```bash
python3 src/cli.py input_dir output_dir
python3 src/cli.py input_dir input_dir --model gpt-4o
```

**Where to look for the output:**

- `<output_dir>/<filename>.json` — one structured document per input file
  (fields, line items, taxes, validation results, review decision).
- `<output_dir>/review_queue.json` — written only if at least one document
  needs a human look; a compact list of what to check and why.
- The terminal prints a one-line-per-document summary table plus the overall
  auto-approval rate.

## 5. Score against gold labels (`score.py`)

Compares the JSON in your output directory against hand-labelled files in
`gold/` and reports accuracy, line-item precision/recall, auto-approval rate,
and false-clean rate.

```bash
PYTHONPATH=src python3 src/score.py --pred output --gold gold
```

**Where to look for the output:**

- The terminal prints a per-document pass/fail breakdown, the aggregate
  metrics table, and any hard-failure assertions.
- `score_logs/<timestamp>.json` — every run also writes a timestamped log
  with the same numbers, so results are comparable across runs. Use
  `--log-dir` to change where that goes.