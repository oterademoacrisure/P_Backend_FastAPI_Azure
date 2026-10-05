"""
Shared prompt construction for document generation: the format-specific
output instructions, the guardrailed system message, and the user message
layout. Used by both the direct /generate pipeline (app/main.py) and the
LangGraph pipeline (app/services/openai_service.py) so the two never drift
into different guardrails for the same output format.
"""

from __future__ import annotations

# In-scope process areas for prompt governance and scope validation
IN_SCOPE_DOMAINS = (
    "data intake and ingestion, reporting and analytics, enterprise data model "
    "alignment, FRDs, STTMs, requirements gap analysis, epics/features/user "
    "stories, data quality and validation, security/privacy/compliance, and "
    "payment integrity for U.S. healthcare payer data"
)

_AGILE_ROLES = (
    "Reporting Analyst, Business Analyst, Product Owner, Claims Analyst, Provider "
    "Analyst, Payment Integrity Analyst, Quality Analyst, Data Steward, Compliance "
    "Analyst, Finance Analyst, Member Services Representative"
)

_AGILE_ARTIFACT_INSTRUCTION = (
    "Generate an Agile artifact as PLAIN TEXT: one Feature in the organization's "
    "Feature template layout (Feature Template.docx), followed by its User Stories "
    "per the enterprise instruction document's Epic / Feature / Story rules (§22) "
    "and the User Story template. Write exactly these 13 sections, in this order, "
    "each preceded by a '## N. <Section Title>' heading line with this exact "
    "numbering and title -- do not add, omit, reorder, or rename any section:\n"
    "## 1. Feature Title\n## 2. Feature Description\n## 3. Business Objective\n"
    "## 4. Key Capabilities\n## 5. Out of Scope\n## 6. Assumptions\n"
    "## 7. Dependencies\n## 8. Acceptance Criteria (System Must)\n"
    "## 9. Gherkin Acceptance Criteria\n## 10. Risks / Open Questions\n"
    "## 11. Feature Outcome\n## 12. User Stories\n## 13. Story Acceptance Criteria\n\n"
    "Sections 1-9 and 11 are plain prose or '-' bullets, never '|'-delimited:\n"
    "- 1: one line, 'E1-F1 <feature title>'.\n"
    "- 2: three lines, 'As a <role>', 'I want <capability>', 'So that <business value>'.\n"
    "- 3: the objective, then the outcomes it aims for as bullets.\n"
    "- 4: numbered 'KC-1 <capability name>' items, each followed by a one-line description.\n"
    "- 7: a 'Business Dependencies' list, then a 'Technical Dependencies' list.\n"
    "- 8: numbered 'AC-1 <name>' items, each followed by a 'System Must ...' statement.\n"
    "- 9: one or more 'Scenario N: <name>' blocks as Given / When / Then / And lines.\n"
    "Sections 10, 12 and 13 are '|'-delimited tables: a header row with exactly the "
    "columns below, then one row per item.\n"
    "- 10. Risks / Open Questions: 'ID | Risk / Question', IDs R-001, R-002, ...\n"
    "- 12. User Stories: 'Story ID | Story Title | As a | I Want | So That | Story "
    "Points | MoSCoW Priority | Business Rules | Prerequisites | Assumptions | "
    "Constraints & Dependencies'. Write 3 to 7 stories for the feature, IDs E1-F1-S1, "
    "E1-F1-S2, .... 'As a' must be one of these roles: " + _AGILE_ROLES + ". Story "
    "Points is a whole number from 1 to 5; MoSCoW Priority is Must, Should, Could or "
    "Won't. Separate several items inside one cell with ';', never '|'.\n"
    "- 13. Story Acceptance Criteria: 'AC ID | Story ID | Scenario Type | Given | When "
    "| Then', IDs <Story ID>-AC1, <Story ID>-AC2, ... (e.g. E1-F1-S1-AC1). Scenario "
    "Type is Happy path, Validation or Edge case. Every story has at least one Happy "
    "path and one Validation row, plus Edge case rows where applicable; cover "
    "auditability and reporting impact in a Then cell where the story affects them.\n\n"
    "Do not use markdown tables (no '---' separator rows): one row per line, a single "
    "'|' between cells, and every row has exactly as many cells as its header -- write "
    "'N/A' in a cell with no applicable value. Never invent capabilities, "
    "dependencies, business rules, or acceptance criteria not grounded in the uploaded "
    "source material or knowledge-base context -- where the material doesn't specify "
    "something a section needs, label it an Assumption or raise it in Risks / Open "
    "Questions instead of stating it as fact."
)

