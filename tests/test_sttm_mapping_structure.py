"""
Regression coverage for the STTM Mapping column-shift bug this session spent
a long back-and-forth diagnosing by hand: a row missing one '|' cell (most
often a blank Join/Filter/Aggregation Logic or Default Handling value) makes
every later column in that row -- Validation Rule, Privacy Classification,
Mapping Confidence, Open Question, Notes -- land one or more columns left of
where it belongs, with no error anywhere in the pipeline. See
app/services/draft_repair.has_malformed_rows for the full mechanism and
app/services/prompt_templates.FORMAT_INSTRUCTIONS['sttm'] for the prompt-side
fix.

TestMalformedRowDetection is a fast, deterministic unit test of the detector
itself -- no network, safe to run on every commit.

TestSttmGenerationStructure is a live end-to-end test: it uploads the real
sample vendor file and runs the actual "Generate STTM" instruction through
openai_service.generate_document() (Azure OpenAI) and
azure_search_service.retrieve_grounding() (Azure AI Search), then checks the
built .xlsx against the structural rules the enterprise instruction document
defines for the Mapping Confidence and Validation Rule columns (see the
Healthcare_Payer_Data_Reporting_PI_Enterprise_Instruction_Document.docx
excerpt quoted in MAPPING_CONFIDENCE_VALUES below). Marked `integration` and
skipped automatically when Azure credentials aren't configured -- it costs a
real API call and its output is non-deterministic, so it checks structure
(cell counts, allowed values, no blanket 'N/A') rather than exact text.
"""

from __future__ import annotations

import os
from pathlib import Path

import openpyxl
import pytest
from dotenv import load_dotenv

from app import xlsx_builder
from app.services import draft_repair

load_dotenv()

SAMPLE_FILE = (
    Path(__file__).parent.parent
    / "generated_outputs" / "upload" / "Input_VendorXYZ_Overpayment_Claims_Sample.xlsx"
)

# Per the enterprise instruction document's "Mapping Confidence" column
# definition ("Use Confirmed, Candidate, or Needs SME Review.") -- this is a
# fixed three-value enum, not free text.
MAPPING_CONFIDENCE_VALUES = {"Confirmed", "Candidate", "Needs SME Review"}


def _read_xlsx_as_text(path: Path) -> str:
    """Mirrors app/services/file_extraction.extract_text()'s xlsx branch,
    without needing a FastAPI UploadFile to drive it."""
    wb = openpyxl.load_workbook(path, data_only=True)
    lines = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            lines.append(", ".join(str(c) for c in row if c is not None))
    return "\n".join(lines)


def _find_column(header: list, *name_parts: str) -> int:
    """Case-insensitive substring lookup so this test doesn't break on minor
    header wording drift (e.g. 'Open Question' vs 'Open Questions') -- fails
    loudly if a required column is missing or ambiguous."""
    matches = [
        i for i, h in enumerate(header)
        if h and all(part.lower() in str(h).lower() for part in name_parts)
    ]
    assert len(matches) == 1, (
        f"expected exactly one column matching {name_parts!r} in header {header!r}, "
        f"found {len(matches)}"
    )
    return matches[0]


class TestMalformedRowDetection:
    """Fast unit coverage for draft_repair.has_malformed_rows -- no network."""

    def test_well_formed_rows_pass(self):
        draft = (
            "## 2. STTM Mapping\n"
            "Mapping ID | Target Field Name | Validation Rule | Mapping Confidence\n"
            "M-001 | Claim_Number | Not Null | Confirmed\n"
            "M-002 | Paid_Date | Must not be future date | Confirmed\n"
        )
        assert draft_repair.has_malformed_rows(draft) is False

    def test_dropped_pipe_for_a_blank_cell_is_detected(self):
        # Row 2 is missing the Validation Rule cell entirely (no pipe for
        # it) instead of writing 'N/A' -- the exact failure mode this test
        # suite exists to catch.
        draft = (
            "## 2. STTM Mapping\n"
            "Mapping ID | Target Field Name | Validation Rule | Mapping Confidence\n"
            "M-001 | Claim_Number | Not Null | Confirmed\n"
            "M-002 | Join_Logic_Field | Confirmed\n"
        )
        assert draft_repair.has_malformed_rows(draft) is True

    def test_non_tabular_sections_are_ignored(self):
        draft = "## 1. Overview\nThis is plain prose, not a table.\nNo pipes here at all.\n"
        assert draft_repair.has_malformed_rows(draft) is False


