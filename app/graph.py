"""
LangGraph StateGraph for PayerIQ document generation with self-correction
and persisted session history.

Wires into the service layer:
- app.services.azure_search_service     (retrieval)
- app.services.openai_service           (generation)
- app.services.content_safety_service   (guardrails: Prompt Shields + Groundedness)
- app.xlsx_builder                      (final output assembly)

Mounted via app/routergenerator.py, included from app/main.py under /v2.
"""

from __future__ import annotations

import inspect
import time
from typing import Awaitable, Callable, Literal, TypedDict
from datetime import datetime, timezone

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph, START, END

from app import xlsx_builder
from app.services import azure_search_service, content_safety_service, draft_repair, openai_service, telemetry
from app.services.openai_service import is_content_filter_error

# A retry no longer gates whether a low-scoring draft reaches the customer
# -- finalize_node is reached either way once retries are exhausted (there's
# no human-review pause to fall back to; see route_after_groundedness).
# A retry is purely a best-effort "try to raise the score" at the cost of
# one more full generate+groundedness-check cycle (~20-25s observed) -- and
# real testing showed several formats' scores staying flat or getting worse
# across attempts rather than improving. With that cost buying no reliable
# benefit, 0 keeps latency predictable: one drafting pass, one groundedness
# check for the record, then finalize regardless of the score.
MAX_RETRIES = 0

# How many times generate_node will ask the model to fix a malformed
# '|'-delimited row (wrong cell count for its section) before giving up and
# shipping the draft as-is. A single attempt often isn't enough -- the same
# failure mode (dropping a '|' for a blank cell) can recur on the "fixed"
# draft too -- so this retries a few times rather than accepting the first
# correction attempt unconditionally.
MAX_MALFORMED_ROW_RETRIES = 3

# How many times generate_node will ask the model to fix an STTM Mapping
# Confidence quality issue: a row marked 'Confirmed' that contradicts itself
# or the document's own Assumptions/Open Questions section
# (find_confirmed_confidence_contradictions), or a row whose Open Question
# cell is a bare ID like 'Q004' instead of the actual question
# (find_placeholder_open_questions). The corresponding prompt-level rules
# (see prompt_templates.FORMAT_INSTRUCTIONS['sttm']) only hold up on roughly
# half of real generations, so this closes the gap in code the same way
# MAX_MALFORMED_ROW_RETRIES does for column-shifted rows.
MAX_CONFIDENCE_CONTRADICTION_RETRIES = 3


class DraftAttempt(TypedDict):
    draft: str
    groundedness_score: float
    retry_index: int


class SourceFile(TypedDict):
    filename: str
    text: str


class PayerIQState(TypedDict):
    session_id: str
    output_format: Literal["STTM", "FRD", "Agile"]
    # Every file uploaded so far this session (initial /generate call plus
    # any /refine turn), oldest first, never replaced -- only appended to.
    # Kept across turns so an instruction like "use the new vendor file
    # instead of the earlier one" has both files to compare.
    source_files: list[SourceFile]
    project_name: str

    instruction_history: list[dict]   # [{instruction, timestamp}], one per turn
    draft_history: list[DraftAttempt]  # one per generation attempt, across all turns

    current_instruction: str
    retrieved_context: str
    grounding_sources: list[str]
    current_draft: str

    attack_detected: bool
    # True when Azure OpenAI's own content filter (a second, independent
    # safety layer from Prompt Shields -- see is_content_filter_error() in
    # openai_service.py) rejected the completion call inside generate_node.
    content_filtered: bool
    groundedness_score: float
    grounded: bool
    retry_count: int
    feedback: str

    status: Literal["in_progress", "completed", "rejected"]
    output_path: str


# ---------------------------------------------------------------- nodes ----

async def input_guardrail_node(state: PayerIQState) -> dict:
    result = await content_safety_service.check_prompt_shields(
        user_prompt=state["current_instruction"],
        documents=[f["text"] for f in state.get("source_files", [])],
    )
    return {"attack_detected": result.attack_detected}


def reject_node(state: PayerIQState) -> dict:
    """Terminal node for a request Prompt Shields flagged as a jailbreak /
    prompt-injection attempt. Exists so the rejection is actually recorded in
    `status` -- routing input_guardrail straight to END on attack_detected
    would leave `status` at whatever it was before this turn."""
    telemetry.track_event("guardrail_rejected", {
        "reason": "prompt_shields",
        "session_id": state.get("session_id", ""),
        "output_format": state.get("output_format", ""),
    })
    return {
        "status": "rejected",
        "feedback": "Request blocked: Prompt Shields detected a jailbreak or "
        "prompt-injection attempt in the instruction or an uploaded document.",
    }


