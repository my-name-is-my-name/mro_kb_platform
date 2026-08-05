from __future__ import annotations

try:
    from pydantic import BaseModel, ConfigDict, Field
except ModuleNotFoundError as exc:  # pragma: no cover - startup guard.
    raise RuntimeError(
        "mro-request-assessment requires Pydantic v2. "
        "Install project dependencies with `pip install -e .` in Python 3.12."
    ) from exc

__all__ = ["BaseModel", "ConfigDict", "Field"]
