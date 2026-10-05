import asyncio
import logging
import os
import time
from typing import List, Dict, Any, Optional
from urllib.parse import unquote, urlparse
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorizableTextQuery
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "")
SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY", "")
SEARCH_INDEX = os.getenv("AZURE_SEARCH_INDEX_NAME", "rag-1788391708053")

# The index's vector field (1536-dim, populated by the Azure AI Search
# "Import and vectorize data" wizard's built-in Azure OpenAI vectorizer at
# indexing time). Queries use VectorizableTextQuery against this field, which
# sends the raw query text to Azure AI Search and lets the same vectorizer
# embed it server-side -- this service never calls an embedding model itself.
VECTOR_FIELD = "text_vector"

# The blob's full URL (the indexer's metadata_storage_path), e.g.
# https://<account>.blob.core.windows.net/sharepoint-docs/payment-integrity/Rules.docx
# The wizard-built index doesn't include it -- scripts/add_storage_path_to_index.py
# adds it. retrieve_grounding() uses it to limit a project's grounding to that
# project's blob folder.
PATH_FIELD = "storage_path"

MIN_RELEVANCE_SCORE = 0.0  # Hybrid search's fused (RRF) scores aren't 0-1 normalized
                            # like raw cosine similarity; set a real floor here once
                            # you've seen typical fused scores for your index.

# Number of chunks to retrieve per indexed document during grounding (see
# retrieve_grounding() below). Every document in the index gets its own
# query, so this many chunks come back per document regardless of the
# current document count in the index.
CHUNKS_PER_DOCUMENT = 2

# How long the indexed-document title list (see retrieve_grounding()'s
# _document_list_cache below) is trusted before re-querying Azure AI Search.
# That list only changes when someone re-indexes the knowledge base (a rare,
# manual event -- editing a template in blob storage and re-running the
# indexer), so caching just the titles is a freshness/latency tradeoff, not a
# correctness one. The chunks retrieved *within* each document are no longer
# cached alongside it: since they're now selected by hybrid relevance to the
# caller's actual prompt (not a prompt-independent wildcard query), caching
# them here would silently serve one request's grounding to a different
# request. Overridable via env var for a faster feedback loop in dev.
GROUNDING_CACHE_TTL_SECONDS = int(os.getenv("GROUNDING_CACHE_TTL_SECONDS", "900"))  # 15 min

_document_list_cache: List[Dict[str, str]] | None = None
_document_list_cache_at: float = 0.0
_document_list_cache_lock = asyncio.Lock()


