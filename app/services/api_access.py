from __future__ import annotations

from typing import Dict

from flask import current_app

_VALID_MODES = {"off", "public", "key_required"}


def normalize_mode(value: str, fallback: str = "key_required") -> str:
    mode = str(value or "").strip().lower()
    if mode in _VALID_MODES:
        return mode
    return fallback


def get_api_access_config() -> Dict[str, object]:
    cfg = current_app.config.get("API_ACCESS", {}) or {}
    if not isinstance(cfg, dict):
        return {"DEFAULT": "key_required", "ENDPOINTS": {}}
    endpoints = cfg.get("ENDPOINTS", {}) if isinstance(cfg.get("ENDPOINTS", {}), dict) else {}
    return {
        "DEFAULT": normalize_mode(str(cfg.get("DEFAULT", "key_required")), "key_required"),
        "ENDPOINTS": {str(k): normalize_mode(str(v), "key_required") for k, v in endpoints.items()},
    }


def get_endpoint_access_mode(endpoint_key: str, default_mode: str = "key_required") -> str:
    data = get_api_access_config()
    endpoints = data.get("ENDPOINTS", {})
    if isinstance(endpoints, dict) and endpoint_key in endpoints:
        return normalize_mode(str(endpoints[endpoint_key]), default_mode)
    return normalize_mode(str(data.get("DEFAULT", default_mode)), default_mode)


def build_default_api_access() -> Dict[str, object]:
    return {
        "DEFAULT": "key_required",
        "ENDPOINTS": {
            "ping": "public",
            "public_info": "public",
            "chat": "key_required",
            "rag": "key_required",
        },
    }
