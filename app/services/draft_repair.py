"""
Deterministic guard against a refine turn's free-text rewrite silently
overwriting a row an earlier turn added, instead of appending a new one.

In this domain, a business instruction like "add one more column status" or
"add a status field" doesn't add a spreadsheet column -- the STTM's columns
(Mapping ID, Target Field Name, ...) are fixed by the template. It adds a
new *row* whose Target Field Name is "Status". app/services/openai_service
.py already tells the model to keep everything the current instruction
doesn't touch unchanged, but that's just a prompt-level ask on a
full-document rewrite -- nothing enforces it, and a later instruction like
"add one more column status1" can get misread as a correction to the
existing "Status" row instead of a new one, silently overwriting it in
place (same Mapping ID, new value) rather than appending a new row.

This module enforces the guarantee in code: if the current instruction
doesn't say to remove/rename/replace anything and reads as an addition, any
row whose key (its section's first all-unique column, e.g. Mapping ID)
existed in the prior draft is restored to its prior content, and whatever
the model put there instead is appended as a genuinely new row.

Called from app/graph.py's generate_node right after openai_service
.generate_document() produces a revision, whenever a prior draft exists.

Caveat: the "is this an addition" check is a keyword heuristic (see
_ADD_KEYWORDS/_REMOVAL_KEYWORDS) -- an edit instruction that happens to
contain a word like "add" (e.g. "add more detail to the Total_Refund_Amount
definition") could be mis-split into a restore+append. This is a pragmatic
first-pass safety net, not a full structured-state fix (see the
architecture discussion around this module's introduction) -- it only
guards against a genuinely dropped/overwritten row.
"""

from __future__ import annotations

import re

from app.xlsx_builder import is_tabular, split_sections

_REMOVAL_KEYWORDS = (
    "remove", "delete", "drop", "rename", "replace", "rid of", "take out",
)
_ADD_KEYWORDS = (
    "add", "one more", "another", "additional", "new column", "new field", "new row",
)

_ID_SUFFIX_RE = re.compile(r"^(.*?)(\d+)$")


def _mentions_removal(instruction: str) -> bool:
    lowered = instruction.lower()
    return any(kw in lowered for kw in _REMOVAL_KEYWORDS)


def _mentions_addition(instruction: str) -> bool:
    lowered = instruction.lower()
    return any(kw in lowered for kw in _ADD_KEYWORDS)


def _parse_table(body: str) -> tuple[list[str], list[dict[str, str]]] | None:
    if not is_tabular(body):
        return None
    lines = [line for line in body.splitlines() if line.strip()]
    if not lines:
        return None
    columns = [c.strip() for c in lines[0].split("|")]
    rows = []
    for line in lines[1:]:
        cells = [c.strip() for c in line.split("|")]
        rows.append({col: (cells[i] if i < len(cells) else "") for i, col in enumerate(columns)})
    return columns, rows


def _render_table(columns: list[str], rows: list[dict[str, str]]) -> str:
    lines = ["|".join(columns)]
    for row in rows:
        lines.append("|".join(row.get(col, "") for col in columns))
    return "\n".join(lines)


def _find_key_column(columns: list[str], *row_lists: list[dict[str, str]]) -> str | None:
    """The first column whose values are non-empty and unique *within each*
    given row list -- e.g. "Mapping ID" for the STTM Mapping section, "ID"
    for the Assumptions section (its first column, "Type", repeats:
    "Assumption", "Assumption", ...). Uniqueness is checked per list, not
    across their concatenation -- a persisting id like "M-011" is expected
    to appear in both the prior and the new rows, that's what makes
    matching them possible. Checking every given list (not just the prior
    one) matters the other direction: a column that was unique before a
    turn can stop being unique after it (e.g. the model reuses a "Review
    Area" label across more rows than it originally had), and matching rows
    on a since-collided key would silently collapse distinct rows together.
    Returns None if no column qualifies in every list, so the caller can
    skip repair rather than risk mismatching rows."""
    for col in columns:
        if all(
            (values := [r.get(col, "") for r in rows]) and all(values) and len(set(values)) == len(values)
            for rows in row_lists
        ):
            return col
    return None


