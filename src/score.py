#!/usr/bin/env python3
"""Score predictions against the hand-written gold labels.

    python score.py --pred out --gold gold

Reports field accuracy and line-item precision/recall, plus the two numbers
that matter operationally: auto-approval rate, and false-clean rate (a
document we passed that had a critical error). Router expectations in the gold
files are treated as hard assertions -- a pipeline that extracts perfectly and
then flags every document is still broken.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from money import parse_date, parse_money, parse_rate
from schema import CRITICAL_FIELDS, DATE_FIELDS, MONEY_FIELDS, SCALAR_FIELDS

DESC_THRESHOLD = 0.85
VENDOR_THRESHOLD = 0.90
_SUFFIXES = re.compile(r"\b(inc|llc|ltd|gmbh|plc|co|corp|sa|nv|bv|ag|pty)\b\.?", re.I)

logger = logging.getLogger(__name__)


def norm(s) -> str:
    """Collapse whitespace and casefold, for tolerant string comparison."""
    return re.sub(r"\s+", " ", str(s or "")).strip().casefold()


def norm_vendor(s) -> str:
    """Normalise a name for fuzzy matching by dropping legal suffixes and punctuation."""
    return re.sub(r"[^a-z0-9 ]", "", _SUFFIXES.sub("", norm(s))).strip()


def ratio(a: str, b: str) -> float:
    """Fuzzy string similarity in [0, 1]."""
    return difflib.SequenceMatcher(None, a, b).ratio()


def value_matches(name: str, gold_field: dict, pred: str | None) -> bool:
    """Does the predicted value match gold for this field, using the right comparison per field type?"""
    gold = gold_field.get("value")
    if gold is None:
        return pred is None
    if pred is None:
        return False
    if name in MONEY_FIELDS:
        g, p = parse_money(gold), parse_money(pred)
        return g is not None and g == p
    if name in DATE_FIELDS:
        return parse_date(gold) == parse_date(pred)
    if name == "currency":
        return norm(gold).upper() == norm(pred).upper()

    accepted = [gold, *gold_field.get("accept", [])]
    if name in ("vendor_name", "customer_name"):
        return any(ratio(norm_vendor(a), norm_vendor(pred)) >= VENDOR_THRESHOLD for a in accepted)
    return any(norm(a) == norm(pred) for a in accepted)


def match_lines(gold: list, pred: list) -> list[tuple[dict, dict]]:
    """Greedy one-to-one matching: SKU exact first, then description fuzzy."""
    pairs, pool = [], list(pred)
    for g in gold:
        best, score = None, 0.0
        for p in pool:
            if g.get("sku") and p.get("sku") and norm(g["sku"]) == norm(p["sku"]):
                best, score = p, 1.0
                break
            s = ratio(norm(g.get("description")), norm(p.get("description")))
            if s > score:
                best, score = p, s
        if best is not None and score >= DESC_THRESHOLD:
            pairs.append((g, best))
            pool.remove(best)
    return pairs


def line_exact(g: dict, p: dict) -> bool:
    """Is this matched line item byte-for-byte correct on qty, unit_price, amount, and qty_unit?"""
    for key in ("qty", "unit_price", "amount"):
        gv, pv = parse_money(g.get(key)), parse_money(p.get(key))
        if (gv is None) != (pv is None) or (gv is not None and gv != pv):
            return False
    return norm(g.get("qty_unit")) == norm(p.get("qty_unit"))


def taxes_match(gold: list, pred: list) -> bool:
    """Do the predicted tax lines match gold's, as a set (order-independent, by amount and rate)?"""
    if len(gold) != len(pred):
        return False
    pool = list(pred)
    for g in gold:
        hit = next((p for p in pool
                    if parse_money(g.get("amount")) == parse_money(p.get("amount"))
                    and parse_rate(g.get("rate")) == parse_rate(p.get("rate"))), None)
        if hit is None:
            return False
        pool.remove(hit)
    return True


