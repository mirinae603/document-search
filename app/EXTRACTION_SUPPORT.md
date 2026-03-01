# Document Extraction Support Matrix

*Last Updated: 2026-02-14 04:46:30*

## Overview

- **Total File Types**: 17
- **Supported**: 6 ✅
- **Failed**: 0 ❌
- **Warnings**: 11 ⚠️
- **OCR Enabled**: 3 types


## Documents

| File Type | MIME Type | Status | Method | Text Length | Performance | Notes |
|-----------|-----------|--------|--------|-------------|-------------|-------|
| `.pdf` | pdf | ⚠️ No Test File | - | 0 chars | - | No .pdf file found in test directory |
| `.docx` | vnd.openxmlformats-officedocum | ⚠️ No Test File | - | 0 chars | - | No .docx file found in test directory |
| `.doc` | msword | ⚠️ No Test File | - | 0 chars | - | No .doc file found in test directory |
| `.txt` | plain | ✅ Passed | kreuzberg | 215 chars | 12.51ms |  |
| `.html` | html | ✅ Passed | kreuzberg | 211 chars | 4.14ms |  |
| `.rtf` | rtf | ⚠️ No Test File | - | 0 chars | - | No .rtf file found in test directory |

## Images

| File Type | MIME Type | Status | Method | Text Length | Performance | Notes |
|-----------|-----------|--------|--------|-------------|-------------|-------|
| `.png` | png | ✅ Passed | tesseract_ocr | 153 chars | 473.24ms |  |
| `.jpg` | jpeg | ✅ Passed | tesseract_ocr | 152 chars | 490.49ms |  |
| `.tiff` | tiff | ⚠️ No Test File | - | 0 chars | - | No .tiff file found in test directory |
| `.bmp` | bmp | ✅ Passed | tesseract_ocr | 153 chars | 437.86ms |  |
| `.webp` | webp | ⚠️ No Test File | - | 0 chars | - | No .webp file found in test directory |

## Presentations

| File Type | MIME Type | Status | Method | Text Length | Performance | Notes |
|-----------|-----------|--------|--------|-------------|-------------|-------|
| `.pptx` | vnd.openxmlformats-officedocum | ⚠️ No Test File | - | 0 chars | - | No .pptx file found in test directory |
| `.ppt` | vnd.ms-powerpoint | ⚠️ No Test File | - | 0 chars | - | No .ppt file found in test directory |

## Scanneds

| File Type | MIME Type | Status | Method | Text Length | Performance | Notes |
|-----------|-----------|--------|--------|-------------|-------------|-------|
| `_scanned.pdf` | pdf | ⚠️ No Test File | - | 0 chars | - | No _scanned.pdf file found in test directory |

## Spreadsheets

| File Type | MIME Type | Status | Method | Text Length | Performance | Notes |
|-----------|-----------|--------|--------|-------------|-------------|-------|
| `.xlsx` | vnd.openxmlformats-officedocum | ⚠️ No Test File | - | 0 chars | - | No .xlsx file found in test directory |
| `.xls` | vnd.ms-excel | ⚠️ No Test File | - | 0 chars | - | No .xls file found in test directory |
| `.csv` | csv | ✅ Passed | kreuzberg | 152 chars | 1.14ms |  |

## Features

### Standard Extraction
- PDF, DOCX, DOC, TXT, HTML, RTF documents
- XLSX, XLS, CSV spreadsheets
- PPTX, PPT presentations
- Powered by Kreuzberg library

### OCR Support
- PNG, JPEG, TIFF, BMP, WEBP images
- Scanned PDFs (automatic fallback)
- Powered by Tesseract OCR
- Multi-language support

### Advanced Features
- Automatic OCR fallback for scanned documents
- Metadata extraction
- Performance monitoring
- Comprehensive error handling


## Installation

```bash
# Core dependencies
pip install kreuzberg pytesseract pillow pdf2image

# System dependencies (Ubuntu/Debian)
sudo apt-get install tesseract-ocr poppler-utils

# System dependencies (macOS)
brew install tesseract poppler
```


## Usage

```python
from document_extractor import DocumentExtractor

# Initialize with OCR support
extractor = DocumentExtractor(ocr_enabled=True, ocr_lang='eng')

# Extract text from document
with open('document.pdf', 'rb') as f:
    content = f.read()

result = await extractor.extract_text(
    content=content,
    content_type='application/pdf'
)

print(f"Extracted {len(result['text'])} characters")
print(f"Method: {result['method']}")
print(f"Success: {result['success']}")
```