class TestConfidenceContradictionDetection:
    """Fast unit coverage for draft_repair.find_confirmed_confidence_contradictions
    -- no network. Covers the two shapes seen in real generations tonight: a
    row's own Open Question contradicting its 'Confirmed' status, and a
    separate Assumptions/Open Questions entry contradicting it instead."""

    _MAPPING_HEADER = (
        "Mapping ID | Target Field Name | Mapping Confidence | Open Question\n"
    )

    def test_confirmed_row_with_its_own_open_question_is_flagged(self):
        draft = (
            "## 2. STTM Mapping\n" + self._MAPPING_HEADER +
            "M-001 | Claim_Number | Confirmed | N/A\n"
            "M-002 | Dependent_Number | Confirmed | Confirm data type with SME\n"
        )
        assert draft_repair.find_confirmed_confidence_contradictions(draft) == ["Dependent_Number"]

    def test_confirmed_row_referenced_in_open_questions_section_is_flagged(self):
        draft = (
            "## 2. STTM Mapping\n" + self._MAPPING_HEADER +
            "M-001 | Claim_Number | Confirmed | N/A\n"
            "M-002 | Dependent_Number | Confirmed | N/A\n"
            "## 3. Assumptions and Open Questions\n"
            "Type | ID | Statement | Basis / Note\n"
            "Open Question | Q-001 | Clarify if Dependent Number should be stored separately | Ambiguous source\n"
        )
        assert draft_repair.find_confirmed_confidence_contradictions(draft) == ["Dependent_Number"]

    def test_consistent_draft_has_no_contradictions(self):
        draft = (
            "## 2. STTM Mapping\n" + self._MAPPING_HEADER +
            "M-001 | Claim_Number | Confirmed | N/A\n"
            "M-002 | Dependent_Number | Candidate | Confirm data type with SME\n"
        )
        assert draft_repair.find_confirmed_confidence_contradictions(draft) == []

    def test_non_sttm_draft_is_ignored(self):
        draft = "## 1. Overview\nThis is plain prose, not an STTM mapping table.\n"
        assert draft_repair.find_confirmed_confidence_contradictions(draft) == []


class TestPlaceholderOpenQuestionDetection:
    """Fast unit coverage for draft_repair.find_placeholder_open_questions --
    the bare-ID-instead-of-real-question defect (e.g. 'Q004' instead of
    'Confirm mapping to enterprise member key')."""

    _MAPPING_HEADER = (
        "Mapping ID | Target Field Name | Mapping Confidence | Open Question\n"
    )

    def test_bare_id_open_question_is_flagged(self):
        draft = (
            "## 2. STTM Mapping\n" + self._MAPPING_HEADER +
            "M-001 | Claim_Number | Confirmed | N/A\n"
            "M-002 | Dependent_Number | Needs SME Review | Q004\n"
        )
        assert draft_repair.find_placeholder_open_questions(draft) == ["Dependent_Number"]

    def test_full_prose_open_question_is_not_flagged(self):
        draft = (
            "## 2. STTM Mapping\n" + self._MAPPING_HEADER +
            "M-001 | Claim_Number | Confirmed | N/A\n"
            "M-002 | Dependent_Number | Needs SME Review | Confirm mapping to enterprise member key\n"
        )
        assert draft_repair.find_placeholder_open_questions(draft) == []

    def test_na_open_question_is_not_flagged(self):
        draft = (
            "## 2. STTM Mapping\n" + self._MAPPING_HEADER +
            "M-001 | Claim_Number | Confirmed | N/A\n"
        )
        assert draft_repair.find_placeholder_open_questions(draft) == []


