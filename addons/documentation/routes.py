from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import List, Optional
import logging
import re

from flask import Blueprint, abort, jsonify, render_template, request, g
from flask_login import current_user, login_required

from app.services.access_control import addon_enabled, can_access_addon
from app.services.app_logger import log_warning
from app.services.audit import audit
from app.services.pages_service import is_page_enabled


bp = Blueprint(
    "documentation_addon",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static/addons/documentation",
)
log = logging.getLogger(__name__)


@bp.before_request
def _force_addon_english():
    g.ui_lang = "en"


def _project_root() -> Path:
    # addons/<name>/routes.py -> addons/<name> -> addons -> project root
    return Path(__file__).resolve().parents[2]


def _docs_dir() -> Path:
    return _project_root() / "docs"


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return p.read_text(encoding="latin-1", errors="replace")
    except Exception as exc:
        log_warning("documentation.read_failed", "Failed to read documentation file", logger=log, context={"path": str(p), "error": str(exc)[:240]})
        return f"Impossibile leggere: {p.name}"


def _is_safe_rel(rel: str) -> bool:
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        return False
    parts = [p for p in rel.split("/") if p]
    if any(p in {"..", "."} for p in parts):
        return False
    return True


def _list_md_files() -> List[str]:
    base = _docs_dir()
    out: List[str] = []
    if not base.exists():
        return out
    for p in sorted(base.rglob("*.md")):
        try:
            rel = p.relative_to(base).as_posix()
        except Exception as exc:
            log_warning("documentation.relative_path_failed", "Failed to resolve documentation relative path", logger=log, context={"path": str(p), "error": str(exc)[:240]})
            continue
        if rel.startswith("."):
            continue
        out.append(rel)
    return out


def _resolve_doc(rel: str) -> Optional[Path]:
    if not _is_safe_rel(rel):
        return None
    base = _docs_dir()
    target = (base / rel).resolve()
    try:
        if not base.exists():
            return None
        if not str(target).startswith(str(base.resolve())):
            return None
        if not target.exists() or not target.is_file():
            return None
        if target.suffix.lower() != ".md":
            return None
        return target
    except Exception as exc:
        log_warning("documentation.resolve_failed", "Failed to resolve documentation path", logger=log, context={"rel": rel, "error": str(exc)[:240]})
        return None


def _gate() -> None:
    if not is_page_enabled("docs_viewer"):
        abort(404)
    if not addon_enabled("documentation"):
        abort(404)
    if not can_access_addon("documentation", current_user):
        abort(403)


@dataclass(frozen=True)
class TocItem:
    level: int
    slug: str
    title: str


@dataclass(frozen=True)
class DocNavItem:
    title: str
    rel_path: str | None
    url: str
    kind: str
    active: bool


def _strip_scripts(html: str) -> str:
    # Simple safety net if bleach isn't installed.
    import re

    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"on\w+\s*=\s*\"[^\"]*\"", "", html, flags=re.IGNORECASE)
    html = re.sub(r"on\w+\s*=\s*\'[^\']*\'", "", html, flags=re.IGNORECASE)
    return html


def _sanitize_html(html: str) -> str:
    try:
        import bleach  # type: ignore

        allowed_tags = [
            "p",
            "br",
            "hr",
            "pre",
            "code",
            "strong",
            "em",
            "b",
            "i",
            "u",
            "s",
            "blockquote",
            "ul",
            "ol",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "a",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
            "span",
            "div",
            "img",
            "input",
        ]
        allowed_attrs = {
            "*": ["class", "id", "title", "aria-label"],
            "a": ["href", "title", "rel", "target"],
            "code": ["class"],
            "pre": ["class"],
            "th": ["colspan", "rowspan"],
            "td": ["colspan", "rowspan"],
            "div": ["class"],
            "img": ["src", "alt", "title", "loading"],
            "input": ["type", "checked", "disabled"],
        }
        allowed_protocols = ["http", "https", "mailto", "data"]

        cleaned = bleach.clean(
            str(html or ""),
            tags=allowed_tags,
            attributes=allowed_attrs,
            protocols=allowed_protocols,
            strip=True,
        )
        cleaned = bleach.linkify(
            cleaned,
            callbacks=[bleach.callbacks.nofollow, bleach.callbacks.target_blank],
            skip_tags=["pre", "code"],
        )
        return cleaned
    except Exception:
        return _strip_scripts(str(html or ""))