def score_one(gold: dict, pred: dict) -> dict:
    """Score one document against its gold label: field/line/route correctness plus error detail."""
    fields, errors = {}, []
    for name in SCALAR_FIELDS:
        g = (gold.get("fields") or {}).get(name)
        if g is None:
            continue
        p = (pred.get("fields") or {}).get(name, {})
        ok = value_matches(name, g, p.get("value"))
        status_ok = g.get("status") == p.get("status")
        fields[name] = {"value_ok": ok, "status_ok": status_ok}
        if not ok:
            errors.append(f"{name}: expected {g.get('value')!r}, got {p.get('value')!r}")
        elif not status_ok:
            errors.append(f"{name}: status expected {g.get('status')!r}, got {p.get('status')!r}")

    gold_lines, pred_lines = gold.get("line_items", []), pred.get("line_items", [])
    pairs = match_lines(gold_lines, pred_lines)

    if gold.get("line_items_status") != pred.get("line_items_status"):
        errors.append(f"line_items_status: expected {gold.get('line_items_status')!r}, "
                      f"got {pred.get('line_items_status')!r}")
    if not taxes_match(gold.get("taxes", []), pred.get("taxes", [])):
        errors.append(f"taxes: expected {len(gold.get('taxes', []))} matching, "
                      f"got {len(pred.get('taxes', []))}")

    got_severity = (pred.get("review") or {}).get("severity")
    want_severity = (gold.get("expected_review") or {}).get("severity")

    mismatches = []
    outcomes: dict[str, set] = {}
    for v in pred.get("validations", []):
        outcomes.setdefault(v["check"], set()).add(v["result"])
    for check, want in (gold.get("expected_validations") or {}).items():
        got = outcomes.get(check)
        if got is None:
            mismatches.append(f"{check}: never ran (expected {want})")
        elif want == "pass" and got != {"pass"}:
            mismatches.append(f"{check}: expected all pass, got {sorted(got)}")
        elif want != "pass" and want not in got:
            mismatches.append(f"{check}: expected {want}, got {sorted(got)}")

    return {
        "source_file": gold.get("source_file"),
        "field_total": len(fields),
        "field_correct": sum(1 for f in fields.values() if f["value_ok"]),
        "status_correct": sum(1 for f in fields.values() if f["status_ok"]),
        "critical_ok": all(fields[n]["value_ok"] for n in CRITICAL_FIELDS if n in fields),
        "lines_gold": len(gold_lines),
        "lines_pred": len(pred_lines),
        "lines_exact": sum(1 for g, p in pairs if line_exact(g, p)),
        "got_severity": got_severity,
        "want_severity": want_severity,
        "router_ok": got_severity == want_severity,
        "blocked_wrongly": bool((gold.get("expected_review") or {}).get("must_not_block"))
                           and got_severity == "blocked",
        "validation_mismatches": mismatches,
        "errors": errors,
    }


def frac(a, b) -> float | None:
    """a / b, or None (not zero) when there's nothing to divide by."""
    return None if not b else a / b


# (aggregate key, printed label, is a fraction shown as a percentage)
AGGREGATE_LABELS = [
    ("documents", "documents", False),
    ("field_accuracy", "field accuracy", True),
    ("status_accuracy", "status accuracy", True),
    ("document_perfect_rate", "document perfect rate", True),
    ("line_exact_precision", "line exact precision", True),
    ("line_exact_recall", "line exact recall", True),
    ("auto_approval_rate", "auto-approval rate", True),
    ("false_clean_rate", "false-clean rate", True),
    ("router_agreement", "router agreement", True),
]


def score_all(gold_dir: Path, pred_dir: Path) -> tuple[list[dict], set[str | None]]:
    """Score every gold file that has a matching prediction."""
    rows, models = [], set()
    for gold_path in sorted(gold_dir.glob("*.json")):
        gold = json.loads(gold_path.read_text())
        pred_path = pred_dir / f"{Path(gold['source_file']).stem}.json"
        if not pred_path.exists():
            logger.warning("no prediction for %s, skipping", gold["source_file"])
            continue
        pred = json.loads(pred_path.read_text())
        models.add((pred.get("provenance") or {}).get("model"))
        rows.append(score_one(gold, pred))
    return rows, models


