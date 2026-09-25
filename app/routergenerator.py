"""
FastAPI endpoints wiring the LangGraph pipeline (app/graph.py) into PayerIQ.

Mounted from app/main.py under the /v2 prefix, alongside the existing direct
/generate endpoint -- the two coexist so the deployed frontend's contract
against /generate isn't broken by this rollout. A follow-up submission calls
/v2/refine/{session_id} with the session_id returned from the first call;
draft history, retry state, and groundedness scores are carried forward
automatically by the checkpointer.

One session_id can cover several output formats, and the set of formats is
not fixed at the first /generate call -- the UI lets a user check FRD or
Agile Artifact well after an STTM session is already underway. Since
LangGraph state is per-thread and PayerIQState.output_format is a single
value, each format gets its own LangGraph thread_id -- _thread_id(session_id,
fmt) -- nested under the shared session_id, rather than all formats fighting
over one thread (which would mean a second format's ainvoke() overwrites the
first format's current_draft/instruction_history, and this module's
draft-repair logic misreading one format's table shape as another's).
/v2/refine cold-starts a format's thread the same way /v2/generate would
(full initial_state, no prior checkpoint to load) whenever that format's
thread has no checkpoint yet -- i.e. it's being added to the session for the
first time -- seeding it with the session's already-known source_files so
"create the FRD too" doesn't need the vendor file re-uploaded.

Requires env vars (shared with app/services/document_history_service.py):
    AZURE_COSMOS_ENDPOINT
    AZURE_COSMOS_KEY
    AZURE_COSMOS_DATABASE_NAME          (default "PayerIQ")
    AZURE_COSMOS_CHECKPOINT_CONTAINER_NAME  (default "Checkpoints")

Every route below also requires a valid Bearer token (see app/auth_router.py
and app/services/auth_service.py) -- issued by POST /v2/auth/login, which
looks up the caller's credentials in a *different* Cosmos database
(AZURE_COSMOS_AUTH_DATABASE_NAME, default "payeriqdb") than the one holding
this module's own checkpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import List, Optional

from azure.cosmos.aio import CosmosClient
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.auth_router import require_auth
from app.graph import build_graph, PayerIQState, SourceFile
from app.services.cosmos_checkpoint import AsyncCosmosDBSaver
from app.services.file_extraction import extract_and_log

logger = logging.getLogger(__name__)
router = APIRouter()

COSMOS_ENDPOINT = os.getenv("AZURE_COSMOS_ENDPOINT", "")
COSMOS_KEY = os.getenv("AZURE_COSMOS_KEY", "")
COSMOS_DATABASE_NAME = os.getenv("AZURE_COSMOS_DATABASE_NAME", "PayerIQ")
CHECKPOINT_CONTAINER_NAME = os.getenv("AZURE_COSMOS_CHECKPOINT_CONTAINER_NAME", "Checkpoints")

_client: CosmosClient | None = None
_checkpointer: AsyncCosmosDBSaver | None = None
_graph = None


async def init_graph_resources() -> None:
    """Opens the Cosmos DB client backing the LangGraph checkpointer and
    creates its container if needed. Called from app/main.py's startup hook
    rather than at import time, since it needs a running event loop.

    The /v2 pipeline is additive -- the existing /generate endpoint must keep
    working even if Cosmos isn't configured or reachable yet, so a missing
    or failing connection here degrades to "/v2 endpoints return 503"
    (see _require_graph) rather than aborting the whole app's startup."""
    global _client, _checkpointer, _graph

    if not (COSMOS_ENDPOINT and COSMOS_KEY):
        print("Warning: AZURE_COSMOS_ENDPOINT/AZURE_COSMOS_KEY not set -- /v2 LangGraph endpoints disabled.")
        return

    try:
        _client = CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY)
        _checkpointer = AsyncCosmosDBSaver(_client, COSMOS_DATABASE_NAME, CHECKPOINT_CONTAINER_NAME)
        await _checkpointer.setup()

        _graph = build_graph(_checkpointer)
    except Exception as e:
        print(f"Warning: /v2 LangGraph pipeline init failed, endpoints disabled: {e}")


async def shutdown_graph_resources() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def _require_graph():
    if _graph is None:
        raise HTTPException(
            503, "LangGraph pipeline is not initialized (Cosmos DB unavailable at startup)."
        )
    return _graph


async def _extract_new_files(files: List[UploadFile], uploaded_by: str) -> list[SourceFile]:
    new_files: list[SourceFile] = []
    for f in files:
        extracted = await extract_and_log(f, uploaded_by)
        if extracted:
            filename, text = extracted
            new_files.append({"filename": filename, "text": text})
    return new_files


