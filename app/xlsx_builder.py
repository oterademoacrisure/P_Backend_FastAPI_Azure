"""
Assembles the final .xlsx deliverable for the LangGraph pipeline
(app/graph.py's finalize_node) from the model's generated plain-text draft.

The generated text follows the convention documented in
app/services/prompt_templates.py: one or more '## <Section Title>' headed
sections, each either plain prose/bullets or '|'-delimited rows (header row
first). This builder turns that into one worksheet per section -- a '|'-row
section becomes a table (bold header row), a prose section becomes wrapped
text in a single column.
"""

from __future__ import annotations

import os
import re
from copy import copy
from datetime import date
from io import BytesIO

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.worksheet.worksheet import Worksheet

OUTPUT_DIR = os.getenv("GENERATED_OUTPUT_DIR", "generated_outputs")

_SECTION_HEADING_RE = re.compile(r"^##\s*(.+?)\s*$", re.MULTILINE)


def split_sections(content: str) -> list[tuple[str, str]]:
    """Splits the draft into (title, body) pairs on '## ' headings. A draft
    with no headings at all becomes a single ("Content", draft) section.
    Public so app/services/draft_repair.py can parse drafts the same way."""
    matches = list(_SECTION_HEADING_RE.finditer(content))
    if not matches:
        return [("Content", content.strip())]

    sections = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sections.append((m.group(1), content[start:end].strip()))
    return sections


def _safe_sheet_title(title: str, used: set[str]) -> str:
    # Excel sheet names: <=31 chars, no []:*?/\\
    cleaned = re.sub(r"[\[\]:*?/\\]", " ", title).strip() or "Sheet"
    cleaned = cleaned[:31]
    candidate = cleaned
    n = 2
    while candidate in used:
        suffix = f" ({n})"
        candidate = cleaned[: 31 - len(suffix)] + suffix
        n += 1
    used.add(candidate)
    return candidate


def _write_table_section(ws: Worksheet, body: str) -> None:
    rows = [line for line in body.splitlines() if line.strip()]
    for r, line in enumerate(rows, start=1):
        cells = [c.strip() for c in line.split("|")]
        for c, value in enumerate(cells, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            if r == 1:
                cell.font = Font(bold=True)
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = 40


def _write_prose_section(ws: Worksheet, body: str) -> None:
    lines = body.splitlines() or [""]
    for r, line in enumerate(lines, start=1):
        cell = ws.cell(row=r, column=1, value=line)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 120


def is_tabular(body: str) -> bool:
    data_lines = [line for line in body.splitlines() if line.strip()]
    if not data_lines:
        return False
    return sum(1 for line in data_lines if "|" in line) / len(data_lines) >= 0.5


_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _label(text: str) -> str:
    """'2. STTM Mapping' / 'STTM  mapping ' -> 'sttm mapping': section and
    column names compared without numbering, case or spacing."""
    return " ".join(re.sub(r"^\s*\d+\.\s*", "", text or "").lower().split())


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.split("|")]


def _copy_style(src, dst) -> None:
    if src.has_style:
        dst._style = copy(src._style)


def _fill_summary(ws: Worksheet, lines: list[str]) -> None:
    """Writes each 'Attribute | Detail' row next to the template's own
    label in column A; attributes the template doesn't list go below it,
    styled like the template's last labelled row."""
    rows = [_cells(line) for line in lines]
    if rows and [_label(c) for c in rows[0][:2]] == ["attribute", "detail"]:
        rows = rows[1:]
    label_rows = {
        _label(str(ws.cell(r, 1).value)): r
        for r in range(1, ws.max_row + 1)
        if ws.cell(r, 1).value and r > 1
    }
    last = max(label_rows.values(), default=ws.max_row)
    for cells in rows:
        if not cells or not cells[0]:
            continue
        detail = cells[1] if len(cells) > 1 else ""
        if "date" in _label(cells[0]):
            # The model has no clock and writes a plausible-looking past
            # date; stamp the generation date (frontend's stampCurrentDate
            # does the same for the on-screen copy).
            detail = _DATE_RE.sub(date.today().isoformat(), detail)
        r = label_rows.get(_label(cells[0]))
        if r is None:
            last += 1
            r = last
            ws.cell(r, 1, cells[0])
            _copy_style(ws.cell(last - 1, 1), ws.cell(r, 1))
            _copy_style(ws.cell(last - 1, 2), ws.cell(r, 2))
        ws.cell(r, 2, detail)
        ws.cell(r, 2).alignment = Alignment(wrap_text=True, vertical="top")


