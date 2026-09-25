import os
from datetime import datetime, timezone
from typing import Optional

from azure.cosmos.aio import CosmosClient
from azure.storage.blob.aio import BlobServiceClient
from dotenv import load_dotenv

load_dotenv()

COSMOS_ENDPOINT = os.getenv("AZURE_COSMOS_ENDPOINT", "")
COSMOS_KEY = os.getenv("AZURE_COSMOS_KEY", "")
COSMOS_DATABASE_NAME = os.getenv("AZURE_COSMOS_DATABASE_NAME", "PayerIQ")
COSMOS_CONTAINER_NAME = os.getenv("AZURE_COSMOS_CONTAINER_NAME", "DocumentHistory")

DOCS_STORAGE_CONNECTION_STRING = os.getenv("AZURE_DOCS_STORAGE_CONNECTION_STRING", "")
DOCUMENT_HISTORY_CONTAINER = os.getenv("DOCUMENT_HISTORY_INDEX_NAME", "documenthistory")

HISTORY_ENABLED = bool(COSMOS_ENDPOINT and COSMOS_KEY and DOCS_STORAGE_CONNECTION_STRING)


def _slugify(name: str) -> str:
    stem = name.rsplit(".", 1)[0]
    slug = "".join(c if c.isalnum() else "-" for c in stem).strip("-").lower()
    return slug or "document"


async def _next_version(document_id: str) -> str:
    """Counts existing history records for this documentId and returns the next 'V<n>' label."""
    async with CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY) as client:
        container = client.get_database_client(COSMOS_DATABASE_NAME).get_container_client(COSMOS_CONTAINER_NAME)
        count = 0
        async for item in container.query_items(
            query="SELECT VALUE COUNT(1) FROM c WHERE c.documentId = @id",
            parameters=[{"name": "@id", "value": document_id}],
            partition_key=document_id,
        ):
            count = item
    return f"V{count + 1}"


async def upload_document_blob(raw_bytes: bytes, blob_name: str, content_type: Optional[str]) -> str:
    """
    Uploads the raw file to the compliance blob container and returns its URL.
    content_type always resolves to a concrete value (never None) before the
    upload call -- the async aiohttp transport injects a default Content-Type
    header for a None value *after* the request is signed, which then no
    longer matches the signature and the upload fails authentication.
    """
    async with BlobServiceClient.from_connection_string(DOCS_STORAGE_CONNECTION_STRING) as service:
        container = service.get_container_client(DOCUMENT_HISTORY_CONTAINER)
        blob = container.get_blob_client(blob_name)
        await blob.upload_blob(raw_bytes, overwrite=True, content_type=content_type or "application/octet-stream")
        return blob.url


async def log_document_history(
    document_id: str,
    version: str,
    document_name: str,
    document_type: str,
    uploaded_by: str,
    blob_url: str,
    action_type: str = "upload",
    status: str = "stored",
) -> dict:
    """Writes one compliance history record (matching the DocumentHistory container schema) to Cosmos DB."""
    record = {
        "id": f"{document_id}-{version}",
        "documentId": document_id,
        "version": version,
        "documentName": document_name,
        "documentType": document_type,
        "uploadedBy": uploaded_by,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "blobUrl": blob_url,
        "actionType": action_type,
        "status": status,
    }
    async with CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY) as client:
        container = client.get_database_client(COSMOS_DATABASE_NAME).get_container_client(COSMOS_CONTAINER_NAME)
        await container.upsert_item(record)
    return record


async def store_uploaded_document(
    raw_bytes: bytes,
    filename: str,
    uploaded_by: str,
    document_id: Optional[str] = None,
    content_type: Optional[str] = None,
    action_type: str = "upload",
) -> dict:
    """
    Archives an uploaded file to the compliance blob container and records a
    timestamped version entry for it in Cosmos DB. document_id defaults to a
    slug of the filename, so repeated uploads of a same-named file are tracked
    as sequential versions (V1, V2, ...) of one logical document.
    """
    document_id = document_id or _slugify(filename)
    document_type = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    version = await _next_version(document_id)
    blob_name = f"{document_id}-{version}.{document_type}" if document_type else f"{document_id}-{version}"
    blob_url = await upload_document_blob(raw_bytes, blob_name, content_type)
    return await log_document_history(
        document_id=document_id,
        version=version,
        document_name=filename,
        document_type=document_type,
        uploaded_by=uploaded_by,
        blob_url=blob_url,
        action_type=action_type,
    )
