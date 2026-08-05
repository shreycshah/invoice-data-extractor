"""Vision extraction: page images in, schema-shaped document out.

Sends one model call per document to whichever provider (Anthropic or
OpenAI) serves the requested model, with a single JSON-repair round if the
reply doesn't parse. After that it gives up cleanly rather than looping
against a model that is not going to comply.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time

from ingest import Ingested
from prompt import PROMPT_VERSION, REPAIR_PROMPT, SYSTEM_PROMPT, USER_PROMPT
from schema import SCALAR_FIELDS, SCHEMA_VERSION, empty_field

DEFAULT_MODEL = os.environ.get("INVOICE_MODEL", "claude-sonnet-5")
MAX_TOKENS = 4096

# Per million tokens, for the cost line in provenance. Defaults match Claude
# Sonnet 5's introductory rate; override for other models. Rough attribution,
# not accounting.
COST_IN = float(os.environ.get("INVOICE_COST_IN", "0.15"))
COST_OUT = float(os.environ.get("INVOICE_COST_OUT", "0.60"))

# The provider is inferred from the model name at call time, not selected by a
# separate flag: "--model gpt-4o" and "--model claude-sonnet-5" should just
# work without the CLI having to know which vendor serves which model.
_ANTHROPIC_PREFIXES = ("claude",)
_OPENAI_PREFIXES = ("gpt", "o1", "o3", "o4")

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")

logger = logging.getLogger(__name__)


class ExtractionError(RuntimeError):
    pass


def _loads(raw: str) -> dict:
    """Parse a reply that should be a bare JSON object."""
    s = _FENCE.sub("", raw.strip())
    if not s.startswith("{"):                 # a model that adds a preamble
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in response")
        s = s[start : end + 1]
    parsed = json.loads(s)
    if not isinstance(parsed, dict):
        raise ValueError("top-level JSON value is not an object")
    return parsed


def _provider(model: str) -> str:
    """Infer which vendor (anthropic/openai) serves this model name."""
    if model.startswith(_ANTHROPIC_PREFIXES):
        return "anthropic"
    if model.startswith(_OPENAI_PREFIXES):
        return "openai"
    raise ExtractionError(f"don't know which provider serves model {model!r}")


def _client(provider: str):
    """Build an authenticated SDK client for the given provider."""
    if provider == "anthropic":
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise ExtractionError("pip install anthropic") from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ExtractionError("ANTHROPIC_API_KEY is not set")
        return Anthropic()

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ExtractionError("pip install openai") from exc
    if not os.environ.get("OPENAI_API_KEY"):
        raise ExtractionError("OPENAI_API_KEY is not set")
    return OpenAI()


def _image_block(provider: str, png: bytes) -> dict:
    """Build one provider-specific image content block from a page PNG."""
    b64 = base64.standard_b64encode(png).decode()
    if provider == "anthropic":
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}


def _send(provider: str, client, model: str, messages: list[dict]) -> tuple[str, int, int]:
    """One request/response round-trip. Returns (raw_text, tokens_in, tokens_out)."""
    if provider == "anthropic":
        response = client.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT, messages=messages,
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        return text, response.usage.input_tokens, response.usage.output_tokens

    response = client.chat.completions.create(
        model=model, max_completion_tokens=MAX_TOKENS,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, *messages],
    )
    text = response.choices[0].message.content or ""
    return text, response.usage.prompt_tokens, response.usage.completion_tokens


def _call(doc: Ingested, model: str) -> tuple[dict, dict]:
    """Call the model, retrying once on invalid JSON, and return the payload plus usage stats."""
    provider = _provider(model)
    client = _client(provider)
    images = [_image_block(provider, p) for p in doc.page_images]
    messages = [{"role": "user", "content": images + [{"type": "text", "text": USER_PROMPT}]}]

    logger.info("extracting %s via %s (%s, %d page image(s))",
                doc.source_file, model, provider, len(images))
    started, tokens_in, tokens_out, last_error = time.time(), 0, 0, None
    for attempt in range(2):
        raw, t_in, t_out = _send(provider, client, model, messages)
        tokens_in += t_in
        tokens_out += t_out
        try:
            payload = _loads(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.warning("%s: model reply was not valid JSON (attempt %d), retrying: %s",
                            doc.source_file, attempt + 1, exc)
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": REPAIR_PROMPT.format(error=exc)},
            ]
            continue
        usage = {
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "attempts": attempt + 1,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": round(tokens_in / 1e6 * COST_IN + tokens_out / 1e6 * COST_OUT, 6),
            "latency_ms": int((time.time() - started) * 1000),
        }
        return payload, usage

    raise ExtractionError(f"model did not return valid JSON after 2 attempts: {last_error}")


def _num(raw) -> str | None:
    """Numbers must reach JSON as strings: a float total silently becomes
    119.62999999999999."""
    if raw is None:
        return None
    if isinstance(raw, float):
        return format(raw, ".2f")
    return str(raw).strip() or None


def _field(raw) -> dict:
    """Normalise a raw model field into the schema's {value, status, source_text, page} shape."""
    out = empty_field()
    if not isinstance(raw, dict):
        return out
    value = _num(raw.get("value")) if isinstance(raw.get("value"), (int, float)) else raw.get("value")
    if value is not None:
        value = str(value).strip() or None
    status = raw.get("status") or ("present" if value is not None else "missing")
    if status == "present" and value is None:
        # Surface this for review rather than let it pass as populated.
        status = "extraction_failed"
    out.update(value=value, status=status,
               source_text=raw.get("source_text") or None, page=raw.get("page"))
    return out


def to_document(payload: dict, doc: Ingested, usage: dict) -> dict:
    """Assemble the output document from a raw model payload."""
    meta = doc.meta()
    meta["is_invoice"] = bool(payload.get("is_invoice", True))
    raw_fields = payload.get("fields") or {}

    taxes = [
        {"label": t.get("label") or "Tax", "rate": _num(t.get("rate")),
         "amount": _num(t.get("amount")), "base": t.get("base") or "subtotal",
         "source_text": t.get("source_text")}
        for t in (payload.get("taxes") or []) if isinstance(t, dict)
    ]

    lines = [
        {"index": i, "sku": l.get("sku") or None,
         "description": (l.get("description") or "").strip() or None,
         "qty": _num(l.get("qty")), "qty_unit": l.get("qty_unit") or None,
         "unit_price": _num(l.get("unit_price")), "amount": _num(l.get("amount"))}
        for i, l in enumerate(payload.get("line_items") or []) if isinstance(l, dict)
    ]

    li_status = payload.get("line_items_status") or ("present" if lines else "extraction_failed")
    if lines:
        li_status = "present"        # keep the schema invariant true

    return {
        "schema_version": SCHEMA_VERSION,
        "doc": meta,
        "fields": {name: _field(raw_fields.get(name)) for name in SCALAR_FIELDS},
        "taxes": taxes,
        "line_items_status": li_status,
        "line_items": lines,
        "validations": [],
        "review": {"required": True, "severity": "review", "reasons": [], "notes": []},
        "provenance": {**usage, "ingest_warnings": doc.warnings},
    }


def extract(doc: Ingested, model: str | None = None) -> dict:
    """Extract one document: call the model and assemble the result into the output schema."""
    model = model or DEFAULT_MODEL
    payload, usage = _call(doc, model)
    return to_document(payload, doc, usage)