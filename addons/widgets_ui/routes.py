from __future__ import annotations

from flask import jsonify, Blueprint, abort, render_template, g
from flask_login import login_required, current_user

from app.services.access_control import addon_enabled, can_access_addon
from app.services.audit import audit
from app.services.pages_service import get_runtime_feature_flags, is_page_enabled

bp = Blueprint(
    "widgets_ui_addon",
    __name__,
    template_folder="templates",
    static_folder="static",
)


@bp.before_request
def _force_addon_english():
    g.ui_lang = "en"


@bp.get("/widgets")
@login_required
def widgets_home():
    runtime_features = get_runtime_feature_flags()
    if not is_page_enabled("widgets") or not runtime_features.get("widgets", True):
        abort(404)
    if not addon_enabled("widgets_ui"):
        abort(404)
    if not can_access_addon("widgets_ui", current_user):
        abort(403)
    audit("page.view", "Viewed widgets (addon)")
    return render_template(
        "addons/widgets_ui/widgets.html",
        view_mode="user",
        user_url="/widgets",
        admin_url="/admin/addons/widgets_ui",
    )


@bp.get("/admin/addons/widgets_ui")
@login_required
def widgets_admin():
    if not addon_enabled("widgets_ui"):
        abort(404)
    if not getattr(current_user, "is_admin", lambda: False)():
        abort(403)
    audit("page.view", "Viewed widgets admin (addon)")
    return render_template(
        "addons/widgets_ui/widgets.html",
        view_mode="admin",
        user_url="/widgets",
        admin_url="/admin/addons/widgets_ui",
    )


@bp.get("/api/health")
@login_required
def api_health():
    # Mirrors the UI access policy for this add-on.
    if not addon_enabled("widgets_ui") or not can_access_addon("widgets_ui", current_user):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify({"ok": True, "addon": "widgets_ui"})


@bp.get("/api/meta")
@login_required
def api_meta():
    if not addon_enabled("widgets_ui") or not can_access_addon("widgets_ui", current_user):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    user = getattr(current_user, "email", None) or getattr(current_user, "username", None) or str(getattr(current_user, "id", ""))
    return jsonify({
        "ok": True,
        "addon": "widgets_ui",
        "user": str(user),
        "role": str(getattr(current_user, "role", "") or ""),
    })