@pytest.mark.integration
@pytest.mark.skipif(
    not (os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_API_KEY")),
    reason="Azure OpenAI not configured -- see .env",
)
@pytest.mark.skipif(not SAMPLE_FILE.exists(), reason=f"sample file not found at {SAMPLE_FILE}")
class TestSttmGenerationStructure:
    async def test_generate_sttm_from_sample_vendor_file_is_structurally_sound(self, tmp_path):
        from app import graph
        from app.services import azure_search_service

        source_text = _read_xlsx_as_text(SAMPLE_FILE)
        instruction = "Generate STTM"

        retrieved_context, _sources = await azure_search_service.retrieve_grounding(
            prompt=instruction, project="STTM structure test"
        )

        # Goes through the actual generate_node, not openai_service.generate_document
        # directly -- the raw single-shot model output is NOT guaranteed to be
        # well-formed every time (that's exactly why generate_node's retry loops
        # exist). Calling generate_document directly would only test the
        # unprotected first draft, not the guarantee production actually makes
        # via /v2/generate.
        result = await graph.generate_node({
            "output_format": "STTM",
            "instruction_history": [{"instruction": instruction}],
            "retrieved_context": retrieved_context,
            "source_files": [{"filename": SAMPLE_FILE.name, "text": source_text}],
            "session_id": "sttm-structure-test",
        })
        draft = result["current_draft"]

        # This is the exact defect this whole test module exists to catch:
        # a row silently missing a cell, shifting every later column left.
        # generate_node retries up to MAX_MALFORMED_ROW_RETRIES times on this,
        # so a failure here means the retry loop itself didn't hold, not just
        # that the model's first attempt wasn't perfect.
        assert not draft_repair.has_malformed_rows(draft), (
            "generated draft still has a row with the wrong cell count for its "
            "section header after generate_node's retry loop -- see "
            "draft_repair.has_malformed_rows and graph.MAX_MALFORMED_ROW_RETRIES"
        )

        # Same reasoning for the Confirmed/Open-Question contradiction retry.
        contradictions = draft_repair.find_confirmed_confidence_contradictions(draft)
        assert not contradictions, (
            f"generated draft still has 'Confirmed' rows contradicted by their own "
            f"Open Question or the Assumptions/Open Questions section after "
            f"generate_node's retry loop: {contradictions} -- see "
            f"draft_repair.find_confirmed_confidence_contradictions and "
            f"graph.MAX_CONFIDENCE_CONTRADICTION_RETRIES"
        )

        # Same reasoning for the bare-ID Open Question retry.
        placeholders = draft_repair.find_placeholder_open_questions(draft)
        assert not placeholders, (
            f"generated draft still has Open Question cells that are bare IDs "
            f"(e.g. 'Q004') instead of the actual question text after "
            f"generate_node's retry loop: {placeholders} -- see "
            f"draft_repair.find_placeholder_open_questions and "
            f"graph.MAX_CONFIDENCE_CONTRADICTION_RETRIES"
        )

        xlsx_builder.OUTPUT_DIR = str(tmp_path)
        path = xlsx_builder.build_output(
            output_format="STTM", content=draft, session_id="sttm-structure-test"
        )

        wb = openpyxl.load_workbook(path, data_only=True)
        mapping_sheets = [n for n in wb.sheetnames if "mapping" in n.lower() and "summary" not in n.lower()]
        assert mapping_sheets, f"no 'STTM Mapping' sheet found among {wb.sheetnames!r}"
        ws = wb[mapping_sheets[0]]

        header = [c.value for c in ws[1]]
        confidence_col = _find_column(header, "mapping", "confidence")
        validation_col = _find_column(header, "validation", "rule")

        data_rows = list(ws.iter_rows(min_row=2, values_only=True))
        assert data_rows, "STTM Mapping sheet has no data rows"

        for row in data_rows:
            trimmed = list(row)
            while trimmed and trimmed[-1] is None:
                trimmed.pop()
            assert len(trimmed) == len(header), (
                f"row has {len(trimmed)} cells, header has {len(header)}: {trimmed!r}"
            )

            confidence = row[confidence_col]
            assert confidence in MAPPING_CONFIDENCE_VALUES, (
                f"Mapping Confidence {confidence!r} is not one of {MAPPING_CONFIDENCE_VALUES}"
            )

            validation_rule = row[validation_col]
            assert validation_rule and str(validation_rule).strip().upper() != "N/A", (
                f"Validation Rule must never be blank/N/A, got {validation_rule!r} "
                f"for row {row!r}"
            )