def _extract_toc_from_html(html: str) -> List[TocItem]:
    toc: List[TocItem] = []
    # headings must have id="..."
    heading_re = re.compile(r"<h([1-6])[^>]*\sid=\"([^\"]+)\"[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
    for m in heading_re.finditer(html or ""):
        level = int(m.group(1))
        slug = m.group(2)
        title_raw = re.sub(r"<[^>]+>", "", m.group(3)).strip()
        if title_raw:
            toc.append(TocItem(level=level, slug=slug, title=title_raw))
    return toc


def _decode_code_payloads(html: str) -> str:
    html = re.sub(r"(<code\b[^>]*>)(.*?)(</code>)", lambda m: f"{m.group(1)}{unescape(m.group(2) or '')}{m.group(3)}", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'(<div\b[^>]*class="[^"]*\bmermaid\b[^"]*"[^>]*>)(.*?)(</div>)', lambda m: f"{m.group(1)}{unescape(m.group(2) or '')}{m.group(3)}", html, flags=re.IGNORECASE | re.DOTALL)
    return html


def _language_label(raw: str) -> str:
    value = str(raw or "").strip().lower()
    aliases = {
        "py": "Python",
        "python": "Python",
        "js": "JavaScript",
        "javascript": "JavaScript",
        "ts": "TypeScript",
        "typescript": "TypeScript",
        "html": "HTML",
        "xml": "HTML",
        "css": "CSS",
        "scss": "SCSS",
        "sql": "SQL",
        "sh": "Shell",
        "bash": "Bash",
        "shell": "Shell",
        "zsh": "Shell",
        "console": "Console",
        "shellsession": "Console",
        "md": "Markdown",
        "markdown": "Markdown",
        "mermaid": "Mermaid",
        "yml": "YAML",
        "yaml": "YAML",
        "json": "JSON",
        "toml": "TOML",
        "ini": "INI",
        "dotenv": "dotenv",
        "env": "dotenv",
    }
    if value in aliases:
        return aliases[value]
    if not value:
        return "Plain text"
    return value.replace("-", " ").replace("_", " ").title()


def _decorate_code_blocks(html: str) -> tuple[str, bool]:
    has_code_blocks = False

    def _code_repl(match: re.Match) -> str:
        nonlocal has_code_blocks
        code_class = match.group("class_attr") or ""
        code_inner = match.group("code") or ""
        lang_match = re.search(r"(?:^|\s)language-([a-zA-Z0-9_+-]+)(?:\s|$)", code_class)
        language = (lang_match.group(1) if lang_match else "").strip().lower()
        if language == "mermaid":
            return match.group(0)
        has_code_blocks = True
        label = _language_label(language)
        data_lang = language or "text"
        pre_class = f' class="docs-code-pre language-{data_lang}"'
        code_attr = f' class="{code_class.strip()}"' if code_class.strip() else ""
        return (
            f'<div class="docs-code-block" data-lang="{data_lang}">'
            f'<div class="docs-code-head"><span class="docs-code-lang">{label}</span></div>'
            f"<pre{pre_class}><code{code_attr}>{code_inner}</code></pre>"
            f"</div>"
        )

    wrapped = re.sub(
        r"<pre(?:\s+class=\"[^\"]*\")?><code(?:\s+class=\"(?P<class_attr>[^\"]*)\")?>(?P<code>.*?)</code></pre>",
        _code_repl,
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return wrapped, has_code_blocks


def _render_markdown(md_text: str) -> tuple[str, List[TocItem], bool]:
    """Render Markdown to sanitized HTML + TOC + Mermaid flag.

    Uses the same stack as the core app (markdown-it-py + mdit-py-plugins),
    so it works with the project's requirements without extra deps.
    """
    text = str(md_text or "")
    if not text.strip():
        return "", [], False

    # 1) Markdown -> HTML
    html = ""
    try:
        from markdown_it import MarkdownIt  # type: ignore
        from mdit_py_plugins.tasklists import tasklists_plugin  # type: ignore
        from mdit_py_plugins.anchors import anchors_plugin  # type: ignore

        md = (
            MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
            .enable(["table", "strikethrough"])
            .use(tasklists_plugin, enabled=True)
            .use(anchors_plugin, permalink=False)  # adds ids to headings
        )
        html = md.render(text)
    except Exception:
        import html as _html

        html = f"<pre><code>{_html.escape(text)}</code></pre>"

    # 2) Mermaid blocks: <pre><code class=\"language-mermaid\">...</code></pre> -> <div class=\"mermaid\">...</div>
    has_mermaid = False

    def _mermaid_repl(m: re.Match) -> str:
        nonlocal has_mermaid
        has_mermaid = True
        code_inner = m.group(1) or ""
        code_inner = unescape(code_inner)
        return (
            '<div class="docs-mermaid-block">'
            '<div class="docs-code-head"><span class="docs-code-lang">Mermaid diagram</span></div>'
            f'<div class="mermaid">{code_inner}</div>'
            "</div>"
        )

    html = re.sub(
        r"<pre><code[^>]*class=\"[^\"]*(?:language-)?mermaid[^\"]*\"[^>]*>(.*?)</code></pre>",
        _mermaid_repl,
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # 3) Code blocks: wrap fenced code in a richer shell for language labels and layout.
    html, _has_code_blocks = _decorate_code_blocks(html)

    # 4) Sanitize + TOC
    html = _sanitize_html(html)
    html = _decode_code_payloads(html)
    toc = _extract_toc_from_html(html)
    return html, toc, has_mermaid
@dataclass(frozen=True)
class DocRef:
    kind: str  # "readme" | "doc"
    title: str
    rel_path: Optional[str]
    abs_path: Path


def _nav_query() -> str:
    return (request.args.get("q") or "").strip()


def _human_title(rel_path: str | None) -> str:
    if not rel_path:
        return "README"
    raw = Path(rel_path).stem.replace("_", " ").replace("-", " ").strip()
    return raw or Path(rel_path).name


def _build_doc_url(kind: str, rel_path: str | None, *, view_mode: str, query: str = "") -> str:
    if view_mode == "admin":
        base = "/admin/addons/documentation"
        if rel_path:
            base = f"{base}?file={rel_path}"
    elif kind == "readme":
        base = "/addons/documentation/readme"
    else:
        base = f"/addons/documentation/docs/{rel_path}"
    if query:
        joiner = "&" if "?" in base else "?"
        return f"{base}{joiner}q={query}"
    return base


def _nav_items(*, active_kind: str, active_file: str | None, view_mode: str) -> list[DocNavItem]:
    query = _nav_query().lower()
    items: list[DocNavItem] = [
        DocNavItem(
            title="README",
            rel_path=None,
            url=_build_doc_url("readme", None, view_mode=view_mode, query=_nav_query()),
            kind="readme",
            active=active_kind == "readme",
        )
    ]
    for rel_path in _list_md_files():
        title = _human_title(rel_path)
        if query and query not in title.lower() and query not in rel_path.lower():
            continue
        items.append(
            DocNavItem(
                title=title,
                rel_path=rel_path,
                url=_build_doc_url("doc", rel_path, view_mode=view_mode, query=_nav_query()),
                kind="doc",
                active=active_kind == "doc" and active_file == rel_path,
            )
        )
    return items


def _estimate_read_minutes(md_text: str) -> int:
    words = len(str(md_text or "").split())
    return max(1, round(words / 220)) if words else 1


def _adjacent_docs(items: list[DocNavItem], *, active_kind: str, active_file: str | None) -> tuple[DocNavItem | None, DocNavItem | None]:
    current_key = ("readme", None) if active_kind == "readme" else ("doc", active_file)
    for idx, item in enumerate(items):
        item_key = (item.kind, item.rel_path)
        if item_key != current_key:
            continue
        prev_item = items[idx - 1] if idx > 0 else None
        next_item = items[idx + 1] if idx + 1 < len(items) else None
        return prev_item, next_item
    return None, None


def _render_doc_view(*, ref: DocRef, md: str, view_mode: str):
    html, toc, has_mermaid = _render_markdown(md)
    nav_items = _nav_items(active_kind=ref.kind, active_file=ref.rel_path, view_mode=view_mode)
    prev_item, next_item = _adjacent_docs(nav_items, active_kind=ref.kind, active_file=ref.rel_path)
    source_path = "README.md" if ref.kind == "readme" else f"docs/{ref.rel_path}"
    return render_template(
        "addons/documentation/view.html",
        doc_title=ref.title,
        doc_kind=ref.kind,
        active_file=ref.rel_path,
        md_files=_list_md_files(),
        nav_items=nav_items,
        nav_query=_nav_query(),
        doc_html=html,
        toc=toc,
        has_mermaid=has_mermaid,
        read_minutes=_estimate_read_minutes(md),
        heading_count=len(toc),
        source_path=source_path,
        total_docs=max(0, len(nav_items) - 1),
        prev_item=prev_item,
        next_item=next_item,
        view_mode=view_mode,
        user_url=_build_doc_url(ref.kind, ref.rel_path, view_mode="user"),
        admin_url=_build_doc_url(ref.kind, ref.rel_path, view_mode="admin"),
    )


def _get_doc_ref() -> DocRef:
    file_rel = (request.args.get("file") or "").strip().replace("\\", "/").lstrip("/")
    if file_rel:
        p = _resolve_doc(file_rel)
        if p is None:
            return DocRef(kind="doc", title="Documento", rel_path=file_rel, abs_path=_docs_dir() / "__missing__.md")
        title = p.stem.replace("_", " ").replace("-", " ").strip() or p.name
        return DocRef(kind="doc", title=title, rel_path=file_rel, abs_path=p)

    readme = _project_root() / "README.md"
    return DocRef(kind="readme", title="README", rel_path=None, abs_path=readme)


@bp.get("/addons/documentation", strict_slashes=False)
@bp.get("/addons/documentation/", strict_slashes=False)
@login_required
def docs_home():
    _gate()
    ref = _get_doc_ref()

    if ref.abs_path.exists():
        md = _read_text(ref.abs_path)
    else:
        md = "# README\n\nFile mancante: README.md"

    audit("page.view", "Viewed Documentation (addon)", context={"kind": ref.kind, "file": ref.rel_path or "README.md"})

    return _render_doc_view(ref=ref, md=md, view_mode="user")


@bp.get("/addons/documentation/docs/<path:rel_path>")
@login_required
def docs_file(rel_path: str):
    _gate()
    rel_path = (rel_path or "").strip().replace("\\", "/").lstrip("/")
    return docs_home() if not rel_path else docs_home_with_file(rel_path)


def docs_home_with_file(rel_path: str):
    p = _resolve_doc(rel_path)
    if p is None:
        md = f"# Documento\n\nPercorso non valido o file mancante: `{rel_path}`"
        ref = DocRef(kind="doc", title="Documento", rel_path=rel_path, abs_path=_docs_dir() / "__missing__.md")
    else:
        md = _read_text(p)
        ref = DocRef(kind="doc", title=_human_title(rel_path), rel_path=rel_path, abs_path=p)

    audit("page.view", "Viewed Documentation file (addon)", context={"file": rel_path})
    return _render_doc_view(ref=ref, md=md, view_mode="user")


@bp.get("/admin/addons/documentation", strict_slashes=False)
@bp.get("/admin/addons/documentation/", strict_slashes=False)
@login_required
def docs_home_admin():
    if not addon_enabled("documentation"):
        abort(404)
    if not getattr(current_user, "is_admin", lambda: False)():
        abort(403)
    ref = _get_doc_ref()
    md = _read_text(ref.abs_path) if ref.abs_path.exists() else "# README\n\nFile mancante: README.md"
    audit("page.view", "Viewed Documentation admin (addon)", context={"kind": ref.kind, "file": ref.rel_path or "README.md"})
    return _render_doc_view(ref=ref, md=md, view_mode="admin")


@bp.get("/addons/documentation/<path:slug>")
@login_required
def docs_compat(slug: str):
    _gate()
    slug = (slug or "").strip().lstrip("/")
    if not slug or slug.lower() in {"index", "docs"}:
        return docs_home()

    if slug.lower().endswith(".md") or slug.lower().startswith("docs/"):
        rel = slug[5:] if slug.lower().startswith("docs/") else slug
        return docs_home_with_file(rel)

    aliases = {"readme": None, "home": None, "start": None}
    if slug.lower() in aliases:
        return docs_home()

    return docs_home_with_file(f"{slug}.md")


@bp.get("/addons/documentation/api/health")
@login_required
def api_health():
    if not addon_enabled("documentation") or not can_access_addon("documentation", current_user):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify({"ok": True, "addon": "documentation"})
