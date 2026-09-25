"""
Single source of truth for which Azure OpenAI deployment(s) this service uses.

Azure OpenAI identifies a model by *deployment name*, not the underlying model
name directly -- the deployment is what actually gets repointed when the model
behind it changes (e.g. gpt-4.1-mini -> a newer model). Every module that calls
the API should read its deployment name from here instead of calling
os.getenv() itself, so there is exactly one place that decides "which model
are we using right now," instead of each module drifting independently.
"""

import os
from dotenv import load_dotenv

load_dotenv()

DEFAULT_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "").strip()
FALLBACK_DEPLOYMENT = os.getenv("AZURE_OPENAI_FALLBACK_DEPLOYMENT_NAME", "").strip()

ALLOWED_DEPLOYMENTS = {d for d in (DEFAULT_DEPLOYMENT, FALLBACK_DEPLOYMENT) if d}


def validate_startup_config() -> None:
    """
    Fail fast at app boot if no default deployment is configured. Deliberately
    does not make a live call to Azure here -- that would block startup and
    take the whole service down on a transient Azure hiccup. Use GET /health
    for an on-demand check that the deployment actually responds.
    """
    if not DEFAULT_DEPLOYMENT:
        raise RuntimeError(
            "AZURE_OPENAI_DEPLOYMENT_NAME is not set. The service cannot serve "
            "any /generate requests without a default model deployment."
        )


def resolve_deployment(requested: str | None) -> str:
    """
    Resolves a caller-supplied model override to a configured deployment name.
    Returns the default deployment when no override is given. Raises ValueError
    if the override isn't one of the deployments configured via env vars, so
    callers can turn that into a 400 instead of silently using the wrong model.
    """
    if not requested:
        return DEFAULT_DEPLOYMENT
    if requested not in ALLOWED_DEPLOYMENTS:
        raise ValueError(
            f"Unknown model '{requested}'. Allowed values: {sorted(ALLOWED_DEPLOYMENTS)}"
        )
    return requested
