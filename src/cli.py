#!/usr/bin/env python3
"""CLI entry point for the invoice pipeline: ingest -> vision -> VLM -> validate -> route.

Runs every supported file in a directory (or a single file) through the full
pipeline, writes one JSON document per input, and prints a summary table.

    python cli.py <input_dir> <output_dir> [--model NAME]
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()   # before the extract import below, which reads INVOICE_MODEL at import time

from extract import DEFAULT_MODEL, extract
from ingest import SUPPORTED, ingest
from route import review_item, route
from schema import check_shape
from validate import run_checks

logger = logging.getLogger(__name__)


def targets(path: Path) -> list[Path]:
    """A directory of invoices, or a single file."""
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(path)
    return sorted(p for p in path.iterdir() if p.suffix.lower() in SUPPORTED)


def process(path: Path, output_dir: Path, model: str) -> dict:
    """Run one document through ingest -> extract -> validate -> route and write its JSON."""
    ingested = ingest(path)

    doc = extract(ingested, model=model)

    doc["validations"] = run_checks(doc, symbols_seen=ingested.symbols_seen)

    doc["review"] = route(doc)

    # A malformed output is a loud failure, not a silent bad row.
    problems = check_shape(doc)
    if problems:
        logger.warning("%s: schema invariant violation(s): %s", path.name, "; ".join(problems))
        doc["review"]["severity"] = "blocked"
        doc["review"]["required"] = True
        doc["review"]["reasons"] = [f"schema invariant: {p}" for p in problems] + doc["review"]["reasons"]

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{path.stem}.json").write_text(json.dumps(doc, indent=2))
    return doc


def main(argv=None) -> int:
    """CLI entry point: process every matching file in a directory and print a summary."""
    ap = argparse.ArgumentParser(description="Extract invoices to structured JSON.")
    ap.add_argument("input_dir", type=Path, help="directory of invoices (or a single file)")
    ap.add_argument("output_dir", type=Path, help="where to write the JSON outputs")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"vision model (default: {DEFAULT_MODEL})")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    files = targets(args.input_dir)
    logger.info("processing %d file(s) from %s", len(files), args.input_dir)

    rows, queue, failures = [], [], 0
    for target in files:
        logger.info("processing %s", target.name)
        try:
            doc = process(target, args.output_dir, args.model)
            rows.append((target.name, doc["review"]["severity"], len(doc["review"]["reasons"])))
            if doc["review"]["required"]:
                queue.append(review_item(doc))
        except Exception as exc:            # one bad file must not stop the batch
            failures += 1
            rows.append((target.name, f"ERROR: {type(exc).__name__}", 0))
            logger.exception("failed to process %s", target.name)

    if queue:
        (args.output_dir / "review_queue.json").write_text(json.dumps(queue, indent=2))

    width = max((len(r[0]) for r in rows), default=10)
    print(f"\n{'document'.ljust(width)}  outcome    reasons")
    print("-" * (width + 21))
    for name, severity, count in rows:
        print(f"{name.ljust(width)}  {severity:<9}  {count}")
    if rows:
        clean = sum(1 for _, s, _ in rows if s == "clean")
        print(f"\nauto-approved {clean}/{len(rows)} ({clean / len(rows):.0%})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())