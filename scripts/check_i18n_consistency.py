#!/usr/bin/env python3
"""Audit bot i18n consistency and common mixed-language leaks."""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, TRANSLATIONS, t  # noqa: E402

USER_TEXT_METHODS = {
    "answer",
    "answer_invoice",
    "edit_text",
    "reply",
    "send_message",
    "send_photo",
    "send_video",
    "send_document",
}
CALLBACK_TEXT_METHODS = {"answer"}
USER_TEXT_KEYWORDS = {"text", "caption", "title", "description", "label", "error_message"}
SOURCE_INCLUDE_DIRS = (REPO_ROOT / "app", REPO_ROOT / "wallet_bot")
SOURCE_EXCLUDE_PARTS = {"__pycache__", ".pytest_cache"}

RU_FORBIDDEN_RE = re.compile(r"\b(send|upload|failed|image|video|settings|prompt)\b", re.IGNORECASE)
EN_FORBIDDEN_RE = re.compile(r"(ошибка|изображение|отправьте|генерация|настройка)", re.IGNORECASE)
RU_SCRIPT_RE = re.compile(r"[А-Яа-яЁё]")
EN_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z_-]*\b")
I18N_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")
PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")
HTML_TAG_RE = re.compile(r"<[^>]+>")

ALLOWED_IDENTICAL_VALUES = {
    "Telegram",
    "Telegram Stars",
    "OpenAI",
    "Google",
    "Kling",
    "Grok",
    "MiniMax",
    "ByteDance",
    "Alibaba",
    "Wan AI",
    "NOWPayments",
    "IMai",
    "jpeg",
    "png",
    "webp",
    "1:1",
    "16:9",
    "9:16",
    "4:3",
    "3:4",
    "FPS",
    "Lipsync",
    "Crypto",
    "₿ Crypto",
    "Pro",
    "Turbo",
    "Standard",
    "normal",
    "{message}",
    "🛟 Support: {support_link}",
}
ALLOWED_EN_WORDS_IN_RU = {
    "ai",
    "api",
    "cloudflare",
    "crypto",
    "fps",
    "gpt",
    "imai",
    "jpeg",
    "kling",
    "mb",
    "minimax",
    "nano",
    "nowpayments",
    "openai",
    "png",
    "r2",
    "stars",
    "telegram",
    "wan",
    "webp",
}


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    location: str
    message: str


def _iter_python_files() -> Iterable[Path]:
    for root in SOURCE_INCLUDE_DIRS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in SOURCE_EXCLUDE_PARTS for part in path.parts):
                continue
            yield path


def _is_user_text_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    method_name = func.attr
    if method_name in USER_TEXT_METHODS:
        return True
    if method_name in CALLBACK_TEXT_METHODS and isinstance(func.value, ast.Name) and "callback" in func.value.id:
        return True
    return False


def _string_constant(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts)
    return None


def _is_i18n_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id in {"t", "safe_t", "build_user_error_message", "format_user_error"}:
        return True
    if isinstance(func, ast.Attribute) and func.attr in {"t", "safe_t"}:
        return True
    return False


