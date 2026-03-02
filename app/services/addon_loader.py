from __future__ import annotations

import importlib
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

from flask import Flask, has_app_context
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from .addon_contract import AddonApiMount, AddonManifest, AddonNavItem, version_gte
from .addon_data_service import ensure_addon_registry, record_addon_install_event
from ..extensions import db
from ..models import AddonRegistry

log = logging.getLogger(__name__)


def _dir_checksum(path: Path) -> str:
    h = hashlib.sha256()
    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file():
            continue
        rel_path = file_path.relative_to(path)
        if "__pycache__" in rel_path.parts or file_path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        h.update(str(rel_path).encode("utf-8"))
        h.update(file_path.read_bytes())
    return h.hexdigest()


def addons_root(app: Flask | None = None) -> Path:
    if app is not None:
        raw = str(app.config.get("ADDONS_ROOT", "") or "").strip()
        if raw:
            return Path(raw).resolve()
    # project_root/addons (outside app/)
    # app/services/... -> app -> project root
    # NOTE: __file__ = <project>/app/services/addon_loader.py
    # parents[0]=services, [1]=app, [2]=<project>
    return Path(__file__).resolve().parents[2] / "addons"


def _discover_addon_names(app: Flask | None = None) -> list[str]:
    base = addons_root(app)
    if not base.exists():
        return []
    out: list[str] = []
    for p in base.iterdir():
        if not p.is_dir():
            continue
        if p.name.startswith("_"):
            continue
        if (p / "addon.py").exists() and (p / "config.json").exists() and (p / "visual.json").exists():
            out.append(p.name)
    return sorted(out)


def _purge_addon_modules(addon_id: str) -> None:
    prefix = f"addons.{str(addon_id).strip()}"
    stale = [name for name in list(sys.modules.keys()) if name == prefix or name.startswith(prefix + ".")]
    for name in stale:
        sys.modules.pop(name, None)


def _enabled_names(app: Flask, discovered: list[str]) -> list[str]:
    cfg = app.config.get("ADDONS", {}) or {}
    items_cfg = cfg.get("ITEMS") if isinstance(cfg.get("ITEMS"), dict) else {}
    enabled_cfg = cfg.get("ENABLED")
    disabled_cfg = cfg.get("DISABLED")
    env_enabled = os.getenv("ADDONS_ENABLED", "").strip()
    has_runtime_item_policies = False

    if env_enabled:
        wanted = [x.strip() for x in env_enabled.split(",") if x.strip()]
    elif isinstance(items_cfg, dict) and items_cfg:
        valid_policy_keys = [name for name in items_cfg.keys() if str(name) in discovered]
        has_runtime_item_policies = bool(valid_policy_keys)
        wanted = []
        for name, policy in items_cfg.items():
            if name not in discovered:
                continue
            if isinstance(policy, dict) and bool(policy.get("enabled", True)):
                wanted.append(str(name))
        # Installed add-ons that exist on disk but are not yet present in the
        # persisted runtime config must still be considered enabled-by-default.
        # This makes ZIP installation robust even if the config row is stale
        # during the first restart, while explicit disable flags still win.
        known_keys = {str(name) for name in items_cfg.keys()}
        for name in discovered:
            if name not in known_keys:
                wanted.append(str(name))
        if not has_runtime_item_policies and discovered:
            wanted = list(discovered)
    elif isinstance(enabled_cfg, list):
        wanted = [str(x).strip() for x in enabled_cfg if str(x).strip()]
    elif isinstance(enabled_cfg, str) and enabled_cfg.strip():
        wanted = [x.strip() for x in enabled_cfg.split(",") if x.strip()]
    else:
        wanted = list(discovered)

    disabled: set[str] = set()
    if isinstance(disabled_cfg, list):
        disabled = {str(x).strip() for x in disabled_cfg if str(x).strip()}
    elif isinstance(disabled_cfg, str) and disabled_cfg.strip():
        disabled = {x.strip() for x in disabled_cfg.split(",") if x.strip()}

    final = [n for n in wanted if n in discovered and n not in disabled]

    # Fallback only when no real runtime policy exists for discovered add-ons.
    # If the admin explicitly disabled every discovered add-on, we must keep it empty.
    if not final and discovered and not env_enabled and not has_runtime_item_policies:
        final = [n for n in discovered if n not in disabled]

    return sorted(dict.fromkeys(final))


def _ensure_addon_extension_slots(app: Flask) -> None:
    app.extensions.setdefault("addon_nav", {"user": [], "admin": []})
    app.extensions.setdefault("addon_registry", {})
    app.extensions.setdefault("addon_api_mounts", {})


