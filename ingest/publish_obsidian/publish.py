from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re

from core.models.entities import CaseSummary, DocumentChunk


def _safe_name(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "-_ ." else "_" for ch in value.strip())
    return "_".join(text.split()) or "item"


def _resolve_source_markdown(source_file: str) -> Path | None:
    if not source_file:
        return None
    candidate = Path("/mnt/ii_models/Users/hizhenkov/MRO_RAG/data/mro_markdown") / source_file
    return candidate if candidate.exists() else None


def _extract_figure_captions(markdown_text: str) -> list[str]:
    lines = markdown_text.splitlines()
    captions: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() != "<!-- image -->":
            index += 1
            continue
        caption_lines: list[str] = []
        cursor = index + 1
        while cursor < len(lines):
            line = lines[cursor].strip()
            if not line:
                if caption_lines:
                    break
                cursor += 1
                continue
            if line == "<!-- image -->":
                break
            caption_lines.append(line)
            if re.match(r"^(Рисунок|Figure)\b", line):
                next_line = lines[cursor + 1].strip() if cursor + 1 < len(lines) else ""
                if next_line and next_line != "<!-- image -->" and not re.match(r"^#{1,6}\s", next_line):
                    caption_lines.append(next_line)
                    cursor += 1
                break
            cursor += 1
        caption = " ".join(part.strip() for part in caption_lines if part.strip())
        if caption:
            captions.append(caption)
        index = cursor + 1
    return captions


def _figure_asset_relpath(document_id: str, figure_number: int) -> str:
    return f"assets/{_safe_name(document_id)}/figure_{figure_number:03d}.png"


def _ensure_obsidian_scaffold(vault_root: Path) -> None:
    obsidian_dir = vault_root / ".obsidian"
    obsidian_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = vault_root / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    app_json = obsidian_dir / "app.json"
    if not app_json.exists():
        app_json.write_text('{\n  "showLineNumber": false\n}\n', encoding="utf-8")

    appearance_json = obsidian_dir / "appearance.json"
    if not appearance_json.exists():
        appearance_json.write_text('{\n  "cssTheme": ""\n}\n', encoding="utf-8")

    core_plugins_json = obsidian_dir / "core-plugins.json"
    if not core_plugins_json.exists():
        core_plugins_json.write_text(
            '[\n  "file-explorer",\n  "search",\n  "outgoing-link",\n  "backlink",\n  "tag-pane",\n  "page-preview"\n]\n',
            encoding="utf-8",
        )

    workspace_json = obsidian_dir / "workspace.json"
    if not workspace_json.exists():
        workspace_json.write_text(
            '{\n'
            '  "active": "root",\n'
            '  "lastOpenFiles": [\n'
            '    "MRO KB.md"\n'
            "  ]\n"
            '}\n',
            encoding="utf-8",
        )


