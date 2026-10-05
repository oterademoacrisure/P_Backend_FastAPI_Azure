"""
Unit tests for app/services/ontology_service.py against the real
Payment Integrity ontology (ontology/excellus/payment-integrity.json) -- no network.
Covers per-project lookup, the prompt block, STTM target validation, the
in-code confidence cap, the groundedness excerpt, and how the block reaches
the system prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

from app.services import draft_repair, ontology_service, project_registry
from app.services.prompt_templates import build_system_message

# Never reach Blob Storage from unit tests -- lookups use local files only.
ontology_service.ONTOLOGY_STORAGE_CONNECTION_STRING = ""

ONT = ontology_service.load_file(
    os.path.join(ontology_service.ONTOLOGY_DIR, "excellus", "payment-integrity.json"), "excellus/payment-integrity"
)

HEADER = (
    "Mapping ID|Target Table / Output Object (Entity Name)|Target Field Name|"
    "Target Data Type|Mapping Confidence|Open Question|Notes"
)


def _draft(*rows: str) -> str:
    return "## 1. STTM Summary\nAttribute|Detail\nVersion|1.0\n\n## 2. STTM Mapping\n" + "\n".join((HEADER,) + rows)


def _mapping_rows(draft: str) -> list[dict[str, str]]:
    return draft_repair._find_mapping_table(draft)


def _lookup(project, directory=None, client="Excellus"):
    """get_ontology() for a registered project (resolved through the
    registry, as the pipeline does) with a fresh cache, optionally against
    another directory."""
    registered = project_registry.resolve(client, project)
    folder = registered.folder if registered else None
    saved_dir = ontology_service.ONTOLOGY_DIR
    ontology_service._cache.clear()
    if directory:
        ontology_service.ONTOLOGY_DIR = directory
    try:
        return asyncio.run(ontology_service.get_ontology(folder))
    finally:
        ontology_service.ONTOLOGY_DIR = saved_dir
        ontology_service._cache.clear()


class TestPerProjectLookup:
    def test_project_id_maps_to_its_own_file(self):
        ontology = _lookup("Payment Integrity")
        assert ontology.project == "excellus/payment-integrity"
        assert ontology.source.replace("\\", "/").endswith("excellus/payment-integrity.json")
        assert ontology.find_entity("PI Opportunity") == "pi opportunity"

    def test_project_without_ontology_gets_none_not_another_projects(self):
        assert _lookup("Correspondence mapping") is None
        assert _lookup("Qnxt to EDW mapping", client="Scan") is None

    def test_no_project_id(self):
        assert _lookup("") is None and _lookup(None) is None

    def test_each_project_validates_against_its_own_model(self):
        with tempfile.TemporaryDirectory() as d:
            other = {"name": "Correspondence", "entities": [{"name": "Letter", "status": "approved",
                     "attributes": [{"name": "Letter_ID", "dataType": "VARCHAR", "status": "approved"}]}]}
            os.makedirs(os.path.join(d, "excellus"))
            with open(os.path.join(d, "excellus", "correspondence-mapping.json"), "w", encoding="utf-8") as f:
                json.dump(other, f)
            correspondence = _lookup("Correspondence mapping", d)
        row = "M-001|Claim Header|Claim_ID|VARCHAR|Confirmed|N/A|n"
        assert ontology_service.find_violations(_draft(row), ONT) == []
        assert [v.kind for v in ontology_service.find_violations(_draft(row), correspondence)] == ["unknown_entity"]
        assert "Letter" in ontology_service.prompt_context("STTM", correspondence)
        assert "\nClaim Header -- " not in ontology_service.prompt_context("STTM", correspondence)

    def test_invalid_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "amerihealth"))
            with open(os.path.join(d, "amerihealth", "req.json"), "w", encoding="utf-8") as f:
                f.write("{not json")
            assert _lookup("Req", d, client="AmeriHealth") is None

    def test_result_is_cached(self):
        ontology_service._cache.clear()
        first = asyncio.run(ontology_service.get_ontology("excellus/payment-integrity"))
        second = asyncio.run(ontology_service.get_ontology("Excellus/Payment-Integrity/"))
        ontology_service._cache.clear()
        assert first is second


class TestMedicalClaimsProject:
    """The second real project: same template, different columns and rules."""

    def test_lookup_and_separation(self):
        medical = _lookup("Medical Claims")
        assert medical.project == "excellus/medical-claims"
        medical_prompt = ontology_service.prompt_context("STTM", medical)
        pi_prompt = ontology_service.prompt_context("STTM", ONT)
        assert "PI Opportunity" not in medical_prompt and "MC-G1" in medical_prompt
        assert "Claim_Line_Status" not in pi_prompt and "PI-G1" in pi_prompt

    def test_provider_roles_in_aliases(self):
        medical = _lookup("Medical Claims")
        prompt = ontology_service.prompt_context("STTM", medical)
        assert "Billing Provider NPI, Billing NPI -> Provider.NPI (Billing role) [proposed]" in prompt

    def test_every_alias_targets_a_real_attribute(self):
        medical = _lookup("Medical Claims")
        for alias in medical.raw["aliases"]:
            entity, attribute = alias["target"].split(".", 1)
            key = medical.find_entity(entity)
            assert key and ontology_service._norm(attribute) in medical.attributes[key], alias["target"]

    def test_reference_sttm_targets_are_flagged(self):
        """Targets from the hand-made reference STTM that aren't in the model."""
        medical = _lookup("Medical Claims")
        draft = _draft(
            "M-001|Claim_Line|Claim_Number|VARCHAR|Confirmed|N/A|n",
            "M-002|Provider|Billing_Provider_NPI|VARCHAR|Confirmed|N/A|n",
            "M-003|Claim Line|Claim_Line_Number|INTEGER|Confirmed|N/A|n",
        )
        kinds = [(v.kind, v.target) for v in ontology_service.find_violations(draft, medical)]
        assert ("unknown_attribute", "Claim_Line.Claim_Number") in kinds
        assert ("unknown_attribute", "Provider.Billing_Provider_NPI") in kinds
        assert not any(t == "Claim Line.Claim_Line_Number" for _, t in kinds)


