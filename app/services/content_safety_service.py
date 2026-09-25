"""
Thin wrapper around Azure AI Content Safety: Prompt Shields (jailbreak /
prompt-injection detection) and Groundedness Detection. Used by the
LangGraph pipeline (app/graph.py) as its input guardrail and post-generation
groundedness gate.

Requires env vars:
    CONTENT_SAFETY_ENDPOINT
    CONTENT_SAFETY_KEY
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

ENDPOINT = os.getenv("CONTENT_SAFETY_ENDPOINT", "").rstrip("/")
API_KEY = os.getenv("CONTENT_SAFETY_KEY", "")
API_VERSION = "2024-09-01"
# Groundedness Detection is still preview-only and 404s under the GA API
# version above -- it needs its own, separate api-version.
GROUNDEDNESS_API_VERSION = "2024-09-15-preview"

# Tune against a labeled sample of your own STTM/FRD outputs before
# trusting this in production — start conservative and loosen if too
# many correct drafts get flagged.
GROUNDEDNESS_THRESHOLD = 0.85

# Azure's documented per-call limits for text:detectGroundedness are 7,500
# chars for `text` and 55,000 combined for `groundingSources` -- exceeding
# either returns a 400 InvalidRequestBody ("text length exceeds limit").
# Real retrieved context / drafts can easily run longer, so both are
# truncated before the call rather than letting the request fail outright.
MAX_TEXT_CHARS = 7000
MAX_GROUNDING_SOURCES_CHARS = 50000

# text:shieldPrompt caps userPrompt + documents combined at 10,000 chars --
# Microsoft's own guidance is to pack to <=9,500 for safety margin.
MAX_SHIELD_PROMPT_TOTAL_CHARS = 9500


@dataclass
class ShieldResult:
    attack_detected: bool
    raw: dict


@dataclass
class GroundednessResult:
    score: float  # 1.0 = fully grounded, 0.0 = fully ungrounded
    raw: dict


def _headers() -> dict:
    return {"Ocp-Apim-Subscription-Key": API_KEY, "Content-Type": "application/json"}


def _raise_with_body(resp: httpx.Response) -> None:
    """httpx's raise_for_status() only reports the status line, discarding
    Azure's actual error body (e.g. which field failed validation and why)
    -- surface that body in the raised error instead."""
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise httpx.HTTPStatusError(f"{e}\nResponse body: {resp.text}", request=e.request, response=e.response) from None


def _cap_combined_length(items: list[str], max_total_chars: int) -> list[str]:
    """Truncate a list of strings so their combined length stays under
    max_total_chars, cutting off later items first."""
    capped = []
    remaining = max_total_chars
    for item in items:
        if remaining <= 0:
            break
        piece = item[:remaining]
        capped.append(piece)
        remaining -= len(piece)
    return capped


def _require_configured() -> None:
    if not ENDPOINT or not API_KEY:
        raise RuntimeError(
            "CONTENT_SAFETY_ENDPOINT / CONTENT_SAFETY_KEY are not set -- the /v2 "
            "LangGraph pipeline cannot run its guardrails without Azure AI Content Safety."
        )


async def check_prompt_shields(user_prompt: str, documents: list[str]) -> ShieldResult:
    """Call before generation. Flags both direct jailbreak attempts in the
    instruction box and indirect prompt injection hidden inside an
    uploaded document."""
    _require_configured()
    capped_prompt = user_prompt[:MAX_SHIELD_PROMPT_TOTAL_CHARS]
    capped_documents = _cap_combined_length(
        documents, MAX_SHIELD_PROMPT_TOTAL_CHARS - len(capped_prompt)
    )
    body = {"userPrompt": capped_prompt, "documents": capped_documents}
    async with httpx.AsyncClient(timeout=10) as http_client:
        resp = await http_client.post(
            f"{ENDPOINT}/contentsafety/text:shieldPrompt?api-version={API_VERSION}",
            headers=_headers(),
            json=body,
        )
    _raise_with_body(resp)
    data = resp.json()
    attack = data.get("userPromptAnalysis", {}).get("attackDetected", False)
    attack = attack or any(
        d.get("attackDetected") for d in data.get("documentsAnalysis", [])
    )
    return ShieldResult(attack_detected=attack, raw=data)


async def check_groundedness(text: str, grounding_sources: list[str]) -> GroundednessResult:
    """Call after generation. Checks whether the generated draft is
    actually supported by the retrieved source material — this is the
    check that drives the self-correction retry loop."""
    _require_configured()
    body = {
        "domain": "Generic",
        "task": "Summarization",
        "text": text[:MAX_TEXT_CHARS],
        "groundingSources": _cap_combined_length(grounding_sources, MAX_GROUNDING_SOURCES_CHARS),
        "reasoning": False,
    }
    async with httpx.AsyncClient(timeout=15) as http_client:
        resp = await http_client.post(
            f"{ENDPOINT}/contentsafety/text:detectGroundedness?api-version={GROUNDEDNESS_API_VERSION}",
            headers=_headers(),
            json=body,
        )
    _raise_with_body(resp)
    data = resp.json()
    score = 1.0 - data.get("ungroundedPercentage", 0.0)
    return GroundednessResult(score=score, raw=data)