FORMAT_INSTRUCTIONS = {
    "gherkin": _AGILE_ARTIFACT_INSTRUCTION,
    "agile artifact": _AGILE_ARTIFACT_INSTRUCTION,
    "agile": _AGILE_ARTIFACT_INSTRUCTION,
    "frd": (
        "Generate a Functional Requirements Document (FRD) as PLAIN TEXT, following the "
        "enterprise instruction document's FRD standards (§13) and FRD rules (§19). "
        "Produce a complete first draft even when information is incomplete. Write "
        "exactly these 17 sections, in this order, each preceded by a '## N. <Section "
        "Title>' heading line with this exact numbering and title -- do not add, omit, "
        "reorder, or rename any section:\n"
        "## 1. Executive Summary\n## 2. Business Objective\n## 3. Current State\n"
        "## 4. Future State\n## 5. Scope\n## 6. Out of Scope\n## 7. Personas\n"
        "## 8. Functional Requirements\n## 9. Business Rules\n## 10. Data Requirements\n"
        "## 11. Reporting Requirements\n## 12. Integration Requirements\n"
        "## 13. Security Requirements\n## 14. NFRs\n## 15. Assumptions\n## 16. Risks\n"
        "## 17. Open Questions\n\n"
        "Sections 8, 9, 10, 16 and 17 are '|'-delimited tables: a header row with "
        "exactly the columns below, then one row per item. Every other section is "
        "plain prose or '-' bullets, never '|'-delimited.\n"
        "- 8. Functional Requirements: 'Requirement ID | Requirement | Source / "
        "Rationale | Acceptance Criteria'. IDs are FR-001, FR-002, ... (three digits, "
        "sequential). Each requirement is one clear, measurable, testable 'The system "
        "shall ...' statement; Acceptance Criteria states how it is verified.\n"
        "- 9. Business Rules: 'Rule ID | Business Rule | Source / Rationale', IDs "
        "BR-001, BR-002, ...\n"
        "- 10. Data Requirements: 'Data Requirement ID | Target Entity | Target Field "
        "Name | Target Data Type | Requirement | Mapping Confidence | Open Question', "
        "IDs DR-001, DR-002, .... Target Entity and Target Field Name must be spelled "
        "exactly as in the CANONICAL ONTOLOGY -- never invent an entity or attribute. "
        "Mapping Confidence is 'Confirmed', 'Candidate' or 'Needs SME Review', with the "
        "same meaning as in an STTM; a row that is not 'Confirmed' must spell out its "
        "question in full in Open Question (otherwise 'N/A'). A data need with no "
        "matching ontology attribute goes in Open Questions, not in this table.\n"
        "- 16. Risks: 'Risk ID | Risk | Impact | Mitigation', IDs R-001, R-002, ...\n"
        "- 17. Open Questions: 'Question ID | Open Question | Related Item', IDs "
        "Q-001, Q-002, ...; Related Item names the FR/BR/DR/R ID it concerns, or 'N/A'.\n"
        "Section 14 lists non-functional requirements as '-' bullets numbered NFR-001, "
        "NFR-002, ..., each measurable. Section 15 lists assumptions as '-' bullets, "
        "then a line 'Dependencies:' followed by the business and technical "
        "dependencies as '-' bullets. Section 7 names each persona and what they need "
        "from the system.\n\n"
        "Do not use markdown tables (no '---' separator rows): one row per line, a "
        "single '|' between cells, and every row has exactly as many cells as its "
        "header -- write 'N/A' in a cell with no applicable value. Never invent source "
        "systems, fields, volumes, SLAs, or calculations not in the provided material: "
        "where the material doesn't specify something a section needs, state it as an "
        "Assumption or Open Question instead of as fact, and if a whole section has no "
        "grounding, write one line saying so and raise an Open Question for it."
    ),
    "sttm": (
        "Generate a Source-to-Target Mapping (STTM) deliverable as PLAIN TEXT. The "
        "KNOWLEDGE BASE CONTEXT below includes the organization's STTM template "
        "(source: STTM_Data_Ingestion_Template.xlsx) -- it is the single source of "
        "truth for this format. Reproduce its sections in the same order, each "
        "preceded by a '## N. <Section Title>' heading line matching the template's "
        "own numbering and titles exactly. For each section, emit the template's "
        "column list as the header row, then one '|'-delimited row per record, "
        "using every column from the template in the same order -- do not add, "
        "omit, reorder, or rename any section or column. If the template excerpt "
        "is missing from KNOWLEDGE BASE CONTEXT for this run, state that in an "
        "Open Question instead of guessing at a structure.\n\n"
        "Exception -- the summary/metadata section (e.g. 'STTM Summary'): templates "
        "commonly lay these fields out as column headers with a single data row "
        "beneath. Always transpose that into row-wise form instead: a header row "
        "'Attribute | Detail', then one '|'-delimited row per attribute (the "
        "attribute name, then its value), in the same attribute order the template "
        "uses. Every other section keeps the template's own column layout as-is "
        "(header row of the template's columns, then one row per record).\n\n"
        "Do not use markdown tables (no '---' separator rows) -- every data line must use a "
        "single '|' character between cells, exactly one row per line, so the values split "
        "cleanly on '|'. Every data row MUST have exactly as many '|'-delimited cells as its "
        "section's header row -- if a cell has no applicable value (most often Join Logic, "
        "Filter Logic, Aggregation Logic, or Default Handling, and occasionally Open Question), "
        "still write the literal text 'N/A' in it rather than dropping the cell or collapsing "
        "two pipes together -- doing so shifts every column after it into the wrong field for "
        "that row (this is the single most common mistake in this format, so double-check cell "
        "counts before finishing each row). Validation Rule is NEVER 'N/A' -- every target field "
        "has at least a datatype and a nullability rule, so populate it with the actual "
        "datatype, mandatory/nullable, reference, range, duplicate, cross-field, or business "
        "validation that applies (at minimum, state whether the field is mandatory or nullable "
        "and its datatype constraint). When a row's Mapping Confidence is 'Candidate' or 'Needs "
        "SME Review', its Open Question cell must spell out the actual question in full "
        "(e.g. 'Confirm mapping to enterprise member key', 'Confirm canonical audit category "
        "mapping') -- never a bare cross-reference like 'Q004' or 'See Q5' pointing at the "
        "Assumptions/Open Questions section; a reviewer scanning this row alone must be able "
        "to tell what needs resolving without opening another sheet. It is fine for the same "
        "question to also appear as its own entry in the Assumptions/Open Questions section. "
        "Notes is also rarely blank -- populate it with the "
        "specific lineage, source evidence, or implementation detail behind that row (e.g. what "
        "the field represents in the source file, or why a transformation/default was chosen); "
        "use 'N/A' there only for the rare row where truly nothing applies, not as a default. "
        "Never invent source tables, "
        "source fields, physical joins, exact "
        "calculations, or database structures that are not in the provided source material or "
        "knowledge-base context -- if a target column has no matching source field, set "
        "the transformation/logic column to 'Set as Default Value' and the mapping-confidence "
        "column to 'Needs SME Review', never 'Confirmed'. Use 'Candidate' instead for a row "
        "whose mapping is inferred rather than directly evidenced -- e.g. the source field name "
        "doesn't match the data dictionary/template exactly, or the source-to-target "
        "relationship required guessing. This is a hard constraint, not a stylistic "
        "preference: decide which target fields will need an Assumption or Open Question "
        "BEFORE writing the Mapping row for them, not after -- if a field ends up with a "
        "corresponding Assumption or Open Question anywhere else in this deliverable, its "
        "Mapping Confidence in the STTM Mapping section MUST be 'Candidate' or 'Needs SME "
        "Review', never 'Confirmed'; a row cannot be simultaneously confirmed in one section "
        "and questioned in another. Reserve 'Confirmed' for rows where the source "
        "field, its meaning, and the transformation are all unambiguous from the provided "
        "material, and use these three values sparingly -- most rows with a clear, direct "
        "source-field match should be 'Confirmed'."
    ),
}

