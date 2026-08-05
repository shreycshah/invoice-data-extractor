"""Routing: turn validation results into one of three outcomes.

    blocked  -- do not post. The document is defective.
    review   -- post only after a person looks. Something is uncertain.
    clean    -- post automatically.

The asymmetry is deliberate. A false flag costs a few minutes of reviewer
time; a false clean puts wrong data in the accounting system. So anything
ambiguous routes to review, and only a fully verified document goes clean.
"""

from __future__ import annotations

from schema import CRITICAL_FIELDS

UNCERTAIN_STATUSES = {"extraction_failed", "illegible"}


def route(doc: dict) -> dict:
    """Turn a document's validations and field statuses into a clean/review/blocked verdict."""
    blocking, review, notes = [], [], []

    for v in doc.get("validations", []):
        if v.get("result") != "fail":
            continue
        label = f"{v['check']} failed at {v.get('scope', 'document')}"
        detail = v.get("note") or (
            f"expected {v.get('expected')}, got {v.get('actual')}"
            if v.get("expected") is not None else None
        )
        if detail:
            label += f" ({detail})"
        (blocking if v.get("severity") == "blocking" else review).append(label)

    for name, field in doc.get("fields", {}).items():
        status = field.get("status")
        if status in UNCERTAIN_STATUSES:
            review.append(f"{name} is {status}")
        elif status == "missing" and name in CRITICAL_FIELDS:
            review.append(f"critical field {name} not found")
        elif status == "stated_absent":
            notes.append(f"{name} stated_absent")

    li_status = doc.get("line_items_status")
    if li_status == "absent_by_design":
        notes.append("line_items absent_by_design")
    elif li_status == "extraction_failed":
        review.append("line_items extraction_failed")

    severity = "blocked" if blocking else ("review" if review else "clean")
    return {
        "required": severity != "clean",
        "severity": severity,
        "reasons": blocking + review,
        "notes": notes,
    }


def review_item(doc: dict) -> dict:
    """Compact payload for a reviewer: what to look at, and why.

    Routing a person to a named field beats handing them the whole document.
    Bounding boxes would let a UI crop the exact region, but the vision path
    does not return reliable coordinates.
    """
    focus = [
        {"check": v["check"], "scope": v.get("scope"), "expected": v.get("expected"),
         "actual": v.get("actual"), "delta": v.get("delta")}
        for v in doc.get("validations", []) if v.get("result") == "fail"
    ]
    focus += [
        {"field": name, "status": f.get("status"), "source_text": f.get("source_text")}
        for name, f in doc.get("fields", {}).items() if f.get("status") in UNCERTAIN_STATUSES
    ]
    return {
        "doc_id": doc.get("doc", {}).get("doc_id"),
        "source_file": doc.get("doc", {}).get("source_file"),
        "severity": doc["review"]["severity"],
        "reasons": doc["review"]["reasons"],
        "focus": focus,
    }