class TestLoading:
    def test_entity_lookup(self):
        assert ONT.find_entity("Claim Header") == "claim header"
        assert ONT.find_entity("gold.Claim_Header (Silver)") == "claim header"
        assert ONT.find_entity("Claims Warehouse") is None

    def test_summary_counts(self):
        s = ontology_service.summary(ONT, "excellus/payment-integrity")
        assert s["loaded"] is True and s["project"] == "excellus/payment-integrity"
        assert s["entities"] == {"approved": 31, "proposed": 4}
        assert ontology_service.summary(None, "amerihealth/req") == {"loaded": False, "project": "amerihealth/req"}

    def test_prompt_names_the_project(self):
        assert "project excellus/payment-integrity" in ontology_service.prompt_context("STTM", ONT)

    def test_guardrails_rendered(self):
        ontology = ontology_service.Ontology(
            {"entities": [], "guardrails": [{"id": "G1", "text": "Never map PHI to free text.", "status": "approved"}]},
            project="req",
        )
        assert "PROJECT GUARDRAILS:\n- G1: Never map PHI to free text." in ontology_service.prompt_context("STTM", ontology)


class TestPromptContext:
    def test_sttm_and_frd_get_the_block(self):
        for fmt in ("STTM", "sttm", "FRD"):
            text = ontology_service.prompt_context(fmt, ONT)
            assert "Claim Header" in text and "RELATIONSHIPS" in text and "PI-R1" in text

    def test_frd_instruction_lists_the_17_section_headings_in_order(self):
        from app.services.prompt_templates import resolve_format_instruction

        instruction = resolve_format_instruction("FRD")
        titles = [
            "Executive Summary", "Business Objective", "Current State", "Future State", "Scope",
            "Out of Scope", "Personas", "Functional Requirements", "Business Rules",
            "Data Requirements", "Reporting Requirements", "Integration Requirements",
            "Security Requirements", "NFRs", "Assumptions", "Risks", "Open Questions",
        ]
        positions = [instruction.index(f"## {n}. {t}\n") for n, t in enumerate(titles, start=1)]
        assert positions == sorted(positions)
        assert "FR-001" in instruction

    def test_agile_does_not(self):
        assert ontology_service.prompt_context("Agile", ONT) == ""

    def test_proposed_items_are_tagged(self):
        text = ontology_service.prompt_context("STTM", ONT)
        assert "PI Opportunity [proposed]" in text
        assert "Dependent_Sequence VARCHAR(2)" in text

    def test_system_message_includes_block_only_when_given(self):
        with_block = build_system_message("kb", ontology_service.prompt_context("STTM", ONT))
        assert "=== CANONICAL ONTOLOGY" in with_block
        assert with_block.index("=== CANONICAL ONTOLOGY") < with_block.index("=== KNOWLEDGE BASE CONTEXT ===")
        assert "CANONICAL ONTOLOGY" not in build_system_message("kb")


