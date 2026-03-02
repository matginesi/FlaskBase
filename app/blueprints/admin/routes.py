from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, flash, jsonify, redirect, render_template, request, url_for, send_file, g
from flask_login import current_user

from ...extensions import db, limiter
from ...models import (
    AddonConfig,
    AddonSecret,
    ApiToken,
    ApiTokenReveal,
    BroadcastMessage,
    BroadcastMessageRead,
    EmailVerificationToken,
    LogEvent,
    User,
    UserMessage,
    UserSession,
    now_utc,
)
from ...security import roles_required
from ...services.audit import audit
from ...services.addon_data_service import ensure_addon_registry
from ...services.email_service import runtime_email_settings
from ...services.html_sanitize import sanitize_html
from ...services.message_delivery_service import get_message_email_templates
from ...services.job_service import enqueue_job
from ...services.redis_service import redis_runtime_snapshot, redis_ping, redis_flush_namespace
from ...services.app_logger import instrument_app_views, log_error, log_warning
from ...services.error_log import log_exception
from ...services.addon_config_service import load_addon_config_panels
from ...services.addon_installer import AddonInstallError, install_addon_zip, export_addon_zip, uninstall_addon
from ...services.app_settings_service import (
    build_runtime_payload_from_form,
    build_settings_export_payload,
    get_app_settings_raw,
    get_effective_settings,
    import_settings_payload,
    update_settings,
)
from ...services.addon_loader import load_addons
from ...services.database_admin_service import (
    analyze_database,
    clear_all_logs,
    execute_readonly_query,
    export_database_snapshot_json,
    get_database_overview,
    purge_old_logs,
    vacuum_analyze_database,
)
from ...services.pages_service import read_pages, write_pages
from ...services.runtime_control import issue_runtime_refresh
from ...utils import get_client_ip, get_runtime_config_dict, validate_action_url
from .forms import ConfigJsonForm, AddonInstallForm

bp = Blueprint("admin", __name__, url_prefix="/admin")
log = logging.getLogger(__name__)

BUILTIN_ADDONS = {"documentation", "hello_world", "widgets_ui"}
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def _parse_dt_local(raw: str | None) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def _is_production_env() -> bool:
    env_name = str(os.getenv("APP_ENV", os.getenv("FLASK_ENV", os.getenv("ENV", "")))).strip().lower()
    return env_name in ("prod", "production")


def _wants_json() -> bool:
    return request.accept_mimetypes.best == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _addon_install_response(payload: dict[str, Any], status_code: int = 200):
    if _wants_json():
        return jsonify(payload), status_code
    if payload.get("ok"):
        flash(str(payload.get("message") or "Operazione completata."), "success")
    else:
        flash(str(payload.get("error") or payload.get("message") or "Operazione fallita."), "danger")
    return redirect(url_for("admin.settings"))


def _password_policy_error(password: str) -> str | None:
    raw = str(password or "")
    if len(raw) < 8:
        return "Password troppo corta (min 8 caratteri)."
    if not re.search(r"[A-Z]", raw):
        return "La password deve contenere almeno una lettera maiuscola."
    if not re.search(r"[a-z]", raw):
        return "La password deve contenere almeno una lettera minuscola."
    if not re.search(r"\d", raw):
        return "La password deve contenere almeno un numero."
    if not re.search(r"[^A-Za-z0-9]", raw):
        return "La password deve contenere almeno un carattere speciale."
    return None


