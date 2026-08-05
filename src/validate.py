"""Deterministic validation. The model extracts; this module decides.

Two rules govern everything here:

1. Nothing is taken on the model's word. Every arithmetic claim is recomputed
   in Decimal from the extracted parts.
2. Every check returns pass, fail, or *skipped*. A check that cannot run
   because the document legitimately lacks its inputs must skip, not fail. One
   sample invoice has no line items by design; a validator that fails it is
   worse than one that does not run.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from money import SYMBOL_TO_ISO, fmt, parse_date, parse_money, parse_rate, q2
from schema import CRITICAL_FIELDS

LINE_TOL = Decimal("0.01")   # per-line arithmetic
AGG_TOL = Decimal("0.02")    # cross-total arithmetic, absorbs line rounding
DATE_WINDOW_DAYS = 730

# Blocking is reserved for defects a person reading the page cannot fix: it is
# not an invoice, or the total contradicts its own components. A missing or
# illegible field is exactly what review exists for, so it warns.
BLOCKING = "blocking"
WARNING = "warning"


def _r(check, outcome, scope="document", *, expected=None, actual=None,
       delta=None, severity=None, reason=None, note=None) -> dict:
    """Build one validation-result dict in the shared check/result/scope shape."""
    out = {"check": check, "result": outcome, "scope": scope}
    if expected is not None:
        out["expected"] = fmt(expected) if isinstance(expected, Decimal) else expected
    if actual is not None:
        out["actual"] = fmt(actual) if isinstance(actual, Decimal) else actual
    if delta is not None:
        out["delta"] = fmt(abs(delta))
    out["severity"] = severity if outcome == "fail" else None
    if reason:
        out["reason"] = reason
    if note:
        out["note"] = note
    return out


def _val(doc, name):
    """Get a field's extracted value from the document."""
    return (doc.get("fields", {}).get(name) or {}).get("value")


def _status(doc, name):
    """Get a field's status from the document, defaulting to 'missing'."""
    return (doc.get("fields", {}).get(name) or {}).get("status", "missing")


# --- checks ----------------------------------------------------------------


def _is_invoice(doc):
    """Fail (blocking) if the model didn't classify this document as an invoice."""
    if doc.get("doc", {}).get("is_invoice") is False:
        return [_r("is_invoice", "fail", severity=BLOCKING, note="not identified as an invoice")]
    return [_r("is_invoice", "pass")]


def _critical_fields(doc):
    """Check that every field in CRITICAL_FIELDS has status 'present'."""
    out = []
    for name in CRITICAL_FIELDS:
        st = _status(doc, name)
        out.append(
            _r("critical_field_present", "pass", scope=f"fields.{name}") if st == "present"
            else _r("critical_field_present", "fail", scope=f"fields.{name}",
                    severity=WARNING, note=f"status={st}")
        )
    return out


def _line_arithmetic(doc):
    """qty * unit_price == amount, per line."""
    st = doc.get("line_items_status")
    if st != "present":
        return [_r("line_qty_times_price", "skipped", reason=f"line_items_status={st}")]

    out = []
    for i, line in enumerate(doc.get("line_items", [])):
        scope = f"line[{i}]"
        qty, price = parse_money(line.get("qty")), parse_money(line.get("unit_price"))
        amount = parse_money(line.get("amount"))
        if qty is None or price is None or amount is None:
            out.append(_r("line_qty_times_price", "skipped", scope=scope,
                          reason="qty, unit_price or amount not parseable"))
            continue
        expected = q2(qty * price)
        delta = expected - amount
        note = (f"qty carried unit {line['qty_unit']!r}; arithmetic uses the numeric part"
                if line.get("qty_unit") else None)
        out.append(_r("line_qty_times_price", "pass" if abs(delta) <= LINE_TOL else "fail",
                      scope=scope, expected=expected, actual=amount, delta=delta,
                      severity=WARNING, note=note))
    return out


def _lines_sum(doc):
    """Sum of line amounts should equal the subtotal."""
    st = doc.get("line_items_status")
    if st != "present":
        return [_r("lines_sum_to_subtotal", "skipped", reason=f"line_items_status={st}")]
    subtotal = parse_money(_val(doc, "subtotal"))
    if subtotal is None:
        return [_r("lines_sum_to_subtotal", "skipped", reason="subtotal not available")]

    amounts = [parse_money(l.get("amount")) for l in doc.get("line_items", [])]
    if not amounts or any(a is None for a in amounts):
        return [_r("lines_sum_to_subtotal", "skipped", reason="a line amount is missing")]

    total = q2(sum(amounts, Decimal(0)))
    delta = total - subtotal
    # A warning, not blocking: a genuine invoice may carry shipping, a discount
    # or a rounding line that is not itemised.
    return [_r("lines_sum_to_subtotal", "pass" if abs(delta) <= AGG_TOL else "fail",
               expected=subtotal, actual=total, delta=delta, severity=WARNING)]


