import logging
from typing import Optional, Dict
from kreuzberg import extract_bytes
import pytesseract
from PIL import Image
import io

logger = logging.getLogger(__name__)

class DocumentExtractor:
    """Enhanced document text extraction with OCR support"""
    
    MIME_MAP = {
        "application/pdf": "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/plain": "text/plain"
        
    }
    
    OCR_SUPPORTED_TYPES = [
        "image/png", "image/jpeg", "image/jpg", 
        "image/tiff", "image/bmp", "image/webp"
    ]
    
    def __init__(self, ocr_enabled: bool = True, ocr_lang: str = "eng"):
        """
        Initialize extractor
        
        Args:
            ocr_enabled: Enable OCR for images and scanned PDFs
            ocr_lang: Tesseract language code (default: 'eng')
        """
        self.ocr_enabled = ocr_enabled
        self.ocr_lang = ocr_lang
        
    async def extract_text(
        self, 
        content: bytes, 
        content_type: str,
        fallback_to_ocr: bool = True
    ) -> Dict[str, any]:
        """
        Extract text from document
        
        Args:
            content: Document bytes
            content_type: MIME type
            fallback_to_ocr: Try OCR if standard extraction fails
            
        Returns:
            Dict with 'text', 'method', 'success', 'error' keys
        """
        result = {
            "text": "",
            "method": None,
            "success": False,
            "error": None,
            "metadata": {}
        }
        
        try:
            mime_type = content_type.lower()
            
            
            if mime_type in self.OCR_SUPPORTED_TYPES and self.ocr_enabled:
                return await self._extract_with_ocr(content, mime_type)
            
            
            try:
                extracted = await extract_bytes(content, mime_type=mime_type)
                
                if hasattr(extracted, 'content'):
                    text = extracted.content
                elif hasattr(extracted, 'text'):
                    text = extracted.text
                else:
                    text = str(extracted)
                
                result["text"] = text
                result["method"] = "kreuzberg"
                result["success"] = True
                
                
                if hasattr(extracted, 'metadata'):
                    result["metadata"] = extracted.metadata
                
                
                if mime_type == "application/pdf" and fallback_to_ocr and self.ocr_enabled:
                    if len(text.strip()) < 50:  # Minimal text threshold
                        logger.info("PDF extraction returned minimal text, attempting OCR")
                        ocr_result = await self._extract_pdf_with_ocr(content)
                        if ocr_result["success"] and len(ocr_result["text"]) > len(text):
                            result = ocr_result
                            result["method"] = "kreuzberg+ocr_fallback"
                
            except Exception as e:
                logger.warning(f"Standard extraction failed: {e}")
                
                
                if fallback_to_ocr and self.ocr_enabled:
                    if mime_type == "application/pdf":
                        return await self._extract_pdf_with_ocr(content)
                    elif mime_type in self.OCR_SUPPORTED_TYPES:
                        return await self._extract_with_ocr(content, mime_type)
                
                raise
                
        except Exception as e:
            logger.error(f"Extraction failed for {content_type}: {e}")
            result["error"] = str(e)
            result["success"] = False
            
        return result
    
    async def _extract_with_ocr(self, content: bytes, mime_type: str) -> Dict[str, any]:
        """Extract text from image using OCR"""
        result = {
            "text": "",
            "method": "tesseract_ocr",
            "success": False,
            "error": None,
            "metadata": {}
        }
        
        try:
            
            image = Image.open(io.BytesIO(content))
            
            
            result["metadata"] = {
                "size": image.size,
                "mode": image.mode,
                "format": image.format
            }
            
            # Perform OCR
            text = pytesseract.image_to_string(
                image, 
                lang=self.ocr_lang,
                config='--psm 3'  # Fully automatic page segmentation
            )
            
            result["text"] = text
            result["success"] = True
            
        except Exception as e:
            logger.error(f"OCR extraction failed: {e}")
            result["error"] = str(e)
            
        return result
    
    async def _extract_pdf_with_ocr(self, content: bytes) -> Dict[str, any]:
        """Extract text from PDF using OCR (for scanned PDFs)"""
        result = {
            "text": "",
            "method": "pdf2image+tesseract_ocr",
            "success": False,
            "error": None,
            "metadata": {}
        }
        
        try:
            from pdf2image import convert_from_bytes
            
            # Convert PDF to images
            images = convert_from_bytes(content, dpi=300)
            
            result["metadata"]["pages"] = len(images)
            
            # OCR each page
            full_text = []
            for i, image in enumerate(images):
                try:
                    page_text = pytesseract.image_to_string(
                        image,
                        lang=self.ocr_lang,
                        config='--psm 3'
                    )
                    full_text.append(f"--- Page {i+1} ---\n{page_text}")
                except Exception as page_error:
                    logger.warning(f"OCR failed for page {i+1}: {page_error}")
                    full_text.append(f"--- Page {i+1} ---\n[OCR Failed]")
            
            result["text"] = "\n\n".join(full_text)
            result["success"] = True
            
        except ImportError:
            result["error"] = "pdf2image not installed. Install with: pip install pdf2image"
            logger.error(result["error"])
        except Exception as e:
            logger.error(f"PDF OCR extraction failed: {e}")
            result["error"] = str(e)
            
        return result

async def extract_document_text(content: bytes, content_type: str) -> str:
    """
    Extract text from document (legacy function)
    
    Args:
        content: Document bytes
        content_type: MIME type
        
    Returns:
        Extracted text string
    """
    extractor = DocumentExtractor(ocr_enabled=True)
    result = await extractor.extract_text(content, content_type)
    return result.get("text", "")