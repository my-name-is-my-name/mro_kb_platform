from __future__ import annotations

from urllib.parse import quote


def obsidian_uri(vault_name: str, note_path: str, block_id: str | None = None) -> str:
    encoded_file = quote(note_path.replace("\\", "/"))
    uri = f"obsidian://open?vault={quote(vault_name)}&file={encoded_file}"
    if block_id:
        uri += f"%23{quote(block_id)}"
    return uri


def source_label(case_id: str, document_id: str, section_title: str) -> str:
    section = section_title or "-"
    return f"{case_id} / {document_id} / {section}"