def _tax_arithmetic(doc):
    """rate * base == tax amount, per tax line."""
    taxes = doc.get("taxes") or []
    if not taxes:
        return [_r("tax_rate_times_base", "skipped", reason="no taxes on document")]

    subtotal = parse_money(_val(doc, "subtotal"))
    out = []
    for i, tax in enumerate(taxes):
        scope = f"tax[{i}]"
        rate, amount = parse_rate(tax.get("rate")), parse_money(tax.get("amount"))
        if rate is None or amount is None:
            out.append(_r("tax_rate_times_base", "skipped", scope=scope,
                          reason="rate or amount not parseable"))
            continue
        base = subtotal if (tax.get("base") or "subtotal") == "subtotal" else parse_money(tax.get("base_amount"))
        if base is None:
            out.append(_r("tax_rate_times_base", "skipped", scope=scope,
                          reason="tax base not available"))
            continue
        expected = q2(base * rate)
        delta = expected - amount
        out.append(_r("tax_rate_times_base", "pass" if abs(delta) <= AGG_TOL else "fail",
                      scope=scope, expected=expected, actual=amount, delta=delta,
                      severity=WARNING))
    return out


def _total(doc):
    """subtotal + sum(taxes) == total. Blocking: if the payable figure cannot
    be explained by its own components, nobody should pay it."""
    subtotal, total = parse_money(_val(doc, "subtotal")), parse_money(_val(doc, "total_amount"))
    if total is None:
        return [_r("subtotal_plus_tax_equals_total", "skipped", reason="total_amount missing")]
    if subtotal is None:
        return [_r("subtotal_plus_tax_equals_total", "skipped", reason="subtotal missing")]

    tax_amounts = [parse_money(t.get("amount")) for t in (doc.get("taxes") or [])]
    if any(a is None for a in tax_amounts):
        return [_r("subtotal_plus_tax_equals_total", "skipped",
                   reason="a tax amount is not parseable")]

    expected = q2(subtotal + sum(tax_amounts, Decimal(0)))
    delta = expected - total
    return [_r("subtotal_plus_tax_equals_total", "pass" if abs(delta) <= AGG_TOL else "fail",
               expected=expected, actual=total, delta=delta, severity=BLOCKING,
               note=f"{len(tax_amounts)} tax line(s) summed")]


def _currency(doc, symbols_seen):
    """Cross-check any currency glyph seen on the page against the declared ISO code."""
    currency = _val(doc, "currency")
    if not currency:
        return [_r("currency_glyph_matches_declared", "skipped", reason="currency missing")]
    if len(currency) != 3 or not currency.isalpha():
        return [_r("currency_glyph_matches_declared", "fail", expected="ISO-4217",
                   actual=currency, severity=WARNING, note="not a 3-letter code")]
    for symbol in symbols_seen or set():
        # '$' is unmapped on purpose: it cannot distinguish USD from CAD, so it
        # can never contradict a declaration.
        implied = SYMBOL_TO_ISO.get(symbol)
        if implied and implied != currency.upper():
            return [_r("currency_glyph_matches_declared", "fail", expected=implied,
                       actual=currency.upper(), severity=WARNING,
                       note=f"glyph {symbol!r} implies {implied}")]
    return [_r("currency_glyph_matches_declared", "pass", actual=currency.upper())]


def _dates(doc, today):
    """Invoice date should be recent, and no later than the due date."""
    inv, due = parse_date(_val(doc, "invoice_date")), parse_date(_val(doc, "due_date"))
    out = []

    if inv is None:
        out.append(_r("invoice_date_within_window", "skipped", reason="invoice_date missing"))
    else:
        drift = abs((today - inv).days)
        out.append(_r("invoice_date_within_window",
                      "pass" if drift <= DATE_WINDOW_DAYS else "fail",
                      actual=inv.isoformat(), severity=WARNING,
                      note=f"{drift} days from run date"))

    if due is None:
        # The common case: the document says there is no due date, which is
        # valid and must not be penalised.
        out.append(_r("invoice_date_before_due_date", "skipped",
                      reason=f"due_date {_status(doc, 'due_date')}"))
    elif inv is None:
        out.append(_r("invoice_date_before_due_date", "skipped", reason="invoice_date missing"))
    else:
        out.append(_r("invoice_date_before_due_date", "pass" if inv <= due else "fail",
                      expected=f">= {inv.isoformat()}", actual=due.isoformat(), severity=WARNING))
    return out


def run_checks(doc: dict, symbols_seen: set[str] | None = None,
               today: date | None = None) -> list[dict]:
    """Run every check and return the flat list of results."""
    today = today or date.today()
    return (
        _is_invoice(doc)
        + _critical_fields(doc)
        + _line_arithmetic(doc)
        + _lines_sum(doc)
        + _tax_arithmetic(doc)
        + _total(doc)
        + _currency(doc, symbols_seen)
        + _dates(doc, today)
    )