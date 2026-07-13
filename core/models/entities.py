from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class CaseSummary:
    case_id: str
    aircraft_type: str = ""
    msn: str = ""
    subject: str = ""
    problem_summary: str = ""
    ata_list: list[str] = field(default_factory=list)
    applicable_ap_refs: list[str] = field(default_factory=list)
    source_document_id: str = ""
    source_system: str = "mro_rag"
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class DocumentChunk:
    case_id: str
    document_id: str
    chunk_id: str
    chunk_kind: str
    section_title: str
    text: str
    search_text: str = ""
    section_label: str = ""
    heading_path: list[str] = field(default_factory=list)
    chunk_level: str = ""
    document_family: str = ""
    table_refs: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    page_number: int | None = None
    source_system: str = "mro_rag"
    source_file: str = ""
    block_id: str = ""
    vault_note_path: str = ""
    page_image_path: str = ""