class TestFindViolations:
    def test_clean_draft(self):
        draft = _draft(
            "M-001|Claim Header|Claim_ID|VARCHAR(50)|Confirmed|N/A|n",
            "M-002|PI Opportunity|Audit_Type|VARCHAR(50)|Candidate|Confirm audit category|n",
        )
        assert ontology_service.find_violations(draft, ONT) == []

    def test_proposed_target_marked_confirmed(self):
        draft = _draft("M-001|PI Opportunity|Audit_Type|VARCHAR|Confirmed|N/A|n")
        assert [v.kind for v in ontology_service.find_violations(draft, ONT)] == ["proposed_confirmed"]

    def test_proposed_attribute_on_approved_entity(self):
        draft = _draft("M-001|Member|Dependent_Sequence|VARCHAR(2)|Confirmed|N/A|n")
        assert [v.kind for v in ontology_service.find_violations(draft, ONT)] == ["proposed_confirmed"]

    def test_misspelled_entity_suggests_match_even_when_not_confirmed(self):
        draft = _draft("M-001|Claim Headers|Claim_ID|VARCHAR|Candidate|Confirm|n")
        (v,) = ontology_service.find_violations(draft, ONT)
        assert v.kind == "unknown_entity" and "Claim Header" in v.message

    def test_misspelled_attribute(self):
        draft = _draft("M-001|Claim Header|Claim_IDs|VARCHAR|Confirmed|N/A|n")
        (v,) = ontology_service.find_violations(draft, ONT)
        assert v.kind == "unknown_attribute" and "Claim_ID" in v.message

    def test_unknown_target_only_flagged_when_confirmed(self):
        gap = "M-001|Claim Header|Vendor_Batch_Code|VARCHAR|Needs SME Review|Not in canonical model|n"
        assert ontology_service.find_violations(_draft(gap), ONT) == []
        confirmed = gap.replace("Needs SME Review", "Confirmed")
        assert [v.kind for v in ontology_service.find_violations(_draft(confirmed), ONT)] == ["unknown_attribute"]

    def test_datatype_mismatch_with_synonyms(self):
        ok = _draft("M-001|Claim Header|Total_Paid_Amount|NUMERIC(12,2)|Confirmed|N/A|n")
        assert ontology_service.find_violations(ok, ONT) == []
        bad = _draft("M-001|Claim Header|Total_Paid_Amount|VARCHAR(20)|Confirmed|N/A|n")
        assert [v.kind for v in ontology_service.find_violations(bad, ONT)] == ["datatype_mismatch"]

    def test_no_mapping_section(self):
        assert ontology_service.find_violations("## Introduction\nSome FRD prose.", ONT) == []

    def test_copied_proposed_tag_is_stripped_and_tolerated(self):
        """Live output: every PI row read 'PI Opportunity [proposed]', which
        no longer named an entity, so all were capped at Needs SME Review."""
        draft = _draft("M-001|PI Opportunity [proposed]|Audit_Type|VARCHAR(50)|Candidate|Confirm category|n")
        assert ONT.find_entity("PI Opportunity [proposed]") == ONT.find_entity("PI Opportunity")
        cleaned = ontology_service.strip_status_tags(draft)
        assert "M-001|PI Opportunity|Audit_Type" in cleaned
        assert "|Candidate|" in ontology_service.enforce_confidence(cleaned, ONT)

    def test_frd_data_requirements_are_checked(self):
        """The FRD prompt's Data Requirements table reuses the STTM column
        names, so the same ontology check covers an FRD."""
        header = (
            "Data Requirement ID|Target Entity|Target Field Name|Target Data Type|"
            "Requirement|Mapping Confidence|Open Question"
        )
        draft = (
            "## 8. Functional Requirements\nRequirement ID|Requirement|Source / Rationale|Acceptance Criteria\n"
            "FR-001|The system shall load claims.|Spec|Rows load\n\n"
            f"## 10. Data Requirements\n{header}\n"
            "DR-001|Claim Header|Claim_ID|VARCHAR(50)|Required key|Confirmed|N/A\n"
            "DR-002|Claim Headers|Claim_ID|VARCHAR(50)|Required key|Confirmed|N/A\n"
        )
        (v,) = ontology_service.find_violations(draft, ONT)
        assert v.kind == "unknown_entity" and "Claim Header" in v.message

    def test_feedback_names_each_target(self):
        draft = _draft("M-001|PI Opportunity|Audit_Type|VARCHAR|Confirmed|N/A|n")
        feedback = ontology_service.correction_feedback(ontology_service.find_violations(draft, ONT))
        assert "PI Opportunity.Audit_Type" in feedback