def _register_nav_item(app: Flask, addon_id: str, item: AddonNavItem) -> None:
    """Register a nav item honoring per-add-on visibility policy.

    Visibility policy is stored in DB-backed config under:
      ADDONS.ITEMS.<addon_id>.visibility

    Allowed values:
      - auto   (default): use item.role (admin/user)
      - admin  : force admin-only
      - user   : force user slot
      - both   : show to both slots
      - hidden : never show in sidebar
    """

    cfg = app.config.get("ADDONS", {}) or {}
    items_cfg = cfg.get("ITEMS") if isinstance(cfg.get("ITEMS"), dict) else {}
    policy = items_cfg.get(addon_id) if isinstance(items_cfg.get(addon_id), dict) else {}
    settings_cfg = app.config.get("ADDON_SETTINGS", {}) or {}
    addon_settings = settings_cfg.get(addon_id) if isinstance(settings_cfg.get(addon_id), dict) else {}
    visibility = str((policy or {}).get("visibility", "auto") or "auto").strip().lower()
    if visibility not in ("auto", "admin", "user", "both", "hidden"):
        visibility = "auto"
    if visibility == "hidden":
        return

    nav = app.extensions.setdefault("addon_nav", {"user": [], "admin": []})
    custom_label = str((addon_settings or {}).get("display_name", "") or str((policy or {}).get("display_name", "") or "")).strip()
    custom_icon = str((addon_settings or {}).get("icon", "") or str((policy or {}).get("icon", "") or "")).strip()
    nav_item = {
        "addon_id": addon_id,
        "id": item.id,
        "label": custom_label or item.label,
        "href": item.href,
        "icon": custom_icon or item.icon,
        "section": item.section,
        "order": int(item.order),
        "page_key": item.page_key,
        "feature_key": item.feature_key,
        "active_prefix": item.active_prefix or item.href,
    }

    if visibility == "both":
        nav.setdefault("user", []).append(dict(nav_item))
        nav.setdefault("admin", []).append(dict(nav_item))
        return
    if visibility == "admin":
        nav.setdefault("admin", []).append(nav_item)
        return
    if visibility == "user":
        nav.setdefault("user", []).append(nav_item)
        return

    # auto
    slot = "admin" if str(item.role).strip().lower() == "admin" else "user"
    nav.setdefault(slot, []).append(nav_item)


def _finalize_nav_order(app: Flask) -> None:
    nav = app.extensions.setdefault("addon_nav", {"user": [], "admin": []})
    for slot in ("user", "admin"):
        items = list(nav.get(slot, []))
        items.sort(key=lambda x: (int(x.get("order", 100)), str(x.get("label", "")).lower()))
        nav[slot] = items


def _apply_manifest(app: Flask, manifest: AddonManifest) -> dict[str, Any]:
    pending_restart = False

    # Flask does not allow registering blueprints after the first request.
    # Add-ons installed at runtime must therefore be activated on next restart.
    for bp in manifest.blueprints:
        try:
            # Idempotency: if the blueprint is already registered (common when
            # reloading add-ons after config changes), skip re-registration.
            if getattr(bp, "name", None) in getattr(app, "blueprints", {}):
                continue
            app.register_blueprint(bp)
        except AssertionError:
            pending_restart = True
            log.warning(
                "addon.pending_restart | name=%s | reason=register_blueprint_after_first_request",
                manifest.addon_id,
            )

    page_map = app.extensions.setdefault("page_endpoint_map", {})
    page_map.update(manifest.page_endpoint_map)

    for item in manifest.nav_items:
        _register_nav_item(app, manifest.addon_id, item)

    api_mounts = app.extensions.setdefault("addon_api_mounts", {})
    api_mounts[manifest.addon_id] = list(manifest.api_mounts or [])

    _finalize_nav_order(app)

    meta = {
        "id": manifest.addon_id,
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "min_app_version": manifest.min_app_version,
        "routes": [str(getattr(bp, "url_prefix", "") or "") for bp in manifest.blueprints],
        "page_endpoints": list(manifest.page_endpoint_map.keys()),
        "nav_items": [item.id for item in manifest.nav_items],
        "api_mounts": [
            {
                "id": str(mount.id),
                "prefix": str(mount.prefix),
                "tags": list(mount.tags or []),
                "public": bool(mount.public),
                "roles": list(mount.roles or ()),
                "required_scopes": list(mount.required_scopes or ()),
                "summary": str(mount.summary or ""),
            }
            for mount in manifest.api_mounts
            if isinstance(mount, AddonApiMount)
        ],
        "pending_restart": pending_restart,
    }
    try:
        row = ensure_addon_registry(
            manifest.addon_id,
            title=manifest.name,
            version=manifest.version,
            description=manifest.description,
            source_type="builtin",
            source_path=str(addons_root(app) / manifest.addon_id),
            is_builtin=True,
            status="pending_restart" if pending_restart else "loaded",
            manifest_json={
                "addon_id": manifest.addon_id,
                "name": manifest.name,
                "version": manifest.version,
                "description": manifest.description,
                "min_app_version": manifest.min_app_version,
                "nav_items": [item.id for item in manifest.nav_items],
                "metadata": dict(manifest.metadata or {}),
            },
        )
        row.last_loaded_at = row.last_loaded_at or row.created_at
        db.session.flush()
    except Exception:
        if has_app_context():
            db.session.rollback()
    return meta