def content_blocked_node(state: PayerIQState) -> dict:
    """Terminal node for a request Azure OpenAI's own content filter rejected
    during generate_node -- a phrasing mild enough to pass Prompt Shields
    (input_guardrail_node) can still be refused by the model provider's own
    Responsible AI policy once it's embedded in the actual completion prompt.
    Reuses the same "rejected" status shape reject_node produces, so the
    frontend's existing per-format statuses handling covers this failure mode
    with no separate UI path needed."""
    telemetry.track_event("guardrail_rejected", {
        "reason": "content_filter",
        "session_id": state.get("session_id", ""),
        "output_format": state.get("output_format", ""),
    })
    return {
        "status": "rejected",
        "feedback": "Request blocked: Azure OpenAI's content safety filter flagged "
        "this request as unsafe before a document could be drafted. No document "
        "was generated.",
    }


def merge_history_node(state: PayerIQState) -> dict:
    """Fold the new instruction into the running history and reset the
    per-turn retry counter. The previous draft, if any, stays in
    draft_history and generate_node uses it as the base to revise rather
    than starting over."""
    history = state.get("instruction_history", [])
    history.append({
        "instruction": state["current_instruction"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return {
        "instruction_history": history,
        "retry_count": 0,
        "feedback": "",
    }


async def retrieve_node(state: PayerIQState) -> dict:
    context, sources = await azure_search_service.retrieve_grounding(
        prompt=state["current_instruction"],
        project=state.get("project_name") or "Untitled Project",
    )
    return {"retrieved_context": context, "grounding_sources": sources}


async def generate_node(state: PayerIQState) -> dict:
    prior_draft = (
        state["draft_history"][-1]["draft"] if state.get("draft_history") else None
    )
    try:
        draft = await openai_service.generate_document(
            output_format=state["output_format"],
            instruction_history=state["instruction_history"],
            retrieved_context=state["retrieved_context"],
            source_files=state.get("source_files", []),
            prior_draft=prior_draft,
            correction_feedback=state.get("feedback", ""),
        )
    except Exception as e:
        if not is_content_filter_error(e):
            raise
        # Azure OpenAI's own content filter, not Prompt Shields, rejected
        # this one -- see route_after_generate/content_blocked_node. Nothing
        # to repair/dedupe below since no draft was produced this turn.
        return {"current_draft": "", "content_filtered": True}

    if prior_draft:
        draft = draft_repair.restore_dropped_rows(
            prior_draft, draft, state["current_instruction"]
        )
        if draft_repair.addition_not_applied(prior_draft, draft, state["current_instruction"]):
            # The model ignored an "add a new field/row" instruction outright --
            # distinct from restore_dropped_rows's case (it applied the addition
            # by overwriting an existing row) and from low groundedness (which
            # MAX_RETRIES=0 deliberately no longer retries, see that constant's
            # docstring: broad retries often didn't raise the score anyway).
            # This failure mode is different -- testing showed it more likely on
            # a long, multi-turn document -- and worth exactly one extra
            # attempt, paid only when actually needed, not on every turn.
            try:
                retry_draft = await openai_service.generate_document(
                    output_format=state["output_format"],
                    instruction_history=state["instruction_history"],
                    retrieved_context=state["retrieved_context"],
                    source_files=state.get("source_files", []),
                    prior_draft=prior_draft,
                    correction_feedback=(
                        "Your previous revision did not actually add the new field/row "
                        "the instruction asked for. Add it now as a new row -- do not "
                        "just repeat or lightly edit the existing rows."
                    ),
                )
            except Exception:
                # This corrective retry is a best-effort improvement on an
                # already-successful primary draft -- a failure here (content
                # filter or otherwise) should fall back to the primary draft,
                # not lose a working document over an optional retry.
                retry_draft = None
            if retry_draft is not None:
                retry_draft = draft_repair.restore_dropped_rows(
                    prior_draft, retry_draft, state["current_instruction"]
                )
                if not draft_repair.addition_not_applied(prior_draft, retry_draft, state["current_instruction"]):
                    draft = retry_draft

    malformed_attempts = 0
    while draft_repair.has_malformed_rows(draft) and malformed_attempts < MAX_MALFORMED_ROW_RETRIES:
        # A '|'-delimited row came back with the wrong cell count -- almost
        # always the model dropping a '|' for a blank/inapplicable field
        # instead of writing an explicit placeholder, which silently shifts
        # every later column in that row (most visibly, Mapping Confidence
        # ends up blank while an earlier column absorbs its value). See
        # draft_repair.has_malformed_rows for the full explanation. Passing
        # the malformed `draft` itself as prior_draft (rather than this
        # turn's actual prior_draft, if any) is what makes generate_document
        # actually include correction_feedback in the prompt -- it only does
        # so when prior_draft is set. Looped (not a single attempt) because
        # the same failure mode can recur on the "fixed" draft too.
        malformed_attempts += 1
        try:
            repaired_draft = await openai_service.generate_document(
                output_format=state["output_format"],
                instruction_history=state["instruction_history"],
                retrieved_context=state["retrieved_context"],
                source_files=state.get("source_files", []),
                prior_draft=draft,
                correction_feedback=(
                    "One or more rows in your previous output had the wrong number of "
                    "'|'-delimited cells for their section's header -- this happens when "
                    "a field with no applicable value is left blank without its own '|', "
                    "which shifts every following column in that row into the wrong "
                    "field. Rewrite the document so every data row has EXACTLY the same "
                    "number of '|'-delimited cells as its section's header row, using "
                    "the literal text 'N/A' for any field with no applicable value -- "
                    "never omit a '|' for an empty cell. Keep every other value exactly "
                    "as it was; only fix the malformed rows."
                ),
            )
        except Exception:
            # Best-effort improvement on an already-produced draft -- a
            # failure here should fall back to the malformed-but-present
            # draft, not lose the document over an optional retry.
            break
        if prior_draft:
            repaired_draft = draft_repair.restore_dropped_rows(
                prior_draft, repaired_draft, state["current_instruction"]
            )
        draft = repaired_draft

    if draft_repair.has_malformed_rows(draft):
        # Every correction attempt still came back misaligned -- ship the
        # draft anyway (a malformed document is still more useful than none),
        # but record it so this doesn't fail silently the way it did before
        # this retry loop existed.
        telemetry.track_event("malformed_rows_unresolved", {
            "session_id": state.get("session_id", ""),
            "output_format": state.get("output_format", ""),
            "attempts": malformed_attempts,
        })

    contradiction_attempts = 0
    contradictions = draft_repair.find_confirmed_confidence_contradictions(draft)
    placeholders = draft_repair.find_placeholder_open_questions(draft)
    while (contradictions or placeholders) and contradiction_attempts < MAX_CONFIDENCE_CONTRADICTION_RETRIES:
        contradiction_attempts += 1
        feedback_parts = []
        if contradictions:
            feedback_parts.append(
                f"These target fields are marked 'Confirmed' in the STTM Mapping "
                f"section's Mapping Confidence column, but each one either has its "
                f"own non-'N/A' Open Question, or is referenced by name in the "
                f"Assumptions or Open Questions section elsewhere in this document: "
                f"{', '.join(contradictions)}. A row cannot be simultaneously confirmed "
                f"and questioned -- change Mapping Confidence to 'Candidate' or 'Needs "
                f"SME Review' for exactly these rows (matching whichever this document's "
                f"own conventions call for)."
            )
        if placeholders:
            feedback_parts.append(
                f"These target fields have a bare ID (like 'Q004') in their Open Question "
                f"cell instead of the actual question, forcing a reader to open another "
                f"sheet to know what needs resolving: {', '.join(placeholders)}. Rewrite "
                f"each one's Open Question cell to spell out the question in full prose "
                f"instead of the ID (it's fine for the same question to also appear as its "
                f"own entry in the Assumptions/Open Questions section)."
            )
        feedback_parts.append("Keep every other row and every other column's value exactly as it was.")
        try:
            corrected_draft = await openai_service.generate_document(
                output_format=state["output_format"],
                instruction_history=state["instruction_history"],
                retrieved_context=state["retrieved_context"],
                source_files=state.get("source_files", []),
                prior_draft=draft,
                correction_feedback=" ".join(feedback_parts),
            )
        except Exception:
            # Best-effort improvement on an already-produced draft -- a
            # failure here should fall back to the contradictory-but-present
            # draft, not lose the document over an optional retry.
            break
        if prior_draft:
            corrected_draft = draft_repair.restore_dropped_rows(
                prior_draft, corrected_draft, state["current_instruction"]
            )
        draft = corrected_draft
        contradictions = draft_repair.find_confirmed_confidence_contradictions(draft)
        placeholders = draft_repair.find_placeholder_open_questions(draft)

    if contradictions or placeholders:
        # Every correction attempt still left an issue -- ship the draft
        # anyway rather than lose it, but record it so this doesn't fail
        # silently the way it did before this retry loop existed.
        telemetry.track_event("confidence_contradiction_unresolved", {
            "session_id": state.get("session_id", ""),
            "output_format": state.get("output_format", ""),
            "attempts": contradiction_attempts,
            "contradictions": ", ".join(contradictions),
            "placeholder_open_questions": ", ".join(placeholders),
        })

    draft = draft_repair.dedupe_repeated_rows(draft)
    return {"current_draft": draft, "content_filtered": False}


async def groundedness_node(state: PayerIQState) -> dict:
    result = await content_safety_service.check_groundedness(
        text=state["current_draft"],
        grounding_sources=[state["retrieved_context"]],
    )
    history = state.get("draft_history", [])
    history.append({
        "draft": state["current_draft"],
        "groundedness_score": result.score,
        "retry_index": state.get("retry_count", 0),
    })
    grounded = result.score >= content_safety_service.GROUNDEDNESS_THRESHOLD
    telemetry.track_event("groundedness_scored", {
        "session_id": state.get("session_id", ""),
        "output_format": state.get("output_format", ""),
        "score": f"{result.score:.4f}",
        "grounded": grounded,
        "retry_index": state.get("retry_count", 0),
    })
    return {
        "groundedness_score": result.score,
        "grounded": grounded,
        "draft_history": history,
    }


def prepare_retry_node(state: PayerIQState) -> dict:
    return {
        "retry_count": state.get("retry_count", 0) + 1,
        "feedback": (
            f"Your previous draft scored {state['groundedness_score']:.2f} on "
            f"groundedness (threshold {content_safety_service.GROUNDEDNESS_THRESHOLD}). "
            "Revise it so every field mapping and rule is directly traceable to the "
            "retrieved source content — remove or flag anything not supported by it."
        ),
    }


def finalize_node(state: PayerIQState) -> dict:
    path = xlsx_builder.build_output(
        output_format=state["output_format"],
        content=state["current_draft"],
        session_id=state["session_id"],
    )
    return {"status": "completed", "output_path": path}


# --------------------------------------------------------- conditional -----

def route_after_guardrail(state: PayerIQState) -> str:
    return "rejected" if state["attack_detected"] else "retrieve"


def route_after_generate(state: PayerIQState) -> str:
    return "content_blocked" if state.get("content_filtered") else "groundedness_check"


def route_after_groundedness(state: PayerIQState) -> str:
    """Retries up to MAX_RETRIES times to raise the groundedness score; once
    exhausted, finalizes with the best draft produced rather than pausing
    for a human reviewer -- human review was dropped from this pipeline."""
    if state["grounded"]:
        return "finalize"
    if state.get("retry_count", 0) < MAX_RETRIES:
        return "retry"
    return "finalize"


# --------------------------------------------------------- telemetry -------

def _timed(name: str, fn: Callable) -> Callable[[PayerIQState], Awaitable[dict]]:
    """Wraps a node function to emit a `node_completed` custom event with
    per-node wall-clock time, without scattering timing/telemetry calls
    through every node body above. Always returns an async wrapper -- safe
    because build_graph()'s caller only ever drives the compiled graph
    through astream()/aget_state() (see routergenerator.py), never the sync
    invoke() path."""
    is_async = inspect.iscoroutinefunction(fn)

    async def wrapper(state: PayerIQState) -> dict:
        start = time.monotonic()
        result = await fn(state) if is_async else fn(state)
        telemetry.track_event("node_completed", {
            "node": name,
            "duration_ms": f"{(time.monotonic() - start) * 1000:.1f}",
            "session_id": state.get("session_id", ""),
            "output_format": state.get("output_format", ""),
        })
        return result

    return wrapper


# ------------------------------------------------------------- build -------

def build_graph(checkpointer: BaseCheckpointSaver):
    g = StateGraph(PayerIQState)

    g.add_node("input_guardrail", _timed("input_guardrail", input_guardrail_node))
    g.add_node("reject", _timed("reject", reject_node))
    g.add_node("content_blocked", _timed("content_blocked", content_blocked_node))
    g.add_node("merge_history", _timed("merge_history", merge_history_node))
    g.add_node("retrieve", _timed("retrieve", retrieve_node))
    g.add_node("generate", _timed("generate", generate_node))
    g.add_node("groundedness_check", _timed("groundedness_check", groundedness_node))
    g.add_node("prepare_retry", _timed("prepare_retry", prepare_retry_node))
    g.add_node("finalize", _timed("finalize", finalize_node))

    g.add_edge(START, "input_guardrail")
    g.add_conditional_edges("input_guardrail", route_after_guardrail, {
        "retrieve": "merge_history",
        "rejected": "reject",
    })
    g.add_edge("reject", END)
    g.add_edge("merge_history", "retrieve")
    g.add_edge("retrieve", "generate")
    g.add_conditional_edges("generate", route_after_generate, {
        "groundedness_check": "groundedness_check",
        "content_blocked": "content_blocked",
    })
    g.add_edge("content_blocked", END)
    g.add_conditional_edges("groundedness_check", route_after_groundedness, {
        "finalize": "finalize",
        "retry": "prepare_retry",
    })
    g.add_edge("prepare_retry", "generate")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer)
