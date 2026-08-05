"""Ingest: identify the document and produce page images.

Rasterisation is unconditional -- vision is the extraction path. The text
layer is probed anyway, because the currency glyph cross-check needs it, and
because it's how a scanned PDF is told apart from one with real text.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from PIL import Image

from money import CURRENCY_GLYPHS

RASTER_DPI = 200          # legible small print without wasting tokens
MAX_EDGE_PX = 1568        # the vision API downsamples above this anyway
MIN_TEXT_CHARS = 200      # below any real invoice, above stray scan headers

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff"}
SUPPORTED = {".pdf"} | IMAGE_SUFFIXES


@dataclass
class Ingested:
    doc_id: str
    source_file: str
    input_kind: str                  # pdf_text_layer | pdf_scanned | image
    page_images: list[bytes]         # PNG bytes, one per page
    page_text: str | None            # None when there is no usable text layer
    symbols_seen: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    def meta(self) -> dict:
        """Metadata block for the output document's `doc` field."""
        return {
            "doc_id": self.doc_id,
            "source_file": self.source_file,
            "page_count": len(self.page_images),
            "input_kind": self.input_kind,
            "text_layer": self.page_text is not None,
            "is_invoice": None,      # set by the extractor
        }


def _to_png(data: bytes) -> bytes:
    """Normalise to PNG, shrinking if oversized.

    Always re-encodes: page_images must be PNG because that is the media_type
    extract.py declares. Passing a JPEG through unchanged would mislabel it.
    """
    with Image.open(io.BytesIO(data)) as im:
        if max(im.size) > MAX_EDGE_PX:
            scale = MAX_EDGE_PX / max(im.size)
            im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
        out = io.BytesIO()
        im.convert("RGB").save(out, "PNG")
        return out.getvalue()


def _pdf_text(path: Path) -> str | None:
    """Text layer if there is a usable one, else None.

    layout=True preserves column alignment, which is what binds a quantity to
    its rate. Naive extract_text() reads in DOM order and scrambles tables.
    """
    try:
        with pdfplumber.open(str(path)) as pdf:
            text = "\n".join(p.extract_text(layout=True) or "" for p in pdf.pages)
    except Exception:
        return None
    return text if len(text.strip()) >= MIN_TEXT_CHARS else None


def ingest(path: str | Path) -> Ingested:
    """Read one file and produce page images plus whatever text-layer evidence exists."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"Unsupported input type: {suffix}")

    if suffix == ".pdf":
        text = _pdf_text(path)
        pdf = pdfium.PdfDocument(str(path))
        images = [_to_png(_render(pdf, i)) for i in range(len(pdf))]
        kind = "pdf_text_layer" if text else "pdf_scanned"
    else:
        text, kind = None, "image"
        images = [_to_png(path.read_bytes())]

    return Ingested(
        doc_id="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        source_file=path.name,
        input_kind=kind,
        page_images=images,
        page_text=text,
        symbols_seen=set(re.findall(f"[{CURRENCY_GLYPHS}]", text)) if text else set(),
        warnings=[] if text else ["No text layer: currency glyph cross-check will skip."],
    )


def _render(pdf, i: int) -> bytes:
    """Rasterise one PDF page to PNG bytes at RASTER_DPI."""
    buf = io.BytesIO()
    pdf[i].render(scale=RASTER_DPI / 72).to_pil().save(buf, "PNG")
    return buf.getvalue()