def _looks_like_id_sequence(keys: list[str]) -> bool:
    """True only if every key is a "prefix + trailing digits" id, e.g.
    "M-001", "A-002" -- the STTM Mapping and Assumptions sections. False for
    a fixed attribute/value section like STTM Summary ("Version / Date",
    "Report / Pipeline Name", ...): those keys are unique too (which is
    exactly what _find_key_column looks for), but they're named metadata
    slots, not a growable list of records -- there's no such thing as
    "append a new Version / Date". A changed value there (e.g. a version
    bump) is a routine, intentional edit, never a swallowed addition, so
    restore_dropped_rows must not run its restore+split logic on it."""
    return bool(keys) and all(_ID_SUFFIX_RE.match(k) for k in keys)


def _next_id(existing_ids: list[str]) -> str:
    prefix, width, numbered = "", 0, []
    for id_ in existing_ids:
        m = _ID_SUFFIX_RE.match(id_)
        if m:
            numbered.append(int(m.group(2)))
            prefix, width = m.group(1), len(m.group(2))
    if not numbered:
        return f"row-{len(existing_ids) + 1}"
    return f"{prefix}{max(numbered) + 1:0{width}d}"


def restore_dropped_rows(prior_draft: str, new_draft: str, instruction: str) -> str:
    """Fixes up `new_draft` so a row present in `prior_draft` is never
    silently overwritten by this turn's addition. See module docstring."""
    if _mentions_removal(instruction):
        return new_draft
    adding = _mentions_addition(instruction)

    prior_sections = dict(split_sections(prior_draft))
    new_sections = split_sections(new_draft)

    repaired: list[str] = []
    for title, body in new_sections:
        prior_body = prior_sections.get(title)
        prior_table = _parse_table(prior_body) if prior_body is not None else None
        new_table = _parse_table(body)

        if not prior_table or not new_table:
            repaired.append(f"## {title}\n{body}")
            continue

        columns, prior_rows = prior_table
        new_columns, new_rows = new_table
        if new_columns != columns:
            repaired.append(f"## {title}\n{body}")
            continue

        key_col = _find_key_column(columns, prior_rows, new_rows)
        if key_col is None:
            repaired.append(f"## {title}\n{body}")
            continue

        prior_by_key = {r[key_col]: r for r in prior_rows}
        new_by_key = {r[key_col]: r for r in new_rows}

        missing_ids = [k for k in prior_by_key if k not in new_by_key]
        changed_ids = [
            k for k in prior_by_key
            if k in new_by_key and new_by_key[k] != prior_by_key[k]
        ]

        if not missing_ids and not changed_ids:
            repaired.append(f"## {title}\n{body}")
            continue

        result_rows = [dict(r) for r in new_rows]
        all_ids = list(prior_by_key.keys()) + [r[key_col] for r in new_rows]

        # Only treat a changed row as "overwritten instead of appended" when
        # this section's row count didn't grow -- that's the actual
        # signature of the bug (the model reused an existing row's slot for
        # the new content instead of adding one). If the row count already
        # grew, the model did add new rows for this turn; a changed row
        # alongside that growth is more likely an intentional edit elsewhere
        # in the same rewrite, not a swallowed addition, so leave it as the
        # model wrote it rather than risk mangling a legitimate change.
        if (
            adding and changed_ids and not missing_ids
            and len(new_rows) <= len(prior_rows)
            and _looks_like_id_sequence(list(prior_by_key.keys()))
        ):
            for cid in changed_ids:
                idx = next(i for i, r in enumerate(result_rows) if r[key_col] == cid)
                overwritten_row = result_rows[idx]
                new_id = _next_id(all_ids)
                all_ids.append(new_id)
                overwritten_row[key_col] = new_id
                result_rows[idx] = dict(prior_by_key[cid])
                result_rows.append(overwritten_row)

        for mid in missing_ids:
            result_rows.append(dict(prior_by_key[mid]))

        repaired.append(f"## {title}\n{_render_table(columns, result_rows)}")

    return "\n\n".join(repaired)


