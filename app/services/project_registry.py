"""
The clients and projects BA Assist serves (app/config/projects.json), and
the one place that turns a client/project -- as typed, as stored in an old
mapping, or as sent by the frontend -- into a known project and its Blob
folder.

Two problems this exists to prevent (ACCESS_AND_PROJECT_MAPPING.md §4):
1. Two clients with a project of the same name sharing one folder. A
   project's folder is always <clientId>/<projectId>, e.g.
   excellus/payment-integrity, so AmeriHealth's "Payment Integrity" could
   never read Excellus's documents or ontology.
2. A free-text id silently pointing at a folder that doesn't exist
   ("paymentintegrity" vs "payment-integrity"): resolve() matches ids and
   display names loosely (case, spaces, hyphens and underscores ignored),
   and returns None for anything unknown, which callers reject with an
   error instead of generating without the project's documents and model.

Mappings saved before this registry stored display names ("Excellus",
"Payment Integrity"); those still resolve, so no data migration is needed.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REGISTRY_PATH = os.getenv(
    "PROJECT_REGISTRY_PATH", str(Path(__file__).resolve().parent.parent / "config" / "projects.json")
)


@dataclass(frozen=True)
class Project:
    client_id: str
    client_name: str
    project_id: str
    project_name: str

    @property
    def folder(self) -> str:
        """Blob folder (and ontology location) for this project."""
        return f"{self.client_id}/{self.project_id}"

    def as_dict(self) -> dict:
        return {
            "clientId": self.client_id,
            "clientName": self.client_name,
            "projectId": self.project_id,
            "projectName": self.project_name,
        }


def _key(text: str | None) -> str:
    """'Payment Integrity', 'payment-integrity', 'PaymentIntegrity ' -> 'paymentintegrity'."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


@lru_cache(maxsize=1)
def all_projects() -> tuple[Project, ...]:
    """Every registered project. Raises at load if two clients or two
    projects of one client would resolve to the same key -- that ambiguity
    is exactly what this registry is meant to rule out."""
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    projects: list[Project] = []
    client_keys: dict[str, str] = {}
    for c in raw.get("clients", []):
        for k in {_key(c["clientId"]), _key(c["clientName"])}:
            if client_keys.setdefault(k, c["clientId"]) != c["clientId"]:
                raise ValueError(f"Client {c['clientId']!r} clashes with {client_keys[k]!r} in {REGISTRY_PATH}")
        project_keys: dict[str, str] = {}
        for p in c.get("projects", []):
            for k in {_key(p["projectId"]), _key(p["projectName"])}:
                if project_keys.setdefault(k, p["projectId"]) != p["projectId"]:
                    raise ValueError(
                        f"Project {p['projectId']!r} clashes with {project_keys[k]!r} under {c['clientId']!r}"
                    )
            projects.append(Project(c["clientId"], c["clientName"], p["projectId"], p["projectName"]))
    return tuple(projects)


def resolve(client: str | None, project: str | None) -> Project | None:
    """The registered project for this client and project, given as ids or
    display names in any case or spacing; None if either is unknown."""
    ck, pk = _key(client), _key(project)
    if not (ck and pk):
        return None
    for p in all_projects():
        if ck in (_key(p.client_id), _key(p.client_name)) and pk in (_key(p.project_id), _key(p.project_name)):
            return p
    return None


def clients() -> list[dict]:
    """Registry grouped by client, for the admin Register page's dropdowns."""
    grouped: dict[str, dict] = {}
    for p in all_projects():
        entry = grouped.setdefault(p.client_id, {"clientId": p.client_id, "clientName": p.client_name, "projects": []})
        entry["projects"].append({"projectId": p.project_id, "projectName": p.project_name})
    return list(grouped.values())
