"""Decimal-safe parsing for money, rates and dates.

Amounts are Decimal internally and strings at the boundaries. Floats are
never used: 1450 * 0.0825 in binary float is 119.62499999999999, which
rounds the wrong way.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")

# ROUND_HALF_UP is deliberate and load-bearing. One sample invoice lands on an
# exact half-cent tie (1450 * 0.0825 = 119.625, printed as 119.63). Python's
# Decimal default is ROUND_HALF_EVEN, which gives 119.62 and fails a clean
# document.
ROUNDING = ROUND_HALF_UP

# '$' is deliberately unmapped: USD, CAD and AUD all use it, so seeing one can
# only ever falsify a non-dollar declaration.
SYMBOL_TO_ISO = {"€": "EUR", "£": "GBP", "₹": "INR"}
CURRENCY_GLYPHS = "$€£¥₹"

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d %b %Y", "%b %d, %Y")


def q2(x: Decimal) -> Decimal:
    """Quantize to cents, half-up."""
    return x.quantize(CENT, rounding=ROUNDING)


def fmt(x) -> str | None:
    """Render a Decimal as a two-decimal string for JSON output."""
    return None if x is None else str(q2(x))


def parse_money(raw) -> Decimal | None:
    """Parse an invoice amount. Returns None rather than raising, so one bad
    cell cannot abort a whole document.

    Locale rule: if both ',' and '.' appear, the rightmost is the decimal
    mark. A lone ',' is a decimal mark when 1-2 digits follow and a thousands
    separator when 3 do. A lone '.' is always a decimal mark, which misreads
    German '1.450' as 1.45 -- a known gap.
    """
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw

    s = str(raw).strip()
    if not s or s.lower() in {"-", "--", "n/a", "none"}:
        return None

    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    if s.startswith("-"):
        negative, s = True, s[1:]

    s = re.sub(r"[^\d.,]", "", s)      # drop symbols, letters, spaces
    if not s:
        return None

    if "," in s and "." in s:
        dec = "," if s.rindex(",") > s.rindex(".") else "."
        s = s.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif "," in s:
        tail = len(s) - s.rindex(",") - 1
        s = s.replace(",", "." if tail in (1, 2) else "")

    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return -value if negative else value


def parse_rate(raw) -> Decimal | None:
    """Parse a tax rate. '8.25%' and '0.0825' both give Decimal('0.0825')."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    value = parse_money(s.rstrip("%"))
    if value is None:
        return None
    # A bare number above 1 is a percentage written without the sign.
    return value / 100 if s.endswith("%") or value > 1 else value


def parse_date(raw) -> date | None:
    """Parse a date. ISO-8601 is the expected input."""
    if raw is None:
        return None
    s = str(raw).strip()
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None