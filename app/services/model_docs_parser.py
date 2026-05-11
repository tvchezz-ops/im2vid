"""Parse Wavespeed docs pages into static model parameter metadata."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any


@dataclass(frozen=True)
class ModelDocsField:
    name: str
    field_type: str = "string"
    required: bool = False
    default: Any = None
    enum_options: tuple[str, ...] = ()
    min_value: str | None = None
    max_value: str | None = None
    description: str = ""


@dataclass(frozen=True)
class ModelDocsSchema:
    fields: tuple[ModelDocsField, ...] = field(default_factory=tuple)


_FIELD_NAME_RE = re.compile(r'"?([a-zA-Z_][a-zA-Z0-9_]*)"?\s*[:|]\s*([^\n,}]+)')
_ENUM_RE = re.compile(r"enum\s*[:=]\s*\[([^\]]+)\]", re.IGNORECASE)
_DEFAULT_RE = re.compile(r"default\s*[:=]\s*([^,|\n]+)", re.IGNORECASE)
_MIN_RE = re.compile(r"(?:min|min_value|minimum)\s*[:=]\s*([^,|\n]+)", re.IGNORECASE)
_MAX_RE = re.compile(r"(?:max|max_value|maximum)\s*[:=]\s*([^,|\n]+)", re.IGNORECASE)


def _strip_markup(content: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", content or "", flags=re.IGNORECASE)
    text = re.sub(r"</(?:tr|p|li|div|h\d)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text)


def _clean_value(value: Any) -> str:
    return str(value).strip().strip('"\'` ')


def _normalize_field_type(value: str) -> str:
    normalized = value.strip().lower()
    if "integer" in normalized or "int" in normalized:
        return "integer"
    if "number" in normalized or "float" in normalized:
        return "number"
    if "boolean" in normalized or "bool" in normalized:
        return "boolean"
    if "array" in normalized or "list" in normalized:
        return "array"
    if "enum" in normalized:
        return "enum"
    return "string"


def _parse_enum_options(line: str) -> tuple[str, ...]:
    match = _ENUM_RE.search(line)
    if not match:
        return ()
    raw_options = match.group(1)
    return tuple(
        option
        for option in (_clean_value(part) for part in raw_options.split(","))
        if option
    )


def _parse_bound(line: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(line)
    if not match:
        return None
    return _clean_value(match.group(1)) or None


def _parse_default(line: str) -> Any:
    match = _DEFAULT_RE.search(line)
    if not match:
        return None
    raw_value = _clean_value(match.group(1))
    if raw_value.lower() in {"true", "false"}:
        return raw_value.lower() == "true"
    return raw_value


def _field_from_mapping(name: str, data: dict[str, Any]) -> ModelDocsField:
    enum_options = data.get("enum") or data.get("options") or []
    if isinstance(enum_options, str):
        enum_options = [enum_options]
    return ModelDocsField(
        name=name,
        field_type=_normalize_field_type(str(data.get("type", "enum" if enum_options else "string"))),
        required=bool(data.get("required", False)),
        default=data.get("default"),
        enum_options=tuple(str(option) for option in enum_options),
        min_value=None if data.get("min") is None and data.get("minimum") is None else str(data.get("min", data.get("minimum"))),
        max_value=None if data.get("max") is None and data.get("maximum") is None else str(data.get("max", data.get("maximum"))),
        description=str(data.get("description", "")),
    )


def _extract_json_schema(page_content: str) -> ModelDocsSchema | None:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", page_content or ""):
        try:
            parsed, _ = decoder.raw_decode(page_content[match.start():])
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        properties = parsed.get("properties") or parsed.get("input") or parsed.get("request")
        if not isinstance(properties, dict):
            continue
        required = set(parsed.get("required") or [])
        fields = []
        for name, field_data in properties.items():
            if not isinstance(field_data, dict):
                continue
            normalized_data = dict(field_data)
            normalized_data["required"] = name in required or bool(normalized_data.get("required"))
            fields.append(_field_from_mapping(str(name), normalized_data))
        if fields:
            return ModelDocsSchema(fields=tuple(fields))
    return None


def parse_model_docs(page_content: str) -> ModelDocsSchema:
    """Parse a Wavespeed docs page into a best-effort request schema."""
    json_schema = _extract_json_schema(page_content)
    if json_schema is not None:
        return json_schema

    fields: dict[str, ModelDocsField] = {}
    for raw_line in _strip_markup(page_content).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _FIELD_NAME_RE.search(line)
        if not match:
            continue
        name = match.group(1)
        if name in {"id", "status", "outputs", "created_at", "error"}:
            continue
        field_type = _normalize_field_type(match.group(2))
        enum_options = _parse_enum_options(line)
        if enum_options:
            field_type = "enum"
        required = "required" in line.lower() and "not required" not in line.lower()
        fields[name] = ModelDocsField(
            name=name,
            field_type=field_type,
            required=required,
            default=_parse_default(line),
            enum_options=enum_options,
            min_value=_parse_bound(line, _MIN_RE),
            max_value=_parse_bound(line, _MAX_RE),
            description=line,
        )
    return ModelDocsSchema(fields=tuple(fields.values()))


def extract_required_fields(schema: ModelDocsSchema) -> tuple[str, ...]:
    return tuple(field.name for field in schema.fields if field.required)


def extract_optional_fields(schema: ModelDocsSchema) -> tuple[str, ...]:
    return tuple(field.name for field in schema.fields if not field.required)


def extract_enum_options(schema: ModelDocsSchema) -> dict[str, tuple[str, ...]]:
    return {field.name: field.enum_options for field in schema.fields if field.enum_options}


def extract_default_values(schema: ModelDocsSchema) -> dict[str, Any]:
    return {field.name: field.default for field in schema.fields if field.default is not None}


def extract_field_types(schema: ModelDocsSchema) -> dict[str, str]:
    return {field.name: field.field_type for field in schema.fields}
