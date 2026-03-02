from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from flask import Blueprint


@dataclass
class AddonNavItem:
    id: str
    label: str
    href: str
    icon: str = "puzzle"
    role: str = "user"  # user | admin
    section: str = "tools"
    order: int = 100
    page_key: str | None = None
    feature_key: str | None = None
    active_prefix: str | None = None


@dataclass
class AddonApiMount:
    id: str
    build_router: Callable[[Any], Any]
    prefix: str
    tags: list[str] = field(default_factory=list)
    public: bool = False
    include_in_schema: bool = True
    roles: tuple[str, ...] = ("user", "admin")
    required_scopes: tuple[str, ...] = ()
    summary: str = ""


@dataclass
class AddonManifest:
    addon_id: str
    name: str
    version: str
    description: str = ""
    min_app_version: str = "1.0.0"
    blueprints: list[Blueprint] = field(default_factory=list)
    page_endpoint_map: dict[str, tuple[str, str]] = field(default_factory=dict)
    nav_items: list[AddonNavItem] = field(default_factory=list)
    api_mounts: list[AddonApiMount] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _to_int(v: str) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def parse_version_tuple(raw: str) -> tuple[int, int, int]:
    parts = [p.strip() for p in str(raw or "").split(".")]
    while len(parts) < 3:
        parts.append("0")
    return (_to_int(parts[0]), _to_int(parts[1]), _to_int(parts[2]))


def version_gte(current: str, minimum: str) -> bool:
    return parse_version_tuple(current) >= parse_version_tuple(minimum)