def _looks_like_user_text(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if I18N_KEY_RE.match(stripped):
        return False
    if stripped.startswith(("http://", "https://", "gen:", "/")):
        return False
    if len(stripped) <= 2:
        return False
    return any(char.isalpha() for char in stripped)


def scan_hardcoded_user_text() -> list[Issue]:
    issues: list[Issue] = []
    for path in _iter_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            issues.append(Issue("error", "syntax", str(path.relative_to(REPO_ROOT)), str(exc)))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_user_text_call(node):
                continue
            candidates: list[ast.AST] = list(node.args[:1])
            candidates.extend(keyword.value for keyword in node.keywords if keyword.arg in USER_TEXT_KEYWORDS)
            for candidate in candidates:
                if _is_i18n_call(candidate):
                    continue
                literal = _string_constant(candidate)
                if literal is None or not _looks_like_user_text(literal):
                    continue
                issues.append(
                    Issue(
                        "error",
                        "hardcoded-user-text",
                        f"{path.relative_to(REPO_ROOT)}:{getattr(candidate, 'lineno', getattr(node, 'lineno', 0))}",
                        literal.strip().replace("\n", "\\n")[:160],
                    )
                )
    return issues


def scan_translation_keys() -> list[Issue]:
    issues: list[Issue] = []
    expected_keys = set(TRANSLATIONS[DEFAULT_LANGUAGE])
    for language in SUPPORTED_LANGUAGES:
        keys = set(TRANSLATIONS[language])
        for key in sorted(expected_keys - keys):
            issues.append(Issue("error", "missing-key", f"locale:{language}", key))
        for key in sorted(keys - expected_keys):
            issues.append(Issue("error", "extra-key", f"locale:{language}", key))
    for language, catalog in TRANSLATIONS.items():
        for key, value in catalog.items():
            if isinstance(value, str) and I18N_KEY_RE.match(value.strip()):
                issues.append(Issue("error", "raw-i18n-key-value", f"locale:{language}:{key}", value))
    return issues


def _contains_unallowed_en_word(value: str) -> bool:
    normalized = HTML_TAG_RE.sub(" ", PLACEHOLDER_RE.sub(" ", value))
    words = {match.group(0).lower() for match in EN_WORD_RE.finditer(normalized)}
    return any(word not in ALLOWED_EN_WORDS_IN_RU for word in words)


def _scan_text(value: str) -> str:
    return HTML_TAG_RE.sub(" ", PLACEHOLDER_RE.sub(" ", value))


def scan_mixed_language() -> list[Issue]:
    issues: list[Issue] = []
    ru_catalog = TRANSLATIONS.get("ru", {})
    for key, value in ru_catalog.items():
        text = _scan_text(str(value))
        match = RU_FORBIDDEN_RE.search(text)
        if match:
            issues.append(Issue("warning", "ru-forbidden-english-word", f"locale:ru:{key}", match.group(0)))
        if _contains_unallowed_en_word(text) and not key.startswith(("provider.label.", "model.")):
            issues.append(Issue("warning", "ru-latin-word", f"locale:ru:{key}", str(value)[:160].replace("\n", "\\n")))
    en_catalog = TRANSLATIONS.get("en", {})
    for key, value in en_catalog.items():
        text = _scan_text(str(value))
        match = EN_FORBIDDEN_RE.search(text)
        if match:
            issues.append(Issue("warning", "en-russian-word", f"locale:en:{key}", match.group(0)))
        if RU_SCRIPT_RE.search(text):
            issues.append(Issue("warning", "en-cyrillic", f"locale:en:{key}", str(value)[:160].replace("\n", "\\n")))
    return issues


def scan_untranslated_identical_values() -> list[Issue]:
    issues: list[Issue] = []
    en_catalog = TRANSLATIONS[DEFAULT_LANGUAGE]
    for language in SUPPORTED_LANGUAGES:
        if language == DEFAULT_LANGUAGE:
            continue
        for key, value in TRANSLATIONS[language].items():
            en_value = en_catalog.get(key)
            if not isinstance(value, str) or not isinstance(en_value, str) or value != en_value:
                continue
            if value in ALLOWED_IDENTICAL_VALUES:
                continue
            comparable_value = _scan_text(value).strip()
            if not any(char.isalpha() for char in comparable_value):
                continue
            if key.startswith(("provider.label.", "settings.option.jpeg", "settings.option.png", "settings.option.webp")):
                continue
            issues.append(Issue("warning", "identical-copy", f"locale:{language}:{key}", value[:160].replace("\n", "\\n")))
    return issues


def scan_runtime_missing_keys() -> list[Issue]:
    issues: list[Issue] = []
    for language in SUPPORTED_LANGUAGES:
        if t("__missing_runtime_probe__", language) == "__missing_runtime_probe__":
            continue
        issues.append(Issue("error", "unexpected-missing-key-fallback", f"locale:{language}", "missing key did not return probe"))
    return issues


def run_scan() -> list[Issue]:
    issues: list[Issue] = []
    issues.extend(scan_translation_keys())
    issues.extend(scan_hardcoded_user_text())
    issues.extend(scan_mixed_language())
    issues.extend(scan_untranslated_identical_values())
    issues.extend(scan_runtime_missing_keys())
    return issues


def print_report(issues: list[Issue]) -> None:
    if not issues:
        print("i18n consistency scan passed: no issues found")
        return
    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    print(f"i18n consistency scan: {len(errors)} error(s), {len(warnings)} warning(s)")
    for issue in issues:
        print(f"[{issue.severity}] {issue.code} {issue.location}: {issue.message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict-warnings", action="store_true", help="Exit non-zero when warnings are present.")
    args = parser.parse_args()
    issues = run_scan()
    print_report(issues)
    has_errors = any(issue.severity == "error" for issue in issues)
    has_warnings = any(issue.severity == "warning" for issue in issues)
    return 1 if has_errors or (args.strict_warnings and has_warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
