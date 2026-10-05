"""
The common output templates every project shares (today the STTM Data
Ingestion workbook), as raw file bytes for app/xlsx_builder to fill.

Azure AI Search only holds a template's *text* (so the model knows its
sections and columns); filling the real workbook -- its title banner, label
column, header styling, banding, column widths, frozen panes -- needs the
file itself. Lookup order:
1. Blob Storage: <TEMPLATE_CONTAINER>/<name> at the container root, where
   the common documents live (see ontology.md §5.0).
2. Local file: <TEMPLATE_DIR>/<name> -- the copy shipped with the repo, for
   development and when Blob is unreachable.
Same timeout / short-retry behaviour as ontology_service, for the same
reason: an unreachable storage account must not stall a generation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob.aio import BlobServiceClient

from app.services.ontology_service import (
    ONTOLOGY_BLOB_RETRY_SECONDS,
    ONTOLOGY_BLOB_TIMEOUT_SECONDS,
    ONTOLOGY_CACHE_TTL_SECONDS,
    ONTOLOGY_CONTAINER,
    ONTOLOGY_STORAGE_CONNECTION_STRING,
)

logger = logging.getLogger(__name__)

TEMPLATE_DIR = os.getenv(
    "TEMPLATE_DIR", str(Path(__file__).resolve().parent.parent.parent / "templates")
)
TEMPLATE_CONTAINER = os.getenv("TEMPLATE_CONTAINER", ONTOLOGY_CONTAINER)
STTM_TEMPLATE_NAME = os.getenv("STTM_TEMPLATE_NAME", "STTM_Data_Ingestion_Template.xlsx")

_cache: dict[str, tuple[float, bytes | None]] = {}
_cache_lock = asyncio.Lock()


async def _read_blob(name: str) -> bytes | None:
    if not ONTOLOGY_STORAGE_CONNECTION_STRING:
        return None
    async with BlobServiceClient.from_connection_string(ONTOLOGY_STORAGE_CONNECTION_STRING) as service:
        try:
            downloaded = await service.get_blob_client(TEMPLATE_CONTAINER, name).download_blob()
        except ResourceNotFoundError:
            return None
        return await downloaded.readall()


def _read_local(name: str) -> bytes | None:
    path = os.path.join(TEMPLATE_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


async def get_template(name: str = STTM_TEMPLATE_NAME) -> bytes | None:
    """The template's bytes, or None if neither Blob nor the local copy has
    it (the caller then builds a plain workbook instead)."""
    cached = _cache.get(name)
    if cached and time.monotonic() - cached[0] < ONTOLOGY_CACHE_TTL_SECONDS:
        return cached[1]

    async with _cache_lock:
        cached = _cache.get(name)
        if cached and time.monotonic() - cached[0] < ONTOLOGY_CACHE_TTL_SECONDS:
            return cached[1]

        cached_at = time.monotonic()
        source = "blob"
        try:
            data = await asyncio.wait_for(_read_blob(name), ONTOLOGY_BLOB_TIMEOUT_SECONDS)
        except Exception as e:  # including asyncio.TimeoutError
            logger.warning("Template Blob read failed for %r, trying local copy: %r", name, e)
            data = None
            cached_at -= ONTOLOGY_CACHE_TTL_SECONDS - ONTOLOGY_BLOB_RETRY_SECONDS
        if data is None:
            data, source = _read_local(name), "local"
        if data is None:
            logger.warning("Template %r not found in Blob or %s", name, TEMPLATE_DIR)
        else:
            logger.info("Loaded template %r from %s", name, source)
        _cache[name] = (cached_at, data)
        return data