def _thread_id(session_id: str, output_format: str) -> str:
    """One LangGraph thread per (session, format) pair -- see module
    docstring for why formats can't share a thread within a session."""
    return f"{session_id}::{output_format}"


def _initial_state(
    session_id: str,
    output_format: str,
    source_files: list[SourceFile],
    project_name: str,
    instructions: str,
) -> PayerIQState:
    return {
        "session_id": session_id,
        "output_format": output_format,
        "source_files": source_files,
        "project_name": project_name,
        "instruction_history": [],
        "draft_history": [],
        "current_instruction": instructions,
        "retrieved_context": "",
        "grounding_sources": [],
        "current_draft": "",
        "attack_detected": False,
        "content_filtered": False,
        "groundedness_score": 0.0,
        "grounded": False,
        "retry_count": 0,
        "feedback": "",
        "status": "in_progress",
        "output_path": "",
    }


def _ndjson(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def _stage_message(node_name: str, node_output: dict, fmt: str) -> str | None:
    """Human-readable progress line for one completed graph node, or None
    for a node whose completion isn't worth surfacing (e.g. the instant
    bookkeeping step that just appends to instruction_history)."""
    if node_name == "input_guardrail":
        return "Checking your instructions for safety issues..."
    if node_name == "reject":
        return "Request blocked by the safety guardrail."
    if node_name == "retrieve":
        return "Retrieving relevant knowledge-base content..."
    if node_name == "generate":
        return f"Drafting the {fmt} document..."
    if node_name == "content_blocked":
        return "Request blocked by Azure OpenAI's content safety filter."
    if node_name == "groundedness_check":
        # Deliberately no raw score in this customer-facing line -- 0.35 or
        # 0.33 reads as "this document is 35% correct" to someone watching
        # live, when groundedness actually measures traceability to source
        # material, not overall quality. The real number is still in every
        # per-format result (groundedness_scores), just not narrated here.
        return "Checking that every mapping is traceable to your source material..."
    if node_name == "prepare_retry":
        return f"Revising the {fmt} draft (attempt {node_output.get('retry_count')})..."
    if node_name == "finalize":
        return f"Finalizing the {fmt} document..."
    return None


async def _run_format_stream(graph, fmt: str, graph_input: dict, config: dict):
    """Runs one format through the graph, yielding ("progress", ndjson_line)
    as each node completes, then ("result", final_state) once the run
    finishes. final_state is reconstructed by overlaying each node's partial
    update onto the input dict, in execution order -- exactly how LangGraph
    merges TypedDict channel state internally -- so it's equivalent to what
    graph.ainvoke() would have returned, just observed incrementally via
    astream(..., stream_mode="updates") instead of awaited all at once."""
    state_acc: dict = dict(graph_input)
    async for update in graph.astream(graph_input, config=config, stream_mode="updates"):
        for node_name, node_output in update.items():
            state_acc.update(node_output)
            message = _stage_message(node_name, node_output, fmt)
            if message:
                yield "progress", _ndjson({
                    "type": "progress",
                    "format": fmt,
                    "node": node_name,
                    "message": message,
                })
    yield "result", state_acc


async def _run_formats_concurrently(
    graph, output_format: list[str], make_input, thread_id_fn, results: dict
):
    """Runs every requested format's graph execution concurrently instead of
    one after another. Each format already lives on its own LangGraph
    thread (see module docstring), so there's no shared mutable state to
    race on -- running them sequentially was multiplying wall-clock time by
    however many formats were requested (a multi-retry drafting call is
    already the single most expensive step; doing that twice or three times
    in a row for no reason is most of why a multi-format request felt slow).

    Progress lines from whichever format is furthest along are merged into
    one stream in true arrival order via a shared queue. Populates
    results[fmt] with each format's final state as it finishes; the caller
    reads that dict once this generator is exhausted. If one format's run
    raises, every other format is still allowed to finish before the first
    exception is re-raised, so a single failure doesn't cut short progress
    the user was already watching."""
    queue: asyncio.Queue = asyncio.Queue()
    errors: dict[str, Exception] = {}
    DONE = object()

    async def run_one(fmt: str):
        try:
            config = {"configurable": {"thread_id": thread_id_fn(fmt)}}
            graph_input = make_input(fmt)
            async for kind, payload in _run_format_stream(graph, fmt, graph_input, config):
                if kind == "progress":
                    await queue.put(payload)
                else:
                    results[fmt] = payload
        except Exception as e:
            errors[fmt] = e
        finally:
            await queue.put(DONE)

    tasks = [asyncio.create_task(run_one(fmt)) for fmt in output_format]
    remaining = len(tasks)
    while remaining:
        item = await queue.get()
        if item is DONE:
            remaining -= 1
        else:
            yield item
    await asyncio.gather(*tasks)  # already finished; surfaces any task-level bug
    if errors:
        raise next(iter(errors.values()))


@router.post("/generate")
async def generate(
    output_format: List[str] = Form(...),
    instructions: str = Form(...),
    project_name: Optional[str] = Form(""),
    uploaded_by: Optional[str] = Form(None),
    files: List[UploadFile] = File(default=[]),
    _auth: dict = Depends(require_auth),
):
    """First turn for a new project/session. Any uploaded vendor files /
    standards are parsed and kept in session state for this and every
    subsequent /refine turn.

    output_format accepts one or more values (the frontend's checkbox UI
    posts one 'output_format' field per checked box) -- one for a single
    format, several for "generate FRD and STTM together". Each selected
    format runs on its own LangGraph thread nested under this session_id
    (see module docstring), so a later /refine turn -- including one that
    checks a format that wasn't selected here -- can continue or cold-start
    each format's own instruction/draft history independently.

    Streams newline-delimited JSON (media type application/x-ndjson) rather
    than returning one JSON body -- a full run (guardrail -> retrieval ->
    drafting -> groundedness check -> up to 2 retries -> finalize, times
    however many formats were requested) can take long enough that the
    frontend was showing a bare spinner with no feedback. Each line is one
    of:
        {"type": "progress", "format": "STTM", "node": "generate", "message": "..."}
        {"type": "result", "session_id": ..., "outputs": {...}, ...}   -- last line on success
        {"type": "error", "message": "..."}                           -- last line on failure
    The final "result" line carries the exact same fields the old
    single-JSON response did, so a client can ignore every "progress" line
    and just consume the last one for today's behavior."""
    graph = _require_graph()
    session_id = str(uuid.uuid4())
    source_files = await _extract_new_files(files, uploaded_by or project_name or "unknown")

    async def stream():
        outputs: dict[str, str] = {}
        groundedness_scores: dict[str, float] = {}
        retry_counts: dict[str, int] = {}
        statuses: dict[str, str] = {}
        messages: dict[str, str] = {}
        results: dict[str, dict] = {}
        try:
            make_input = lambda fmt: _initial_state(  # noqa: E731
                session_id, fmt, source_files, project_name or "", instructions
            )
            async for line in _run_formats_concurrently(
                graph, output_format, make_input, lambda fmt: _thread_id(session_id, fmt), results
            ):
                yield line

            for fmt in output_format:
                state = results[fmt]
                outputs[fmt] = state.get("current_draft", "")
                groundedness_scores[fmt] = state.get("groundedness_score")
                retry_counts[fmt] = state.get("retry_count")
                # Per-format outcome -- top-level "status" below only ever
                # reflects the last requested format, which silently hides a
                # Prompt Shields rejection on any format that isn't last. A
                # caller that wants to know "was THIS format blocked" (e.g.
                # to grey out its own download / show its own message) needs
                # this map, not the single aggregate field.
                statuses[fmt] = state.get("status")
                # reject_node/content_blocked_node set "feedback" to a
                # human-readable reason ("Prompt Shields detected...",
                # "Azure OpenAI's content safety filter flagged...") -- this
                # used to be computed and then dropped on the floor, leaving
                # the frontend with only status:"rejected" and no way to say
                # *why* to the analyst. Empty string (not omitted) for a
                # format that wasn't rejected, so the frontend can key off
                # this map the same uniform way it already keys off statuses.
                messages[fmt] = state.get("feedback", "")
            last = results[output_format[-1]]

            yield _ndjson({
                "type": "result",
                "session_id": session_id,
                "status": last.get("status"),
                "statuses": statuses,
                "messages": messages,
                "outputs": outputs,
                "groundedness_score": last.get("groundedness_score"),
                "groundedness_scores": groundedness_scores,
                "retry_count": last.get("retry_count"),
                "retry_counts": retry_counts,
                "output_path": last.get("output_path"),
                "source_filenames": [f["filename"] for f in last.get("source_files", [])],
            })
        except Exception as e:
            logger.exception("Unhandled error in /v2/generate")
            yield _ndjson({"type": "error", "message": str(e)})

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@router.post("/refine/{session_id}")
async def refine(
    session_id: str,
    output_format: List[str] = Form(...),
    instructions: str = Form(...),
    uploaded_by: Optional[str] = Form(None),
    files: List[UploadFile] = File(default=[]),
    _auth: dict = Depends(require_auth),
):
    """Follow-up turn: user wasn't satisfied, gave a new instruction, or
    checked a format that wasn't part of the session yet -- optionally with
    a new or replacement file attached (e.g. "regenerate using the new
    vendor file instead of the earlier one"). Each requested format resumes
    from its own thread's last checkpoint if it has one (instruction_history,
    draft_history, and earlier source_files are already there); a format
    with no checkpoint yet is cold-started exactly like /v2/generate would,
    seeded with this session's source_files (pulled from whichever other
    requested format already has a thread) so the newly-added format still
    sees the originally-uploaded vendor file. Any newly uploaded files this
    turn are appended for every requested format, not replacing what's
    there, so the model can see both and honor "instead of" / "in addition
    to" instructions correctly. Streams newline-delimited JSON the same way
    /v2/generate does -- see that endpoint's docstring for the line shapes.
    The final "result" line carries the same `outputs`-map shape as
    /v2/generate's."""
    graph = _require_graph()
    new_files = await _extract_new_files(files, uploaded_by or "unknown")

    # A format cold-starting this turn has no source_files/project_name of
    # its own yet -- borrow them from whichever requested format already
    # has a thread, so "also generate the FRD" doesn't need the vendor
    # file re-uploaded or lose the project name from retrieval queries.
    # None only if every requested format is new, which means the
    # session_id itself is unknown (no prior /generate call ever ran for
    # it under any format). This lookup, and the 404 it can raise, happens
    # before streaming starts so a truly unknown session still gets a plain
    # HTTP 404 rather than an in-stream error line.
    known_source_files: list[SourceFile] | None = None
    known_project_name = ""
    snapshots: dict[str, object] = {}
    for fmt in output_format:
        config = {"configurable": {"thread_id": _thread_id(session_id, fmt)}}
        snapshot = await graph.aget_state(config)
        snapshots[fmt] = snapshot
        if snapshot.values and known_source_files is None:
            known_source_files = snapshot.values.get("source_files", [])
            known_project_name = snapshot.values.get("project_name", "")
    if known_source_files is None:
        raise HTTPException(404, "Unknown session")

    def make_input(fmt: str) -> dict:
        snapshot = snapshots[fmt]
        if snapshot.values:
            graph_input: dict = {"current_instruction": instructions}
            if new_files:
                graph_input["source_files"] = snapshot.values.get("source_files", []) + new_files
            return graph_input
        return _initial_state(
            session_id, fmt, known_source_files + new_files, known_project_name, instructions
        )

    async def stream():
        outputs: dict[str, str] = {}
        groundedness_scores: dict[str, float] = {}
        retry_counts: dict[str, int] = {}
        statuses: dict[str, str] = {}
        messages: dict[str, str] = {}
        results: dict[str, dict] = {}
        try:
            async for line in _run_formats_concurrently(
                graph, output_format, make_input, lambda fmt: _thread_id(session_id, fmt), results
            ):
                yield line

            for fmt in output_format:
                state = results[fmt]
                outputs[fmt] = state.get("current_draft", "")
                groundedness_scores[fmt] = state.get("groundedness_score")
                retry_counts[fmt] = state.get("retry_count")
                statuses[fmt] = state.get("status")
                messages[fmt] = state.get("feedback", "")
            last = results[output_format[-1]]

            yield _ndjson({
                "type": "result",
                "session_id": session_id,
                "status": last.get("status"),
                "statuses": statuses,
                "messages": messages,
                "outputs": outputs,
                "groundedness_score": last.get("groundedness_score"),
                "groundedness_scores": groundedness_scores,
                "retry_count": last.get("retry_count"),
                "retry_counts": retry_counts,
                "output_path": last.get("output_path"),
                "source_filenames": [f["filename"] for f in last.get("source_files", [])],
            })
        except Exception as e:
            logger.exception("Unhandled error in /v2/refine/%s", session_id)
            yield _ndjson({"type": "error", "message": str(e)})

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@router.get("/status/{session_id}")
async def status(session_id: str, output_format: str, _auth: dict = Depends(require_auth)):
    """output_format is required as a query param (?output_format=STTM) --
    each format lives on its own thread, so status is per-format."""
    try:
        graph = _require_graph()
        config = {"configurable": {"thread_id": _thread_id(session_id, output_format)}}
        snapshot = await graph.aget_state(config)
        if not snapshot.values:
            raise HTTPException(404, "Unknown session")
        return {"status": snapshot.values.get("status"), "session_id": session_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error in /v2/status/%s", session_id)
        raise HTTPException(status_code=500, detail=str(e))


