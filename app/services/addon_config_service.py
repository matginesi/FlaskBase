from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import current_app


def _addons_root() -> Path:
    raw = ""
    try:
        raw = str(current_app.config.get("ADDONS_ROOT", "") or "").strip()
    except Exception:
        raw = ""
    if raw:
        return Path(raw).resolve()
    return Path(__file__).resolve().parents[2] / "addons"


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _normalize_options(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str):
            val = item.strip()
            if not val:
                continue
            out.append({"value": val, "label": val})
            continue
        if isinstance(item, dict):
            val = str(item.get("value", "")).strip()
            if not val:
                continue
            label = str(item.get("label", val)).strip() or val
            out.append({"value": val, "label": label})
    return out


def _normalize_field(field: Any, defaults: dict[str, Any], values: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(field, dict):
        return None
    key = str(field.get("key", "")).strip()
    if not key:
        return None
    field_type = str(field.get("type", "text")).strip().lower() or "text"
    if field_type not in {"text", "number", "checkbox", "select", "textarea", "password"}:
        field_type = "text"
    fallback = field.get("default", defaults.get(key))
    value = values.get(key, fallback)
    out = {
        "key": key,
        "type": field_type,
        "label": str(field.get("label", key.replace("_", " ").title())).strip() or key,
        "help": str(field.get("help", "")).strip(),
        "placeholder": str(field.get("placeholder", "")).strip(),
        "required": bool(field.get("required", False)),
        "min": field.get("min"),
        "max": field.get("max"),
        "step": field.get("step"),
        "rows": int(field.get("rows", 3)) if field_type == "textarea" else None,
        "options": _normalize_options(field.get("options")),
        "value": value,
    }
    return out


def _ensure_runtime_nav_fields(fields: list[dict[str, Any]], addon_id: str, visual: dict[str, Any], defaults: dict[str, Any], values: dict[str, Any]) -> list[dict[str, Any]]:
    keys = {str(item.get("key", "")).strip() for item in fields}
    label_default = str(defaults.get("display_name", visual.get("title", addon_id.replace("_", " ").title()))).strip() or addon_id
    icon_default = str(defaults.get("icon", visual.get("icon", "puzzle"))).strip() or "puzzle"
    if "display_name" not in keys:
        fields.insert(
            0,
            {
                "key": "display_name",
                "type": "text",
                "label": "Display name",
                "help": "Label shown in sidebar and mobile navigation.",
                "placeholder": label_default,
                "required": False,
                "min": None,
                "max": None,
                "step": None,
                "rows": None,
                "options": [],
                "value": values.get("display_name", label_default),
            },
        )
    if "icon" not in keys:
        fields.insert(
            1,
            {
                "key": "icon",
                "type": "text",
                "label": "Icon name",
                "help": "Bootstrap Icons name used for this add-on in the navigation.",
                "placeholder": icon_default,
                "required": False,
                "min": None,
                "max": None,
                "step": None,
                "rows": None,
                "options": [],
                "value": values.get("icon", icon_default),
            },
        )
    return fields


def load_addon_config_panels(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Produces the same data structure expected by the existing Admin UI.

    Add-ons are now discovered in project_root/addons/<code>/ with:
      - config.json   (defaults)
      - visual.json   (UI schema)
    """
    settings = dict(config.get("SETTINGS") or {})
    saved_cfg = dict(settings.get("ADDONS_CONFIG") or {})
    root = _addons_root()
    if not root.exists():
        return []

    out: list[dict[str, Any]] = []
    for addon_dir in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not addon_dir.is_dir() or addon_dir.name.startswith("_"):
            continue
        if not (addon_dir / "addon.py").exists():
            continue
        addon_id = addon_dir.name
        defaults = _read_json_object(addon_dir / "config.json")
        visual = _read_json_object(addon_dir / "visual.json")
        values = dict(saved_cfg.get(addon_id) or {})

        fields: list[dict[str, Any]] = []
        for field in list(visual.get("fields") or []):
            normalized = _normalize_field(field, defaults, values)
            if normalized:
                fields.append(normalized)
        fields = _ensure_runtime_nav_fields(fields, addon_id, visual, defaults, values)

        panel = {
            "addon_id": addon_id,
            "title": str(visual.get("title", addon_id.replace("_", " ").title())).strip() or addon_id,
            "description": str(visual.get("description", "")).strip(),
            "icon": str(visual.get("icon", "sliders")).strip() or "sliders",
            "fields": fields,
        }
        out.append(panel)
    return out