def aggregate(rows: list[dict]) -> tuple[dict, list[str]]:
    """Roll per-document rows into the numbers that matter, plus hard failures."""
    n = len(rows)
    pred_lines = sum(r["lines_pred"] for r in rows)
    gold_lines = sum(r["lines_gold"] for r in rows)
    exact = sum(r["lines_exact"] for r in rows)
    clean = [r for r in rows if r["got_severity"] == "clean"]
    false_clean = [r for r in clean if not r["critical_ok"]]

    metrics = {
        "documents": n,
        "field_accuracy": frac(sum(r["field_correct"] for r in rows),
                                sum(r["field_total"] for r in rows)),
        "status_accuracy": frac(sum(r["status_correct"] for r in rows),
                                 sum(r["field_total"] for r in rows)),
        "document_perfect_rate": frac(sum(1 for r in rows if r["critical_ok"]), n),
        "line_exact_precision": frac(exact, pred_lines),
        "line_exact_recall": frac(exact, gold_lines),
        "auto_approval_rate": frac(len(clean), n),
        "false_clean_rate": frac(len(false_clean), n),
        "router_agreement": frac(sum(1 for r in rows if r["router_ok"]), n),
    }

    hard = []
    if any(r["blocked_wrongly"] for r in rows):
        hard.append("a document that must not block was blocked")
    if false_clean:
        hard.append(f"{len(false_clean)} document(s) passed clean with a critical error")
    return metrics, hard


def print_report(rows: list[dict], metrics: dict, hard: list[str]) -> None:
    """Print the per-document breakdown, the aggregate table, and any hard-failure assertions."""
    print("\nPer document\n" + "-" * 68)
    for r in rows:
        good = r["critical_ok"] and r["router_ok"] and not r["validation_mismatches"]
        print(f"{'ok ' if good else 'BAD'} {r['source_file']}")
        print(f"      fields {r['field_correct']}/{r['field_total']}   "
              f"lines {r['lines_exact']}/{r['lines_gold']} exact   "
              f"route {r['got_severity']} (want {r['want_severity']})")
        for e in r["errors"] + r["validation_mismatches"]:
            print(f"      - {e}")

    print("\nAggregate\n" + "-" * 68)
    for key, label, is_pct in AGGREGATE_LABELS:
        value = metrics[key]
        shown = ("n/a" if value is None else f"{value:.1%}") if is_pct else str(value)
        print(f"  {label:<27}{shown}")

    print()
    for h in hard:
        print(f"FAIL: {h}")
    if not hard:
        print("hard assertions passed")
    print(f"\nn={metrics['documents']}. Too small for confidence intervals; treat as a smoke test.")


def write_log(log_dir: Path, *, pred_dir: Path, gold_dir: Path,
              models: set[str | None], metrics: dict, hard: list[str], rows: list[dict]) -> Path:
    """Write this run's aggregate metrics and per-document rows to a timestamped JSON log."""
    model = next(iter(models)) if len(models) == 1 else sorted(m for m in models if m)
    timestamp = datetime.now(timezone.utc)
    log = {
        "timestamp": timestamp.isoformat(),
        "model": model,
        "pred_dir": str(pred_dir),
        "gold_dir": str(gold_dir),
        "aggregate": metrics,
        "hard_failures": hard,
        "per_document": rows,
    }
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}.json"
    log_path.write_text(json.dumps(log, indent=2))
    return log_path


def main(argv=None) -> int:
    """CLI entry point: score a prediction directory against gold and report the result."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", type=Path, default=Path("out"))
    ap.add_argument("--gold", type=Path, default=Path("gold"))
    ap.add_argument("--log-dir", type=Path, default=Path("score_logs"),
                     help="where to write the per-run JSON log (default: score_logs/)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    rows, models = score_all(args.gold, args.pred)
    if not rows:
        print("nothing to score")
        return 1

    metrics, hard = aggregate(rows)
    print_report(rows, metrics, hard)

    log_path = write_log(args.log_dir, pred_dir=args.pred, gold_dir=args.gold,
                          models=models, metrics=metrics, hard=hard, rows=rows)
    print(f"\nwrote {log_path}")

    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main())