async def search_knowledge_base(
    query: str,
    top_k: int = 5,
    filter_expression: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Retrieves relevant document chunks from Azure AI Search across the enterprise knowledge base,
    using hybrid search: `query` is matched both as full-text keywords and, via VectorizableTextQuery,
    as a vector embedded server-side by the index's built-in vectorizer against VECTOR_FIELD. Azure AI
    Search fuses the two rankings (RRF) itself -- this combines exact-term matches (IDs, field names,
    acronyms in claims/payment data) with semantic matches that plain keyword search would miss.

    `filter_expression` is an optional OData filter (e.g., "search.ismatch('document_name*', 'title')")
    to scope searches to specific documents or categories when required.
    Requires target fields (e.g., 'title') to be configured as filterable in the Azure AI Search index schema.

    Returns [] (never raises) if search is unconfigured, unreachable, or returns no matches.
    Callers must treat an empty result as "no grounding available for this run" --
    though a call that failed even after its retry is also reported this way; see
    the warning logged below for how to tell the two apart after the fact.

    Retries once on failure (e.g. a transient throttle/timeout from a burst of
    concurrent per-document queries in retrieve_grounding()) before giving up --
    a single Azure AI Search hiccup on one document's query used to silently drop
    that document's chunks from that run's grounding with no visible signal.
    """
    if not SEARCH_ENDPOINT or not SEARCH_KEY:
        return []

    for attempt in range(2):
        try:
            async with SearchClient(
                endpoint=SEARCH_ENDPOINT,
                index_name=SEARCH_INDEX,
                credential=AzureKeyCredential(SEARCH_KEY),
            ) as client:

                search_kwargs = {
                    "search_text": query,
                    "vector_queries": [
                        VectorizableTextQuery(
                            text=query, k_nearest_neighbors=top_k, fields=VECTOR_FIELD
                        )
                    ],
                    "top": top_k,
                    "select": ["chunk", "title"],
                }
                if filter_expression:
                    search_kwargs["filter"] = filter_expression

                results = await client.search(**search_kwargs)

                chunks: List[Dict[str, Any]] = []
                async for result in results:
                    title = result.get("title") or "Unknown Document"
                    content = result.get("chunk", "")
                    score = result.get("@search.score", 0.0)

                    if content and score >= MIN_RELEVANCE_SCORE:
                        chunks.append({
                            "source_document": title,
                            "excerpt": content,
                            "relevance_score": round(float(score), 4),
                        })

                return chunks

        except Exception as e:
            if attempt == 0:
                logger.warning(
                    "Azure AI Search retrieval failed (filter=%s), retrying once: %s",
                    filter_expression, e,
                )
                await asyncio.sleep(0.5)
                continue
            logger.warning(
                "Azure AI Search retrieval failed twice (filter=%s), giving up for this call: %s",
                filter_expression, e,
            )
            return []


def _folder_of(storage_path: str) -> str:
    """The folder a blob sits in, relative to its container ("" for a file
    at the container root). Path is https://host/<container>/<folder...>/<file>."""
    parts = unquote(urlparse(storage_path).path).strip("/").split("/")
    return "/".join(parts[1:-1]).lower()


def documents_for_project(documents: List[Dict[str, str]], folder: str) -> List[Dict[str, str]]:
    """Documents a project may use: files at the container root (common to
    every client and project: templates, standards, data dictionary), the
    client's folder (shared by that client's projects), and the project's
    own folder. `folder` is project_registry.Project.folder, e.g.
    "excellus/payment-integrity" -> root, "excellus/", "excellus/payment-integrity/".
    Another client's folder never matches, even for a project of the same name."""
    parts = folder.strip("/").lower().split("/")
    allowed = {""} | {"/".join(parts[: i + 1]) for i in range(len(parts))}
    return [d for d in documents if _folder_of(d["path"]) in allowed]


async def list_indexed_documents() -> List[Dict[str, str]]:
    """
    Returns the distinct documents currently present in the index, each as
    {"title": file name, "path": full blob URL}, so callers
    can query each document individually and guarantee every document contributes
    at least one chunk (instead of a single blended top-k query, where a strongly
    matching document can crowd the rest out of the results).

    Returns [] if search is unconfigured, unreachable, or the index is empty.
    """
    if not SEARCH_ENDPOINT or not SEARCH_KEY:
        return []

    try:
        async with SearchClient(
            endpoint=SEARCH_ENDPOINT,
            index_name=SEARCH_INDEX,
            credential=AzureKeyCredential(SEARCH_KEY),
        ) as client:
            results = await client.search(
                search_text="*",
                top=1000,
                select=["title", PATH_FIELD],
            )

            documents: List[Dict[str, str]] = []
            seen = set()
            async for result in results:
                path = result.get(PATH_FIELD)
                if path and path not in seen:
                    seen.add(path)
                    documents.append({"title": result.get("title") or path, "path": path})

            return documents

    except Exception as e:
        logger.warning("Azure AI Search document listing failed: %s", e)
        return []


def build_path_filter(path: str) -> str:
    """OData filter scoping a search_knowledge_base() call to one document,
    by exact blob path -- unlike a title match, two projects' files with the
    same name can't bleed into each other."""
    escaped = path.replace("'", "''")
    return f"{PATH_FIELD} eq '{escaped}'"


def format_chunks(chunks: List[Dict[str, Any]]) -> str:
    """Renders Azure AI Search chunks into a labeled, citable context block."""
    if not chunks:
        return "(no matching content retrieved from the enterprise knowledge base)"
    lines = []
    for c in chunks:
        lines.append(
            f"[Source: {c['source_document']} | relevance {c['relevance_score']}]\n{c['excerpt']}"
        )
    return "\n\n".join(lines)


async def _list_indexed_documents_cached() -> List[Dict[str, str]]:
    """
    Cached wrapper around list_indexed_documents(). The title list only changes
    when the knowledge base is re-indexed (a rare, manual event), so it's safe
    to cache for GROUNDING_CACHE_TTL_SECONDS -- unlike the chunks retrieved
    within each document, which now depend on the caller's prompt and must
    never be cached across requests (see retrieve_grounding()).
    """
    global _document_list_cache, _document_list_cache_at

    now = time.monotonic()
    if _document_list_cache is not None and (now - _document_list_cache_at) < GROUNDING_CACHE_TTL_SECONDS:
        return _document_list_cache

    async with _document_list_cache_lock:
        now = time.monotonic()
        if _document_list_cache is not None and (now - _document_list_cache_at) < GROUNDING_CACHE_TTL_SECONDS:
            return _document_list_cache  # filled by a concurrent caller while we waited

        documents = await list_indexed_documents()
        if documents:
            # Only cache a non-empty result -- an empty list is more likely a
            # transient search outage than a genuinely empty index, and caching
            # it would degrade every grounding call for the full TTL.
            _document_list_cache = documents
            _document_list_cache_at = now
        return documents


async def retrieve_grounding(
    prompt: str, project: str, folder: Optional[str] = None
) -> tuple[str, List[str]]:
    """
    With `folder` (the project's project_registry folder, e.g.
    "excellus/payment-integrity"), only the container root, the client's
    folder and the project's folder are searched (documents_for_project) --
    and if none of them
    can be found, nothing is returned rather than falling back to the whole
    index, so one project's documents can never ground another's output.
    Without it (admin-only sessions created before projects existed), the
    whole index is searched as before.

    Per-document retrieval against the Azure AI Search knowledge base index.
    Queries each indexed document individually so every document contributes at
    least one chunk, rather than a single blended top-k query where a strongly
    matching document can crowd the others out of the results.

    Each per-document query is a hybrid (keyword + vector) search on the
    caller's actual `prompt`/`project` text, filtered to that document -- so
    the chunks chosen *within* each document are the ones most relevant to
    this request, not an arbitrary/first-N selection. That makes the result
    genuinely request-dependent, so (unlike the indexed-document title list
    itself, cached in _list_indexed_documents_cached()) it is never cached.

    Shared by the direct /generate pipeline (app/main.py) and the LangGraph
    pipeline (app/graph.py) so both retrieve grounding the same way.
    """
    documents = await _list_indexed_documents_cached()
    query = f"{project}: {prompt}"

    if folder:
        documents = documents_for_project(documents, folder)
        if not documents:
            logger.warning(
                "retrieve_grounding: no indexed documents found for folder %r -- "
                "returning no grounding. Is %r in the index (scripts/add_storage_path_to_index.py)?",
                folder, PATH_FIELD,
            )
            return format_chunks([]), []

    if not documents:
        # Search unconfigured/unreachable or index empty -- degrade to a
        # single blended query using the caller's actual prompt.
        chunks = await search_knowledge_base(query=query, top_k=10)
        sources = sorted({c["source_document"] for c in chunks})
        return format_chunks(chunks), sources

    # Queried concurrently rather than one at a time -- each is an
    # independent read against the same index, so there's no reason to
    # pay N sequential round trips when N concurrent ones return in
    # roughly the time of one. This concurrent burst is exactly what makes
    # an individual document's query more likely to hit a transient
    # Azure AI Search throttle/timeout -- search_knowledge_base() retries
    # once internally to absorb that, but a document can still come back
    # with zero chunks after its retry too.
    per_document_chunks = await asyncio.gather(*(
        search_knowledge_base(
            query=query,
            top_k=CHUNKS_PER_DOCUMENT,
            filter_expression=build_path_filter(doc["path"]),
        )
        for doc in documents
    ))
    chunks = [c for doc_chunks in per_document_chunks for c in doc_chunks]
    sources = sorted({c["source_document"] for c in chunks})

    # Every indexed document is expected to contribute at least one chunk
    # every time (top_k=CHUNKS_PER_DOCUMENT with no relevance floor other than
    # MIN_RELEVANCE_SCORE). A title present in `documents` but absent from
    # `sources` means that document's query came back empty this run -- most
    # likely a retry-exhausted transient failure, not a genuinely empty
    # document (an actually empty/unchunked document would fail the same way
    # on every call, which this log makes visible too). Flagging it here is
    # what makes a silently incomplete grounding result diagnosable instead of
    # looking identical to "this document just wasn't relevant."
    missing = sorted({d["title"] for d in documents} - set(sources))
    if missing:
        logger.warning(
            "retrieve_grounding: %d of %d indexed document(s) contributed no "
            "grounding chunks this call: %s",
            len(missing), len(documents), missing,
        )

    return format_chunks(chunks), sources