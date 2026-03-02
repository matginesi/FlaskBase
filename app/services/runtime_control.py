from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from flask import Flask

from ..models import now_utc


def _runtime_control_path(app: Flask) -> Path:
    return Path(app.instance_path) / "runtime_control.json"


def read_runtime_control(app: Flask) -> dict[str, Any]:
    path = _runtime_control_path(app)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def write_runtime_control(app: Flask, payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    path = _runtime_control_path(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def issue_runtime_refresh(
    app: Flask,
    *,
    message: str,
    requested_by: str = "",
    reason: str = "runtime-restart",
) -> dict[str, Any]:
    current = read_runtime_control(app)
    current.update(
        {
            "refresh_token": secrets.token_urlsafe(18),
            "refresh_message": str(message or "A refresh was requested.").strip(),
            "refresh_reason": str(reason or "runtime-restart").strip(),
            "refresh_requested_at": now_utc().isoformat(),
            "refresh_requested_by": str(requested_by or "").strip(),
        }
    )
    return write_runtime_control(app, current)
