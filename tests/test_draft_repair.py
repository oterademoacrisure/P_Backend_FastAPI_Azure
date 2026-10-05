"""
Unit tests for draft_repair.restore_dropped_rows() -- no network.
"""

from __future__ import annotations

from app.services import draft_repair

HEADER = "Mapping ID|Target Field Name|Validation Rule|Mapping Confidence"


def _sttm(*rows: str) -> str:
    return "## 2. STTM Mapping\n" + "\n".join((HEADER,) + rows)


def _rows(draft: str) -> list[dict[str, str]]:
    return draft_repair._find_mapping_table(draft)


PRIOR = _sttm(
    "M-001|Claim_ID|Not null|Confirmed",
    "M-002|Units|Not null|Confirmed",
)


class TestRestoreDroppedRows:
    def test_edit_to_same_field_is_kept_even_with_the_word_add(self):
        """Live refine: 'Add a validation rule to the Units row'."""
        new = _sttm(
            "M-001|Claim_ID|Not null|Confirmed",
            "M-002|Units|Not null; Units must be greater than 0|Confirmed",
        )
        fixed = draft_repair.restore_dropped_rows(PRIOR, new, "Add a validation rule to the Units row")
        rows = _rows(fixed)
        assert len(rows) == 2
        assert rows[1]["Validation Rule"] == "Not null; Units must be greater than 0"

    def test_new_field_that_overwrote_a_row_is_still_split(self):
        """The case the guard exists for: 'add a Status field' reusing M-002's slot."""
        new = _sttm(
            "M-001|Claim_ID|Not null|Confirmed",
            "M-002|Status|Must be one of Paid, Denied|Candidate",
        )
        fixed = draft_repair.restore_dropped_rows(PRIOR, new, "add a status field")
        rows = _rows(fixed)
        assert [r["Target Field Name"] for r in rows] == ["Claim_ID", "Units", "Status"]
        assert rows[2]["Mapping ID"] == "M-003"

    def test_dropped_row_is_restored(self):
        new = _sttm("M-001|Claim_ID|Not null|Confirmed")
        fixed = draft_repair.restore_dropped_rows(PRIOR, new, "tighten the Claim_ID rule")
        assert [r["Target Field Name"] for r in _rows(fixed)] == ["Claim_ID", "Units"]


STORY_HEADER = (
    "Story ID|Story Title|As a|I Want|So That|Story Points|MoSCoW Priority|"
    "Business Rules|Prerequisites|Assumptions|Constraints & Dependencies"
)
AC_HEADER = "AC ID|Story ID|Scenario Type|Given|When|Then"


def _agile(stories: list[str], criteria: list[str]) -> str:
    return (
        "## 1. Feature Title\nE1-F1 Vendor file onboarding\n\n"
        "## 12. User Stories\n" + "\n".join([STORY_HEADER] + stories) + "\n\n"
        "## 13. Story Acceptance Criteria\n" + "\n".join([AC_HEADER] + criteria)
    )


AGILE = _agile(
    ["E1-F1-S1|Upload file|Business Analyst|to upload a vendor file|it is validated|3|Must|N/A|N/A|N/A|N/A"],
    [
        "E1-F1-S1-AC1|E1-F1-S1|Happy path|a valid file|it is uploaded|it is accepted",
        "E1-F1-S1-AC2|E1-F1-S1|Validation|a file with no header|it is uploaded|it is rejected",
    ],
)


class TestAgileTables:
    def test_well_formed_draft_passes_the_row_checks(self):
        assert not draft_repair.has_malformed_rows(AGILE)
        assert draft_repair.dedupe_repeated_rows(AGILE) == AGILE

    def test_missing_cell_is_caught(self):
        broken = AGILE.replace("|it is accepted", "")
        assert draft_repair.has_malformed_rows(broken)

    def test_criteria_keyed_by_ac_id_not_story_id(self):
        """Story ID repeats across criteria rows, so it must not be the key."""
        rows = draft_repair._parse_table(AC_HEADER + "\n" + AGILE.split(AC_HEADER + "\n")[1])
        assert draft_repair._find_key_column(*rows) == "AC ID"

    def test_story_added_on_refine_keeps_the_first(self):
        new = _agile(
            ["E1-F1-S1|Export file|Business Analyst|to export|it is shared|2|Should|N/A|N/A|N/A|N/A"],
            [
                "E1-F1-S1-AC1|E1-F1-S1|Happy path|a valid file|it is uploaded|it is accepted",
                "E1-F1-S1-AC2|E1-F1-S1|Validation|a file with no header|it is uploaded|it is rejected",
            ],
        )
        fixed = draft_repair.restore_dropped_rows(AGILE, new, "Add a story for exporting the file")
        assert "Upload file" in fixed and "Export file" in fixed


def test_agile_instruction_lists_the_13_sections_in_order():
    from app.services.prompt_templates import resolve_format_instruction

    instruction = resolve_format_instruction("Agile")
    assert instruction == resolve_format_instruction("gherkin")
    titles = [
        "Feature Title", "Feature Description", "Business Objective", "Key Capabilities",
        "Out of Scope", "Assumptions", "Dependencies", "Acceptance Criteria (System Must)",
        "Gherkin Acceptance Criteria", "Risks / Open Questions", "Feature Outcome",
        "User Stories", "Story Acceptance Criteria",
    ]
    positions = [instruction.index(f"## {n}. {t}\n") for n, t in enumerate(titles, start=1)]
    assert positions == sorted(positions)
    assert "Payment Integrity Analyst" in instruction and "E1-F1-S1" in instruction