# Maps the LangGraph pipeline's PayerIQState.output_format values (capitalized,
# one per run) onto the same FORMAT_INSTRUCTIONS keys the direct /generate
# pipeline uses (lowercase, one per requested format string).
_OUTPUT_FORMAT_TO_KEY = {
    "STTM": "sttm",
    "FRD": "frd",
    "Agile": "agile",
}


def resolve_format_instruction(output_format: str) -> str:
    """Looks up the output instruction for either a raw /generate `formats[]`
    value (e.g. "sttm", "gherkin") or a LangGraph `output_format` value
    (e.g. "STTM", "Agile"). Returns "" if the format is unrecognized -- callers
    fall back to no special instruction rather than failing the request."""
    key = _OUTPUT_FORMAT_TO_KEY.get(output_format, (output_format or "").strip().lower())
    return FORMAT_INSTRUCTIONS.get(key, "")


def build_system_message(knowledge_base_context: str, ontology_context: str = "") -> str:
    """The shared guardrailed system prompt. Identical for every output format
    except for `ontology_context` -- the canonical ontology block from
    app/services/ontology_service.prompt_context(), given only to formats that
    name target entities/attributes (STTM, FRD) -- and the user message's
    "Output requirement" line."""
    ontology_section = (
        "=== CANONICAL ONTOLOGY (approved target model -- binding) ===\n"
        f"{ontology_context}\n\n"
        if ontology_context else ""
    )
    return (
        "You are an expert PayIntegrity / Healthcare Payer Business Analyst "
        "drafting precise, production-ready specification documents (FRD, STTM, "
        "Agile Artifacts) strictly grounded in the source files, knowledge-base "
        "context, and instructions below.\n\n"
        "GUARDRAILS (apply in order):\n"
        "1. GROUNDING: Base every claim, mapping, and calculation on the uploaded "
        "source material or knowledge-base context. Where neither covers a detail, "
        "label it an Assumption, a Candidate/Needs SME Review mapping, or an Open Question -- never state "
        "an inference as confirmed fact.\n"
        "2. AUTHORITATIVE RULES: The KNOWLEDGE BASE CONTEXT section below contains "
        "excerpts from the organization's payer validation and business-rule "
        "documentation (FRD/STTM standards, mapping conventions). Treat these excerpts "
        "as binding instructions, not optional background -- apply their rules and "
        "conventions even where they are stricter than the uploaded source material, "
        "and flag any conflict between the two as an Open Question rather than "
        "silently picking one.\n"
        "3. SOURCE RESTRICTION: Do not use general knowledge or invent source tables, "
        "fields, joins, calculations, or structures not present in the material below.\n"
        "4. SCOPE TEST: This agent covers only these payer process areas: "
        f"{IN_SCOPE_DOMAINS}. If the instructions fall outside them -- even if "
        "terminology overlaps -- respond with exactly this refusal and nothing else: "
        "\"This request falls outside the configured scope of this healthcare payer "
        "requirements agent. The agent supports approved payer domains and artifacts "
        "including data intake, reporting, enterprise data model alignment, FRDs, "
        "STTMs, gap analysis, agile artifacts, data quality, security, compliance, and "
        "payment integrity. For requests outside those areas, contact the designated "
        "Product Owner or PayerIQ administrator.\"\n"
        "5. If only one source file/rule-set is provided, focus solely on it -- do not "
        "assume a comparative analysis. Use formal requirements language ('shall', "
        "'must') for functional requirements.\n"
        "6. Follow the requested output format EXACTLY -- headings, column order, and "
        "the '|' delimiter convention -- no extra commentary or unrequested sections.\n"
        "7. Every data row in a '|'-delimited section must contain EXACTLY the same "
        "number of '|'-delimited cells as that section's header row -- never omit a '|' "
        "for a field with no applicable value. Write the literal text 'N/A' in that "
        "cell instead of leaving it blank between two pipes or collapsing two pipes "
        "into one; a missing cell shifts every following column in that row into the "
        "wrong field.\n\n"
        f"{ontology_section}"
        "=== KNOWLEDGE BASE CONTEXT ===\n"
        f"{knowledge_base_context}"
    )


def build_user_message(project: str, prompt: str, source_text: str, instruction: str) -> str:
    return f"""Project: {project}

Instructions from analyst:
{prompt}

Uploaded source material:
{source_text if source_text.strip() else "(none attached)"}

Output requirement: {instruction}
"""
