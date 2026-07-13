from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from core.models.entities import CaseSummary, DocumentChunk


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


def _vault_note_path(source_file: str) -> str:
    return source_file.replace("\\", "/").lstrip("/")


def import_mro_documents(demo_data_root: Path) -> tuple[list[CaseSummary], list[dict[str, str]], list[DocumentChunk], str]:
    cases: list[CaseSummary] = []
    documents: list[dict[str, str]] = []
    chunks: list[DocumentChunk] = []
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
                        source_file=_normalize_scalar(table.get("source_file", "")),
                        metadata=_normalize_metadata(table.get("metadata", {})),
                        vault_note_path=_vault_note_path(_normalize_scalar(table.get("source_file", "")) or source_file),
                    )
                )
    return cases, documents, chunks, digest.hexdigest()