def _load_manifest_mode(app: Flask, module_name: str) -> AddonManifest:
    mod = importlib.import_module(module_name)
    build_fn = getattr(mod, "build_addon", None)
    if not callable(build_fn):
        raise RuntimeError("build_addon(app) mancante")
    manifest = build_fn(app)
    if not isinstance(manifest, AddonManifest):
        raise RuntimeError("build_addon(app) deve restituire AddonManifest")
    app_ver = str(app.config.get("APP_VERSION", "1.0.0"))
    if not version_gte(app_ver, manifest.min_app_version):
        raise RuntimeError(
            f"Addon incompatibile: richiede app>={manifest.min_app_version}, app corrente={app_ver}"
        )
    return manifest


def load_addons(app: Flask) -> dict[str, Any]:
    # Reset nav/registry each time so enable/disable reflects immediately.
    app.extensions["addon_nav"] = {"user": [], "admin": []}
    app.extensions["addon_registry"] = {}
    app.extensions["addon_api_mounts"] = {}
    app.extensions.setdefault("page_endpoint_map", {})

    _ensure_addon_extension_slots(app)

    # Ensure project root is importable so that `import addons.<name>.addon` works.
    # NOTE: __file__ = <project>/app/services/addon_loader.py
    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    importlib.invalidate_caches()

    discovered = _discover_addon_names(app)
    enabled = _enabled_names(app, discovered)
    loaded: dict[str, Any] = {}
    failed: dict[str, str] = {}
    registry = app.extensions.setdefault("addon_registry", {})
    addon_registry_ready = False
    try:
        addon_registry_ready = inspect(db.engine).has_table("addon_registry")
    except Exception:
        addon_registry_ready = False

    for name in enabled:
        module_name = f"addons.{name}.addon"
        try:
            addon_path = addons_root(app) / name
            actual_checksum = _dir_checksum(addon_path)
            if addon_registry_ready:
                row = AddonRegistry.query.filter_by(addon_key=name).first()
                if row is None:
                    row = ensure_addon_registry(
                        name,
                        title=name,
                        source_type="builtin",
                        source_path=str(addon_path),
                        is_builtin=True,
                    )
                expected_checksum = str(getattr(row, "checksum_sha256", "") or "").strip()
                if expected_checksum and expected_checksum != actual_checksum:
                    log.critical(
                        "addon.checksum_mismatch | name=%s | expected=%s | actual=%s",
                        name,
                        expected_checksum,
                        actual_checksum,
                    )
                    raise RuntimeError("Addon checksum mismatch; loading refused")
            _purge_addon_modules(name)
            importlib.invalidate_caches()
            manifest = _load_manifest_mode(app, module_name)
            meta = _apply_manifest(app, manifest)
            meta["checksum_sha256"] = actual_checksum
            loaded[name] = meta
            registry[name] = meta
            log.info("addon.loaded | name=%s", name)
            if addon_registry_ready:
                try:
                    record_addon_install_event(name, action="load", status="ok", source="runtime", message="Addon loaded into runtime", payload_json=meta)
                    db.session.commit()
                except Exception:
                    if has_app_context():
                        db.session.rollback()
        except SQLAlchemyError as ex:
            failed[name] = str(ex)
            registry[name] = {"id": name, "status": "failed", "error": str(ex)}
            log.exception("addon.failed | name=%s", name)
        except Exception as ex:
            failed[name] = str(ex)
            registry[name] = {"id": name, "status": "failed", "error": str(ex)}
            log.exception("addon.failed | name=%s", name)
            if addon_registry_ready:
                try:
                    ensure_addon_registry(name, title=name, status="failed", source_type="builtin", source_path=str(addons_root(app) / name), is_builtin=True)
                    record_addon_install_event(name, action="load", status="failed", source="runtime", message=str(ex)[:255], payload_json={"error": str(ex)})
                    db.session.commit()
                except Exception:
                    if has_app_context():
                        db.session.rollback()

    state = {
        "discovered": discovered,
        "enabled": enabled,
        "loaded": loaded,
        "failed": failed,
    }
    app.extensions["addons"] = state
    return state


def sync_addon_runtime_state(app: Flask) -> dict[str, Any]:
    """Refresh in-memory add-on state only when config/fs drift is detected."""
    current_state = dict(app.extensions.get("addons", {}) or {})
    discovered = _discover_addon_names(app)
    enabled = _enabled_names(app, discovered)
    current_discovered = list(current_state.get("discovered", []) or [])
    current_enabled = list(current_state.get("enabled", []) or [])

    if current_discovered == discovered and current_enabled == enabled:
        return current_state
    return load_addons(app)
