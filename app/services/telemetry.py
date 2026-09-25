"""
Thin, fail-open wrapper around Azure Monitor OpenTelemetry.

Every telemetry call in the app -- per-node timing in graph.py, guardrail
and groundedness outcomes -- goes through track_event() here instead of the
SDK directly, so a missing APPLICATIONINSIGHTS_CONNECTION_STRING degrades to
a no-op rather than crashing a request. That matches the pattern already
used for Cosmos DB and Content Safety elsewhere in this codebase: an Azure
dependency that's required in production but must not block local/dev work
when it isn't configured.

configure() wires OpenTelemetry's auto-instrumentation (FastAPI, httpx,
logging, ...) into Azure Monitor and must run before FastAPI() is
instantiated, since the FastAPI instrumentor patches the class itself --
see app/main.py, where it's called immediately after load_dotenv().

Requires env var:
    APPLICATIONINSIGHTS_CONNECTION_STRING
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_enabled = False


def configure() -> None:
    global _enabled
    if not os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        print("Warning: APPLICATIONINSIGHTS_CONNECTION_STRING not set -- telemetry disabled.")
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor()
        _enabled = True
    except Exception as e:
        print(f"Warning: telemetry init failed, continuing without it: {e}")


def track_event(name: str, properties: dict | None = None) -> None:
    """Fire-and-forget a customEvents row. Safe to call whether or not
    configure() has run or succeeded -- a no-op until telemetry is enabled,
    and any SDK failure is swallowed rather than allowed to fail the
    request that triggered it."""
    if not _enabled:
        return
    try:
        from azure.monitor.events.extension import track_event as _track_event

        _track_event(name, {k: str(v) for k, v in (properties or {}).items()})
    except Exception:
        logger.exception("track_event(%s) failed", name)