def _request_server_restart() -> tuple[bool, str, str]:
    issue_runtime_refresh(
        current_app,
        message="The server is restarting. A page refresh is required to continue.",
        requested_by=getattr(current_user, "email", "") or getattr(current_user, "name", "") or "admin",
        reason="server-restart",
    )
    server_software = " ".join(
        [
            str(request.environ.get("SERVER_SOFTWARE", "") or "").strip(),
            str(os.getenv("SERVER_SOFTWARE", "") or "").strip(),
        ]
    ).lower()

    if "gunicorn" in server_software:
        try:
            os.kill(os.getppid(), signal.SIGHUP)
            return True, "Server restart requested. Connected users will be forced to refresh.", "success"
        except Exception as exc:
            return False, f"Gunicorn reload failed: {exc}", "danger"

    restart_cmd_raw = str(os.getenv("WEBAPP_RESTART_COMMAND", "") or "").strip()
    runtime_kind = str(os.getenv("WEBAPP_RUNTIME_KIND", "")).strip().lower()
    if runtime_kind == "simple_debug" and restart_cmd_raw:
        try:
            cmd = json.loads(restart_cmd_raw)
            if not isinstance(cmd, list) or not cmd or any(not isinstance(part, str) or not part.strip() for part in cmd):
                raise RuntimeError("invalid restart command")
            helper = (
                "import os,sys,time,subprocess,json;"
                "cmd=json.loads(sys.argv[1]);"
                "cwd=sys.argv[2];"
                "time.sleep(1.2);"
                "subprocess.Popen(cmd,cwd=cwd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True)"
            )
            subprocess.Popen(
                [sys.executable, "-c", helper, json.dumps(cmd), str(Path(current_app.root_path).resolve().parent)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            threading.Timer(0.7, lambda: os._exit(0)).start()
            return True, "Server restart requested. The runtime will relaunch and connected users will refresh.", "success"
        except Exception as exc:
            return False, f"Simple server restart failed: {exc}", "danger"

    if bool(current_app.debug) or os.getenv("WERKZEUG_RUN_MAIN") == "true":
        try:
            target = Path(current_app.root_path).resolve().parent / "wsgi.py"
            target.touch()
            return True, "Server restart requested. Connected users will be forced to refresh.", "success"
        except Exception as exc:
            return False, f"Werkzeug reload failed: {exc}", "danger"

    try:
        marker = Path(current_app.instance_path) / "restart.requested"
        marker.write_text(f"{now_utc().isoformat()}Z\n", encoding="utf-8")
    except Exception:
        pass
    return False, "Automatic restart is unavailable for this server mode. Restart requested marker written to instance/restart.requested.", "warning"




@bp.get("/deploy-health")
@roles_required("admin")
def deploy_health():
    """Deployment diagnostics page.

    Helps validate BASE_URL, Host allow-list, reverse proxy headers, cookie flags,
    and rate limit/runtime settings in a human-friendly way.
    """
    audit("admin.deploy_health", "Viewed deployment health")

    runtime_cfg = get_runtime_config_dict("SETTINGS")
    sec_cfg = get_runtime_config_dict("SECURITY")

    report = {
        "env": str(os.getenv("APP_ENV", os.getenv("FLASK_ENV", os.getenv("ENV", "development")))).strip() or "development",
        "is_production": bool(getattr(g, "is_production", _is_production_env())),
        "base_url": str(getattr(g, "runtime_config", {}).get("BASE_URL") or current_app.config.get("BASE_URL") or ""),
        "request": {
            "method": request.method,
            "host": str(getattr(g, "request_host", request.host or "")),
            "url_root": str(request.url_root or ""),
            "path": str(request.path or ""),
            "remote_addr": str(request.remote_addr or ""),
            "client_ip": str(get_client_ip() or ""),
        },
        "proxy_fix": dict(getattr(g, "proxy_fix", {})),
        "forwarded": dict(getattr(g, "forwarded", {})),
        "host_allowlist": {
            "allowed_hosts": list(getattr(g, "allowed_hosts_effective", []) or []),
            "host_allowed": bool(getattr(g, "request_host_allowed", True)),
            "extra_allowed_hosts_env": str(os.getenv("EXTRA_ALLOWED_HOSTS", "") or "").strip(),
        },
        "cookies": {
            "session_cookie_secure": bool(current_app.config.get("SESSION_COOKIE_SECURE")),
            "remember_cookie_secure": bool(current_app.config.get("REMEMBER_COOKIE_SECURE")),
            "session_cookie_samesite": str(current_app.config.get("SESSION_COOKIE_SAMESITE")),
        },
        "rate_limit": {
            "enabled": bool(current_app.config.get("RATELIMIT_ENABLED", True)),
            "storage_uri": str(current_app.config.get("RATELIMIT_STORAGE_URI", "")),
            "login_rate_limit": str(sec_cfg.get("LOGIN_RATE_LIMIT", "") or ""),
        },
        "notes": {
            "recommended": {
                "base_url": "Set SETTINGS.BASE_URL to the public canonical URL (https://...) in production.",
                "proxy_fix": "Enable PROXY_FIX_* only when behind a reverse proxy that sets X-Forwarded-* headers.",
                "allowed_hosts": "Use BASE_URL + EXTRA_ALLOWED_HOSTS to keep host checks safe but practical.",
            }
        },
    }

    warnings_list = list(getattr(g, "deployment_warnings", []) or [])

    as_json = request.args.get("format") == "json" or _wants_json()
    if as_json:
        return jsonify({"ok": True, "report": report, "warnings": warnings_list})

    return render_template(
        "admin/deploy_health.html",
        report=report,
        warnings=warnings_list,
    )
@bp.get("/dashboard")
@roles_required("admin")
def dashboard():
    audit("page.view", "Viewed admin dashboard (unified)")
    return redirect(url_for("main.dashboard"))


@bp.route("/settings", methods=["GET", "POST"])
@roles_required("admin")
def settings():
    current, theme_raw, visual_raw = get_effective_settings()
    addon_config_panels = []
    addon_registry = []
    addon_panels_by_id: dict[str, Any] = {}

    def _rebuild_addon_ui_state(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        panels = load_addon_config_panels(cfg)
        registry_rows: list[dict[str, Any]] = []
        panel_map: dict[str, Any] = {}
        loaded_state = current_app.extensions.get("addons", {}) or {}
        loaded_addons = dict(loaded_state.get("loaded") or {})
        failed_addons = dict(loaded_state.get("failed") or {})
        discovered_addons = set(loaded_state.get("discovered") or [])
        for panel in panels:
            try:
                addon_id = str(panel.get("addon_id", "")).strip()
                if not addon_id:
                    continue
                panel_map[addon_id] = panel
                title = str(panel.get("title", addon_id)).strip() or addon_id
                icon = str(panel.get("icon", "puzzle")).strip() or "puzzle"
                desc = str(panel.get("description", "")).strip()
                hint = desc or title
                meta = dict(loaded_addons.get(addon_id) or {})
                registry_rows.append(
                    {
                        "id": addon_id,
                        "label": title,
                        "icon": icon,
                        "hint": hint,
                        "is_builtin": addon_id in {"documentation", "hello_world", "widgets_ui"},
                        "is_loaded": addon_id in loaded_addons,
                        "is_discovered": addon_id in discovered_addons,
                        "pending_restart": bool(meta.get("pending_restart")),
                        "load_error": str(failed_addons.get(addon_id, "")).strip(),
                    }
                )
            except Exception:
                continue
        return panels, registry_rows, panel_map

    addon_config_panels, addon_registry, addon_panels_by_id = _rebuild_addon_ui_state(current)
    maintenance_enabled = False
    form = ConfigJsonForm(config_json="{}")
    if request.method == "POST":
        try:
            config_payload, theme_payload, visual_payload = build_runtime_payload_from_form(
                form=request.form,
                current_config=current,
                current_theme=theme_raw,
                current_visual=visual_raw,
                addon_config_panels=addon_config_panels,
            )
            current, theme_raw, visual_raw = update_settings(config=config_payload, theme=theme_payload, visual=visual_payload)
            try:
                load_addons(current_app)
                instrument_app_views(current_app)
            except Exception as exc:
                log_warning("admin.addon_reload_failed", "Failed to reload add-ons after settings update", logger=log, context={"error": str(exc)[:240]})
            audit("admin.config_updated", "Config updated")
            flash("Settings saved.", "success")

            addon_config_panels, addon_registry, addon_panels_by_id = _rebuild_addon_ui_state(current)
            form = ConfigJsonForm(config_json="{}")
        except Exception as e:
            log_exception(e, ctx={"where": "admin.settings"})
            flash("Error while saving settings.", "danger")
    addon_form = AddonInstallForm()
    return render_template(
        "admin/settings.html",
        form=form,
        addon_form=addon_form,
        cfg=current,
        theme_raw=theme_raw,
        visual_raw=visual_raw,
        maintenance_enabled=maintenance_enabled,
        addon_config_panels=addon_config_panels,
        addon_registry=addon_registry,
        addon_panels_by_id=addon_panels_by_id,
    )


@bp.get("/settings/export")
@roles_required("admin")
def settings_export():
    row = get_app_settings_raw()
    payload = build_settings_export_payload(row)
    row.last_exported_at = now_utc()
    db.session.add(row)
    db.session.commit()
    audit("admin.settings_export", "Exported runtime settings")
    return Response(
        json.dumps(payload, indent=2, ensure_ascii=False),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=settings_export.json"},
    )


@bp.post("/settings/import")
@roles_required("admin")
def settings_import():
    upload = request.files.get("settings_json")
    if upload is None or not getattr(upload, "filename", ""):
        flash("Seleziona un file JSON da importare.", "warning")
        return redirect(url_for("admin.settings"))
    try:
        payload = json.load(upload.stream)
        cfg, theme, visual = import_settings_payload(payload if isinstance(payload, dict) else {})
        update_settings(config=cfg, theme=theme, visual=visual)
        row = get_app_settings_raw()
        row.last_imported_at = now_utc()
        db.session.add(row)
        db.session.commit()
        load_addons(current_app)
        instrument_app_views(current_app)
        audit("admin.settings_import", "Imported runtime settings")
        flash("Configurazione importata e applicata.", "success")
    except Exception as exc:
        log_exception(exc, ctx={"where": "admin.settings_import"})
        flash(f"Import configurazione fallito: {exc}", "danger")
    return redirect(url_for("admin.settings"))


@bp.post("/server/restart")
@roles_required("admin")
def server_restart():
    ok, message, category = _request_server_restart()
    audit(
        "admin.server_restart_requested",
        "Server restart requested from admin settings",
        level="WARNING" if not ok else "INFO",
        context={"ok": ok, "message": message},
    )
    if request.accept_mimetypes.best == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        status_code = 200 if ok or category == "warning" else 500
        return jsonify({"ok": ok, "message": message, "category": category}), status_code
    flash(message, category)
    return redirect(url_for("admin.settings"))


@bp.post("/addons/install")
@roles_required("admin")
def addons_install():
    """Secure add-on ZIP installation into the configured add-ons root."""

    form = AddonInstallForm()
    if not form.validate_on_submit():
        log_warning(
            "admin.addon_install_invalid_request",
            "Add-on installation rejected: invalid form submission",
            logger=log,
            context={
                "uid": getattr(current_user, "id", None),
                "ip": get_client_ip(),
                "errors": dict(form.errors or {}),
            },
        )
        return _addon_install_response(
            {
                "ok": False,
                "code": "invalid_request",
                "error": "Richiesta non valida o sessione scaduta. Ricarica la pagina e riprova.",
            },
            400,
        )

    if not bool(getattr(current_user, "mfa_enabled", False)):
        log_warning(
            "admin.addon_install_without_mfa",
            "Add-on installation started by admin without MFA enabled",
            logger=log,
            context={"uid": getattr(current_user, "id", None), "ip": get_client_ip()},
        )

    upload = request.files.get("addon_zip")
    upload_name = str(getattr(upload, "filename", "") or "").strip()[:255]
    if not upload_name:
        log_warning(
            "admin.addon_install_missing_file",
            "Add-on installation rejected because no file was uploaded",
            logger=log,
            context={"uid": getattr(current_user, "id", None), "ip": get_client_ip()},
        )
        return _addon_install_response(
            {"ok": False, "code": "missing_file", "error": "Seleziona un file .zip da installare."},
            400,
        )
    try:
        root = str(current_app.config.get("ADDONS_ROOT", "addons")).strip() or "addons"
        res = install_addon_zip(upload, addons_root=root)
        addon_row = ensure_addon_registry(
            res.addon_id,
            title=res.addon_id,
            source_type="zip",
            source_path=res.target_dir,
            is_builtin=False,
            status="installed",
        )
        addon_row.checksum_sha256 = res.checksum_sha256
        addon_row.installed_by_user_id = int(current_user.id)
        db.session.flush()
        cfg, theme, visual = get_effective_settings()
        addons_root_cfg = dict(cfg.get("ADDONS") or {})
        items_cfg = dict(addons_root_cfg.get("ITEMS") or {})
        slot = dict(items_cfg.get(res.addon_id) or {})
        slot["enabled"] = True
        slot["visibility"] = str(slot.get("visibility", "auto") or "auto").strip().lower() or "auto"
        items_cfg[res.addon_id] = slot
        addons_root_cfg["ITEMS"] = items_cfg
        settings_root = dict(cfg.get("SETTINGS") or {})
        addons_cfg = dict(settings_root.get("ADDONS_CONFIG") or {})
        addons_cfg.setdefault(res.addon_id, dict(addons_cfg.get(res.addon_id) or {}))
        settings_root["ADDONS_CONFIG"] = addons_cfg
        cfg["SETTINGS"] = settings_root
        cfg["ADDONS"] = addons_root_cfg
        update_settings(config=cfg, theme=theme, visual=visual)
        pending_restart = True
        try:
            state = load_addons(current_app)
            meta = dict((state.get("loaded") or {}).get(res.addon_id) or {})
            pending_restart = bool(meta.get("pending_restart", True))
        except Exception as exc:
            log_warning(
                "admin.addon_install_reload_failed",
                "Installed add-on but failed to refresh runtime state",
                logger=log,
                context={"addon_id": res.addon_id, "error": str(exc)[:240]},
            )
        if pending_restart:
            message = f"Add-on installed: {res.addon_id}. Runtime settings were updated. A real server restart is now required."
        else:
            message = f"Add-on installed: {res.addon_id}. Runtime settings were updated and the add-on is available now."
        category = "success"
        flash(message, "success")
        audit("addon.install", "Installed add-on", context={"addon_id": res.addon_id})
        return _addon_install_response(
            {
                "ok": True,
                "addon_id": res.addon_id,
                "pending_restart": pending_restart,
                "settings_applied": True,
                "message": message,
                "category": category,
            }
        )
    except AddonInstallError as e:
        log_warning(
            "admin.addon_install_invalid",
            "Add-on installation rejected",
            logger=log,
            context={
                "uid": getattr(current_user, "id", None),
                "ip": get_client_ip(),
                "filename": upload_name,
                "error": str(e)[:240],
            },
        )
        return _addon_install_response({"ok": False, "code": "invalid_zip", "error": str(e)}, 400)
    except Exception as e:
        log_exception(
            e,
            ctx={
                "where": "admin.addons_install",
                "uid": getattr(current_user, "id", None),
                "ip": get_client_ip(),
                "filename": upload_name,
            },
        )
        log_error(
            "admin.addon_install_failed",
            "Add-on installation failed unexpectedly",
            logger=log,
            context={
                "uid": getattr(current_user, "id", None),
                "ip": get_client_ip(),
                "filename": upload_name,
                "error": str(e)[:240],
            },
            exc_info=(type(e), e, e.__traceback__),
        )
        return _addon_install_response(
            {
                "ok": False,
                "code": "install_failed",
                "error": f"Installazione fallita: {e}",
            },
            500,
        )


@bp.get("/addons/<string:addon_id>/export")
@roles_required("admin")
def addons_export(addon_id: str):
    """Export a single add-on directory as a ZIP (for backup/reinstall)."""
    try:
        root = str(current_app.config.get("ADDONS_ROOT", "addons")).strip() or "addons"
        zip_path = export_addon_zip(addon_id, addons_root=root)
        audit("addon.export", "Exported add-on", context={"addon_id": addon_id})
        return send_file(
            zip_path,
            as_attachment=True,
            download_name=f"{addon_id}.zip",
            mimetype="application/zip",
        )
    except Exception as e:
        log_exception(e, ctx={"where": "admin.addons_export", "addon_id": addon_id})
        flash("Impossibile esportare l'add-on.", "danger")
        return redirect(url_for("admin.settings"))


@bp.get("/addons/<string:addon_id>/status")
@roles_required("admin")
def addons_status(addon_id: str):
    state = current_app.extensions.get("addons", {}) or {}
    loaded = dict(state.get("loaded") or {})
    failed = dict(state.get("failed") or {})
    discovered = set(state.get("discovered") or [])
    enabled = set(state.get("enabled") or [])
    meta = dict(loaded.get(addon_id) or {})
    return jsonify(
        {
            "ok": True,
            "addon_id": addon_id,
            "discovered": addon_id in discovered,
            "enabled": addon_id in enabled,
            "loaded": addon_id in loaded,
            "pending_restart": bool(meta.get("pending_restart")),
            "error": str(failed.get(addon_id, "")).strip(),
        }
    )


@bp.post("/addons/<string:addon_id>/uninstall")
@roles_required("admin")
def addons_uninstall(addon_id: str):
    """Uninstall a single add-on (delete its folder) and reload in-memory add-ons."""
    try:
        if addon_id in BUILTIN_ADDONS:
            flash("Built-in add-ons cannot be removed from this screen.", "warning")
            return redirect(url_for("admin.settings"))
        root = str(current_app.config.get("ADDONS_ROOT", "addons")).strip() or "addons"
        uninstall_addon(addon_id, addons_root=root)
        cfg, theme, visual = get_effective_settings()
        addons_root_cfg = dict(cfg.get("ADDONS") or {})
        items_cfg = dict(addons_root_cfg.get("ITEMS") or {})
        items_cfg.pop(addon_id, None)
        addons_root_cfg["ITEMS"] = items_cfg
        settings_root = dict(cfg.get("SETTINGS") or {})
        addons_cfg = dict(settings_root.get("ADDONS_CONFIG") or {})
        addons_cfg.pop(addon_id, None)
        settings_root["ADDONS_CONFIG"] = addons_cfg
        cfg["SETTINGS"] = settings_root
        cfg["ADDONS"] = addons_root_cfg
        update_settings(config=cfg, theme=theme, visual=visual)
        load_addons(current_app)
        instrument_app_views(current_app)
        audit("addon.uninstall", "Uninstalled add-on", context={"addon_id": addon_id})
        flash(f"Add-on disinstallato: {addon_id}", "success")
    except Exception as e:
        log_exception(e, ctx={"where": "admin.addons_uninstall", "addon_id": addon_id})
        flash("Impossibile disinstallare l'add-on.", "danger")
    return redirect(url_for("admin.settings"))


@bp.post("/settings/maintenance")
@roles_required("admin")
def toggle_maintenance_mode():
    """Toggle maintenance mode in the DB-backed runtime pages/features store."""
    current = read_pages()
    services = current.get("services", {}) or current.get("features", {}) or {}
    slot = services.get("maintenance_mode")
    if not isinstance(slot, dict):
        slot = {
            "enabled": False,
            "label": "Modalità Manutenzione",
            "icon": "cone-striped",
            "description": "Blocca l'accesso non-admin mostrando la pagina di manutenzione",
        }

    requested = (request.form.get("enabled") or "").strip().lower()
    if requested in {"1", "true", "yes", "on"}:
        enabled = True
    elif requested in {"0", "false", "no", "off"}:
        enabled = False
    else:
        enabled = not bool(slot.get("enabled", False))

    slot["enabled"] = enabled
    services["maintenance_mode"] = slot
    current["services"] = services
    current["features"] = {k: dict(v or {}) for k, v in services.items()}
    write_pages(current)

    audit(
        "admin.maintenance_toggled",
        f"Maintenance mode set to {'on' if enabled else 'off'}",
        level="WARNING",
        context={"enabled": enabled},
    )
    flash(f"Maintenance mode {'attivata' if enabled else 'disattivata'}.", "success")
    return redirect(url_for("admin.settings"))


@bp.get("/logs")
@roles_required("admin")
def logs():
    level = request.args.get("level", "").upper() or None
    event_type = request.args.get("type", "") or None
    user_email = request.args.get("user", "") or None
    try:
        page = max(1, int(request.args.get("page", "1")))
    except Exception:
        page = 1
    try:
        per_page = int(request.args.get("per_page", "100"))
    except Exception:
        per_page = 100
    per_page = max(25, min(500, per_page))

    q = LogEvent.query.order_by(LogEvent.ts.desc())
    if level:
        q = q.filter(LogEvent.level == level)
    if event_type:
        q = q.filter(LogEvent.event_type.ilike(f"%{event_type}%"))
    if user_email:
        u = User.query.filter(User.email.ilike(f"%{user_email}%")).first()
        if u:
            q = q.filter(LogEvent.user_id == u.id)
        else:
            q = q.filter(LogEvent.user_id == None)  # noqa

    total_events = q.count()
    total_pages = max(1, ((total_events - 1) // per_page) + 1) if total_events > 0 else 1
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page
    events = q.offset(offset).limit(per_page).all()
    page_start = offset + 1 if total_events > 0 else 0
    page_end = min(total_events, offset + len(events))

    # Count per level for pills
    from sqlalchemy import func
    counts_raw = db.session.query(LogEvent.level, func.count()).group_by(LogEvent.level).all()
    level_order = ["INFO", "WARNING", "ERROR", "DEBUG"]
    level_counts = [(lv, cnt) for lv, cnt in counts_raw if lv in level_order]
    sec = dict(current_app.config.get("SECURITY", {}) or {})
    show_log_text_default = str(sec.get("ALLOW_LOG_TEXT_CONTENT", False)).strip().lower() in ("1", "true", "yes", "on")

    audit("admin.logs_view", "Viewed logs", context={"level": level, "type": event_type})
    return render_template(
        "admin/logs.html",
        events=events,
        level_counts=level_counts,
        show_log_text_default=show_log_text_default,
        page=page,
        per_page=per_page,
        total_events=total_events,
        total_pages=total_pages,
        page_start=page_start,
        page_end=page_end,
    )


@bp.get("/users")
@roles_required("admin")
def users():
    from sqlalchemy import or_

    q = (request.args.get("q", "") or "").strip()
    role = (request.args.get("role", "") or "").strip().lower()
    status = (request.args.get("status", "") or "").strip().lower()
    sort = (request.args.get("sort", "created_desc") or "created_desc").strip().lower()

    query = User.query
    if q:
        pattern = f"%{q}%"
        query = query.filter(or_(User.name.ilike(pattern), User.email.ilike(pattern)))
    if role in ("admin", "user"):
        query = query.filter(User.role == role)
    if status == "active":
        query = query.filter(User.is_active.is_(True))
    elif status == "disabled":
        query = query.filter(User.is_active.is_(False))

    sort_map = {
        "created_desc": User.created_at.desc(),
        "created_asc": User.created_at.asc(),
        "name_asc": User.name.asc(),
        "name_desc": User.name.desc(),
        "email_asc": User.email.asc(),
        "email_desc": User.email.desc(),
        "last_login_desc": User.last_login_at.desc().nullslast(),
        "last_login_asc": User.last_login_at.asc().nullsfirst(),
    }
    order_clause = sort_map.get(sort, sort_map["created_desc"])
    users_list = query.order_by(order_clause).all()
    current_admin = db.session.get(User, int(getattr(current_user, "id", 0) or 0))
    if (
        current_admin
        and str(getattr(current_admin, "role", "") or "").strip().lower() == "admin"
    ):
        allow_by_role = role in ("", "admin")
        allow_by_status = (
            not status
            or (status == "active" and bool(current_admin.is_active))
            or (status == "disabled" and not bool(current_admin.is_active))
        )
        allow_by_query = not q or q.lower() in str(current_admin.name or "").lower() or q.lower() in str(current_admin.email or "").lower()
        if allow_by_role and allow_by_status and allow_by_query:
            users_list = [u for u in users_list if int(getattr(u, "id", 0) or 0) != int(current_admin.id)]
            users_list = [current_admin, *users_list]

    audit(
        "admin.users_view",
        "Viewed users list",
        context={"q": q, "role": role or None, "status": status or None, "sort": sort},
    )
    return render_template(
        "admin/users.html",
        users=users_list,
        users_total=User.query.count(),
        users_filters={"q": q, "role": role, "status": status, "sort": sort},
    )


@bp.post("/users/create")
@roles_required("admin")
def create_user():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = "user"

    if not name or not email or not password or len(password) < 8:
        flash("Dati non validi. Password min 8 caratteri.", "danger")
        return redirect(url_for("admin.users"))
    pwd_error = _password_policy_error(password)
    if pwd_error:
        flash(pwd_error, "danger")
        return redirect(url_for("admin.users"))
    if not EMAIL_RE.match(email):
        flash("Email non valida.", "danger")
        return redirect(url_for("admin.users"))

    if User.query.filter_by(email=email).first():
        flash(f"Email già registrata: {email}", "warning")
        return redirect(url_for("admin.users"))

    u = User(
        email=email,
        name=name,
        role=role,
        is_active=True,
        email_verified=True,
        account_status="active",
        signup_source="admin_panel",
    )
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    audit("admin.user_created", f"Created user {email}", context={"email": email, "role": role})
    flash(f"Utente {email} creato con ruolo user.", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:uid>/delete")
@roles_required("admin")
def delete_user(uid: int):
    u = User.query.get_or_404(uid)
    if u.id == current_user.id:
        flash("Non puoi rimuovere il tuo account.", "danger")
        return redirect(url_for("admin.users"))
    if u.role == "admin":
        flash("Non è consentito rimuovere un utente admin.", "danger")
        return redirect(url_for("admin.users"))

    try:
        email = u.email

        LogEvent.query.filter_by(user_id=u.id).delete(synchronize_session=False)
        EmailVerificationToken.query.filter_by(user_id=u.id).delete(synchronize_session=False)

        # Remove any session/token rows (kept for multi-session safety)
        subq = db.session.query(ApiToken.id).filter(ApiToken.user_id == u.id).subquery()
        ApiTokenReveal.query.filter(ApiTokenReveal.token_id.in_(subq)).delete(synchronize_session=False)
        ApiToken.query.filter_by(user_id=u.id).delete(synchronize_session=False)
        UserSession.query.filter_by(user_id=u.id).delete(synchronize_session=False)
        BroadcastMessageRead.query.filter_by(user_id=u.id).delete(synchronize_session=False)
        AddonConfig.query.filter_by(user_id=u.id).delete(synchronize_session=False)
        AddonSecret.query.filter_by(user_id=u.id).delete(synchronize_session=False)

        BroadcastMessage.query.filter_by(created_by_user_id=u.id).delete(synchronize_session=False)

        db.session.delete(u)
        db.session.commit()
        audit("admin.user_deleted", f"Deleted user {email}", level="WARNING", context={"uid": uid, "email": email})
        flash(f"Utente {email} rimosso.", "success")
    except Exception as ex:
        db.session.rollback()
        flash(f"Errore rimozione utente: {ex}", "danger")

    return redirect(url_for("admin.users"))


@bp.post("/users/<int:uid>/toggle")

@roles_required("admin")
def toggle_user(uid: int):
    u = db.session.get(User, int(uid))
    if not u:
        return ("", 404)
    if u.id == current_user.id:
        flash("Non puoi disabilitare te stesso.", "danger")
        return redirect(url_for("admin.users"))
    if str(u.role or "").strip().lower() == "admin":
        flash("L'account admin non può essere disabilitato da questa pagina.", "danger")
        return redirect(url_for("admin.users"))
    u.is_active = not u.is_active
    if u.is_active:
        u.account_status = "active"
        u.deactivated_at = None
    else:
        u.account_status = "disabled"
        u.deactivated_at = now_utc()
    db.session.commit()
    revoke_ts = now_utc()
    UserSession.query.filter(
        UserSession.user_id == int(uid),
        UserSession.revoked_at.is_(None),
    ).update({"revoked_at": revoke_ts, "status": "revoked"}, synchronize_session=False)
    ApiToken.query.filter(
        ApiToken.user_id == int(uid),
        ApiToken.name == "browser-session",
        ApiToken.revoked_at.is_(None),
    ).update({"revoked_at": revoke_ts}, synchronize_session=False)
    db.session.commit()
    audit("admin.user_sessions_revoked", f"Sessions revoked for user {uid}", context={"uid": uid})
    action = "abilitato" if u.is_active else "disabilitato"
    audit("admin.user_toggled", f"User {u.email} {action}", context={"user_id": uid, "active": u.is_active})
    flash(f"Utente {u.email} {action}.", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:uid>/api-tokens/<int:tid>/revoke")
@roles_required("admin")
def revoke_user_api_token(uid: int, tid: int):
    """Revoke an API token owned by a given user.

    This endpoint is referenced by the admin/users.html template.
    It must be stable to avoid breaking the whole Users page.
    """
    token = ApiToken.query.filter_by(id=tid, user_id=uid).first()
    if not token:
        flash("API key non trovata.", "warning")
        return redirect(url_for("admin.users"))

    if getattr(token, "revoked_at", None):
        flash("API key già revocata.", "info")
        return redirect(url_for("admin.users"))

    try:
        token.revoked_at = now_utc()
        db.session.commit()
        audit(
            "admin.api_token_revoked",
            "Revoked user API token",
            level="WARNING",
            context={"uid": uid, "tid": tid, "token_prefix": getattr(token, "token_prefix", None)},
        )
        flash("API key revocata.", "success")
    except Exception as ex:
        db.session.rollback()
        flash(f"Errore revoca API key: {ex}", "danger")

    return redirect(url_for("admin.users"))


@bp.post("/users/<int:uid>/api-tokens/revoke-bulk")
@roles_required("admin")
def revoke_user_api_tokens_bulk(uid: int):
    token_ids = [str(v).strip() for v in request.form.getlist("token_ids") if str(v).strip()]
    if not token_ids:
        flash("Seleziona almeno una API key da revocare.", "warning")
        return redirect(url_for("admin.users"))

    try:
        normalized_ids = sorted({int(v) for v in token_ids})
    except Exception:
        flash("Selezione API key non valida.", "danger")
        return redirect(url_for("admin.users"))

    tokens = (
        ApiToken.query.filter(ApiToken.user_id == int(uid), ApiToken.id.in_(normalized_ids))
        .order_by(ApiToken.created_at.desc())
        .all()
    )
    if not tokens:
        flash("API key non trovate.", "warning")
        return redirect(url_for("admin.users"))

    changed = 0
    try:
        now = now_utc()
        for token in tokens:
            if token.revoked_at is None:
                token.revoked_at = now
                changed += 1
        db.session.commit()
        audit(
            "admin.api_token_bulk_revoked",
            "Revoked multiple user API tokens",
            level="WARNING",
            context={"uid": uid, "count": changed, "token_ids": normalized_ids[:50]},
        )
        if changed:
            flash(f"Revocate {changed} API key.", "success")
        else:
            flash("Le API key selezionate erano già revocate.", "info")
    except Exception as ex:
        db.session.rollback()
        flash(f"Errore revoca API key: {ex}", "danger")
    return redirect(url_for("admin.users"))






@bp.get("/logs/export")
@roles_required("admin")
def logs_export():
    """Export filtered logs as JSON download."""
    from flask import Response
    import json as _json

    level = request.args.get("level", "").upper() or None
    event_type = request.args.get("type", "") or None
    user_email = request.args.get("user", "") or None

    q = LogEvent.query.order_by(LogEvent.ts.desc())
    if level:
        q = q.filter(LogEvent.level == level)
    if event_type:
        q = q.filter(LogEvent.event_type.ilike(f"%{event_type}%"))
    if user_email:
        u = User.query.filter(User.email.ilike(f"%{user_email}%")).first()
        q = q.filter(LogEvent.user_id == (u.id if u else None))

    events = q.limit(2000).all()
    data = [{
        "id": e.id,
        "ts": e.ts.isoformat(),
        "level": e.level,
        "event_type": e.event_type,
        "message": e.message,
        "user_email": e.user.email if e.user else None,
        "ip": e.ip,
        "path": e.path,
        "method": e.method,
        "context": e.context or {},
    } for e in events]

    audit("admin.logs_export", f"Exported {len(data)} log events")
    payload = _json.dumps(data, indent=2, ensure_ascii=False, default=str)
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=logs_export.json"}
    )


@bp.post("/logs/import")
@roles_required("admin")
def logs_import():
    """Import logs from JSON (skips duplicates by id)."""
    from flask import jsonify
    try:
        if not request.is_json:
            return jsonify({"message": "Content-Type deve essere application/json."}), 400
        data = request.get_json(force=True)
        if not isinstance(data, list):
            return jsonify({"message": "Il JSON deve essere una lista di eventi."}), 400

        imported = 0
        skipped = 0
        for item in data:
            if not isinstance(item, dict):
                continue
            # Skip if id already exists
            eid = item.get("id")
            if eid and LogEvent.query.get(eid):
                skipped += 1
                continue
            ev = LogEvent(
                level=item.get("level", "INFO")[:16],
                event_type=item.get("event_type", "imported")[:80],
                message=str(item.get("message", ""))[:500],
                ip=item.get("ip"),
                path=item.get("path"),
                method=item.get("method"),
                context=item.get("context") or {},
            )
            db.session.add(ev)
            imported += 1

        db.session.commit()
        audit("admin.logs_import", f"Imported {imported} log events (skipped {skipped})")
        return jsonify({"message": f"Importati {imported} eventi ({skipped} già presenti ignorati)."})
    except Exception as ex:
        db.session.rollback()
        log.error("Log import failed: %s", ex)
        return jsonify({"message": f"Errore: {ex}"}), 500


@bp.get("/users/<int:uid>/profile")
@roles_required("admin")
def user_profile(uid: int):
    u = User.query.get_or_404(uid)
    return jsonify(
        {
            "id": int(u.id),
            "name": u.name or "",
            "email": u.email or "",
            "role": u.role or "user",
            "is_active": bool(u.is_active),
            "email_verified": bool(u.email_verified),
            "username": u.username or "",
            "locale": u.locale or "",
            "timezone": u.timezone or "",
            "signup_source": u.signup_source or "",
            "mfa_enabled": bool(u.mfa_enabled),
            "failed_login_count": int(u.failed_login_count or 0),
            "last_ip": u.last_ip or "",
            "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else "",
            "notification_email_enabled": bool(u.notification_email_enabled),
            "notification_security_enabled": bool(u.notification_security_enabled),
            "notes": u.notes or "",
        }
    )


@bp.post("/users/<int:uid>/edit")
@roles_required("admin")
def edit_user(uid: int):
    u = User.query.get_or_404(uid)
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "user")
    locale = request.form.get("locale", "").strip()[:16]
    timezone = request.form.get("timezone", "").strip()[:64]
    username = request.form.get("username", "").strip().lower()[:80]
    signup_source = request.form.get("signup_source", "").strip()[:40]
    notes = request.form.get("notes", "").strip()[:4000]
    notification_email_enabled = request.form.get("notification_email_enabled", "") in ("1", "on", "true", "yes")
    notification_security_enabled = request.form.get("notification_security_enabled", "") in ("1", "on", "true", "yes")
    email_verified = request.form.get("email_verified", "") in ("1", "on", "true", "yes")
    is_active = request.form.get("is_active", "") in ("1", "on", "true", "yes")

    if not name or len(name) < 2:
        flash("Nome non valido.", "danger")
        return redirect(url_for("admin.users"))
    if not EMAIL_RE.match(email):
        flash("Email non valida.", "danger")
        return redirect(url_for("admin.users"))
    if role not in ("admin", "user"):
        role = "user"

    existing = User.query.filter(User.email == email, User.id != uid).first()
    if existing:
        flash(f"Email già in uso: {email}", "warning")
        return redirect(url_for("admin.users"))
    if username:
        existing_un = User.query.filter(User.username == username, User.id != uid).first()
        if existing_un:
            flash(f"Username già in uso: {username}", "warning")
            return redirect(url_for("admin.users"))

    u.name = name
    u.email = email
    u.role = role
    u.locale = locale or None
    u.timezone = timezone or None
    u.username = username or None
    u.signup_source = signup_source or u.signup_source
    u.notes = notes or None
    u.notification_email_enabled = bool(notification_email_enabled)
    u.notification_security_enabled = bool(notification_security_enabled)
    u.is_active = bool(is_active)
    u.account_status = "active" if u.is_active else "disabled"
    if not u.is_active and u.deactivated_at is None:
        u.deactivated_at = now_utc()
    if u.is_active:
        u.deactivated_at = None
    if bool(email_verified) and not bool(u.email_verified):
        u.email_verified_at = now_utc()
    if not bool(email_verified):
        u.email_verified_at = None
    u.email_verified = bool(email_verified)
    db.session.commit()
    revoke_ts = now_utc()
    UserSession.query.filter(
        UserSession.user_id == int(uid),
        UserSession.revoked_at.is_(None),
    ).update({"revoked_at": revoke_ts, "status": "revoked"}, synchronize_session=False)
    ApiToken.query.filter(
        ApiToken.user_id == int(uid),
        ApiToken.name == "browser-session",
        ApiToken.revoked_at.is_(None),
    ).update({"revoked_at": revoke_ts}, synchronize_session=False)
    db.session.commit()
    audit("admin.user_sessions_revoked", f"Sessions revoked for user {uid}", context={"uid": uid})
    audit(
        "admin.user_edited",
        f"Edited user {u.email}",
        context={"uid": uid, "name": name, "role": role, "email_verified": u.email_verified, "active": u.is_active},
    )
    flash(f"Utente {u.email} aggiornato.", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:uid>/reset-password")
@roles_required("admin")
def reset_user_password(uid: int):
    u = User.query.get_or_404(uid)
    new_pwd = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    pwd_error = _password_policy_error(new_pwd)
    if pwd_error:
        flash(pwd_error, "danger")
        return redirect(url_for("admin.users"))
    if new_pwd != confirm:
        flash("Le password non coincidono.", "danger")
        return redirect(url_for("admin.users"))

    u.set_password(new_pwd)
    db.session.commit()
    audit("admin.user_pwd_reset", f"Reset password for {u.email}", context={"uid": uid}, level="WARNING")
    flash(f"Password reimpostata per {u.email}.", "success")
    return redirect(url_for("admin.users"))


# ─────────────── DATABASE MANAGEMENT ───────────────
@bp.get("/database")
@roles_required("admin")
def database():
    audit("admin.db_view", "Viewed database page")
    sec = get_runtime_config_dict("SECURITY")
    return render_template(
        "admin/database.html",
        db_info=get_database_overview(),
        redis_info=redis_runtime_snapshot(),
        allow_sql_console=bool(sec.get("ALLOW_ADMIN_SQL_CONSOLE", not _is_production_env())),
    )


@bp.post("/redis/ping")
@roles_required("admin")
def redis_ping_endpoint():
    ok, msg, ping_ms = redis_ping()
    if ok:
        flash(f"{msg} ({ping_ms} ms)", "success")
    else:
        flash(f"Redis ping failed: {msg}", "danger")
    return redirect(url_for("admin.database"))


@bp.post("/redis/flush")
@roles_required("admin")
def redis_flush_endpoint():
    confirm = str(request.form.get("confirm") or "").strip()
    if confirm != "FLUSH":
        flash("Type FLUSH to confirm.", "warning")
        return redirect(url_for("admin.database"))
    snap = redis_runtime_snapshot()
    ns = str(snap.get("namespace") or "flaskbase")
    deleted, msg = redis_flush_namespace(f"{ns}:")
    if deleted > 0:
        audit("admin.redis_flush", f"Redis namespace flushed ({deleted} keys)", context={"namespace": ns, "deleted": deleted}, level="WARNING")
        flash(msg, "success")
    else:
        flash(msg, "warning")
    return redirect(url_for("admin.database"))


@bp.route("/broadcasts", methods=["GET", "POST"])
@roles_required("admin")
def broadcasts():
    """Admin UI for publishing messages (broadcast + per-user)."""
    email_templates = get_message_email_templates()
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in str(request.headers.get("Accept", ""))
    if request.method == "POST":
        kind = str(request.form.get("kind") or "broadcast").strip().lower()
        title = str(request.form.get("title") or "").strip()
        body = str(request.form.get("body") or "").strip()
        level = str(request.form.get("level") or "info").strip().lower()
        body_format = str(request.form.get("body_format") or "text").strip().lower()
        send_email_flag = bool(request.form.get("send_email"))
        email_template_key = str(request.form.get("email_template_key") or "default").strip().lower() or "default"
        email_subject = str(request.form.get("email_subject") or title).strip()[:180]
        email_preheader = str(request.form.get("email_preheader") or title).strip()[:180] or None
        action_label = str(request.form.get("action_label") or "").strip()[:80] or None
        action_url = validate_action_url(request.form.get("action_url"))
        expires_at = _parse_dt_local(request.form.get("expires_at"))

        if level not in ("info", "success", "warning", "danger"):
            level = "info"
        if body_format not in ("text", "html"):
            body_format = "text"

        if not title or not body:
            message = "Titolo e testo sono obbligatori."
            if wants_json:
                return jsonify({"ok": False, "message": message}), 400
            flash(message, "danger")
            return redirect(url_for("admin.broadcasts"))

        created_by = getattr(current_user, "id", None)

        if kind == "user":
            try:
                user_id = int(request.form.get("user_id") or "0")
            except Exception:
                user_id = 0
            user = db.session.get(User, user_id) if user_id else None
            if not user:
                message = "Seleziona un utente valido."
                if wants_json:
                    return jsonify({"ok": False, "message": message}), 400
                flash(message, "danger")
                return redirect(url_for("admin.broadcasts"))

            # Sanitize HTML if provided
            clean_html: str | None = None
            if body_format == "html":
                clean_html = sanitize_html(body)

            row = UserMessage(
                title=title,
                body=clean_html or body,
                body_format=body_format,
                level=level,
                created_by_user_id=created_by,
                user_id=user.id,
                expires_at=expires_at,
                is_active=True,
                email_requested=bool(send_email_flag),
                email_template_key=email_template_key,
                email_subject=email_subject,
                email_preheader=email_preheader,
                action_label=action_label,
                action_url=action_url,
            )
            db.session.add(row)
            db.session.commit()
            audit("message.user.create", "Created user message", context={"user_id": user.id, "message_id": row.id, "level": level, "fmt": body_format})

            if send_email_flag:
                try:
                    enqueue_job(
                        job_type="deliver_user_message_email",
                        queue_key="email",
                        payload={"message_id": int(row.id)},
                        requested_by_user_id=int(created_by or 0) or None,
                    )
                    row.email_error = None
                    db.session.commit()
                    audit("email.send_queued", "Queued user message email delivery", context={"user_id": user.id, "message_id": row.id})
                    flash("User message created and email queued.", "success")
                except Exception as e:
                    row.email_error = str(e)[:250]
                    db.session.commit()
                    audit("email.send_queue_failed", "Failed to queue user message email", context={"user_id": user.id, "message_id": row.id, "error": row.email_error}, level="ERROR")
                    success_message = "User message created, but email queueing failed."
            else:
                success_message = "User message created."

            if wants_json:
                rows = BroadcastMessage.query.order_by(BroadcastMessage.created_at.desc()).limit(50).all()
                user_msgs = UserMessage.query.order_by(UserMessage.created_at.desc()).limit(50).all()
                return jsonify(
                    {
                        "ok": True,
                        "kind": "user",
                        "message": success_message,
                        "broadcast_rows_html": render_template("admin/_broadcast_rows.html", broadcasts=rows),
                        "user_message_rows_html": render_template("admin/_user_message_rows.html", user_messages=user_msgs),
                    }
                )
            flash(success_message, "success" if "failed" not in success_message.lower() else "warning")
            return redirect(url_for("admin.broadcasts"))

        # Default: broadcast
        rt = runtime_email_settings()
        if send_email_flag and not bool(getattr(rt, "allow_broadcast_email", False)):
            flash("Invio email per broadcast non abilitato da configurazione.", "warning")
            send_email_flag = False

        row = BroadcastMessage(
            title=title,
            body=sanitize_html(body) if body_format == "html" else body,
            body_format=body_format,
            level=level,
            created_by_user_id=created_by,
            expires_at=expires_at,
            is_active=True,
            email_requested=bool(send_email_flag),
            email_template_key=email_template_key,
            email_subject=email_subject,
            email_preheader=email_preheader,
            action_label=action_label,
            action_url=action_url,
        )
        db.session.add(row)
        db.session.commit()
        if send_email_flag:
            try:
                enqueue_job(
                    job_type="deliver_broadcast_email_batch",
                    queue_key="email",
                    payload={"broadcast_id": int(row.id)},
                    requested_by_user_id=int(created_by or 0) or None,
                )
                row.email_error = None
                db.session.add(row)
                db.session.commit()
                success_message = "Broadcast created and email delivery queued."
            except Exception as exc:
                row.email_error = str(exc)[:250] or "queue_error"
                db.session.add(row)
                db.session.commit()
                success_message = "Broadcast created, but email queueing failed."
        audit("broadcast.create", "Created broadcast", context={"broadcast_id": row.id, "level": level, "fmt": body_format, "email": bool(send_email_flag)})
        if not send_email_flag:
            success_message = "Broadcast published."
        rows = BroadcastMessage.query.order_by(BroadcastMessage.created_at.desc()).limit(50).all()
        user_msgs = UserMessage.query.order_by(UserMessage.created_at.desc()).limit(50).all()
        if wants_json:
            return jsonify(
                {
                    "ok": True,
                    "kind": "broadcast",
                    "message": success_message,
                    "broadcast_rows_html": render_template("admin/_broadcast_rows.html", broadcasts=rows),
                    "user_message_rows_html": render_template("admin/_user_message_rows.html", user_messages=user_msgs),
                }
            )
        flash(success_message, "success" if "failed" not in success_message.lower() else "warning")
        return redirect(url_for("admin.broadcasts"))

    rows = BroadcastMessage.query.order_by(BroadcastMessage.created_at.desc()).limit(50).all()
    users = User.query.order_by(User.name.asc()).all()
    user_msgs = UserMessage.query.order_by(UserMessage.created_at.desc()).limit(50).all()
    return render_template("admin/broadcasts.html", broadcasts=rows, users=users, user_messages=user_msgs, email_templates=email_templates)


@bp.post("/database/query")
@roles_required("admin")
def db_query():
    """Execute read-only SQL query and return results as JSON."""
    try:
        sec = get_runtime_config_dict("SECURITY")
        allow_sql_console = bool(sec.get("ALLOW_ADMIN_SQL_CONSOLE", not _is_production_env()))
        if not allow_sql_console:
            return jsonify({"error": "SQL console disabilitata in questo ambiente."}), 403
        if not request.is_json:
            return jsonify({"error": "Content-Type deve essere application/json."}), 400
        payload = request.get_json(silent=True) or {}
        sql = (payload.get("sql") or "").strip()
        t0 = time.time()
        result = execute_readonly_query(sql)
        elapsed = round((time.time() - t0) * 1000, 1)
        audit("admin.db_query", f"SQL query executed", context={"sql": sql[:200]})
        return jsonify({"columns": result["columns"], "rows": result["rows"], "elapsed_ms": elapsed})
    except ValueError as ex:
        return jsonify({"error": str(ex)}), 400
    except Exception as ex:
        return jsonify({"error": str(ex)}), 400


@bp.post("/database/purge-logs")
@roles_required("admin")
def db_purge_logs():
    try:
        days = max(1, min(int(request.form.get("days", 90) or 90), 3650))
    except (ValueError, TypeError):
        flash("Valore 'days' non valido.", "danger")
        return redirect(url_for("admin.database"))
    result = purge_old_logs(days)
    audit(
        "admin.db_purge_logs",
        f"Purged {result['deleted']} log events older than {result['days']} days",
        context=result,
        level="WARNING",
    )
    flash(f"Eliminati {result['deleted']} eventi di log più vecchi di {result['days']} giorni.", "success")
    return redirect(url_for("admin.database"))


# Backward-compatible alias: older templates referenced admin.db_clear_logs.
@bp.post("/database/clear-logs")
@roles_required("admin")
def db_clear_logs():
    result = clear_all_logs()
    audit("admin.db_clear_logs", "Cleared all log events", context=result, level="WARNING")
    flash(f"Eliminati {result['deleted']} eventi di log.", "success")
    return redirect(url_for("admin.database"))


@bp.post("/database/analyze")
@roles_required("admin")
def db_analyze():
    try:
        result = analyze_database()
        audit("admin.db_analyze", result["action"], context=result)
        flash(f"{result['action']} eseguito.", "success")
    except Exception as ex:
        log_exception(ex, ctx={"where": "admin.db_analyze"})
        flash(f"Errore ANALYZE: {ex}", "danger")
    return redirect(url_for("admin.database"))


@bp.post("/database/vacuum")
@roles_required("admin")
def db_vacuum():
    try:
        result = vacuum_analyze_database()
        audit("admin.db_vacuum", result["action"], context=result, level="WARNING")
        if request.accept_mimetypes.best == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": True, "result": result})
        flash(f"{result['action']} eseguito.", "success")
    except Exception as ex:
        log_exception(ex, ctx={"where": "admin.db_vacuum"})
        if request.accept_mimetypes.best == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "error": str(ex)}), 500
        flash(f"Errore VACUUM: {ex}", "danger")
    return redirect(url_for("admin.database"))


@bp.get("/database/backup")
@roles_required("admin")
def db_backup():
    """Download a JSON snapshot of the database metadata and runtime state."""
    payload = export_database_snapshot_json()
    audit("admin.db_backup", f"DB backup downloaded")
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=db_snapshot.json"},
    )


# ─────────────── PAGES MANAGEMENT ───────────────

@bp.post("/settings/pages")
@roles_required("admin")
def save_pages():
    """Save pages toggles submitted from the settings form."""
    from flask import jsonify
    try:
        payload = request.get_json(silent=True) or {}
        current = read_pages()

        for group in ("user", "admin"):
            for page_key, page_data in current.get("pages", {}).get(group, {}).items():
                field = f"pages_{group}_{page_key}"
                page_data["enabled"] = bool(payload.get(field, page_data.get("enabled", True)))

        addons = current.get("addons", {}) or {}
        for addon_key in addons:
            field = f"addons_{addon_key}"
            prev_enabled = bool(addons[addon_key].get("enabled", True))
            addons[addon_key]["enabled"] = bool(payload.get(field, prev_enabled))
        current["addons"] = addons

        write_pages(current)
        audit("admin.pages_updated", "Updated runtime page settings", level="WARNING",
              context={"keys": list(payload.keys())})
        return jsonify({"ok": True, "message": "Configurazione pagine salvata."})
    except Exception as ex:
        log.error("save_pages failed: %s", ex)
        return jsonify({"ok": False, "message": str(ex)}), 500


@bp.post("/logs/clear")
@roles_required("admin")
def logs_clear():
    try:
        deleted = LogEvent.query.delete()
        db.session.commit()
        log.warning("admin.logs_cleared | All logs cleared | deleted=%s", deleted, extra={"context": {"deleted": deleted}})
        flash(f"Log azzerati con successo ({deleted} eventi eliminati).", "success")
    except Exception as ex:
        db.session.rollback()
        flash(f"Errore durante la cancellazione log: {ex}", "danger")
    return redirect(url_for("admin.logs"))


@bp.post("/logs/fill")
@roles_required("admin")
def logs_fill():
    levels = ("DEBUG", "INFO", "WARNING", "ERROR")
    ip = get_client_ip()

    try:
        events = int(request.form.get("events", 200) or 200)
        events = max(1, min(events, 5000))
        bulk = []
        for i in range(events):
            lv = levels[i % len(levels)]
            bulk.append(
                LogEvent(
                    level=lv,
                    event_type=f"admin.fill.{lv.lower()}",
                    message=f"Admin fill logger #{i + 1}/{events}",
                    user_id=current_user.id,
                    ip=ip,
                    path=request.path,
                    method=request.method,
                    context={"seq": i + 1, "batch_total": events, "source": "admin.logs_fill"},
                )
            )
        db.session.add_all(bulk)
        db.session.commit()
        log.info("admin.logs_fill | Generated events", extra={"context": {"events": events, "user_id": current_user.id}})
        audit("admin.logs_fill", "Generated fill logger events", level="WARNING", context={"events": events})
        flash(f"Generati {events} eventi di log.", "success")
    except Exception as ex:
        db.session.rollback()
        flash(f"Errore durante fill logger: {ex}", "danger")
    return redirect(url_for("admin.logs"))