def addition_not_applied(prior_draft: str, new_draft: str, instruction: str) -> bool:
    """True if `instruction` reads as a request to add a new row/field, but
    no table section actually grew a row between `prior_draft` and
    `new_draft` -- the model didn't apply the addition at all, as opposed
    to restore_dropped_rows's case (it applied it by overwriting an
    existing row instead of appending). This can't be detected from missing
    or changed rows the way restore_dropped_rows works: if the model just
    echoes the prior draft back essentially unchanged, no row is missing
    and none looks meaningfully changed either -- the signal here is purely
    "the addition instruction fired, but row counts stayed flat everywhere."
    Used by generate_node to decide whether one extra targeted attempt is
    worth it, without reintroducing a blanket retry-on-every-turn cost."""
    if not _mentions_addition(instruction) or _mentions_removal(instruction):
        return False

    prior_sections = dict(split_sections(prior_draft))
    new_sections = dict(split_sections(new_draft))

    for title, prior_body in prior_sections.items():
        new_body = new_sections.get(title)
        if new_body is None:
            continue
        prior_table = _parse_table(prior_body)
        new_table = _parse_table(new_body)
        if not prior_table or not new_table:
            continue
        _, prior_rows = prior_table
        _, new_rows = new_table
        if len(new_rows) > len(prior_rows):
            return False  # some section grew -- the addition was applied somewhere
    return True


def has_malformed_rows(draft: str) -> bool:
    """True if any '|'-delimited table section has a data row whose cell
    count doesn't match its header row.

    _write_table_section() in app/xlsx_builder.py (and _parse_table() above)
    both map each row's cells to columns purely by position -- cell i goes
    into column i, with no check that the row actually has as many cells as
    the header. When the model drops a '|' for a field with no applicable
    value (most often seen with an empty Privacy Classification cell in the
    STTM Mapping section) instead of writing an explicit placeholder like
    'N/A', every following cell in that row silently shifts one column to
    the left -- e.g. the real Mapping Confidence value lands in the Privacy
    Classification column, and Mapping Confidence itself renders blank in
    the exported spreadsheet, with no error or warning anywhere in the
    pipeline. Used by generate_node (app/graph.py) to trigger one corrective
    retry, the same way addition_not_applied() gates the existing addition
    retry."""
    for title, body in split_sections(draft):
        if not is_tabular(body):
            continue
        lines = [line for line in body.splitlines() if line.strip()]
        if not lines:
            continue
        header_len = len(lines[0].split("|"))
        if any(len(line.split("|")) != header_len for line in lines[1:]):
            return True
    return False


def _find_mapping_table(draft: str) -> list[dict[str, str]]:
    """Returns the STTM Mapping section's rows (keyed by original-case column
    name), or [] if the draft has no such section -- identified by having
    both a 'Mapping Confidence' and a 'Target Field Name' column, so this
    works regardless of the section's numbered heading text."""
    for _title, body in split_sections(draft):
        parsed = _parse_table(body)
        if not parsed:
            continue
        columns, rows = parsed
        lower_columns = [c.lower() for c in columns]
        if "mapping confidence" in lower_columns and "target field name" in lower_columns:
            return rows
    return []


def _row_value(row: dict[str, str], column_lower: str) -> str:
    """_parse_table() keys each row dict by the *original-case* column name,
    so this looks a value up by a case-insensitive column name instead."""
    return next((v for k, v in row.items() if k.lower() == column_lower), "")


