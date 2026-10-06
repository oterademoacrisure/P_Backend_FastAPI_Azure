"""
Each project's own canonical target model (its ontology.json, see
ontology.md) for STTM/FRD generation: loads it for the session's project,
renders it as a compact prompt block that is given to the model *in full* on
every request (instead of the 2 chunks per document Azure AI Search returns),
and checks a generated STTM's target columns against it.

One ontology per project, never shared: projects have different columns,
mappings and guardrail rules (each has its own instruction document), so
project A's targets must never validate project B's STTM. Lookup order for a
project (see get_ontology):
1. Blob Storage: <ONTOLOGY_CONTAINER>/<client>/<project>/ontology.json --
   the same folder that holds the project's documents (the project's
   project_registry folder, e.g. excellus/payment-integrity), so SMEs update
   it alongside them with no redeploy; re-read every ONTOLOGY_CACHE_TTL_SECONDS.
2. Local file: <ONTOLOGY_DIR>/<client>/<project>.json -- for development,
   tests, and environments without Blob access.
A project with neither generates exactly as it did before ontologies existed
(no block, no validation) -- there is deliberately no fallback to another
project's ontology, the same rule retrieve_grounding() follows for documents.

Why in full rather than searched: the whole model (35 entities, ~370
attributes) is a few thousand tokens, so there's nothing to search -- and a
retrieved fragment is exactly what left most vendor columns without a target
before (ontology.md, Gap 2). Azure AI Search keeps doing what it's good at:
narrative documents (instruction document, templates, standards).

Every entity, attribute, alias, value set and rule carries a status:
"approved" (e.g. copied from the Payer Data Dictionary) or "proposed" (a
draft awaiting the project SMEs' sign-off). A proposed target can be
mapped to but never as 'Confirmed' -- find_violations() checks that, and
enforce_confidence() guarantees it in code once the model's retries run out.

Used by:
- app/graph.py              (prompt block, validation loop, groundedness source)
- app/main.py               (prompt block for the direct /generate pipeline)
- app/routergenerator.py    (GET /v2/ontology)
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob.aio import BlobServiceClient
from dotenv import load_dotenv

from app.services import draft_repair
from app.xlsx_builder import split_sections

load_dotenv()

logger = logging.getLogger(__name__)

ONTOLOGY_DIR = os.getenv(
    "ONTOLOGY_DIR", str(Path(__file__).resolve().parent.parent.parent / "ontology")
)
# The knowledge-base storage account (the one the Azure AI Search indexer
# reads), not AZURE_DOCS_STORAGE_CONNECTION_STRING's compliance archive.
ONTOLOGY_STORAGE_CONNECTION_STRING = (
    os.getenv("ONTOLOGY_STORAGE_CONNECTION_STRING") or os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
)
ONTOLOGY_CONTAINER = os.getenv("ONTOLOGY_CONTAINER", "sharepoint-docs")
ONTOLOGY_BLOB_NAME = "ontology.json"
ONTOLOGY_CACHE_TTL_SECONDS = int(os.getenv("ONTOLOGY_CACHE_TTL_SECONDS", "900"))  # 15 min
# An unreachable storage account must not stall generation -- the SDK's own
# retries can take minutes -- so a slow read gives up and uses the local file.
ONTOLOGY_BLOB_TIMEOUT_SECONDS = float(os.getenv("ONTOLOGY_BLOB_TIMEOUT_SECONDS", "10"))
ONTOLOGY_BLOB_RETRY_SECONDS = int(os.getenv("ONTOLOGY_BLOB_RETRY_SECONDS", "60"))

# Output formats whose prompt gets the ontology block. Both name target
# entities/attributes; the Agile Feature template doesn't, so it's left out
# rather than paying for tokens it won't use.
ONTOLOGY_FORMATS = {"sttm", "frd"}

_ENTITY_COLUMN_PREFIXES = ("target table", "target entity")
_FIELD_COLUMN = "target field name"
_TYPE_COLUMN = "target data type"
_CONFIDENCE_COLUMN = "mapping confidence"
_OPEN_QUESTION_COLUMN = "open question"
_SOURCE_COLUMN = "source field"

# Normalizes the type spellings a model or source system tends to use onto
# the dictionary's own seven types, so "INT" vs "INTEGER" isn't a mismatch.
_TYPE_SYNONYMS = {
    "INT": "INTEGER", "BIGINT": "INTEGER", "SMALLINT": "INTEGER", "INTEGER": "INTEGER",
    "VARCHAR": "VARCHAR", "NVARCHAR": "VARCHAR", "STRING": "VARCHAR", "TEXT": "VARCHAR",
    "CHAR": "CHAR", "NCHAR": "CHAR",
    "DECIMAL": "DECIMAL", "NUMERIC": "DECIMAL", "NUMBER": "DECIMAL", "MONEY": "DECIMAL",
    "DATE": "DATE",
    "DATETIME": "DATETIME", "TIMESTAMP": "DATETIME", "DATETIME2": "DATETIME",
    "BOOLEAN": "BOOLEAN", "BOOL": "BOOLEAN", "BIT": "BOOLEAN",
}


def _norm(name: str) -> str:
    """'Claim_Header', ' claim header ', 'Claim-Header' -> 'claim header'."""
    return " ".join(re.split(r"[\s_\-]+", (name or "").strip().lower())).strip()


@dataclass(frozen=True)
class Attribute:
    entity: str
    name: str
    data_type: str
    status: str


@dataclass(frozen=True)
class Violation:
    kind: str           # unknown_entity | unknown_attribute | proposed_confirmed | datatype_mismatch
    target: str         # "Entity.Attribute" as written in the draft
    message: str


class Ontology:
    def __init__(self, raw: dict, project: str = "", source: str = ""):
        self.raw = raw
        self.project = project    # project folder, e.g. "excellus/payment-integrity"
        self.source = source      # where it was read from, for logs and GET /v2/ontology
        self._prompt_text: str | None = None
        self.entity_status: dict[str, str] = {}
        self.entity_names: dict[str, str] = {}               # norm -> canonical
        self.attributes: dict[str, dict[str, Attribute]] = {}  # norm entity -> norm attr -> Attribute
        # (norm entity, norm attr) -> {norm alias: status}, the evidence that
        # lets a source column be 'Confirmed' to a target (see evidence()).
        self.alias_index: dict[tuple[str, str], dict[str, str]] = {}
        for al in raw.get("aliases", []):
            entity, _, attr = al.get("target", "").partition(".")
            names = self.alias_index.setdefault((_norm(entity), _norm(attr)), {})
            for name in al.get("aliases", []):
                if names.get(_norm(name)) != "approved":
                    names[_norm(name)] = al.get("status", "approved")
        for e in raw.get("entities", []):
            key = _norm(e["name"])
            self.entity_names[key] = e["name"]
            self.entity_status[key] = e.get("status", "approved")
            attrs = self.attributes.setdefault(key, {})
            for a in e.get("attributes", []):
                attrs.setdefault(_norm(a["name"]), Attribute(
                    entity=e["name"],
                    name=a["name"],
                    data_type=a.get("dataType", ""),
                    status=a.get("status", "approved"),
                ))

    @property
    def version(self) -> str:
        return self.raw.get("version", "")

    def find_entity(self, name: str) -> str | None:
        """Normalized key of the entity a draft's Target Table cell names, or
        None. Tolerates a schema prefix ('gold.Claim Header') and a trailing
        note ('Member (Silver)' or a copied 'PI Opportunity [proposed]' tag)."""
        cleaned = re.sub(r"\(.*?\)|\[.*?\]", "", name or "").split(".")[-1]
        key = _norm(cleaned)
        return key if key in self.entity_names else None

    def suggest_entity(self, name: str) -> str | None:
        match = difflib.get_close_matches(_norm(name), list(self.entity_names), n=1, cutoff=0.75)
        return self.entity_names[match[0]] if match else None

    def suggest_attribute(self, entity_key: str, name: str) -> str | None:
        attrs = self.attributes.get(entity_key, {})
        match = difflib.get_close_matches(_norm(name), list(attrs), n=1, cutoff=0.75)
        return attrs[match[0]].name if match else None

    def evidence(self, source: str, entity_key: str, attr_key: str) -> str:
        """How a draft's Source Field cell is tied to its target, beyond the
        model's own judgment: "name" (it is the attribute's name, optionally
        prefixed with the entity's: 'Claim ID', 'Member First Name'), the
        status of the alias it matches ("approved" / "proposed"), or "" --
        matched by description only, which is how a column named after a
        person once got a confident target. A file or sheet prefix
        ('Input.xlsx.Claim Number') and a trailing note are ignored."""
        cleaned = re.sub(r"\(.*?\)|\[.*?\]|[`'\"]", "", source or "")
        candidates = {_norm(cleaned), _norm(re.split(r"[.>:]", cleaned)[-1])} - {""}
        if candidates & {attr_key, f"{entity_key} {attr_key}"}:
            return "name"
        aliases = self.alias_index.get((entity_key, attr_key), {})
        statuses = {aliases[c] for c in candidates if c in aliases}
        return "approved" if "approved" in statuses else next(iter(statuses), "")


def load_file(path: str, project: str = "") -> Ontology:
    """Parses one ontology JSON file. Raises on a missing or invalid file."""
    with open(path, encoding="utf-8") as f:
        return Ontology(json.load(f), project=project, source=path)


async def _read_blob(folder: str) -> tuple[dict, str] | None:
    """(parsed JSON, blob URL) of the project's ontology in Blob Storage, or
    None if Blob isn't configured or the project has no ontology there.
    Any other failure (network, auth, invalid JSON) raises."""
    if not ONTOLOGY_STORAGE_CONNECTION_STRING:
        return None
    async with BlobServiceClient.from_connection_string(ONTOLOGY_STORAGE_CONNECTION_STRING) as service:
        blob = service.get_blob_client(ONTOLOGY_CONTAINER, f"{folder}/{ONTOLOGY_BLOB_NAME}")
        try:
            downloaded = await blob.download_blob()
        except ResourceNotFoundError:
            return None
        return json.loads(await downloaded.readall()), blob.url


def _read_local(folder: str) -> tuple[dict, str] | None:
    path = os.path.join(ONTOLOGY_DIR, f"{folder}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f), path


_cache: dict[str, tuple[float, Ontology | None]] = {}
_cache_lock = asyncio.Lock()


async def get_ontology(folder: str | None) -> Ontology | None:
    """The ontology of the project whose registry folder is `folder` (e.g.
    "excellus/payment-integrity", see project_registry), or None if it has
    none (or no folder -- the project couldn't be resolved).
    Cached per project for ONTOLOGY_CACHE_TTL_SECONDS, including "has none",
    so a newly uploaded ontology takes effect within that time. A failed
    Blob read is logged and falls back to the local file, cached for only
    ONTOLOGY_BLOB_RETRY_SECONDS, so a transient outage doesn't pin the
    fallback for the whole TTL and doesn't cost every request a timeout."""
    if not folder:
        return None
    folder = folder.strip("/").lower()

    cached = _cache.get(folder)
    if cached and time.monotonic() - cached[0] < ONTOLOGY_CACHE_TTL_SECONDS:
        return cached[1]

    async with _cache_lock:
        cached = _cache.get(folder)
        if cached and time.monotonic() - cached[0] < ONTOLOGY_CACHE_TTL_SECONDS:
            return cached[1]  # filled by a concurrent caller while we waited

        cached_at = time.monotonic()
        try:
            found = await asyncio.wait_for(_read_blob(folder), ONTOLOGY_BLOB_TIMEOUT_SECONDS)
        except Exception as e:  # including asyncio.TimeoutError
            logger.warning("Ontology Blob read failed for project %r, trying local file: %r", folder, e)
            found = None
            # Keep the fallback for ONTOLOGY_BLOB_RETRY_SECONDS only, so Blob is
            # retried soon -- but not on every request, each of which would
            # otherwise wait out the full timeout again.
            cached_at -= ONTOLOGY_CACHE_TTL_SECONDS - ONTOLOGY_BLOB_RETRY_SECONDS
        try:
            found = found or _read_local(folder)
            ontology = Ontology(found[0], project=folder, source=found[1]) if found else None
        except (OSError, ValueError, KeyError) as e:
            logger.warning("Ontology for project %r is invalid, generating without it: %s", folder, e)
            ontology = None

        if ontology:
            logger.info(
                "Loaded ontology for project %r from %s (v%s, %d entities)",
                folder, ontology.source, ontology.version, len(ontology.entity_names),
            )
        else:
            logger.info("No ontology for project %r -- generating without one", folder)
        _cache[folder] = (cached_at, ontology)
        return ontology


def applies_to(output_format: str) -> bool:
    return (output_format or "").strip().lower() in ONTOLOGY_FORMATS


def _tag(status: str) -> str:
    return "" if status == "approved" else f" [{status}]"


def _render_attribute(a: dict) -> str:
    data_type = a.get("dataType", "")
    if a.get("length"):
        data_type += f"({a['length']})"
    flags = []
    if a.get("primaryKey"):
        flags.append("PK")
    fk = a.get("foreignKey")
    if isinstance(fk, dict):
        flags.append(f"FK->{fk.get('entity') or fk.get('references') or '?'}")
    if a.get("nullable") is False:
        flags.append("NOT NULL")
    if a.get("valueSet"):
        flags.append(f"values: {a['valueSet']}")
    if a.get("codeSet"):
        flags.append(f"codes: {a['codeSet']}")
    privacy = a.get("privacy", "")
    if privacy and privacy != "Non-sensitive":
        flags.append(f"privacy={privacy}")
    flag_text = f" {' '.join(flags)}" if flags else ""
    return f"  - {a['name']} {data_type}{flag_text}{_tag(a.get('status', 'approved'))} -- {a.get('definition', '')}"


def _render_entity(e: dict) -> list[str]:
    return [f"{e['name']}{_tag(e.get('status', 'approved'))} -- {e.get('domain', '')}"] + [
        _render_attribute(a) for a in e.get("attributes", [])
    ]


def _render(ontology: Ontology) -> str:
    if ontology._prompt_text is None:
        ontology._prompt_text = _render_text(ontology)
    return ontology._prompt_text


def _render_text(ontology: Ontology) -> str:
    raw = ontology.raw

    lines = [
        f"{raw.get('name', 'Canonical ontology')} (project {ontology.project or 'n/a'}, "
        f"version {ontology.version})",
        "",
        "HOW TO USE THIS MODEL (binding for every target you name):",
        "- It is the canonical target model. Each STTM Target Table / Output Object must be one "
        "of the ENTITIES below, spelled exactly as written; each Target Field Name must be an "
        "attribute of that entity, spelled exactly as written; Target Data Type, Required "
        "Indicator (NOT NULL = Yes), Privacy Classification and Target Field Business Definition "
        "come from that attribute.",
        "- Use ALIASES to match vendor/source column names to their canonical attribute.",
        "- Every column of the uploaded source file must appear in the STTM Mapping. One file usually "
        "feeds SEVERAL entities: map each column to its own alias target even when that target is in a "
        "different entity from the file's main one (e.g. claim amounts to Claim Header, subscriber and "
        "dependent numbers to Member, provider names to Provider). A column with no target is raised as "
        "an Open Question, never silently dropped.",
        "- Anything tagged [proposed] is a draft not yet approved by this project's SMEs. You may "
        "map to it, but its Mapping Confidence must be 'Candidate' or 'Needs SME Review', never "
        "'Confirmed', and its Notes should say it is a proposed ontology item. The tag is not part "
        "of the name: write the entity or attribute name alone, never with ' [proposed]' after it.",
        "- A source field with no matching attribute is a real gap: do not invent a target name. "
        "Raise it as an Open Question; if the analyst explicitly asks for a new target field, add "
        "the row with Mapping Confidence 'Needs SME Review' and an Open Question stating the field "
        "is not in the canonical model.",
        "- Use VALUE SETS as allowed-value checks and RULES as validation rules in the Validation "
        "Rule column, citing the rule id.",
        "- Follow every PROJECT GUARDRAIL below; they come from this project's own instruction "
        "document and override general conventions.",
        "",
    ]
    if raw.get("guardrails"):
        lines.append("PROJECT GUARDRAILS:")
        lines += [
            f"- {g['id']}{_tag(g.get('status', 'approved'))}: {g['text']}" for g in raw["guardrails"]
        ]
        lines.append("")
    lines.append("ENTITIES (Entity -- domain; attribute type flags -- definition):")
    for e in raw.get("entities", []):
        lines.extend(_render_entity(e))

    if raw.get("relationships"):
        lines += ["", "RELATIONSHIPS (graph edges; use these for Join Logic):"]
        lines += [
            f"- {r['from']} -> {r['to']} via {r['via']} ({r.get('cardinality', '')})"
            f"{' as ' + r['role'] if r.get('role') else ''}{_tag(r.get('status', 'approved'))}"
            for r in raw["relationships"]
        ]
    if raw.get("roles"):
        lines += ["", "ROLES (one entity referenced in more than one role):"]
        lines += [
            f"- {r['name']} = {r['entity']} via {r['on']}.{r['via']}{_tag(r.get('status', 'approved'))}"
            for r in raw["roles"]
        ]
    if raw.get("aliases"):
        lines += ["", "ALIASES (source column names -> canonical attribute):"]
        lines += [
            f"- {', '.join(al['aliases'])} -> {al['target']}"
            f"{' (' + al['role'] + ' role)' if al.get('role') else ''}{_tag(al.get('status', 'approved'))}"
            for al in raw["aliases"]
        ]
    if raw.get("valueSets"):
        lines += ["", "VALUE SETS:"]
        for vs in raw["valueSets"]:
            extensible = " (open list)" if vs.get("open") else ""
            lines.append(f"- {vs['name']}{extensible}{_tag(vs.get('status', 'approved'))}: {', '.join(vs['values'])}")
    if raw.get("rules"):
        lines += ["", "RULES:"]
        for r in raw["rules"]:
            tolerance = f" (tolerance {r['tolerance']})" if r.get("tolerance") is not None else ""
            lines.append(
                f"- {r['id']} [{r.get('severity', 'warning')}]{_tag(r.get('status', 'approved'))}: "
                f"{r['description']} {r['expression']}{tolerance}"
            )
    return "\n".join(lines)


def prompt_context(output_format: str, ontology: Ontology | None) -> str:
    """The compact block of the project's `ontology` for `output_format`'s
    prompt, or "" when the format doesn't use it or the project has none."""
    if ontology is None or not applies_to(output_format):
        return ""
    return _render(ontology)


def grounding_excerpt(draft: str, ontology: Ontology | None) -> str:
    """Just the ontology entities `draft`'s STTM Mapping rows target, as a
    groundedness source. The full block (~36k chars) would crowd the
    retrieved knowledge-base context out of content_safety_service's 50k
    grounding cap; the entities a draft actually names are all that's
    needed to show its target names, types and definitions are grounded."""
    mapping = _mapping_columns(draft)
    if ontology is None or mapping is None:
        return ""
    targeted = {ontology.find_entity(_entity_value(row)) for row in mapping[2]} - {None}
    lines = [f"Canonical ontology {ontology.version} (excerpt):"]
    for e in ontology.raw.get("entities", []):
        if _norm(e["name"]) in targeted:
            lines.extend(_render_entity(e))
    return "\n".join(lines) if targeted else ""


# ------------------------------------------------------------ validation ---

_SUMMARY_ENTITY_KEY = "Target Table (from summary)"


def _summary_entity(draft: str) -> str:
    """The draft-wide target entity from an 'Attribute | Detail' summary
    row such as 'Target Entity | PI Opportunity', or ""."""
    for _title, body in split_sections(draft):
        parsed = draft_repair._parse_table(body)
        if not parsed or [c.lower() for c in parsed[0][:2]] != ["attribute", "detail"]:
            continue
        for row in parsed[1]:
            if row.get(parsed[0][0], "").strip().lower().startswith(_ENTITY_COLUMN_PREFIXES):
                return row.get(parsed[0][1], "").strip()
    return ""


def _mapping_columns(draft: str) -> tuple[str, list[str], list[dict[str, str]]] | None:
    """(section title, columns, rows) of the STTM Mapping section, or None.
    When the mapping has no Target Table column -- seen in a live run where
    the STTM template wasn't retrieved and the model named the entity once,
    in the summary -- each row gets the summary's entity under
    _SUMMARY_ENTITY_KEY. That key isn't in `columns`, so it is never
    rendered back into the draft; without it every row would skip validation."""
    for title, body in split_sections(draft):
        parsed = draft_repair._parse_table(body)
        if not parsed:
            continue
        columns, rows = parsed
        lower = [c.lower() for c in columns]
        if _CONFIDENCE_COLUMN in lower and _FIELD_COLUMN in lower:
            if not any(c.startswith(_ENTITY_COLUMN_PREFIXES) for c in lower):
                entity = _summary_entity(draft)
                for row in rows:
                    row[_SUMMARY_ENTITY_KEY] = entity
            return title, columns, rows
    return None


def _entity_value(row: dict[str, str]) -> str:
    return next(
        (v for k, v in row.items() if k.lower().startswith(_ENTITY_COLUMN_PREFIXES)), ""
    )


def _is_blank(value: str) -> bool:
    return value.strip().upper() in ("", "N/A", "NA", "NONE", "-")


def _base_type(value: str) -> str:
    token = re.split(r"[\s(]", (value or "").strip().upper(), maxsplit=1)[0]
    return _TYPE_SYNONYMS.get(token, "")


def _check_row(ontology: Ontology, row: dict[str, str]) -> tuple[str | None, list[Violation], str]:
    """Returns (the confidence this row must be capped at, if any; its
    violations; the reason for a 'Candidate' cap, for the Open Question).
    The cap is 'Needs SME Review' for a target not in the ontology at all,
    and 'Candidate' for a proposed target or for a source column tied to
    its target by description only (see Ontology.evidence)."""
    entity_cell = _entity_value(row).strip()
    field_cell = draft_repair._row_value(row, _FIELD_COLUMN).strip()
    confidence = draft_repair._row_value(row, _CONFIDENCE_COLUMN).strip().lower()
    confirmed = confidence == "confirmed"
    if _is_blank(entity_cell) or _is_blank(field_cell):
        return None, [], ""
    target = f"{entity_cell}.{field_cell}"
    cap, violations = _check_target(ontology, row, entity_cell, field_cell, target, confirmed)
    if cap is not None:
        reason = "it is a proposed ontology item awaiting SME approval" if cap == "candidate" else ""
        return cap, violations, reason

    # Target is approved and known: 'Confirmed' still needs the source
    # column itself to point at it. Not a violation -- a retry can't add
    # evidence, so this is capped in code (enforce_confidence) instead.
    # A blank source is a default-value row, governed by the prompt's rule.
    source = draft_repair._row_value(row, _SOURCE_COLUMN).strip()
    if _is_blank(source):
        return None, violations, ""
    entity_key = ontology.find_entity(entity_cell)
    found = ontology.evidence(source, entity_key, _norm(field_cell))
    if found in ("name", "approved"):
        return None, violations, ""
    how = "a proposed alias awaiting SME approval" if found == "proposed" else "description only"
    return "candidate", violations, (
        f"source column '{source}' was matched to it by {how}, not by name or an approved alias"
    )


def _check_target(
    ontology: Ontology, row: dict[str, str], entity_cell: str, field_cell: str, target: str, confirmed: bool
) -> tuple[str | None, list[Violation]]:
    entity_key = ontology.find_entity(entity_cell)
    if entity_key is None:
        suggestion = ontology.suggest_entity(entity_cell)
        if suggestion:
            return "needs sme review", [Violation(
                "unknown_entity", target,
                f"'{entity_cell}' is not an ontology entity -- did you mean '{suggestion}'? "
                f"Use the entity name exactly as the ontology spells it.",
            )]
        if confirmed:
            return "needs sme review", [Violation(
                "unknown_entity", target,
                f"'{entity_cell}' is not in the canonical ontology, so this row cannot be "
                f"'Confirmed' -- map it to an ontology entity, or mark it 'Needs SME Review' "
                f"with an Open Question stating the target is not in the canonical model.",
            )]
        return "needs sme review", []

    attribute = ontology.attributes[entity_key].get(_norm(field_cell))
    entity_name = ontology.entity_names[entity_key]
    if attribute is None:
        suggestion = ontology.suggest_attribute(entity_key, field_cell)
        if suggestion:
            return "needs sme review", [Violation(
                "unknown_attribute", target,
                f"'{field_cell}' is not an attribute of {entity_name} -- did you mean "
                f"'{suggestion}'? Use the attribute name exactly as the ontology spells it.",
            )]
        if confirmed:
            return "needs sme review", [Violation(
                "unknown_attribute", target,
                f"{entity_name} has no attribute '{field_cell}' in the canonical ontology, so "
                f"this row cannot be 'Confirmed' -- map it to an existing {entity_name} "
                f"attribute, or mark it 'Needs SME Review' with an Open Question stating the "
                f"field is not in the canonical model.",
            )]
        return "needs sme review", []

    violations = []
    proposed = ontology.entity_status[entity_key] != "approved" or attribute.status != "approved"
    if proposed and confirmed:
        violations.append(Violation(
            "proposed_confirmed", target,
            f"{entity_name}.{attribute.name} is a proposed ontology item not yet approved by "
            f"SMEs -- its Mapping Confidence must be 'Candidate' or 'Needs SME Review', not "
            f"'Confirmed'.",
        ))

    draft_type = _base_type(draft_repair._row_value(row, _TYPE_COLUMN))
    expected = _TYPE_SYNONYMS.get(attribute.data_type.upper(), "")
    if draft_type and expected and draft_type != expected:
        violations.append(Violation(
            "datatype_mismatch", target,
            f"Target Data Type for {entity_name}.{attribute.name} must be {attribute.data_type} "
            f"per the ontology, not '{draft_repair._row_value(row, _TYPE_COLUMN).strip()}'.",
        ))
    return ("candidate" if proposed else None), violations


def find_violations(draft: str, ontology: Ontology | None) -> list[Violation]:
    """Every STTM Mapping row whose target disagrees with the ontology: a
    misspelled entity/attribute that has a close ontology match, an
    unknown target marked 'Confirmed', a proposed target marked
    'Confirmed', or a data type different from the attribute's. [] when the
    draft has no STTM Mapping section or the project has no ontology. Used by
    generate_node (app/graph.py) to trigger a corrective retry, the same
    way draft_repair.has_malformed_rows() does for column-shifted rows."""
    mapping = _mapping_columns(draft)
    if ontology is None or mapping is None:
        return []
    violations: list[Violation] = []
    for row in mapping[2]:
        confidence = draft_repair._row_value(row, _CONFIDENCE_COLUMN).strip()
        if confidence.lower() not in _CONFIDENCE_RANK:
            # Seen live: the model wrote the Open Question text ("Confirm
            # vendor key for Cotiviti") or 'N/A' into this column.
            field = draft_repair._row_value(row, _FIELD_COLUMN).strip()
            violations.append(Violation(
                "invalid_confidence", f"{_entity_value(row).strip()}.{field}",
                f"Mapping Confidence is '{confidence}', but must be exactly one of Confirmed, "
                f"Candidate or Needs SME Review. Put any question in the Open Question column.",
            ))
        violations.extend(_check_row(ontology, row)[1])
    return violations


def _source_columns(text: str) -> list[str]:
    """Column names of an uploaded spreadsheet, from file_extraction's text
    form (one ', '-joined line per row): the widest of the first 15 lines
    whose cells (2 or more) are all short, non-numeric labels. Widest, not
    first, so a 'Vendor, Cotiviti' title row above a 3-column header isn't
    taken for it; a data row has numbers or dates, which disqualify it.
    Call it through _file_columns, which skips non-spreadsheet uploads:
    in prose any line with a comma would qualify."""
    best: list[str] = []
    for line in text.splitlines()[:15]:
        cells = [c.strip() for c in line.split(", ")]
        if len(cells) > max(len(best), 1) and all(
            c and len(c) <= 60 and not re.fullmatch(r"[\d.\-/: ]+", c) for c in cells
        ):
            best = cells
    return best


def _file_columns(source_file: dict) -> list[str]:
    """Column names of one uploaded file; [] unless it is a spreadsheet
    (file_extraction turns .docx/.pdf/.txt into prose, which has no columns)."""
    if not source_file.get("filename", "").lower().endswith((".xlsx", ".xls")):
        return []
    return _source_columns(source_file.get("text", ""))


def find_unmapped_source_columns(draft: str, source_files: list[dict]) -> list[str]:
    """Columns of the uploaded vendor files that the STTM neither maps (in
    its Source Field column) nor raises in its Assumptions/Open Questions
    section. Seen live: the model mapped only the columns that fit one
    target entity and silently dropped the rest (5 of 14). [] when the
    draft has no STTM Mapping section."""
    mapping = _mapping_columns(draft)
    if mapping is None:
        return []
    mentioned = " | ".join(
        _norm(draft_repair._row_value(row, "source field")) for row in mapping[2]
    )
    for title, body in split_sections(draft):
        if "assumption" in title.lower() or "open q" in title.lower():
            mentioned += " | " + _norm(body)
    unmapped = []
    for f in source_files:
        for column in _file_columns(f):
            if _norm(column) not in mentioned and column not in unmapped:
                unmapped.append(column)
    return unmapped


def add_unmapped_rows(draft: str, source_files: list[dict], unmapped: list[str]) -> str:
    """Appends a 'Needs SME Review' STTM Mapping row for each of `unmapped`
    (find_unmapped_source_columns' result once the model's retries ran
    out), so every column of every uploaded file appears in the
    deliverable -- before this they were only logged to telemetry.
    The target is left as 'TBD' rather than guessed in code."""
    mapping = _mapping_columns(draft)
    if not unmapped or mapping is None:
        return draft
    title, columns, rows = mapping
    lower = {c.lower(): c for c in columns}
    if _SOURCE_COLUMN not in lower:
        return draft  # nowhere to name the column; coverage can't be shown
    id_col = columns[0] if "id" in columns[0].lower() else None
    numbers = [re.match(r"(\D*)(\d+)$", r.get(id_col, "").strip()) for r in rows] if id_col else []
    numbers = [m for m in numbers if m]
    prefix = numbers[-1].group(1) if numbers else "M-"
    width = len(numbers[-1].group(2)) if numbers else 3
    next_id = max((int(m.group(2)) for m in numbers), default=0) + 1

    for column in unmapped:
        filename = next(
            (f.get("filename", "") for f in source_files if column in _file_columns(f)), ""
        )
        row = {c: "" for c in columns}
        if id_col:
            row[id_col] = f"{prefix}{next_id:0{width}d}"
            next_id += 1
        for c in columns:
            cl = c.lower()
            if cl.startswith(_ENTITY_COLUMN_PREFIXES) or cl == _FIELD_COLUMN:
                row[c] = "TBD"
            elif cl.startswith("source") and ("file" in cl or "table" in cl or "system" in cl):
                row[c] = filename
        row[lower[_SOURCE_COLUMN]] = column
        if _CONFIDENCE_COLUMN in lower:
            row[lower[_CONFIDENCE_COLUMN]] = "Needs SME Review"
        if _OPEN_QUESTION_COLUMN in lower:
            row[lower[_OPEN_QUESTION_COLUMN]] = (
                f"Source column '{column}'{' in ' + filename if filename else ''} has no target in this "
                f"draft: SME to name its target entity and field, or confirm it is out of scope."
            )
        rows.append(row)
    return "\n\n".join(
        f"## {t}\n{draft_repair._render_table(columns, rows) if t == title else body}"
        for t, body in split_sections(draft)
    )


def correction_feedback(violations: list[Violation], unmapped: list[str] | None = None) -> str:
    parts = []
    if violations:
        lines = "\n".join(f"- {v.target}: {v.message}" for v in violations)
        parts.append(
            "These STTM Mapping rows disagree with the CANONICAL ONTOLOGY in the system message:\n"
            f"{lines}\nFix exactly these rows."
        )
    if unmapped:
        parts.append(
            "These columns of the uploaded source file appear nowhere in the STTM: "
            f"{', '.join(unmapped)}. Add a mapping row for each one, using the ontology's ALIASES "
            "to find its target entity and attribute (a file often feeds several entities, e.g. "
            "Claim Header, Member and Provider as well as the main one). If a column has no "
            "target, raise it as an Open Question instead of dropping it."
        )
    parts.append("Keep every other row and every other column's value exactly as it was.")
    return "\n\n".join(parts)


_CONFIDENCE_RANK = {"confirmed": 0, "candidate": 1, "needs sme review": 2}
_CONFIDENCE_LABEL = {"candidate": "Candidate", "needs sme review": "Needs SME Review"}


_STATUS_TAG_RE = re.compile(r"[ \t]*\[(?:proposed|approved)\]", re.IGNORECASE)


def strip_status_tags(draft: str) -> str:
    """Removes '[proposed]' / '[approved]' tags the model copies from the
    prompt block into entity or field names ('PI Opportunity [proposed]').
    Left in, the deliverable shows them and the cell no longer names an
    ontology entity, so every such row was capped at 'Needs SME Review'."""
    return _STATUS_TAG_RE.sub("", draft)


def enforce_confidence(draft: str, ontology: Ontology | None) -> str:
    """Caps Mapping Confidence in code, so the one guarantee that matters
    for SMEs holds even when the model ignored every correction: a value
    outside Confirmed / Candidate / Needs SME Review becomes 'Needs SME
    Review', a target that is proposed is at most 'Candidate', and one not
    in the ontology is at most 'Needs SME Review'. A capped row with no Open Question gets one
    written out in full (find_placeholder_open_questions forbids bare IDs).
    Names and data types are left alone -- guessing a replacement name in
    code could silently map a field to the wrong target."""
    mapping = _mapping_columns(draft)
    if ontology is None or mapping is None:
        return draft
    title, columns, rows = mapping

    confidence_col = next(c for c in columns if c.lower() == _CONFIDENCE_COLUMN)
    question_col = next((c for c in columns if c.lower() == _OPEN_QUESTION_COLUMN), None)
    changed = False
    for row in rows:
        value = row.get(confidence_col, "").strip()
        if value.lower() not in _CONFIDENCE_RANK:
            # Not one of the three allowed values: usually the row's Open
            # Question written into the wrong column. Move real text there.
            if question_col and _is_blank(row.get(question_col, "")) and not _is_blank(value):
                row[question_col] = value
            row[confidence_col] = "Needs SME Review"
            changed = True
        cap, _, reason = _check_row(ontology, row)
        current = row.get(confidence_col, "").strip().lower()
        if cap is None or _CONFIDENCE_RANK.get(current, 2) >= _CONFIDENCE_RANK[cap]:
            continue
        row[confidence_col] = _CONFIDENCE_LABEL[cap]
        if question_col and _is_blank(row.get(question_col, "")):
            target = f"{_entity_value(row).strip()}.{draft_repair._row_value(row, _FIELD_COLUMN).strip()}"
            row[question_col] = (
                f"Confirm {target}: {reason}."
                if cap == "candidate"
                else f"Confirm target {target}: it is not in the canonical ontology."
            )
        changed = True

    if not changed:
        return draft
    return "\n\n".join(
        f"## {t}\n{draft_repair._render_table(columns, rows) if t == title else body}"
        for t, body in split_sections(draft)
    )


def summary(ontology: Ontology | None, folder: str) -> dict:
    """Source, version and counts by status of a project's ontology, for
    GET /v2/ontology."""
    if ontology is None:
        return {"loaded": False, "project": folder}
    raw = ontology.raw
    attributes = [a for e in raw.get("entities", []) for a in e.get("attributes", [])]

    def by_status(items: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            counts[item.get("status", "approved")] = counts.get(item.get("status", "approved"), 0) + 1
        return counts

    return {
        "loaded": True,
        "project": ontology.project,
        "source": ontology.source,
        "name": raw.get("name"),
        "version": ontology.version,
        "entities": by_status(raw.get("entities", [])),
        "attributes": by_status(attributes),
        "relationships": by_status(raw.get("relationships", [])),
        "aliases": by_status(raw.get("aliases", [])),
        "valueSets": by_status(raw.get("valueSets", [])),
        "rules": by_status(raw.get("rules", [])),
        "openIssues": sum(1 for i in raw.get("knownIssues", []) if i.get("status") == "open"),
        "guardrails": by_status(raw.get("guardrails", [])),
        "promptChars": len(_render(ontology)),
    }
