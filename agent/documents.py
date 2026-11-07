"""Load an uploaded document -> one Page per page: PNG image + ALL extracted text.

- PDF: each page is rendered to an image; text comes from the PDF text layer
  (PyMuPDF's get_text, which returns every text object on the page, including
  text that is invisible when rendered: white-on-white, tiny, off-page, under
  an image). If a page has no text layer and OCR is enabled, it is OCR'd.
- Image (png/jpg/...): normalized to PNG; text comes from OCR if enabled.

The baseline does NOT filter or mark hidden text. It reaches the model as-is.
This module is format-generic: it assumes nothing about which documents
exist or what fields they contain.
"""

from __future__ import annotations

import base64
import hashlib
import io
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from PIL import Image

PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


@dataclass
class Page:
    page_number: int  # 1-based
    image_png: bytes = field(repr=False)
    image_size: tuple[int, int]
    text: str
    text_source: str  # "pdf_text_layer" | "ocr" | "none"

    @property
    def image_data_url(self) -> str:
        return "data:image/png;base64," + base64.b64encode(self.image_png).decode("ascii")

    def summary(self) -> dict:
        """What goes into the trace (no image bytes)."""
        return {
            "page_number": self.page_number,
            "image_size": list(self.image_size),
            "image_sha256": hashlib.sha256(self.image_png).hexdigest(),
            "text_source": self.text_source,
            "text_chars": len(self.text),
            "text": self.text,
        }


@dataclass
class Document:
    path: Path
    kind: str  # "pdf" | "image"
    sha256: str
    total_pages: int
    pages: list[Page]

    @property
    def name(self) -> str:
        return self.path.name

    def summary(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.name,
            "kind": self.kind,
            "sha256": self.sha256,
            "total_pages": self.total_pages,
            "pages_loaded": len(self.pages),
            "pages": [p.summary() for p in self.pages],
        }


class UnsupportedDocumentError(ValueError):
    pass


def _ocr(img: Image.Image, engine: str, lang: str) -> str | None:
    if engine == "none":
        return None
    if engine == "tesseract":
        try:
            import pytesseract
        except ImportError as e:
            raise RuntimeError("OCR_ENGINE=tesseract needs `pip install pytesseract` and the tesseract binary") from e
        import os
        if os.getenv("TESSERACT_CMD"):  # e.g. C:\\Program Files\\Tesseract-OCR\\tesseract.exe on Windows
            pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]
        return pytesseract.image_to_string(img, lang=lang)
    raise ValueError(f"Unknown OCR_ENGINE {engine!r} (use 'none' or 'tesseract')")


def _fit(img: Image.Image, max_side: int) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_side:
        return img
    scale = max_side / max(w, h)
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def _to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def load_document(
    path: str | Path,
    *,
    dpi: int = 150,
    max_image_side: int = 2000,
    max_pages: int = 10,
    ocr_engine: str = "none",
    ocr_lang: str = "eng",
) -> Document:
    path = Path(path)
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    suffix = path.suffix.lower()

    if suffix in PDF_SUFFIXES:
        pages: list[Page] = []
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            total = pdf.page_count
            for i in range(min(total, max_pages)):
                pg = pdf[i]
                pix = pg.get_pixmap(dpi=dpi, alpha=False)
                img = _fit(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"), max_image_side)
                # NFKC: PDF text layers often store shaped Arabic as presentation forms
                # (e.g. "ﻧﻤﻮذج") and Latin ligatures ("ﬁ"); normalize to plain letters.
                text = unicodedata.normalize("NFKC", pg.get_text("text") or "")
                source = "pdf_text_layer" if text.strip() else "none"
                if not text.strip():
                    ocr = _ocr(img, ocr_engine, ocr_lang)
                    if ocr is not None:
                        text, source = ocr, "ocr"
                pages.append(Page(i + 1, _to_png(img), img.size, text, source))
        return Document(path, "pdf", sha, total, pages)

    if suffix in IMAGE_SUFFIXES:
        with Image.open(io.BytesIO(data)) as raw:
            img = _fit(raw.convert("RGB"), max_image_side)
        ocr = _ocr(img, ocr_engine, ocr_lang)
        text, source = (ocr, "ocr") if ocr is not None else ("", "none")
        return Document(path, "image", sha, 1, [Page(1, _to_png(img), img.size, text, source)])

    raise UnsupportedDocumentError(f"Unsupported document type {suffix!r}: {path}")
