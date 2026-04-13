# ingestion/extractor.py
import io
import logging
import re
from typing import Dict, List

import pytesseract
from PIL import Image
from kreuzberg import (
    ExtractionConfig,
    ImageExtractionConfig,
    LayoutDetectionConfig,
    OutputFormat,
    extract_bytes,
)

logger = logging.getLogger(__name__)

OCR_DIRECT_TYPES   = {"image/png", "image/jpeg", "image/jpg", "image/tiff", "image/bmp", "image/webp"}
OCR_TEXT_THRESHOLD = 50

# ── Compiled patterns for markdown annotation ─────────────────────────────────
_MD_TABLE_ROW_RE  = re.compile(r'^\s*\|')           # GFM pipe-table row
_MD_HEADING_RE    = re.compile(r'^(#{1,6})\s+(.+)$')
_MD_IMAGE_RE      = re.compile(r'!\[([^\]]*)\]\([^\)]*\)')
_MD_CODE_FENCE_RE = re.compile(r'^```')

# Heuristic heading detection for plain OCR/pdfplumber text
_PLAIN_HEADING_RE = re.compile(
    r'^(\d+[\.\d]*\s+[A-Z][^\n]{3,60}|[A-Z][A-Z\s]{4,40}|#{1,3}\s+.+)$',
    re.MULTILINE,
)
import os 

# ── Shared kreuzberg config: markdown output + layout + image placeholders ────
_KREUZBERG_CONFIG = ExtractionConfig(
    output_format=OutputFormat.MARKDOWN,          # tables → | col |, images → ![alt](src)
    images=ImageExtractionConfig(
        extract_images=True,
        inject_placeholders=True,                  # embeds ![description](...) markers
    ),
    layout=LayoutDetectionConfig(
        apply_heuristics=True,                     # table-boundary detection
    ),
    include_document_structure=True,
)


