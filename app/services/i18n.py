from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import g, request, session

SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "it": "Italiano",
}

DEFAULT_LANGUAGE = "en"

_TRANSLATIONS_DIR = Path(__file__).resolve().parents[1] / "i18n"


def _load_lang_json(lang: str) -> dict[str, str]:
    fp = _TRANSLATIONS_DIR / f"{lang}.json"
    try:
        if fp.exists():
            return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


TRANSLATIONS: dict[str, dict[str, str]] = {
    "it": _load_lang_json("it"),
    "en": _load_lang_json("en"),
}


def normalize_language(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith("it"):
        return "it"
    if raw.startswith("en"):
        return "en"
    return DEFAULT_LANGUAGE


def resolve_language(*, user_locale: str | None = None) -> str:
    q = request.args.get("lang")
    if q:
        lang = normalize_language(q)
        session["ui_lang"] = lang
        return lang
    session_lang = session.get("ui_lang")
    if session_lang:
        return normalize_language(session_lang)
    if user_locale:
        return normalize_language(user_locale)
    best = request.accept_languages.best_match(list(SUPPORTED_LANGUAGES.keys()))
    return normalize_language(best or DEFAULT_LANGUAGE)


def translate(key: str, default: str | None = None, *, lang: str | None = None, **kwargs: Any) -> str:
    active = normalize_language(lang or getattr(g, "ui_lang", None) or DEFAULT_LANGUAGE)
    if active == "en":
        text = default or key
    else:
        text = TRANSLATIONS.get(active, {}).get(key, default or key)
    try:
        return str(text).format(**kwargs) if kwargs else str(text)
    except Exception:
        return str(text)


def language_label(code: str) -> str:
    return SUPPORTED_LANGUAGES.get(normalize_language(code), SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE])
