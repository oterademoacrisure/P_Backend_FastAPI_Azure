"""
Unit tests for app/services/project_registry.py against the real
app/config/projects.json -- no network.
"""

from __future__ import annotations

import json
import os
import tempfile

from app.services import project_registry


class TestResolve:
    def test_ids_and_display_names_resolve_to_the_same_project(self):
        by_name = project_registry.resolve("Excellus", "Payment Integrity")
        assert by_name is not None
        for client, project in (
            ("excellus", "payment-integrity"),
            ("EXCELLUS ", "paymentintegrity"),
            ("Excellus", "Payment_Integrity"),
        ):
            assert project_registry.resolve(client, project) == by_name

    def test_folder_is_client_plus_project(self):
        assert project_registry.resolve("Excellus", "Payment Integrity").folder == "excellus/payment-integrity"
        assert project_registry.resolve("Scan", "Qnxt to EDW mapping").folder == "scan/qnxt-to-edw-mapping"

    def test_unknown_or_misspelled_is_rejected(self):
        assert project_registry.resolve("Excellus", "Paymnet Integrity") is None
        assert project_registry.resolve("Unknown Client", "Payment Integrity") is None
        assert project_registry.resolve("", "Payment Integrity") is None
        assert project_registry.resolve("Excellus", None) is None

    def test_project_belongs_to_one_client_only(self):
        # Payment Integrity is registered under Excellus, not AmeriHealth.
        assert project_registry.resolve("AmeriHealth", "Payment Integrity") is None

    def test_clients_listing_for_register_page(self):
        listing = {c["clientId"]: [p["projectId"] for p in c["projects"]] for c in project_registry.clients()}
        assert "payment-integrity" in listing["excellus"] and "medical-claims" in listing["excellus"]
        assert listing["amerihealth"] == ["req"]


class TestRegistryValidation:
    def _load(self, raw: dict):
        saved = project_registry.REGISTRY_PATH
        path = os.path.join(tempfile.mkdtemp(), "projects.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        project_registry.REGISTRY_PATH = path
        project_registry.all_projects.cache_clear()
        try:
            return project_registry.all_projects()
        finally:
            project_registry.REGISTRY_PATH = saved
            project_registry.all_projects.cache_clear()

    def test_two_projects_that_would_resolve_alike_are_refused(self):
        raw = {"clients": [{"clientId": "c", "clientName": "C", "projects": [
            {"projectId": "payment-integrity", "projectName": "Payment Integrity"},
            {"projectId": "paymentintegrity", "projectName": "PI v2"},
        ]}]}
        try:
            self._load(raw)
        except ValueError as e:
            assert "clashes" in str(e)
        else:
            raise AssertionError("clashing project ids were accepted")

    def test_same_project_name_under_two_clients_is_fine(self):
        raw = {"clients": [
            {"clientId": "a", "clientName": "A", "projects": [{"projectId": "pi", "projectName": "Payment Integrity"}]},
            {"clientId": "b", "clientName": "B", "projects": [{"projectId": "pi", "projectName": "Payment Integrity"}]},
        ]}
        folders = sorted(p.folder for p in self._load(raw))
        assert folders == ["a/pi", "b/pi"]
