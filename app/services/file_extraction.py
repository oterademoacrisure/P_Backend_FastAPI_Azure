"""
Shared upload-handling for both pipelines: extracts plain text from an
uploaded source file, and (optionally) logs it to the compliance document
history store. Used by:
- app/main.py               (v1 direct /generate)
- app/routergenerator.py    (v2 LangGraph /generate, /refine)
so a vendor file uploaded through either endpoint is parsed identically and
audited identically.
"""

from __future__ import annotations

import io

from fastapi import UploadFile

from app.services.document_history_service import HISTORY_ENABLED, store_uploaded_document


async def extract_text(upload: UploadFile) -> str:
    """Safely extracts plain text content from uploaded PDF, DOCX, XLSX, or TXT files."""
    raw = await upload.read()
    name = (upload.filename or "").lower()

    try:
        if name.endswith(".txt"):
            return raw.decode("utf-8", errors="ignore")

        if name.endswith(".pdf"):
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw))
            return "\n".join(page.extract_text() or "" for page in reader.pages)

        if name.endswith(".docx"):
            import docx

            doc = docx.Document(io.BytesIO(raw))
            return "\n".join(p.text for p in doc.paragraphs)

        if name.endswith((".xlsx", ".xls")):
            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
            lines = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    lines.append(", ".join(str(c) for c in row if c is not None))
            return "\n".join(lines)

    except Exception as e:
        return f"[Could not parse {upload.filename}: {e}]"

    return f"[Unsupported file type: {upload.filename}]"


async def extract_and_log(upload: UploadFile, uploaded_by: str) -> tuple[str, str] | None:
    """Reads one uploaded file once: extracts its text and (if configured)
    archives the raw bytes + a compliance history record. Returns
    (filename, text), or None for a file with no filename (an empty
    multipart slot).

    Reading raw bytes and re-seeking before extract_text() matters here:
    extract_text() consumes the stream, but store_uploaded_document() also
    needs the raw bytes -- reading twice from an already-consumed stream
    would silently archive an empty file."""
    if not upload.filename:
        return None

    raw_bytes = await upload.read()
    await upload.seek(0)
    text = await extract_text(upload)

    if HISTORY_ENABLED:
        try:
            await store_uploaded_document(
                raw_bytes=raw_bytes,
                filename=upload.filename,
                uploaded_by=uploaded_by,
                content_type=upload.content_type,
            )
        except Exception as e:
            print(f"Warning: document history logging failed for {upload.filename}: {e}")

    return upload.filename, text
