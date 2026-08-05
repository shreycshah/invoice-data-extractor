"""The output schema: enums, a field template, and shape invariants.

No pydantic. `check_shape` is 40 lines of readable assertions, which is
enough to catch the failures that actually happen -- floats leaking into
amounts, and a 'present' field with no value behind it.
"""

from __future__ import annotations

SCHEMA_VERSION = "1.0"

# 'missing', 'stated_absent' and 'extraction_failed' are three different
# operational situations. Collapsing them into null throws away what a
# reviewer needs: "the invoice says there is no due date" is not the same as
# "we could not find the due date".
FIELD_STATUSES = {"present", "stated_absent", "missing", "extraction_failed", "illegible"}
LINE_ITEMS_STATUSES = {"present", "absent_by_design", "extraction_failed"}
RESULTS = {"pass", "fail", "skipped"}
REVIEW_SEVERITIES = {"clean", "review", "blocked"}

SCALAR_FIELDS = (
    "vendor_name", "invoice_number", "invoice_date",
    "due_date", "currency", "subtotal", "total_amount",
)

# Fields whose loss makes the document unusable for payment.
CRITICAL_FIELDS = ("vendor_name", "invoice_number", "invoice_date", "currency", "total_amount")
MONEY_FIELDS = ("subtotal", "total_amount")
DATE_FIELDS = ("invoice_date", "due_date")


def empty_field(status: str = "missing") -> dict:
    return {"value": None, "status": status, "source_text": None, "page": None}


def check_shape(doc: dict) -> list[str]:
    """Return a list of invariant violations. Empty means structurally sound."""
    errors = []
    fields = doc.get("fields") or {}

    for name in SCALAR_FIELDS:
        f = fields.get(name)
        if not isinstance(f, dict):
            errors.append(f"fields.{name} missing")
            continue
        status, value = f.get("status"), f.get("value")
        if status not in FIELD_STATUSES:
            errors.append(f"fields.{name}.status invalid: {status!r}")
        if value is not None and not isinstance(value, str):
            errors.append(f"fields.{name}.value must be a string, got {type(value).__name__}")
        if status == "present" and (value in (None, "") or not f.get("source_text")):
            errors.append(f"fields.{name} is present but has no value or no source_text")
        if status in {"missing", "extraction_failed", "illegible"} and value is not None:
            errors.append(f"fields.{name} is {status} but has a value")

    if doc.get("line_items_status") not in LINE_ITEMS_STATUSES:
        errors.append(f"line_items_status invalid: {doc.get('line_items_status')!r}")
    lines = doc.get("line_items")
    if not isinstance(lines, list):
        errors.append("line_items must be a list")
    else:
        if lines and doc.get("line_items_status") != "present":
            errors.append("line_items non-empty but line_items_status is not 'present'")
        for i, line in enumerate(lines):
            for key in ("qty", "unit_price", "amount"):
                if line.get(key) is not None and not isinstance(line[key], str):
                    errors.append(f"line_items[{i}].{key} must be a string")

    for i, tax in enumerate(doc.get("taxes") or []):
        for key in ("rate", "amount"):
            if tax.get(key) is not None and not isinstance(tax[key], str):
                errors.append(f"taxes[{i}].{key} must be a string")

    for i, v in enumerate(doc.get("validations") or []):
        if v.get("result") not in RESULTS:
            errors.append(f"validations[{i}].result invalid: {v.get('result')!r}")
        if v.get("result") == "skipped" and not v.get("reason"):
            errors.append(f"validations[{i}] is skipped but gives no reason")

    if (doc.get("review") or {}).get("severity") not in REVIEW_SEVERITIES:
        errors.append("review.severity invalid")

    return errors