from __future__ import annotations

from flask import jsonify, Blueprint, abort, render_template, g
from flask_login import current_user, login_required

from app.services.access_control import addon_enabled, can_access_addon
from app.services.audit import audit
from app.services.pages_service import is_page_enabled
from app.security import roles_required

bp = Blueprint(
    "hello_world_addon",
    __name__,
    template_folder="templates",
)


@bp.before_request
def _force_addon_english():
    g.ui_lang = "en"


@bp.get("/hello")
@login_required
def hello():
    if not is_page_enabled("hello_world"):
        abort(404)
    if not addon_enabled("hello_world"):
        abort(404)
    if not can_access_addon("hello_world", current_user):
        abort(403)
    audit("page.view", "Viewed Hello World (addon)")
    return render_template(
        "addons/hello_world/hello.html",
        view_mode="user",
        user_url="/hello",
        admin_url="/admin/addons/hello_world",
    )


@bp.get("/admin/addons/hello_world")
@roles_required("admin")
def hello_admin():
    if not addon_enabled("hello_world"):
        abort(404)
    audit("page.view", "Viewed Hello World admin (addon)")
    return render_template(
        "addons/hello_world/hello.html",
        view_mode="admin",
        user_url="/hello",
        admin_url="/admin/addons/hello_world",
    )


@bp.get("/api/health")
@login_required
def api_health():
    # Mirrors the UI access policy for this add-on.
    if not addon_enabled("hello_world") or not can_access_addon("hello_world", current_user):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify({"ok": True, "addon": "hello_world"})


@bp.get("/api/meta")
@login_required
def api_meta():
    if not addon_enabled("hello_world") or not can_access_addon("hello_world", current_user):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    user = getattr(current_user, "email", None) or getattr(current_user, "username", None) or str(getattr(current_user, "id", ""))
    return jsonify({
        "ok": True,
        "addon": "hello_world",
        "user": str(user),
        "role": str(getattr(current_user, "role", "") or ""),
    })
