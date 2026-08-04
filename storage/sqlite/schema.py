from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    aircraft_type TEXT NOT NULL DEFAULT '',
    msn TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    problem_summary TEXT NOT NULL DEFAULT '',
    ata_list_json TEXT NOT NULL DEFAULT '[]',
    applicable_ap_refs_json TEXT NOT NULL DEFAULT '[]',
    source_document_id TEXT NOT NULL DEFAULT '',
    source_system TEXT NOT NULL DEFAULT 'mro_rag',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    source_document_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    document_family TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    source_system TEXT NOT NULL DEFAULT 'mro_rag',
    document_type TEXT NOT NULL DEFAULT '',
    issuer TEXT NOT NULL DEFAULT '',
    aircraft_type TEXT NOT NULL DEFAULT '',
    effectivity TEXT NOT NULL DEFAULT '',
    ata TEXT NOT NULL DEFAULT '',
    revision TEXT NOT NULL DEFAULT '',
    issue_date TEXT NOT NULL DEFAULT '',
    section_reference TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    trust_level TEXT NOT NULL DEFAULT 'internal_reference',
    source_origin TEXT NOT NULL DEFAULT 'internal',
    FOREIGN KEY(case_id) REFERENCES cases(case_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    chunk_kind TEXT NOT NULL DEFAULT '',
    unit_kind TEXT NOT NULL DEFAULT 'chunk',
    chunk_level TEXT NOT NULL DEFAULT '',
    document_family TEXT NOT NULL DEFAULT '',
    section_label TEXT NOT NULL DEFAULT '',
    section_title TEXT NOT NULL DEFAULT '',
    heading_path_json TEXT NOT NULL DEFAULT '[]',
    text TEXT NOT NULL DEFAULT '',
    search_text TEXT NOT NULL DEFAULT '',
    table_refs_json TEXT NOT NULL DEFAULT '[]',
    citation_refs_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    page_number INTEGER,
    source_file TEXT NOT NULL DEFAULT '',
    source_system TEXT NOT NULL DEFAULT 'mro_rag',
    block_id TEXT NOT NULL DEFAULT '',
    vault_note_path TEXT NOT NULL DEFAULT '',
    page_image_path TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(case_id) REFERENCES cases(case_id),
    FOREIGN KEY(document_id) REFERENCES documents(document_id)
);

CREATE TABLE IF NOT EXISTS document_references (
    document_id TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    marker TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    document_number TEXT NOT NULL DEFAULT '',
    document_type TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT '',
    source_document_id TEXT NOT NULL DEFAULT '',
    source_table_id TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(document_id, ref_id),
    FOREIGN KEY(document_id) REFERENCES documents(document_id),
    FOREIGN KEY(case_id) REFERENCES cases(case_id)
);

CREATE TABLE IF NOT EXISTS chunk_references (
    chunk_id TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    PRIMARY KEY(chunk_id, ref_id),
    FOREIGN KEY(chunk_id) REFERENCES chunks(chunk_id),
    FOREIGN KEY(document_id) REFERENCES documents(document_id),
    FOREIGN KEY(case_id) REFERENCES cases(case_id)
);

CREATE TABLE IF NOT EXISTS case_document_links (
    case_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    link_status TEXT NOT NULL DEFAULT 'matched',
    PRIMARY KEY(case_id, document_id),
    FOREIGN KEY(case_id) REFERENCES cases(case_id),
    FOREIGN KEY(document_id) REFERENCES documents(document_id)
);

CREATE TABLE IF NOT EXISTS corpus_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source_root TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_label TEXT NOT NULL,
    source_path TEXT NOT NULL,
    error_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_case_id ON documents(case_id);
CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_case_id ON chunks(case_id);
CREATE INDEX IF NOT EXISTS idx_chunks_unit_kind ON chunks(unit_kind);
CREATE INDEX IF NOT EXISTS idx_document_references_case_ref ON document_references(case_id, ref_id);
CREATE INDEX IF NOT EXISTS idx_document_references_number ON document_references(document_number);
CREATE INDEX IF NOT EXISTS idx_chunk_references_document_ref ON chunk_references(document_id, ref_id);
"""
