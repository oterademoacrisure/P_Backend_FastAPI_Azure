"""
Owns the Azure OpenAI client and the deployment-failover logic, so there is
one place that actually talks to Azure OpenAI instead of app/main.py and
app/graph.py each holding their own client. Used by:
- app/main.py                (direct one-shot /generate pipeline)
- app/services/document generation for the LangGraph pipeline (app/graph.py)
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from openai import AsyncAzureOpenAI, BadRequestError, NotFoundError, RateLimitError

from app.config.model_config import DEFAULT_DEPLOYMENT, FALLBACK_DEPLOYMENT
from app.services.prompt_templates import (
    build_system_message,
    build_user_message,
    resolve_format_instruction,
)

load_dotenv()

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
OPENAI_API_VERSION = os.getenv("OPENAI_API_VERSION", "2024-02-01")

client = AsyncAzureOpenAI(
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_key=AZURE_OPENAI_API_KEY,
    api_version=OPENAI_API_VERSION,
)


def is_content_filter_error(exc: Exception) -> bool:
    """True if `exc` is Azure OpenAI's own Responsible AI content filter
    rejecting a completion -- a second, independent safety layer from this
    project's own Prompt Shields check (content_safety_service.py). It fires
    on the *prompt* sent to the model (which embeds the analyst's raw
    instruction), so a phrasing mild enough to pass Prompt Shields can still
    trip this one. Azure's error body nests the real code one level down
    (`{"error": {"code": "content_filter", ...}}`), not at the top level
    where openai.APIError.code looks for it -- hence checking both, plus a
    string fallback in case the exact shape drifts across API versions."""
    if not isinstance(exc, BadRequestError):
        return False
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        if inner.get("code") == "content_filter":
            return True
    return "content_filter" in str(exc)


async def create_completion_with_failover(
    messages: list[dict], temperature: float, deployment: str
):
    """
    Calls the requested deployment; if it has been retired/renamed
    (NotFoundError) or is throttled (RateLimitError), retries once against
    FALLBACK_DEPLOYMENT so one bad deployment doesn't take generation down.
    Any other exception (e.g. a content-policy rejection) is not retried --
    switching models wouldn't fix it, and retrying would just double latency
    and cost while hiding the real error.
    """
    try:
        return await client.chat.completions.create(
            model=deployment, messages=messages, temperature=temperature
        )
    except (NotFoundError, RateLimitError):
        if not FALLBACK_DEPLOYMENT or FALLBACK_DEPLOYMENT == deployment:
            raise
        return await client.chat.completions.create(
            model=FALLBACK_DEPLOYMENT, messages=messages, temperature=temperature
        )


async def generate_document(
    output_format: str,
    instruction_history: list[dict],
    retrieved_context: str,
    source_files: list[dict],
    prior_draft: str | None,
    correction_feedback: str,
    deployment: str | None = None,
) -> str:
    """
    Drafts (or revises) one document for the LangGraph pipeline.

    - instruction_history carries every instruction given so far this session
      (initial + any /refine turns); only the latest one drives this draft,
      the rest gives the model conversational context.
    - source_files carries every file uploaded so far this session (see
      app.graph.PayerIQState.source_files) -- the customer's vendor
      files/standards, distinct from retrieved_context (the enterprise
      knowledge-base grounding pulled from Azure AI Search).
    - prior_draft is included whenever a previous draft exists, so a
      user-driven refine turn ("keep everything the same, but only revise
      the rows for X") can actually see and revise it, not just regenerate
      from scratch. correction_feedback is the extra instruction added on
      top when this is specifically an automatic groundedness-retry (see
      prepare_retry_node in app/graph.py) rather than a user-initiated turn.
    """
    instruction = resolve_format_instruction(output_format)
    system_msg = build_system_message(retrieved_context)

    latest_instruction = instruction_history[-1]["instruction"] if instruction_history else ""
    prior_turns = instruction_history[:-1]

    prompt_parts = []
    if prior_turns:
        history_lines = "\n".join(f"- {h['instruction']}" for h in prior_turns)
        prompt_parts.append(f"Earlier instructions this session:\n{history_lines}")
    prompt_parts.append(f"Current instruction:\n{latest_instruction}")

    if prior_draft:
        prompt_parts.append(f"You previously produced this draft:\n{prior_draft}")
        if correction_feedback:
            prompt_parts.append(f"Revise it per this feedback:\n{correction_feedback}")
        else:
            prompt_parts.append(
                "Revise that draft according to the current instruction above -- keep "
                "everything not affected by the instruction unchanged, and change only "
                "what the instruction asks for. If the instruction asks you to add a new "
                "field/row, add it exactly once -- never emit the same new field as two "
                "separate rows in the same table."
            )

    source_text = "\n\n".join(
        f"--- {f['filename']} ---\n{f['text']}" for f in source_files
    )

    user_msg = build_user_message(
        project="",
        prompt="\n\n".join(prompt_parts),
        source_text=source_text,
        instruction=instruction,
    )

    resp = await create_completion_with_failover(
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
        deployment=deployment or DEFAULT_DEPLOYMENT,
    )
    return resp.choices[0].message.content.strip()