def _fill_table(ws: Worksheet, lines: list[str]) -> None:
    """Writes the generated rows under the template's header row, placing
    each value in the template column with the same name (so a reordered
    or extra generated column can't shift data). Banding continues from the
    template's own pre-formatted rows 2 and 3."""
    template_cols = {
        _label(str(ws.cell(1, c).value)): c for c in range(1, ws.max_column + 1) if ws.cell(1, c).value
    }
    header, *data = [_cells(line) for line in lines]
    positions = []
    for i, name in enumerate(header):
        col = template_cols.get(_label(name))
        if col is None:  # a column the template doesn't have: append it after the last one
            col = max(template_cols.values(), default=0) + 1
            template_cols[_label(name)] = col
            ws.cell(1, col, name)
            _copy_style(ws.cell(1, col - 1), ws.cell(1, col))
        positions.append(col)
    width = max(template_cols.values(), default=len(header))
    for i, cells in enumerate(data):
        r = i + 2
        band = 2 if i % 2 == 0 else 3
        for c in range(1, width + 1):
            _copy_style(ws.cell(band, c), ws.cell(r, c))
        for value, col in zip(cells, positions):
            ws.cell(r, col, value)


def fill_template(template_bytes: bytes, content: str, path: str) -> str:
    """Fills an output template (e.g. STTM_Data_Ingestion_Template.xlsx)
    with the generated draft and saves it to `path`. Each '## N. Title'
    section goes to the template sheet with the same title; a section the
    template has no sheet for gets a plain sheet of its own at the end, so
    nothing generated is lost."""
    wb = load_workbook(BytesIO(template_bytes))
    sheets = {_label(ws.title): ws for ws in wb.worksheets}
    used_titles = {ws.title for ws in wb.worksheets}
    for title, body in split_sections(content):
        lines = [line for line in body.splitlines() if line.strip()]
        ws = sheets.get(_label(title))
        if ws is None:
            ws = wb.create_sheet(title=_safe_sheet_title(title, used_titles))
            (_write_table_section if is_tabular(body) else _write_prose_section)(ws, body)
        elif lines and is_tabular(body):
            first = _cells(lines[0])
            is_summary = [_label(c) for c in first[:2]] == ["attribute", "detail"] or ws.cell(1, 2).value is None
            (_fill_summary if is_summary else _fill_table)(ws, lines)
    wb.save(path)
    return path


def build_output(
    output_format: str, content: str, session_id: str, template_bytes: bytes | None = None
) -> str:
    """Writes `content` to a .xlsx file under OUTPUT_DIR and returns its
    path -- filled into `template_bytes` when the format has a template
    (STTM), otherwise as a plain one-sheet-per-section workbook."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if template_bytes:
        return fill_template(
            template_bytes, content, os.path.join(OUTPUT_DIR, f"{session_id}_{output_format}.xlsx")
        )

    wb = Workbook()
    wb.remove(wb.active)
    used_titles: set[str] = set()

    for title, body in split_sections(content):
        ws = wb.create_sheet(title=_safe_sheet_title(title, used_titles))
        if is_tabular(body):
            _write_table_section(ws, body)
        else:
            _write_prose_section(ws, body)

    filename = f"{session_id}_{output_format}.xlsx"
    path = os.path.join(OUTPUT_DIR, filename)
    wb.save(path)
    return path