class DocumentExtractor:
    def __init__(self, ocr_enabled: bool = True, ocr_lang: str = "eng"):
        self.ocr_enabled = ocr_enabled
        self.ocr_lang    = ocr_lang

    # ── Public API ────────────────────────────────────────────────────────────

    async def extract(self, content: bytes, content_type: str) -> Dict:
        """
        Flat extraction — backward compatible.
        Returns: { text, method, success, error, metadata }
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

    async def extract_pages(self, content: bytes, content_type: str) -> List[Dict]:
        """
        Returns list of {"page_num": N, "text": "..."} per logical page.
        All paths produce text with semantic structure tags:
          <HEADING level="N">…</HEADING>
          <TABLE>| col | col |\\n…</TABLE>
          <IMAGE description="…"/>
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
                text   = self._annotate_plain_text_headings(text)
            else:
                text = ""
            return [{"page_num": 1, "text": text}] if text.strip() else []

        # ── DOCX ──────────────────────────────────────────────────────────────
        if "word" in mime or mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            return await self._docx_pages(content, mime)

        # ── PPTX ──────────────────────────────────────────────────────────────
        if "presentation" in mime or "powerpoint" in mime:
            return await self._pptx_pages(content)

        # ── XLSX / CSV ────────────────────────────────────────────────────────
        if "spreadsheet" in mime or "excel" in mime or "csv" in mime:
            result = await self._kreuzberg(content, mime)
            text   = result.get("text", "")
            return [{"page_num": 1, "text": text}] if text.strip() else []

        # ── TXT / MD / HTML ───────────────────────────────────────────────────
        if mime.startswith("text/"):
            try:
                raw = content.decode("utf-8", errors="replace")
            except Exception:
                raw = ""
            # For HTML/Markdown, route through kreuzberg for richer annotation
            if mime in {"text/html", "text/markdown"}:
                result = await self._kreuzberg(content, mime)
                text   = result.get("text", "")
            else:
                text = self._annotate_plain_text_headings(raw)
            return self._split_by_headings(text)

        # ── Fallback ─────────────────────────────────────────────────────────
        result = await self._kreuzberg(content, mime)
        text   = result.get("text", "")
        return self._split_by_headings(text) if text.strip() else []

    # ── Structure annotation ──────────────────────────────────────────────────

    def _annotate_markdown_structure(self, text: str, saved_image_paths: List[str] = None) -> str:
        """
        Walk kreuzberg's markdown output line-by-line and wrap structural elements.
        Injects local image paths if provided.
        """
        saved_image_paths = saved_image_paths or []
        img_idx = 0
        
        lines    = text.split('\n')
        output:    List[str] = []
        table_buf: List[str] = []
        in_table  = False
        in_code   = False

        for raw in lines:
            stripped = raw.strip()

            # ── Code fences: pass through verbatim ────────────────────────
            if _MD_CODE_FENCE_RE.match(stripped):
                if in_table:
                    output += ['<TABLE>'] + table_buf + ['</TABLE>']
                    table_buf, in_table = [], False
                in_code = not in_code
                output.append(raw)
                continue

            if in_code:
                output.append(raw)
                continue

            # ── GFM pipe-table row ─────────────────────────────────────────
            if stripped.startswith('|') and _MD_TABLE_ROW_RE.match(stripped):
                if not in_table:
                    in_table = True
                    table_buf = []
                table_buf.append(raw)
                continue

            # Flush completed table block
            if in_table:
                output += ['<TABLE>'] + table_buf + ['</TABLE>']
                table_buf, in_table = [], False

            # ── ATX heading ────────────────────────────────────────────────
            hm = _MD_HEADING_RE.match(stripped)
            if hm:
                level, heading_text = len(hm.group(1)), hm.group(2).strip()
                output.append(f'<HEADING level="{level}">{heading_text}</HEADING>')
                continue

            # ── Inline image placeholders (UPDATED TO INJECT PATHS) ────────
            if _MD_IMAGE_RE.search(raw):
                def replace_img(m):
                    nonlocal img_idx
                    desc = m.group(1) or ""
                    # If we have a saved path for this image, inject it
                    if img_idx < len(saved_image_paths):
                        path = saved_image_paths[img_idx]
                        img_idx += 1
                        return f'<IMAGE path="{path}" description="{desc}" />'
                    # Fallback if no path is available
                    return f'<IMAGE description="{desc}" />' if desc else '<IMAGE />'
                
                annotated = _MD_IMAGE_RE.sub(replace_img, raw)
                output.append(annotated)
                continue

            output.append(raw)

        # Flush any trailing table
        if in_table and table_buf:
            output += ['<TABLE>'] + table_buf + ['</TABLE>']

        return '\n'.join(output)

    def _annotate_plain_text_headings(self, text: str) -> str:
        """
        Lightweight annotation for plain OCR / pdfplumber text where
        kreuzberg markdown is not available. Detects headings only.
        """
        def _tag(m: re.Match) -> str:
            return f'<HEADING>{m.group(0).strip()}</HEADING>'
        return _PLAIN_HEADING_RE.sub(_tag, text)

    # ── Page splitting ────────────────────────────────────────────────────────

    def _split_by_headings(self, text: str, max_chars: int = 3000) -> List[Dict]:
        """
        Split annotated markdown at top-level section boundaries
        (<HEADING level="1"> or level="2">). Falls back to character
        splitting when no headings exist.
        """
        if not text.strip():
            return []

        parts = re.split(r'(?=<HEADING level="[12]">)', text)
        pages: List[Dict] = []
        page_num = 1
        buf = ""

        for part in parts:
            if len(buf) + len(part) > max_chars and buf.strip():
                pages.append({"page_num": page_num, "text": buf.strip()})
                page_num += 1
                buf = part
            else:
                buf += part

        if buf.strip():
            pages.append({"page_num": page_num, "text": buf.strip()})

        return pages or self._split_flat_text(text)

    def _split_flat_text(self, text: str, chars_per_page: int = 3000) -> List[Dict]:
        """Fallback: split flat text into fixed-size pseudo-pages."""
        if not text.strip():
            return []
        pages = []
        for i in range(0, len(text), chars_per_page):
            chunk = text[i:i + chars_per_page].strip()
            if chunk:
                pages.append({"page_num": i // chars_per_page + 1, "text": chunk})
        return pages

    # ── Format-specific page extractors ──────────────────────────────────────

    async def _pdf_pages(self, content: bytes) -> List[Dict]:
        """
        Attempt order:
          1. kreuzberg   — fastest; works well on text-based PDFs
          2. pdfplumber  — page-accurate text extraction for text-based PDFs
          3. OCR         — pdf2image + Tesseract for scanned / image-only PDFs
        """
        # ── Attempt 1: kreuzberg ──────────────────────────────────────────────
        kreuz_result = await self._kreuzberg(content, "application/pdf")
        if kreuz_result["success"]:
            total_chars = len(kreuz_result["text"].strip())
            if total_chars >= OCR_TEXT_THRESHOLD:
                pages = self._split_by_headings(kreuz_result["text"])
                if pages:
                    logger.info(f"PDF kreuzberg: {len(pages)} pages ({total_chars} chars)")
                    return pages
            logger.info(f"kreuzberg returned {total_chars} chars — trying pdfplumber")
        else:
            logger.warning(f"kreuzberg failed ({kreuz_result.get('error')}) — trying pdfplumber")

        # ── Attempt 2: pdfplumber (text-based PDF) ────────────────────────────
        try:
            import pdfplumber

            pages = []
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""
                    if text.strip():
                        annotated = self._annotate_plain_text_headings(text)
                        pages.append({"page_num": i + 1, "text": annotated})

            total_chars = sum(len(p["text"]) for p in pages)
            if pages and total_chars >= OCR_TEXT_THRESHOLD * max(len(pages), 1):
                logger.info(f"pdfplumber extracted {len(pages)} pages ({total_chars} chars)")
                return pages

            logger.info(f"pdfplumber got {total_chars} chars — likely scanned PDF, switching to OCR")

        except ImportError:
            logger.warning("pdfplumber not installed — skipping to OCR")
        except Exception as e:
            logger.warning(f"pdfplumber failed: {e} — trying OCR")

        # ── Attempt 3: scanned PDF → pdf2image + Tesseract ───────────────────
        if self.ocr_enabled:
            return await self._pdf_ocr_pages(content)

        logger.error("All PDF extraction methods failed and OCR is disabled")
        return []

    async def _pdf_ocr_pages(self, content: bytes) -> List[Dict]:
        """pdf2image + Tesseract — one page dict per PDF page."""
        try:
            from pdf2image import convert_from_bytes

            images = convert_from_bytes(content, dpi=300)
            pages  = []
            for i, img in enumerate(images):
                try:
                    raw  = pytesseract.image_to_string(img, lang=self.ocr_lang, config="--psm 3")
                    text = self._annotate_plain_text_headings(raw)
                    if text.strip():
                        pages.append({"page_num": i + 1, "text": text})
                except Exception as pe:
                    logger.warning(f"OCR page {i + 1} failed: {pe}")
            logger.info(f"PDF OCR: {len(pages)} pages")
            return pages

        except ImportError:
            logger.error("pdf2image not installed — pip install pdf2image")
            return []
        except Exception as e:  
            logger.error(f"PDF OCR pages failed: {e}")
            return []

    async def _docx_pages(self, content: bytes, mime: str) -> List[Dict]:
        """
        DOCX extraction via kreuzberg (uses pandoc internally → clean markdown).
        Tables, images and headings are annotated by _kreuzberg().
        The annotated markdown is then split at heading boundaries.
        """
        result = await self._kreuzberg(content, mime)
        if not result["success"] or not result["text"].strip():
            logger.warning(f"kreuzberg DOCX failed: {result.get('error')} — no content")
            return []
        return self._split_by_headings(result["text"])

    async def _pptx_pages(self, content: bytes) -> List[Dict]:
        """
        Attempt order:
          1. kreuzberg    — fastest; handles most PPTX files well
          2. python-pptx  — native slide-by-slide extraction with table support
        """
        PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

        # ── Attempt 1: kreuzberg ──────────────────────────────────────────────
        kreuz_result = await self._kreuzberg(content, PPTX_MIME)
        if kreuz_result["success"] and kreuz_result["text"].strip():
            pages = self._split_by_headings(kreuz_result["text"])
            if pages:
                logger.info(f"PPTX kreuzberg: {len(pages)} logical pages")
                return pages
            logger.info("kreuzberg returned text but no pages — trying python-pptx")
        else:
            logger.warning(f"kreuzberg PPTX failed ({kreuz_result.get('error')}) — trying python-pptx")

        # ── Attempt 2: python-pptx (slide-accurate extraction) ───────────────
        try:
            from pptx import Presentation
            from pptx.enum.shapes import PP_PLACEHOLDER

            prs   = Presentation(io.BytesIO(content))
            pages = []

            for i, slide in enumerate(prs.slides):
                blocks: List[str] = []

                for shape in slide.shapes:
                    # ── Table shape ────────────────────────────────────────
                    if shape.has_table:
                        table    = shape.table
                        tbl_text = "<TABLE>\n"
                        for row in table.rows:
                            cells = [
                                (cell.text_frame.text if cell.text_frame else "")
                                .replace('\n', ' ').strip()
                                for cell in row.cells
                            ]
                            tbl_text += " | ".join(cells) + "\n"
                        tbl_text += "</TABLE>"
                        blocks.append(tbl_text)

                    # ── Text / title shapes ────────────────────────────────
                    elif hasattr(shape, "text") and shape.text.strip():
                        try:
                            if (shape.is_placeholder and
                                    shape.placeholder_format.type in (
                                        PP_PLACEHOLDER.TITLE,
                                        PP_PLACEHOLDER.CENTER_TITLE,
                                    )):
                                blocks.append(f'<HEADING level="1">{shape.text.strip()}</HEADING>')
                            else:
                                blocks.append(shape.text.strip())
                        except Exception:
                            blocks.append(shape.text.strip())

                if blocks:
                    pages.append({"page_num": i + 1, "text": "\n\n".join(blocks)})

            logger.info(f"PPTX python-pptx: {len(pages)} slides extracted")
            return pages

        except ImportError:
            logger.warning("python-pptx not installed — pip install python-pptx")
        except Exception as e:
            logger.warning(f"python-pptx failed: {e}")

        logger.error("All PPTX extraction methods failed")
        return []

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _fallback_kreuzberg_pages(self, content: bytes, mime: str) -> List[Dict]:
        """Last resort: kreuzberg markdown → heading-based split."""
        result = await self._kreuzberg(content, mime)
        text   = result.get("text", "")
        return self._split_by_headings(text) if text.strip() else []

    # ── Core extraction strategies ────────────────────────────────────────────

    async def _kreuzberg(self, content: bytes, mime: str) -> Dict:
        try:
            extracted = await extract_bytes(content, mime_type=mime, config=_KREUZBERG_CONFIG)

            if not extracted:
                logger.error("kreuzberg returned None")
                return {
                    "text": "",
                    "images_bytes": [],
                    "success": False,
                    "error": "Empty extraction"
                }
            
            if isinstance(extracted, dict):
                raw_text = extracted.get("content") or extracted.get("text") or ""
                images_list = extracted.get("images", [])
            else:
                raw_text = getattr(extracted, "content", "") or getattr(extracted, "text", "")
                images_list = getattr(extracted, "images", [])

            # Extract image bytes safely
            images_bytes = []
            for img in images_list or []:
                try:
                    if isinstance(img, dict):
                        img_bytes = img.get("data") or img.get("content") or img.get("bytes")
                    else:
                        img_bytes = getattr(img, "data", None)

                    if img_bytes:
                        images_bytes.append(img_bytes)
                except Exception as e:
                    logger.warning(f"Image parse failed: {e}")

            placeholders = [f"img_{i}" for i in range(len(images_bytes))]
            annotated = self._annotate_markdown_structure(raw_text, placeholders)

            return {
                "text": annotated or "",
                "images_bytes": images_bytes,
                "success": True,
                "error": None
            }

        except Exception as e:
            logger.error(f"kreuzberg failed: {e}", exc_info=True)
            return {
                "text": "",
                "images_bytes": [],
                "success": False,
                "error": str(e)
            }

    async def _image_ocr(self, content: bytes) -> Dict:
        try:
            image = Image.open(io.BytesIO(content))
            text  = pytesseract.image_to_string(image, lang=self.ocr_lang, config="--psm 3")
            return {
                "text":     text,
                "method":   "tesseract_ocr",
                "success":  True,
                "error":    None,
                "metadata": {"size": image.size, "mode": image.mode, "format": image.format},
            }
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
                    pages.append(
                        f"--- Page {i + 1} ---\n"
                        f"{pytesseract.image_to_string(img, lang=self.ocr_lang, config='--psm 3')}"
                    )
                except Exception as pe:
                    logger.warning(f"OCR page {i + 1} failed: {pe}")
                    pages.append(f"--- Page {i + 1} ---\n[OCR Failed]")

            return {
                "text":     "\n\n".join(pages),
                "method":   "pdf2image+tesseract_ocr",
                "success":  True,
                "error":    None,
                "metadata": {"pages": len(images)},
            }
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
    """Returns [{"page_num": N, "text": "..."}] per logical page."""
    extractor = DocumentExtractor(ocr_enabled=True)

    result = await extractor._kreuzberg(content, content_type)

    pages = extractor._split_by_headings(result.get("text", ""))

    return {
        "pages": pages,
        "images_bytes": result.get("images_bytes", [])
    }