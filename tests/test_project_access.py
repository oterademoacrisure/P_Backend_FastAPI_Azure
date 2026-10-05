"""
Tests for client/project access enforcement: GET /v2/auth/me/projects, the
mapping check on /v2/generate (and the legacy /generate), and the session
ownership check on /v2/refine and /v2/status. Mapping lookups
(auth_service.get_user_mappings) and the LangGraph graph are faked, so
nothing here touches Cosmos DB or Azure OpenAI.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.routergenerator as routergenerator
from app.services import auth_service

client = TestClient(main.app)

MAPPING = {"ClientID": "Excellus", "ProjectID": "Payment Integrity", "Role": "User"}
GENERATE_FORM = {
    "client_id": "Excellus",
    "project_id": "Payment Integrity",
    "output_format": "STTM",
    "instructions": "Create STTM",
}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(auth_service, "COSMOS_ENDPOINT", "https://fake.example.com:443/")
    monkeypatch.setattr(auth_service, "COSMOS_KEY", "fake-key")
    monkeypatch.setattr(auth_service, "JWT_SECRET", "test-only-secret")
    monkeypatch.setattr(auth_service, "get_user_mappings", AsyncMock(return_value=[MAPPING]))


def _auth(sub: str = "jdoe", role: str = "user") -> dict:
    now = int(time.time())
    token = jwt.encode(
        {"sub": sub, "role": role, "tenantId": "default", "iat": now, "exp": now + 3600},
        auth_service.JWT_SECRET,
        algorithm=auth_service.JWT_ALGORITHM,
    )
    return {"Authorization": f"Bearer {token}"}


def _fake_session(monkeypatch, values: dict):
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values=values)))
    monkeypatch.setattr(routergenerator, "_graph", graph)


OWNED_BY_JDOE = {
    "status": "completed",
    "source_files": [],
    "project_name": "",
    "client_id": "Excellus",
    "project_id": "Payment Integrity",
    "owner": "jdoe",
}


class TestMyProjects:
    def test_user_gets_their_mappings(self):
        r = client.get("/v2/auth/me/projects", headers=_auth())
        assert r.status_code == 200
        # A mapping saved with display names comes back as registry ids + names.
        assert r.json() == {
            "allProjects": False,
            "projects": [{
                "clientId": "excellus", "clientName": "Excellus",
                "projectId": "payment-integrity", "projectName": "Payment Integrity",
            }],
        }

    def test_mapping_to_unregistered_project_is_left_out(self, monkeypatch):
        monkeypatch.setattr(auth_service, "get_user_mappings", AsyncMock(return_value=[
            {"ClientID": "Excellus", "ProjectID": "Retired Project"}, MAPPING,
        ]))
        r = client.get("/v2/auth/me/projects", headers=_auth())
        assert [p["projectId"] for p in r.json()["projects"]] == ["payment-integrity"]

    def test_admin_gets_all_projects_flag(self):
        r = client.get("/v2/auth/me/projects", headers=_auth(role="admin"))
        assert r.json()["allProjects"] is True

    def test_requires_login(self):
        assert client.get("/v2/auth/me/projects").status_code == 401


class TestGenerateAccess:
    def test_unmapped_project_is_403(self):
        form = {**GENERATE_FORM, "client_id": "Scan", "project_id": "Qnxt to EDW mapping"}
        r = client.post("/v2/generate", data=form, headers=_auth())
        assert r.status_code == 403

    def test_mapped_project_passes_access_check(self, monkeypatch):
        # No graph in this test process, so the request stops at the 503
        # right after the access check -- anything but 403 means it passed.
        monkeypatch.setattr(routergenerator, "_graph", None)
        r = client.post("/v2/generate", data=GENERATE_FORM, headers=_auth())
        assert r.status_code == 503

    def test_admin_can_use_any_project(self, monkeypatch):
        monkeypatch.setattr(routergenerator, "_graph", None)
        monkeypatch.setattr(auth_service, "get_user_mappings", AsyncMock(return_value=[]))
        r = client.post("/v2/generate", data=GENERATE_FORM, headers=_auth(role="admin"))
        assert r.status_code == 503

    def test_ids_and_names_are_interchangeable(self, monkeypatch):
        # Mapping saved as "Excellus" / "Payment Integrity"; request sends ids
        # in another spelling -- same registered project, so access passes.
        monkeypatch.setattr(routergenerator, "_graph", None)
        form = {**GENERATE_FORM, "client_id": "excellus", "project_id": "paymentintegrity"}
        r = client.post("/v2/generate", data=form, headers=_auth())
        assert r.status_code == 503

    def test_unregistered_project_is_400_not_silent(self):
        form = {**GENERATE_FORM, "project_id": "Paymnet Integrity"}
        r = client.post("/v2/generate", data=form, headers=_auth())
        assert r.status_code == 400
        assert "Unknown client/project" in r.json()["detail"]

    def test_same_project_name_under_another_client_is_not_accessible(self):
        # AmeriHealth has no "Payment Integrity" project registered.
        form = {**GENERATE_FORM, "client_id": "AmeriHealth"}
        r = client.post("/v2/generate", data=form, headers=_auth(role="admin"))
        assert r.status_code == 400

    def test_missing_client_or_project_is_422(self):
        form = {k: v for k, v in GENERATE_FORM.items() if k != "project_id"}
        r = client.post("/v2/generate", data=form, headers=_auth())
        assert r.status_code == 422

    def test_legacy_generate_route_is_also_checked(self):
        form = {
            "client_id": "Scan",
            "project_id": "Qnxt to EDW mapping",
            "prompt": "Create STTM",
            "formats": "STTM",
        }
        r = client.post("/generate", data=form, headers=_auth())
        assert r.status_code == 403


class TestSessionOwnership:
    def test_owner_can_read_status(self, monkeypatch):
        _fake_session(monkeypatch, OWNED_BY_JDOE)
        r = client.get("/v2/status/s1?output_format=STTM", headers=_auth("jdoe"))
        assert r.status_code == 200

    def test_other_user_gets_404(self, monkeypatch):
        _fake_session(monkeypatch, OWNED_BY_JDOE)
        r = client.get("/v2/status/s1?output_format=STTM", headers=_auth("mallory"))
        assert r.status_code == 404

    def test_owner_who_lost_project_access_gets_403(self, monkeypatch):
        _fake_session(monkeypatch, OWNED_BY_JDOE)
        monkeypatch.setattr(auth_service, "get_user_mappings", AsyncMock(return_value=[]))
        r = client.get("/v2/status/s1?output_format=STTM", headers=_auth("jdoe"))
        assert r.status_code == 403

    def test_session_without_owner_is_admin_only(self, monkeypatch):
        legacy = {k: v for k, v in OWNED_BY_JDOE.items() if k != "owner"}
        _fake_session(monkeypatch, legacy)
        assert client.get("/v2/status/s1?output_format=STTM", headers=_auth("jdoe")).status_code == 404
        assert (
            client.get("/v2/status/s1?output_format=STTM", headers=_auth(role="admin")).status_code == 200
        )

    def test_other_user_cannot_refine(self, monkeypatch):
        _fake_session(monkeypatch, OWNED_BY_JDOE)
        r = client.post(
            "/v2/refine/s1",
            data={"output_format": "STTM", "instructions": "more"},
            headers=_auth("mallory"),
        )
        assert r.status_code == 404

    def test_non_owner_upload_is_not_archived(self, monkeypatch):
        _fake_session(monkeypatch, OWNED_BY_JDOE)
        archive = AsyncMock()
        monkeypatch.setattr(routergenerator, "extract_and_log", archive)
        r = client.post(
            "/v2/refine/s1",
            data={"output_format": "STTM", "instructions": "more"},
            files={"files": ("vendor.txt", b"a,b", "text/plain")},
            headers=_auth("mallory"),
        )
        assert r.status_code == 404
        archive.assert_not_called()


class TestOutputFormat:
    def test_frontend_spellings_share_one_thread(self):
        # The UI posts 'sttm' and downloads with 'STTM' -- same thread.
        assert routergenerator._thread_id("s1", "sttm") == routergenerator._thread_id("s1", "STTM")
        assert routergenerator._thread_id("s1", "gherkin") == "s1::Agile"

    def test_caller_spelling_kept_and_duplicates_dropped(self):
        assert routergenerator.normalize_formats(["sttm", "STTM", "frd"]) == ["sttm", "frd"]

    def test_unknown_format_is_rejected(self, monkeypatch):
        _fake_session(monkeypatch, OWNED_BY_JDOE)
        r = client.post(
            "/v2/refine/s1",
            data={"output_format": "BRD", "instructions": "more"},
            headers=_auth("jdoe"),
        )
        assert r.status_code == 400

    def test_status_accepts_lowercase(self, monkeypatch):
        _fake_session(monkeypatch, OWNED_BY_JDOE)
        r = client.get("/v2/status/s1?output_format=sttm", headers=_auth("jdoe"))
        assert r.status_code == 200
        routergenerator._graph.aget_state.assert_awaited_with(
            {"configurable": {"thread_id": "s1::STTM"}}
        )


from app.services import azure_search_service as search  # noqa: E402

ROOT = "https://acct.blob.core.windows.net/sharepoint-docs/"
INDEXED = [
    {"title": "STTM_Template.xlsx", "path": ROOT + "STTM_Template.xlsx"},
    {"title": "Client_Policy.docx", "path": ROOT + "excellus/Client_Policy.docx"},
    {"title": "Rules.docx", "path": ROOT + "excellus/payment-integrity/Rules.docx"},
    {"title": "Rules.docx", "path": ROOT + "amerihealth/payment-integrity/Rules.docx"},
    {"title": "Rules.docx", "path": ROOT + "amerihealth/req/Rules.docx"},
    {"title": "Letters.docx", "path": ROOT + "excellus/correspondence-mapping/Letters%20v2.docx"},
]
EXCELLUS_PI = ROOT + "excellus/payment-integrity/Rules.docx"


class TestDocumentScoping:
    def test_project_gets_root_client_and_own_folder(self):
        docs = search.documents_for_project(INDEXED, "excellus/payment-integrity")
        assert [d["path"] for d in docs] == [
            ROOT + "STTM_Template.xlsx", ROOT + "excellus/Client_Policy.docx", EXCELLUS_PI,
        ]

    def test_same_project_name_under_another_client_is_excluded(self):
        docs = search.documents_for_project(INDEXED, "amerihealth/req")
        paths = [d["path"] for d in docs]
        assert EXCELLUS_PI not in paths and ROOT + "excellus/Client_Policy.docx" not in paths
        assert ROOT + "amerihealth/payment-integrity/Rules.docx" not in paths

    def test_per_document_filter_is_exact_path(self):
        assert search.build_path_filter(ROOT + "it's.docx") == f"storage_path eq '{ROOT}it''s.docx'"

    def test_only_project_documents_are_searched(self, monkeypatch):
        import asyncio

        monkeypatch.setattr(search, "_list_indexed_documents_cached", AsyncMock(return_value=INDEXED))
        searched = AsyncMock(return_value=[])
        monkeypatch.setattr(search, "search_knowledge_base", searched)
        asyncio.run(search.retrieve_grounding("q", "p", folder="excellus/payment-integrity"))
        filters = sorted(c.kwargs["filter_expression"] for c in searched.await_args_list)
        assert filters == sorted(
            search.build_path_filter(p)
            for p in (ROOT + "STTM_Template.xlsx", ROOT + "excellus/Client_Policy.docx", EXCELLUS_PI)
        )

    def test_no_project_documents_means_no_grounding_not_whole_index(self, monkeypatch):
        import asyncio

        # e.g. storage_path not in the index yet, so the document list is empty
        monkeypatch.setattr(search, "_list_indexed_documents_cached", AsyncMock(return_value=[]))
        searched = AsyncMock(return_value=[{"source_document": "x", "excerpt": "leak", "relevance_score": 1}])
        monkeypatch.setattr(search, "search_knowledge_base", searched)
        context, sources = asyncio.run(search.retrieve_grounding("q", "p", folder="amerihealth/req"))
        assert sources == []
        searched.assert_not_called()
