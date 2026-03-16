# ingestion/extractor.py
import io
import logging
import re
from typing import Dict, List

from kreuzberg import extract_bytes
import pytesseract
from PIL import Image

logger = logging.getLogger(__name__)

OCR_DIRECT_TYPES   = {"image/png", "image/jpeg", "image/jpg", "image/tiff", "image/bmp", "image/webp"}
OCR_TEXT_THRESHOLD = 50

HEADING_RE = re.compile(
    r'^(\d+[\.\d]*\s+[A-Z][^\n]{3,60}|'
    r'[A-Z][A-Z\s]{4,40}|'
    r'#{1,3}\s+.+)$',
    re.MULTILINE
)


class DocumentExtractor:
    def __init__(self, ocr_enabled: bool = True, ocr_lang: str = "eng"):
        self.ocr_enabled = ocr_enabled
        self.ocr_lang    = ocr_lang

    async def extract(self, content: bytes, content_type: str) -> Dict:
        """
        Returns: { text, method, success, error, metadata }
        Unchanged from original — flat text for backward compat.
        """
        mime = content_type.lower()

        if mime in OCR_DIRECT_TYPES:
            if self.ocr_enabled:
                return await self._image_ocr(content)
            return {"text": "", "method": "ocr_disabled", "success": False,
                    "error": "OCR disabled", "metadata": {}}

        kreuz = await self._kreuzberg(content, mime)

        if not self.ocr_enabled:
            return kreuz

        if mime == "application/pdf" and kreuz["success"] and len(kreuz["text"].strip()) < OCR_TEXT_THRESHOLD:
            logger.info("PDF minimal text → OCR upgrade attempt")
            ocr = await self._pdf_ocr(content)
            if ocr["success"] and len(ocr["text"]) > len(kreuz["text"]):
                ocr["method"] = "kreuzberg+ocr_fallback"
                return ocr

        if not kreuz["success"]:
            logger.warning(f"kreuzberg failed, trying OCR: {kreuz['error']}")
            if mime == "application/pdf":
                return await self._pdf_ocr(content)
            if mime in OCR_DIRECT_TYPES:
                return await self._image_ocr(content)

        return kreuz

    # ── NEW: page-aware extraction ────────────────────────────────────────────

    async def extract_pages(self, content: bytes, content_type: str) -> List[Dict]:
        """
        Returns list of {"page_num": N, "text": "..."} per logical page.

        Strategy per type:
          PDF (text-based)  → pdfplumber  (real page boundaries)
          PDF (scanned)     → pdf2image + tesseract (real page boundaries)
          image/*           → tesseract  (single page)
          DOCX              → kreuzberg flat → split every 30 paragraphs
          PPTX              → kreuzberg flat → split every slide marker
          XLSX/CSV          → kreuzberg flat → single page
          TXT/MD/HTML       → raw decode → split every ~3000 chars
          anything else     → kreuzberg flat → single page
        """
        mime = content_type.lower()

        # ── PDF ───────────────────────────────────────────────────────────────
        if mime == "application/pdf":
            return await self._pdf_pages(content)

        # ── Images ────────────────────────────────────────────────────────────
        if mime in OCR_DIRECT_TYPES:
            if self.ocr_enabled:
                result = await self._image_ocr(content)
                text   = result.get("text", "")
            else:
                text = ""
            return [{"page_num": 1, "text": text}] if text.strip() else []

        # ── DOCX ──────────────────────────────────────────────────────────────
        if "word" in mime or mime in {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}:
            return await self._docx_pages(content)

        # ── PPTX ──────────────────────────────────────────────────────────────
        if "presentation" in mime or "powerpoint" in mime:
            return await self._pptx_pages(content)

        # ── XLSX / CSV ────────────────────────────────────────────────────────
        if "spreadsheet" in mime or "excel" in mime or "csv" in mime:
            result = await self._kreuzberg(content, mime)
            text   = result.get("text", "")
            return [{"page_num": 1, "text": text}] if text.strip() else []

        # ── TXT / MD / HTML ───────────────────────────────────────────────────
        if mime in {"text/plain", "text/markdown", "text/html", "text/csv"} or mime.startswith("text/"):
            try:
                text = content.decode("utf-8", errors="replace")
            except Exception:
                text = ""
            return self._split_flat_text(text)

        # ── Fallback: kreuzberg flat → single or split ────────────────────────
        result = await self._kreuzberg(content, mime)
        text   = result.get("text", "")
        return self._split_flat_text(text)

    # ── PDF page extraction ───────────────────────────────────────────────────

    async def _pdf_pages(self, content: bytes) -> List[Dict]:
        """
        Try pdfplumber first (text PDFs).
        If total extracted text is too short → scanned PDF → OCR path.
        """
        pages = []

        # ── Attempt 1: pdfplumber (text-based PDF) ────────────────────────────
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""
                    if text.strip():
                        pages.append({"page_num": i + 1, "text": text})

            total_chars = sum(len(p["text"]) for p in pages)
            if total_chars >= OCR_TEXT_THRESHOLD * max(len(pages), 1):
                logger.info(f"pdfplumber extracted {len(pages)} pages ({total_chars} chars)")
                return pages if pages else self._fallback_kreuzberg_pages(content, "application/pdf")

            # Too little text → treat as scanned PDF
            logger.info(f"pdfplumber got {total_chars} chars — likely scanned PDF, switching to OCR")

        except ImportError:
            logger.warning("pdfplumber not installed — falling back to OCR for PDF pages")
        except Exception as e:
            logger.warning(f"pdfplumber failed: {e} — trying OCR")

        # ── Attempt 2: scanned PDF → pdf2image + tesseract ────────────────────
        if self.ocr_enabled:
            return await self._pdf_ocr_pages(content)

        # ── Attempt 3: kreuzberg flat fallback ────────────────────────────────
        return await self._fallback_kreuzberg_pages(content, "application/pdf")

    async def _pdf_ocr_pages(self, content: bytes) -> List[Dict]:
        """pdf2image + tesseract — one page dict per PDF page."""
        try:
            from pdf2image import convert_from_bytes
            images = convert_from_bytes(content, dpi=300)
            pages  = []
            for i, img in enumerate(images):
                try:
                    text = pytesseract.image_to_string(img, lang=self.ocr_lang, config="--psm 3")
                    if text.strip():
                        pages.append({"page_num": i + 1, "text": text})
                except Exception as pe:
                    logger.warning(f"OCR page {i+1} failed: {pe}")
            logger.info(f"PDF OCR extracted {len(pages)} pages")
            return pages
        except ImportError:
            logger.error("pdf2image not installed — pip install pdf2image")
            return []
        except Exception as e:
            logger.error(f"PDF OCR pages failed: {e}")
            return []

    # ── DOCX page extraction ──────────────────────────────────────────────────

    async def _docx_pages(self, content: bytes) -> List[Dict]:
        """
        Try python-docx first for paragraph-level grouping.
        Falls back to kreuzberg flat text.
        """
        try:
            import docx as python_docx
            doc    = python_docx.Document(io.BytesIO(content))
            paras  = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            if not paras:
                raise ValueError("No paragraphs extracted")

            # Group every 30 paragraphs as a logical "page"
            pages = []
            for i in range(0, len(paras), 30):
                text = "\n".join(paras[i:i+30])
                pages.append({"page_num": i // 30 + 1, "text": text})
            logger.info(f"DOCX extracted {len(pages)} logical pages from {len(paras)} paragraphs")
            return pages

        except ImportError:
            logger.warning("python-docx not installed — falling back to kreuzberg for DOCX")
        except Exception as e:
            logger.warning(f"python-docx failed: {e} — falling back to kreuzberg")

        return await self._fallback_kreuzberg_pages(content, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    # ── PPTX page extraction ──────────────────────────────────────────────────

    async def _pptx_pages(self, content: bytes) -> List[Dict]:
        """
        Try python-pptx — one page dict per slide.
        Falls back to kreuzberg flat.
        """
        try:
            from pptx import Presentation
            prs   = Presentation(io.BytesIO(content))
            pages = []
            for i, slide in enumerate(prs.slides):
                texts = []
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        texts.append(shape.text.strip())
                text = "\n".join(texts)
                if text:
                    pages.append({"page_num": i + 1, "text": text})
            logger.info(f"PPTX extracted {len(pages)} slides")
            return pages

        except ImportError:
            logger.warning("python-pptx not installed — falling back to kreuzberg for PPTX")
        except Exception as e:
            logger.warning(f"python-pptx failed: {e} — falling back to kreuzberg")

        return await self._fallback_kreuzberg_pages(content, "application/vnd.openxmlformats-officedocument.presentationml.presentation")

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _fallback_kreuzberg_pages(self, content: bytes, mime: str) -> List[Dict]:
        """Last resort: kreuzberg flat text split into pseudo-pages."""
        result = await self._kreuzberg(content, mime)
        text   = result.get("text", "")
        return self._split_flat_text(text)

    def _split_flat_text(self, text: str, chars_per_page: int = 3000) -> List[Dict]:
        """Split flat text into pseudo-pages of ~3000 chars each."""
        if not text.strip():
            return []
        pages = []
        for i in range(0, len(text), chars_per_page):
            chunk = text[i:i+chars_per_page].strip()
            if chunk:
                pages.append({"page_num": i // chars_per_page + 1, "text": chunk})
        return pages

    # ── Original private strategies (unchanged) ───────────────────────────────

    async def _kreuzberg(self, content: bytes, mime: str) -> Dict:
        try:
            extracted = await extract_bytes(content, mime_type=mime)
            text = (
                extracted.content if hasattr(extracted, "content")
                else extracted.text if hasattr(extracted, "text")
                else str(extracted)
            )
            metadata = getattr(extracted, "metadata", {})
            return {"text": text, "method": "kreuzberg", "success": True,
                    "error": None, "metadata": metadata}
        except Exception as e:
            logger.error(f"kreuzberg failed: {e}")
            return {"text": "", "method": "kreuzberg", "success": False,
                    "error": str(e), "metadata": {}}

    async def _image_ocr(self, content: bytes) -> Dict:
        try:
            image = Image.open(io.BytesIO(content))
            text  = pytesseract.image_to_string(image, lang=self.ocr_lang, config="--psm 3")
            return {"text": text, "method": "tesseract_ocr", "success": True, "error": None,
                    "metadata": {"size": image.size, "mode": image.mode, "format": image.format}}
        except Exception as e:
            logger.error(f"image OCR failed: {e}")
            return {"text": "", "method": "tesseract_ocr", "success": False,
                    "error": str(e), "metadata": {}}

    async def _pdf_ocr(self, content: bytes) -> Dict:
        """Original flat OCR — kept for backward compat with extract()."""
        try:
            from pdf2image import convert_from_bytes
            images = convert_from_bytes(content, dpi=300)
            pages  = []
            for i, img in enumerate(images):
                try:
                    pages.append(f"--- Page {i+1} ---\n{pytesseract.image_to_string(img, lang=self.ocr_lang, config='--psm 3')}")
                except Exception as pe:
                    logger.warning(f"OCR page {i+1} failed: {pe}")
                    pages.append(f"--- Page {i+1} ---\n[OCR Failed]")
            return {"text": "\n\n".join(pages), "method": "pdf2image+tesseract_ocr",
                    "success": True, "error": None, "metadata": {"pages": len(images)}}
        except ImportError:
            msg = "pdf2image not installed — pip install pdf2image"
            logger.error(msg)
            return {"text": "", "method": "pdf2image+tesseract_ocr", "success": False,
                    "error": msg, "metadata": {}}
        except Exception as e:
            logger.error(f"PDF OCR failed: {e}")
            return {"text": "", "method": "pdf2image+tesseract_ocr", "success": False,
                    "error": str(e), "metadata": {}}


# ── Convenience functions used by indexer ─────────────────────────────────────

async def extract_text(content: bytes, content_type: str) -> str:
    """Original flat text — backward compat. OCR enabled."""
    result = await DocumentExtractor(ocr_enabled=True).extract(content, content_type)
    return result.get("text", "")

async def extract_pages(content: bytes, content_type: str) -> List[Dict]:
    """NEW — returns [{"page_num": N, "text": "..."}] per page."""
    return await DocumentExtractor(ocr_enabled=True).extract_pages(content, content_type)
