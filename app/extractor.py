import logging
from kreuzberg import extract_bytes

logger = logging.getLogger(__name__)

async def extract_document_text(content: bytes, content_type: str) -> str:
    """Extract text from document using Kreuzberg"""
    try:
        # Map content type to mime type
        mime_map = {
            "application/pdf": "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/plain": "text/plain"
        }
        
        mime_type = mime_map.get(content_type, "application/octet-stream")
        
        # Extract
        result = await extract_bytes(content, mime_type=mime_type)
        
        if hasattr(result, 'content'):
            return result.content
        elif hasattr(result, 'text'):
            return result.text
        else:
            return str(result)
    
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return ""
