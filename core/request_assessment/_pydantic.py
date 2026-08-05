from __future__ import annotations

from enum import Enum
import sys
import types
from typing import Any, Union, get_args, get_origin, get_type_hints

try:  # pragma: no cover - exercised when pydantic is installed.
    from pydantic import BaseModel as BaseModel  # type: ignore
    from pydantic import Field as Field  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - local fallback for this repo.

    def Field(default: Any = None, default_factory: Any = None, **_: Any) -> Any:
        if default_factory is not None:
            return _DefaultFactory(default_factory)
        return default

    class _DefaultFactory:
        def __init__(self, factory: Any) -> None:
            self.factory = factory

    class BaseModel:
        def __init__(self, **data: Any) -> None:
            try:
                hints = get_type_hints(self.__class__)
            except Exception:
                hints = getattr(self, "__annotations__", {})
            for name, annotation in hints.items():
                default = getattr(self.__class__, name, None)
                if name in data:
                    value = data[name]
                elif isinstance(default, _DefaultFactory):
                    value = default.factory()
                else:
                    value = default
                setattr(self, name, _coerce(value, annotation))
            for name, value in data.items():
                if name not in hints:
                    setattr(self, name, value)

        def model_dump(self, mode: str = "python", exclude_none: bool = False) -> dict[str, Any]:
            return {
                key: _dump(value, mode=mode, exclude_none=exclude_none)
                for key, value in self.__dict__.items()
                if not (exclude_none and value is None)
            }

        @classmethod
        def model_validate(cls, value: Any) -> "BaseModel":
            if isinstance(value, cls):
                return value
            if isinstance(value, dict):
                return cls(**value)
            raise TypeError(f"Cannot validate {type(value)!r} as {cls.__name__}")


def _coerce(value: Any, annotation: Any) -> Any:
    if value is None:
        return None
    if isinstance(annotation, str):
        return _coerce_from_string(value, annotation)
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is list and args:
        return [_coerce(item, args[0]) for item in (value or [])]
    if origin is dict:
        return dict(value or {})
    union_type = getattr(types, "UnionType", None)
    if origin in {Union, union_type} and type(None) in args:
        other = next((arg for arg in args if arg is not type(None)), Any)
        return _coerce(value, other)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return value if isinstance(value, annotation) else annotation(value)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel) and isinstance(value, dict):
        return annotation(**value)
    return value


def _coerce_from_string(value: Any, annotation: str) -> Any:
    text = annotation.replace("typing.", "").strip()
    if " | None" in text:
        text = text.replace(" | None", "").strip()
    if text.startswith("list[") and text.endswith("]"):
        inner = text[5:-1]
        return [_coerce_from_string(item, inner) for item in (value or [])]
    if text.startswith("dict["):
        return dict(value or {})
    cls = _find_class(text)
    if cls and isinstance(cls, type):
        if issubclass(cls, Enum):
            return value if isinstance(value, cls) else cls(value)
        if issubclass(cls, BaseModel) and isinstance(value, dict):
            return cls(**value)
    return value


def _find_class(name: str) -> Any:
    simple = name.rsplit(".", 1)[-1]
    for module_name in ("core.request_assessment.models", "core.request_assessment.progress"):
        module = sys.modules.get(module_name)
        if module and hasattr(module, simple):
            return getattr(module, simple)
    return None


def _dump(value: Any, mode: str, exclude_none: bool) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode=mode, exclude_none=exclude_none)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_dump(item, mode=mode, exclude_none=exclude_none) for item in value]
    if isinstance(value, dict):
        return {key: _dump(item, mode=mode, exclude_none=exclude_none) for key, item in value.items()}
    return value