def find_confirmed_confidence_contradictions(draft: str) -> list[str]:
    """Returns the Target Field Names of every STTM Mapping row marked
    'Confirmed' in Mapping Confidence that contradicts itself or the rest of
    the document -- either its own Open Question cell isn't 'N/A', or its
    field name is referenced in the Assumptions/Open Questions section's
    Statement column. A row can't be simultaneously confirmed and questioned
    (see the 'hard constraint' in prompt_templates.FORMAT_INSTRUCTIONS['sttm']),
    but that's a prompt-level ask on a free-text generation -- nothing
    enforces it, and testing showed the model honors it only about half the
    time. Used by generate_node (app/graph.py) to trigger a corrective retry,
    the same way has_malformed_rows() does for column-shifted rows."""
    mapping_rows = _find_mapping_table(draft)
    if not mapping_rows:
        return []

    statements: list[str] = []
    for _title, body in split_sections(draft):
        parsed = _parse_table(body)
        if parsed and "statement" in [c.lower() for c in parsed[0]]:
            statements.extend(row.get("Statement", "").lower() for row in parsed[1])

    contradictions = []
    for row in mapping_rows:
        confidence = _row_value(row, "mapping confidence")
        if confidence.strip().lower() != "confirmed":
            continue
        field = _row_value(row, "target field name").strip()
        if not field:
            continue

        open_question = _row_value(row, "open question")
        in_row_contradiction = open_question.strip().upper() not in ("", "N/A")

        token = field.replace("_", " ").lower()
        cross_section_contradiction = bool(token) and any(token in s for s in statements)

        if in_row_contradiction or cross_section_contradiction:
            contradictions.append(field)
    return contradictions


# A bare cross-reference like 'Q004', 'Q5', or 'A1' -- 1-4 letters then
# digits, no whitespace -- rather than an actual question in prose.
_PLACEHOLDER_OPEN_QUESTION_RE = re.compile(r"^[A-Za-z]{0,4}\d+$")


def find_placeholder_open_questions(draft: str) -> list[str]:
    """Returns the Target Field Names of every STTM Mapping row whose Open
    Question cell is a bare ID token (e.g. 'Q004') pointing at the
    Assumptions/Open Questions section instead of spelling out the actual
    question. A reviewer scanning the STTM Mapping sheet alone -- the
    document most SMEs actually open -- can't tell what needs resolving from
    an ID alone; the question text belongs in the cell itself, not just in a
    different sheet. Used by generate_node (app/graph.py) to trigger a
    corrective retry, the same way find_confirmed_confidence_contradictions()
    does for the Confirmed/Open-Question contradiction."""
    mapping_rows = _find_mapping_table(draft)
    if not mapping_rows:
        return []

    placeholders = []
    for row in mapping_rows:
        field = _row_value(row, "target field name").strip()
        open_question = _row_value(row, "open question").strip()
        if field and open_question and _PLACEHOLDER_OPEN_QUESTION_RE.match(open_question):
            placeholders.append(field)
    return placeholders


def dedupe_repeated_rows(draft: str) -> str:
    """Collapses exact-duplicate rows the model sometimes emits for a single
    requested addition -- e.g. asked to "add new column status101", it
    writes two rows that are identical except for their own id (M-015 and
    M-016 both "Status101", same definition, same everything else). This is
    a model quirk independent of restore_dropped_rows above: it happens
    within a single generation, with no prior draft involved and no row
    overwritten, so nothing there would catch it. Keeps the first
    occurrence of each distinct row and drops the rest, per section,
    ignoring the row's own key column (its id) when comparing -- that's
    expected to differ between "duplicate" rows and is not itself a sign of
    a real difference. Runs on every generation, not just refine turns,
    since the model can duplicate a row on the very first draft too."""
    sections = split_sections(draft)
    deduped_sections: list[str] = []
    for title, body in sections:
        table = _parse_table(body)
        if not table:
            deduped_sections.append(f"## {title}\n{body}")
            continue

        columns, rows = table
        key_col = _find_key_column(columns, rows)

        seen: set[tuple[str, ...]] = set()
        kept_rows: list[dict[str, str]] = []
        for row in rows:
            fingerprint = tuple(row.get(c, "") for c in columns if c != key_col)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            kept_rows.append(row)

        if len(kept_rows) == len(rows):
            deduped_sections.append(f"## {title}\n{body}")
        else:
            deduped_sections.append(f"## {title}\n{_render_table(columns, kept_rows)}")

    return "\n\n".join(deduped_sections)