def publish_obsidian_vault(vault_root: Path, cases: list[CaseSummary], chunks: list[DocumentChunk]) -> None:
    _ensure_obsidian_scaffold(vault_root)
    case_dir = vault_root / "cases"
    doc_dir = vault_root / "documents"
    case_dir.mkdir(parents=True, exist_ok=True)
    doc_dir.mkdir(parents=True, exist_ok=True)

    chunks_by_case: dict[str, list[DocumentChunk]] = defaultdict(list)
    chunks_by_doc: dict[str, list[DocumentChunk]] = defaultdict(list)
    for chunk in chunks:
        chunk.block_id = chunk.block_id or ("" if chunk.vault_note_path else chunk.chunk_id.replace("::", "-").replace("/", "-"))
        chunk.vault_note_path = chunk.vault_note_path or f"documents/{_safe_name(chunk.document_id)}.md"
        chunks_by_case[chunk.case_id].append(chunk)
        chunks_by_doc[chunk.document_id].append(chunk)

    for item in cases:
        note_path = case_dir / f"{_safe_name(item.case_id)}.md"
        linked_docs = sorted({chunk.document_id for chunk in chunks_by_case.get(item.case_id, [])})
        note_path.write_text(
            "\n".join(
                [
                    "---",
                    f"case_id: {item.case_id}",
                    f"aircraft_type: {item.aircraft_type}",
                    f"msn: {item.msn}",
                    "---",
                    "",
                    f"# {item.case_id}",
                    "",
                    item.subject or "_No subject_",
                    "",
                    item.problem_summary or "_No problem summary_",
                    "",
                    "## Linked documents",
                    *(f"- [[documents/{_safe_name(doc_id)}]]" for doc_id in linked_docs),
                    "",
                ]
            ),
            encoding="utf-8",
        )

    index_note = vault_root / "MRO KB.md"
    index_note.write_text(
        "\n".join(
            [
                "---",
                "title: MRO KB",
                "tags:",
                "  - mro",
                "  - knowledge-base",
                "---",
                "",
                "# MRO KB",
                "",
                "## Навигация",
                "- [[cases/_index|Cases]]",
                "- [[documents/_index|Documents]]",
                "",
                f"Всего заявок: {len(cases)}",
                f"Всего чанков: {len(chunks)}",
                "",
                "## Как использовать",
                "- Откройте `cases/_index` для поиска по заявкам.",
                "- Откройте `documents/_index` для перехода к исходным документам.",
                "- Ссылки из RAG ведут на конкретный block внутри заметки документа.",
                "",
                "Vault сформирован автоматически из корпуса MRO.",
            ]
        ),
        encoding="utf-8",
    )

    folder_index_cases = case_dir / "_index.md"
    folder_index_cases.write_text(
        "\n".join(
            [
                "# Cases",
                "",
                *(f"- [[{_safe_name(item.case_id)}]]" for item in sorted(cases, key=lambda row: row.case_id)),
                "",
            ]
        ),
        encoding="utf-8",
    )

    folder_index_docs = doc_dir / "_index.md"
    folder_index_docs.write_text(
        "\n".join(
            [
                "# Documents",
                "",
                *(f"- [[{_safe_name(document_id)}]]" for document_id in sorted(chunks_by_doc.keys())),
                "",
            ]
        ),
        encoding="utf-8",
    )

    for document_id, document_chunks in chunks_by_doc.items():
        note_path = doc_dir / f"{_safe_name(document_id)}.md"
        source_markdown_path = _resolve_source_markdown(document_chunks[0].source_file)
        figure_captions: list[str] = []
        if source_markdown_path is not None:
            figure_captions = _extract_figure_captions(source_markdown_path.read_text(encoding="utf-8", errors="ignore"))

        lines = [
            "---",
            f"document_id: {document_id}",
            f"case_id: {document_chunks[0].case_id}",
            f"source_file: {document_chunks[0].source_file}",
            "---",
            "",
            f"# {document_id}",
            "",
        ]
        if figure_captions:
            lines.append("## Figures")
            lines.append("")
            lines.append("> [!info] Figures detected in source markdown")
            lines.append("> Original image binaries are not present in the current MRO_RAG repository export.")
            lines.append("> The vault keeps stable asset paths so real images can be attached later without changing links.")
            lines.append("")
            for index, caption in enumerate(figure_captions, start=1):
                asset_relpath = _figure_asset_relpath(document_id, index)
                asset_dir = vault_root / Path(asset_relpath).parent
                asset_dir.mkdir(parents=True, exist_ok=True)
                lines.append(f"### Figure {index}")
                lines.append("")
                lines.append(f"Caption: {caption}")
                lines.append("")
                lines.append(f"Expected asset: `{asset_relpath}`")
                lines.append("")
            lines.append("")
        for chunk in sorted(document_chunks, key=lambda item: item.chunk_id):
            lines.append(f"## {chunk.section_title or chunk.chunk_kind or 'Chunk'} ^{chunk.block_id}")
            lines.append("")
            lines.append(chunk.text or "_Empty chunk_")
            lines.append("")
        note_path.write_text("\n".join(lines), encoding="utf-8")
