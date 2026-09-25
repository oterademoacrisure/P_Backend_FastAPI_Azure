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

_AGILE_ARTIFACT_INSTRUCTION = (
    "Generate an Agile Feature artifact as PLAIN TEXT. The KNOWLEDGE BASE CONTEXT "
    "below includes the organization's Feature template (source: Feature "
    "Template.docx) -- it is the single source of truth for this format. Reproduce "
    "its section order, headings, and structure exactly, each preceded by a "
    "'## <Section Title>' heading line matching the template's own titles: Feature "
    "Title; Feature Description (persona / want / so-that statements); Business "
    "Objective; Key Capabilities (numbered KC-N items, each with a lettered "
    "sub-bullet); Out of Scope; Assumptions; Dependencies (separate 'Business "
    "Dependencies' and 'Technical Dependencies' bulleted lists); Acceptance "
    "Criteria (System Must) (numbered AC-N items); Gherkin Acceptance Criteria (one "
    "or more Scenario blocks written as Given/When/Then prose, not a table); Risks / "
    "Open Questions (a header row 'ID | Risk / Question', then one '|'-delimited "
    "row per risk); Feature Outcome (a bulleted list of resulting capabilities). "
    "Only the Risks / Open Questions section uses '|'-delimited rows -- every other "
    "section stays plain prose/bullets exactly as in the template, not "
    "'|'-delimited. Do not add, omit, reorder, or rename any section. If the "
    "template excerpt is missing from KNOWLEDGE BASE CONTEXT for this run, state "
    "that in an Open Question instead of guessing at a structure.\n\n"
    "Never invent capabilities, dependencies, or acceptance criteria not grounded "
    "in the uploaded source material or knowledge-base context -- where the source "
    "material doesn't specify something the template requires, label it an "
    "Assumption or Open Question instead of stating it as fact."
)

FORMAT_INSTRUCTIONS = {
    "gherkin": _AGILE_ARTIFACT_INSTRUCTION,
    "agile artifact": _AGILE_ARTIFACT_INSTRUCTION,
    "agile": _AGILE_ARTIFACT_INSTRUCTION,
    "frd": (
        "Write a formal Functional Requirements Document with sections: "
        "Introduction, Stakeholders, Functional Requirements (numbered FR-xx), "
        "Assumptions & Constraints, Open Questions."
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
        "and its datatype constraint). Notes is also rarely blank -- populate it with the "
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


def build_system_message(knowledge_base_context: str) -> str:
    """The shared guardrailed system prompt. Identical for every output format --
    only the user message's "Output requirement" line varies."""
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
