from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from core.models.entities import CaseSummary, DocumentChunk, DocumentReference


CITATION_REF_RE = re.compile(r"\[(\d{1,3})\]")


def canonical_case_key(value: str) -> str:
    digits = re.findall(r"\d+", value or "")
    return digits[0].lstrip("0") if digits else ""


def _normalize_scalar(value: object) -> str:
    text = str(value or "").strip()
    if text.lower() in {"none", "null", "n/a", "nan"}:
        return ""
    return text


def _stable_suffix(*parts: str) -> str:
    digest = hashlib.sha1()
    for part in parts:
        digest.update(part.encode("utf-8", errors="ignore"))
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def _normalize_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_normalize_scalar(item) for item in value if _normalize_scalar(item)]


def _normalize_metadata(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, item in value.items():
        key_text = _normalize_scalar(key)
        if key_text:
            normalized[key_text] = _normalize_scalar(item)
    return normalized


def _extract_citation_refs(text: str) -> list[str]:
    seen: set[str] = set()
    refs: list[str] = []
    for match in CITATION_REF_RE.finditer(text or ""):
        ref_id = str(int(match.group(1)))
        if ref_id not in seen:
            seen.add(ref_id)
            refs.append(ref_id)
    return refs


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_markdown_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells if cell)


def _parse_markdown_reference_rows(markdown: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    sequence = 1
    for line in (markdown or "").splitlines():
        if "|" not in line:
            continue
        cells = _split_markdown_row(line)
        if _is_markdown_separator(cells):
            continue
        nonempty = [cell for cell in cells if cell]
        if not nonempty:
            continue
        joined = " | ".join(nonempty)
        normalized_joined = joined.lower().replace("ё", "е")
        if (
            "название" in normalized_joined
            and ("номер" in normalized_joined or "шифр" in normalized_joined or "п/п" in normalized_joined)
            and not CITATION_REF_RE.search(joined)
        ):
            continue
        marker_match = CITATION_REF_RE.search(joined)
        ref_id = ""
        value_cells = cells
        if marker_match:
            ref_id = str(int(marker_match.group(1)))
            value_cells = [cell for cell in cells if not CITATION_REF_RE.fullmatch(cell.strip())]
        else:
            first = nonempty[0].strip().strip(".")
            if first.isdigit() and len(nonempty) > 1:
                ref_id = str(int(first))
                value_cells = [cell for cell in cells if cell.strip().strip(".") != first]
            elif len(cells) >= 2 and not cells[0].strip() and len(nonempty) == 1:
                ref_id = str(sequence)
                value_cells = cells[1:]
        raw_text = " ".join(_normalize_scalar(cell) for cell in value_cells if _normalize_scalar(cell))
        if raw_text:
            rows.append({"ref_id": ref_id or str(sequence), "marker": f"[{ref_id or sequence}]", "raw_text": raw_text})
            sequence += 1
    return rows


def _reference_table_candidate(table: dict[str, object]) -> bool:
    metadata = table.get("metadata") if isinstance(table.get("metadata"), dict) else {}
    table_kind = _normalize_scalar(metadata.get("table_kind", "") if isinstance(metadata, dict) else "")
    haystack = " ".join(
        part
        for part in [
            table_kind,
            _normalize_scalar(table.get("section_title", "")),
            _normalize_scalar(table.get("title", "")),
            " ".join(_normalize_list(table.get("heading_path", []))),
        ]
        if part
    ).lower().replace("ё", "е")
    return table_kind == "reference_documents" or "ссылоч" in haystack or "reference document" in haystack


def _clean_reference_title(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip(" |")
    cleaned = CITATION_REF_RE.sub("", cleaned).strip()
    return cleaned


def _infer_document_number(raw_text: str) -> str:
    text = _clean_reference_title(raw_text)
    if not text:
        return ""
    for pattern in (
        r"^(КДСТ\s+FATA-\d+[A-ZА-Я0-9.-]*)\b",
        r"^((?:CMM|AMM|SRM|SB|AOT|FATA)\s+[A-Z0-9_.-]+)\b",
        r"^([A-ZА-Я]{1,8}[- ]?\d[A-ZА-Я0-9_.-]*(?:[-_][A-ZА-Я0-9_.-]+)*)\b",
        r"^(МР-\d+[A-ZА-Я0-9_.-]*(?:-[A-ZА-Я0-9_.-]+)*)\b",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" ,;:")
    return ""


def _normalize_reference_record(
    *,
    case_id: str,
    internal_document_id: str,
    source_document_id: str,
    source_file: str,
    row: dict[str, object],
    fallback_ref_id: int,
) -> DocumentReference | None:
    string_row = {
        str(key): _normalize_scalar(value)
        for key, value in row.items()
        if not str(key).startswith("source_")
    }
    raw_joined = " ".join(value for value in string_row.values() if value)
    marker_match = CITATION_REF_RE.search(raw_joined)
    ref_id = _normalize_scalar(row.get("ref_id", ""))
    if marker_match:
        ref_id = str(int(marker_match.group(1)))
    elif ref_id.isdigit():
        ref_id = str(int(ref_id))
    else:
        ref_id = str(fallback_ref_id)
    marker = _normalize_scalar(row.get("marker", "")) or f"[{ref_id}]"
    source_table_id = _normalize_scalar(row.get("source_table_id", ""))
    raw_text = _normalize_scalar(row.get("raw_text", "")) or raw_joined

    def first_value(*names: str) -> str:
        lowered = {key.lower(): value for key, value in string_row.items()}
        for name in names:
            value = lowered.get(name.lower(), "")
            if value:
                return value
        for key, value in lowered.items():
            if any(name.lower() in key for name in names) and value:
                return value
        return ""

    document_number = first_value(
        "document_number",
        "номер",
        "шифр",
        "обозначение",
        "number",
        "designation",
    )
    document_type = first_value("document_type", "тип", "type", "категория")
    title = first_value("title", "название", "наименование", "name", "description")
    if not title:
        title = _clean_reference_title(raw_text)
    if not document_number:
        document_number = _infer_document_number(raw_text)
    if document_number and title.startswith(document_number):
        title = title[len(document_number) :].strip(" -:;")
    raw_text = _clean_reference_title(raw_text)
    content_probe = raw_text.lower().replace("ё", "е")
    content_probe = re.sub(r"\b(?:n/a|na)\b", " ", content_probe)
    content_probe = content_probe.replace("неприменимо", " ")
    content_probe = re.sub(r"[\W_]+", "", content_probe, flags=re.UNICODE)
    if not content_probe:
        return None
    if not raw_text:
        return None
    return DocumentReference(
        case_id=case_id,
        document_id=internal_document_id,
        ref_id=ref_id,
        marker=marker,
        title=title,
        document_number=_clean_reference_title(document_number),
        document_type=document_type,
        raw_text=raw_text,
        source_document_id=source_document_id,
        source_table_id=source_table_id,
        source_file=source_file,
        raw_json=dict(row),
    )


def _extract_document_references(
    case_id: str,
    internal_document_id: str,
    source_document_id: str,
    source_file: str,
    document: dict[str, object],
) -> list[DocumentReference]:
    candidates: list[dict[str, object]] = []
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    meta_rows = metadata.get("reference_documents", []) if isinstance(metadata, dict) else []
    if isinstance(meta_rows, list):
        for row in meta_rows:
            if isinstance(row, dict):
                candidates.append(dict(row))
    for table in document.get("tables", []):
        if not isinstance(table, dict) or not _reference_table_candidate(table):
            continue
        table_id = _normalize_scalar(table.get("table_id", ""))
        for row in _parse_markdown_reference_rows(_normalize_scalar(table.get("markdown", ""))):
            row["source_table_id"] = table_id
            candidates.append(row)

    references: list[DocumentReference] = []
    seen: set[str] = set()
    for index, row in enumerate(candidates, start=1):
        reference = _normalize_reference_record(
            case_id=case_id,
            internal_document_id=internal_document_id,
            source_document_id=source_document_id,
            source_file=source_file,
            row=row,
            fallback_ref_id=index,
        )
        if reference is None or reference.ref_id in seen:
            continue
        seen.add(reference.ref_id)
        references.append(reference)
    return references


def _vault_note_path(source_file: str) -> str:
    return source_file.replace("\\", "/").lstrip("/")


def import_mro_documents(demo_data_root: Path) -> tuple[list[CaseSummary], list[dict[str, str]], list[DocumentChunk], list[DocumentReference], str]:
    cases: list[CaseSummary] = []
    documents: list[dict[str, str]] = []
    chunks: list[DocumentChunk] = []
    references: list[DocumentReference] = []
    digest = hashlib.sha256()
    for path in sorted(demo_data_root.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
        case_id = f"MRO-{int(_normalize_scalar(payload.get('work_order_id', ''))):03d}"
        cases.append(
            CaseSummary(
                case_id=case_id,
                aircraft_type=_normalize_scalar(payload.get("aircraft_type", "")),
                msn=_normalize_scalar(payload.get("msn", "")),
                subject=_normalize_scalar(payload.get("subject", "")),
                problem_summary=_normalize_scalar(payload.get("problem_summary", "")),
                ata_list=[_normalize_scalar(item) for item in payload.get("ata_list", []) if _normalize_scalar(item)],
                applicable_ap_refs=[_normalize_scalar(item) for item in payload.get("applicable_ap_refs", []) if _normalize_scalar(item)],
                source_document_id=_normalize_scalar(payload.get("source_document_id", "")),
                metadata={
                    "case_type": _normalize_scalar(payload.get("case_type", "")),
                    "case_status": _normalize_scalar(payload.get("case_status", "")),
                    "source_type": _normalize_scalar(payload.get("source_type", "")),
                },
            )
        )
        entry_chunk = payload.get("entry_chunk") if isinstance(payload.get("entry_chunk"), dict) else None
        if entry_chunk:
            case_document_id = f"{case_id}::case"
            documents.append(
                {
                    "document_id": case_document_id,
                    "case_id": case_id,
                    "title": _normalize_scalar(payload.get("subject", "")) or case_id,
                    "subject": _normalize_scalar(payload.get("problem_summary", "")),
                    "document_family": "case",
                    "source_file": _normalize_scalar(entry_chunk.get("source_file", "")),
                    "source_system": "mro_rag",
                    "source_document_id": f"{_normalize_scalar(payload.get('work_order_id', ''))}::case",
                }
            )
            entry_text = _normalize_scalar(entry_chunk.get("text", ""))
            entry_search_text = _normalize_scalar(entry_chunk.get("search_text", "")) or " ".join(
                part
                for part in [
                    _normalize_scalar(payload.get("work_order_id", "")),
                    _normalize_scalar(payload.get("subject", "")),
                    _normalize_scalar(payload.get("problem_summary", "")),
                    entry_text,
                ]
                if part
            )
            chunks.append(
                DocumentChunk(
                    case_id=case_id,
                    document_id=case_document_id,
                    chunk_id=f"{case_document_id}::chunk::{_stable_suffix(case_id, 'entry', entry_search_text[:200])}",
                    chunk_kind="case",
                    chunk_level=_normalize_scalar(entry_chunk.get("chunk_level", "case")),
                    document_family="case",
                    section_label=_normalize_scalar(entry_chunk.get("section_label", "case")),
                    section_title=_normalize_scalar(entry_chunk.get("section_title", "case")),
                    heading_path=_normalize_list(entry_chunk.get("heading_path", [])),
                    text=entry_text or entry_search_text,
                    search_text=entry_search_text,
                    citation_refs=_extract_citation_refs(f"{entry_text}\n{entry_search_text}"),
                    source_file=_normalize_scalar(entry_chunk.get("source_file", "")),
                    metadata=_normalize_metadata(entry_chunk.get("metadata", {})),
                    vault_note_path=_vault_note_path(_normalize_scalar(entry_chunk.get("source_file", ""))),
                )
            )
        for document in payload.get("documents", []):
            source_document_id = _normalize_scalar(document.get("document_id", ""))
            if not source_document_id:
                continue
            source_file = _normalize_scalar(document.get("source_file", ""))
            document_id = f"{case_id}::doc::{_stable_suffix(source_document_id, source_file or path.name)}"
            documents.append(
                {
                    "document_id": document_id,
                    "case_id": case_id,
                    "title": _normalize_scalar(document.get("title", "")),
                    "subject": _normalize_scalar(document.get("subject", "")),
                    "document_family": _normalize_scalar(document.get("document_family", "")),
                    "source_file": source_file,
                    "source_system": "mro_rag",
                    "source_document_id": source_document_id,
                }
            )
            references.extend(
                _extract_document_references(
                    case_id,
                    document_id,
                    source_document_id,
                    source_file,
                    document,
                )
            )
            for chunk in document.get("chunks", []):
                source_chunk_id = _normalize_scalar(chunk.get("chunk_id", ""))
                if not source_chunk_id:
                    continue
                section_title = _normalize_scalar(chunk.get("section_title", ""))
                chunk_text = _normalize_scalar(chunk.get("text", ""))
                search_text = _normalize_scalar(chunk.get("search_text", "")) or " ".join(
                    part
                    for part in [
                        _normalize_scalar(payload.get("subject", "")),
                        _normalize_scalar(payload.get("problem_summary", "")),
                        _normalize_scalar(document.get("document_family", "")),
                        _normalize_scalar(document.get("title", "")),
                        _normalize_scalar(document.get("subject", "")),
                        _normalize_scalar(chunk.get("section_label", "")),
                        section_title,
                        " ".join(_normalize_list(chunk.get("heading_path", []))),
                        chunk_text,
                    ]
                    if part
                )
                chunk_id = f"{document_id}::chunk::{_stable_suffix(source_chunk_id, section_title, chunk_text[:200])}"
                chunks.append(
                    DocumentChunk(
                        case_id=case_id,
                        document_id=document_id,
                        chunk_id=chunk_id,
                        chunk_kind=_normalize_scalar(chunk.get("chunk_kind", "")),
                        chunk_level=_normalize_scalar(chunk.get("chunk_level", "")),
                        document_family=_normalize_scalar(chunk.get("document_family", "")) or _normalize_scalar(document.get("document_family", "")),
                        section_label=_normalize_scalar(chunk.get("section_label", "")),
                        section_title=section_title,
                        heading_path=_normalize_list(chunk.get("heading_path", [])),
                        text=chunk_text,
                        search_text=search_text,
                        table_refs=_normalize_list(chunk.get("table_refs", [])),
                        citation_refs=_extract_citation_refs(f"{chunk_text}\n{search_text}"),
                        metadata=_normalize_metadata(chunk.get("metadata", {})),
                        page_number=None,
                        source_file=_normalize_scalar(chunk.get("source_file", "")),
                        vault_note_path=_vault_note_path(_normalize_scalar(chunk.get("source_file", ""))),
                    )
                )
            for table in document.get("tables", []):
                table_id = _normalize_scalar(table.get("table_id", ""))
                table_text = _normalize_scalar(table.get("markdown", ""))
                if not table_id or not table_text:
                    continue
                section_title = _normalize_scalar(table.get("section_title", ""))
                search_text = " ".join(
                    part
                    for part in [
                        _normalize_scalar(payload.get("subject", "")),
                        _normalize_scalar(payload.get("problem_summary", "")),
                        _normalize_scalar(document.get("document_family", "")),
                        _normalize_scalar(document.get("title", "")),
                        _normalize_scalar(document.get("subject", "")),
                        _normalize_scalar(table.get("section_label", "")),
                        section_title,
                        _normalize_scalar(table.get("title", "")),
                        table_text,
                    ]
                    if part
                )
                chunks.append(
                    DocumentChunk(
                        case_id=case_id,
                        document_id=document_id,
                        chunk_id=f"{document_id}::table::{_stable_suffix(table_id, section_title, table_text[:200])}",
                        chunk_kind="table",
                        chunk_level="table",
                        document_family=_normalize_scalar(document.get("document_family", "")),
                        section_label=_normalize_scalar(table.get("section_label", "")),
                        section_title=section_title or _normalize_scalar(table.get("title", "")),
                        heading_path=_normalize_list(table.get("heading_path", [])),
                        text=table_text,
                        search_text=search_text,
                        citation_refs=_extract_citation_refs(f"{table_text}\n{search_text}"),
                        source_file=_normalize_scalar(table.get("source_file", "")),
                        metadata=_normalize_metadata(table.get("metadata", {})),
                        vault_note_path=_vault_note_path(_normalize_scalar(table.get("source_file", "")) or source_file),
                    )
                )
    return cases, documents, chunks, references, digest.hexdigest()