class TestEntityFromSummary:
    """Live-run layout: no Target Table column, entity named once in the summary."""
    DRAFT = (
        "## 1. STTM Summary\nAttribute|Detail\nTarget Entity|PI Opportunity\n\n"
        "## 2. STTM Mapping\nTarget Field Name|Target Data Type|Mapping Confidence|Open Question\n"
        "Audit_Type|VARCHAR(50)|Confirmed|N/A\n"
        "Identified_Overpayment_Amount|DECIMAL(12,2)|Candidate|Confirm amount\n"
    )

    def test_proposed_target_detected(self):
        found = ontology_service.find_violations(self.DRAFT, ONT)
        assert [(v.kind, v.target) for v in found] == [("proposed_confirmed", "PI Opportunity.Audit_Type")]

    def test_capped_without_adding_a_column(self):
        fixed = ontology_service.enforce_confidence(self.DRAFT, ONT)
        assert "Audit_Type|VARCHAR(50)|Candidate|Confirm PI Opportunity.Audit_Type" in fixed
        assert "from summary" not in fixed
        assert not draft_repair.has_malformed_rows(fixed)


class TestConfidenceValuesAndCoverage:
    """Both seen in a live Payment Integrity run."""

    SOURCE = {"filename": "vendor.xlsx", "text": (
        "Sample Vendor Extract - Cotiviti\n"
        "Claim Number, Paid Date, Total Paid Amount, Subscriber ID, Audit Type\n"
        "CLM1, 2026-01-02 00:00:00, 100.5, S1, DRG Validation\n"
    )}

    def test_source_columns_skip_title_rows(self):
        assert ontology_service._source_columns(self.SOURCE["text"]) == [
            "Claim Number", "Paid Date", "Total Paid Amount", "Subscriber ID", "Audit Type"]
        assert ontology_service._source_columns("Just some prose, nothing tabular here.") == []

    def test_open_question_text_in_confidence_column(self):
        draft = _draft("M-001|PI Opportunity|Vendor_Key|INTEGER|Confirm vendor key for Cotiviti|N/A|n")
        assert [v.kind for v in ontology_service.find_violations(draft, ONT)] == ["invalid_confidence"]
        (row,) = _mapping_rows(ontology_service.enforce_confidence(draft, ONT))
        assert row["Mapping Confidence"] == "Needs SME Review"
        assert row["Open Question"] == "Confirm vendor key for Cotiviti"

    def test_unmapped_columns_found(self):
        draft = (
            "## 2. STTM Mapping\n"
            "Target Table / Output Object (Entity Name)|Target Field Name|Source Field|Mapping Confidence\n"
            "Claim Header|Claim_ID|Claim Number|Confirmed\n\n"
            "## 3. Assumptions and Open Q\n"
            "Type|ID|Statement\n"
            "Open Question|Q-1|Audit Type has no approved target\n"
        )
        assert ontology_service.find_unmapped_source_columns(draft, [self.SOURCE]) == [
            "Paid Date", "Total Paid Amount", "Subscriber ID"]

    def test_feedback_lists_unmapped_columns(self):
        text = ontology_service.correction_feedback([], ["Subscriber ID"])
        assert "Subscriber ID" in text and "ALIASES" in text


class TestEnforceConfidence:
    def test_caps_proposed_and_unknown(self):
        draft = _draft(
            "M-001|Claim Header|Claim_ID|VARCHAR|Confirmed|N/A|n",
            "M-002|PI Opportunity|Audit_Type|VARCHAR|Confirmed|N/A|n",
            "M-003|Claim Header|Vendor_Batch_Code|VARCHAR|Confirmed|N/A|n",
        )
        rows = _mapping_rows(ontology_service.enforce_confidence(draft, ONT))
        assert [r["Mapping Confidence"] for r in rows] == ["Confirmed", "Candidate", "Needs SME Review"]
        assert rows[0]["Open Question"] == "N/A"
        assert "proposed ontology item" in rows[1]["Open Question"]
        assert "not in the canonical ontology" in rows[2]["Open Question"]

    def test_never_raises_confidence_or_overwrites_questions(self):
        draft = _draft("M-001|PI Opportunity|Audit_Type|VARCHAR|Needs SME Review|Which audit list?|n")
        assert ontology_service.enforce_confidence(draft, ONT) == draft

    def test_result_passes_existing_checks(self):
        draft = _draft("M-001|PI Opportunity|Audit_Type|VARCHAR|Confirmed|N/A|n")
        fixed = ontology_service.enforce_confidence(draft, ONT)
        assert not draft_repair.has_malformed_rows(fixed)
        assert draft_repair.find_placeholder_open_questions(fixed) == []
        assert "## 1. STTM Summary" in fixed


class TestGroundingExcerpt:
    def test_only_targeted_entities(self):
        draft = _draft("M-001|Claim Header|Claim_ID|VARCHAR|Confirmed|N/A|n")
        excerpt = ontology_service.grounding_excerpt(draft, ONT)
        assert "Claim Header" in excerpt and "Total_Paid_Amount" in excerpt
        assert "Lab Result" not in excerpt
        assert len(excerpt) < 5000

    def test_empty_without_mapping(self):
        assert ontology_service.grounding_excerpt("## Intro\nprose", ONT) == ""
