"""Parse Wavespeed docs pages into static model parameter metadata."""
from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
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
_REQUEST_SECTION_RE = re.compile(
    r"(?:####\s*)?Request Parameters(?:Permalink for this section)?(?P<body>.*?)(?:####\s*(?:Response Parameters|Result Request Parameters|Result Response Parameters)|##\s+Additional Links|$)",
    re.IGNORECASE | re.DOTALL,
)
_HTML_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HTML_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_FIELD_NAME_ALLOWLIST_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")


def _strip_markup(content: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", content or "", flags=re.IGNORECASE)
    text = re.sub(r"</(?:tr|p|li|div|h\d)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text)


def _clean_cell(value: Any) -> str:
    text = unescape(str(value))
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


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


def _is_required(value: Any) -> bool:
    normalized = str(value).strip().lower()
    return normalized in {"yes", "required", "true", "y"}


def _parse_default_cell(value: str) -> Any:
    cleaned_value = _clean_value(value)
    if not cleaned_value or cleaned_value in {"-", "—"}:
        return None
    if cleaned_value.lower() in {"true", "false"}:
        return cleaned_value.lower() == "true"
    return cleaned_value


def _parse_range_or_options(value: str) -> tuple[tuple[str, ...], str | None, str | None]:
    cleaned_value = _clean_cell(value)
    if not cleaned_value or cleaned_value in {"-", "—"}:
        return (), None, None

    range_match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(?:~|-|to)\s*(-?\d+(?:\.\d+)?)\s*", cleaned_value, re.IGNORECASE)
    if range_match:
        return (), range_match.group(1), range_match.group(2)

    if "," in cleaned_value:
        options = tuple(option for option in (_clean_value(part) for part in cleaned_value.split(",")) if option)
        return options, None, None
    return (), None, None


def _is_field_name(value: str) -> bool:
    return bool(_FIELD_NAME_ALLOWLIST_RE.fullmatch(value)) and value.lower() not in {
        "parameter",
        "field",
        "name",
        "code",
        "id",
        "message",
        "data",
        "data.id",
        "data.model",
        "data.outputs",
        "data.urls",
        "data.status",
        "data.created_at",
        "data.error",
    }


def _extract_request_section(page_content: str) -> str:
    match = _REQUEST_SECTION_RE.search(page_content or "")
    return match.group("body") if match else (page_content or "")


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


def _field_from_mapping(name: str, data: dict[str, Any], required_names: set[str] | None = None) -> ModelDocsField:
    enum_options = data.get("enum") or data.get("options") or []
    if isinstance(enum_options, str):
        enum_options = [enum_options]
    required_names = required_names or set()
    return ModelDocsField(
        name=name,
        field_type=_normalize_field_type(str(data.get("type", "enum" if enum_options else "string"))),
        required=name in required_names or bool(data.get("required", False)),
        default=data.get("default"),
        enum_options=tuple(str(option) for option in enum_options),
        min_value=None if data.get("min") is None and data.get("minimum") is None else str(data.get("min", data.get("minimum"))),
        max_value=None if data.get("max") is None and data.get("maximum") is None else str(data.get("max", data.get("maximum"))),
        description=str(data.get("description", "")),
    )


def _iter_json_values(value: Any) -> Any:
    yield value
    if isinstance(value, dict):
        for child_value in value.values():
            yield from _iter_json_values(child_value)
    elif isinstance(value, list):
        for child_value in value:
            yield from _iter_json_values(child_value)


def _schema_from_json_value(value: Any) -> ModelDocsSchema | None:
    if isinstance(value, dict):
        properties = value.get("properties") or value.get("input") or value.get("request")
        if isinstance(properties, dict):
            required_names = set(str(field_name) for field_name in (value.get("required") or []))
            fields = [
                _field_from_mapping(str(name), field_data, required_names)
                for name, field_data in properties.items()
                if isinstance(field_data, dict) and _is_field_name(str(name))
            ]
            if fields:
                return ModelDocsSchema(fields=tuple(fields))

        field_lists = (
            value.get("parameters"),
            value.get("requestParameters"),
            value.get("request_parameters"),
            value.get("inputs"),
            value.get("fields"),
        )
        for raw_fields in field_lists:
            if not isinstance(raw_fields, list):
                continue
            fields = []
            for raw_field in raw_fields:
                if not isinstance(raw_field, dict):
                    continue
                name = raw_field.get("name") or raw_field.get("key") or raw_field.get("field")
                if name is None or not _is_field_name(str(name)):
                    continue
                fields.append(_field_from_mapping(str(name), raw_field))
            if fields:
                return ModelDocsSchema(fields=tuple(fields))
    return None


def _extract_json_schema(page_content: str) -> ModelDocsSchema | None:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", page_content or ""):
        try:
            parsed, _ = decoder.raw_decode(page_content[match.start():])
        except json.JSONDecodeError:
            continue
        for json_value in _iter_json_values(parsed):
            schema = _schema_from_json_value(json_value)
            if schema is not None:
                return schema
    return None


def _field_from_table_cells(cells: list[str]) -> ModelDocsField | None:
    if len(cells) < 4:
        return None
    name = _clean_cell(cells[0])
    if not _is_field_name(name):
        return None

    field_type = _normalize_field_type(cells[1])
    required = _is_required(cells[2])
    default = _parse_default_cell(cells[3]) if len(cells) > 3 else None
    enum_options: tuple[str, ...] = ()
    min_value = None
    max_value = None
    if len(cells) > 4:
        enum_options, min_value, max_value = _parse_range_or_options(cells[4])
    if enum_options:
        field_type = "enum"
    description = _clean_cell(cells[5]) if len(cells) > 5 else ""
    return ModelDocsField(
        name=name,
        field_type=field_type,
        required=required,
        default=default,
        enum_options=enum_options,
        min_value=min_value,
        max_value=max_value,
        description=description,
    )


def _extract_table_schema(page_content: str) -> ModelDocsSchema | None:
    request_section = _extract_request_section(page_content)
    fields: list[ModelDocsField] = []

    for row_match in _HTML_ROW_RE.finditer(request_section):
        cells = [_clean_cell(cell) for cell in _HTML_CELL_RE.findall(row_match.group(1))]
        field = _field_from_table_cells(cells)
        if field is not None:
            fields.append(field)

    if not fields:
        for raw_line in _strip_markup(request_section).splitlines():
            line = raw_line.strip()
            if not line.startswith("|"):
                continue
            cells = [_clean_cell(cell) for cell in line.strip("|").split("|")]
            field = _field_from_table_cells(cells)
            if field is not None:
                fields.append(field)

    if fields:
        deduped_fields = {field.name: field for field in fields}
        return ModelDocsSchema(fields=tuple(deduped_fields.values()))
    return None


def parse_model_docs(page_content: str) -> ModelDocsSchema:
    """Parse a Wavespeed docs page into a best-effort request schema."""
    json_schema = _extract_json_schema(page_content)
    if json_schema is not None:
        return json_schema

    table_schema = _extract_table_schema(page_content)
    if table_schema is not None:
        return table_schema

    fields: dict[str, ModelDocsField] = {}
    for raw_line in _strip_markup(_extract_request_section(page_content)).splitlines():
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
