from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
MRO_RAG_ROOT = WORKSPACE_ROOT / "MRO_RAG"
DATA_RUNTIME_ROOT = PROJECT_ROOT / "data_runtime"
OBSIDIAN_VAULT_ROOT = DATA_RUNTIME_ROOT / "obsidian_vault"
SQLITE_PATH = DATA_RUNTIME_ROOT / "mro_kb.sqlite3"


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    project_root: Path = PROJECT_ROOT
    workspace_root: Path = WORKSPACE_ROOT
    mro_rag_root: Path = MRO_RAG_ROOT
    data_runtime_root: Path = DATA_RUNTIME_ROOT
    obsidian_vault_root: Path = OBSIDIAN_VAULT_ROOT
    sqlite_path: Path = SQLITE_PATH


def ensure_runtime_dirs() -> RuntimePaths:
    paths = RuntimePaths()
    paths.data_runtime_root.mkdir(parents=True, exist_ok=True)
    paths.obsidian_vault_root.mkdir(parents=True, exist_ok=True)
    (paths.obsidian_vault_root / ".obsidian").mkdir(parents=True, exist_ok=True)
    (paths.obsidian_vault_root / "assets").mkdir(parents=True, exist_ok=True)
    return paths
