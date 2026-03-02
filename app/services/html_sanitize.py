from __future__ import annotations

from dataclasses import dataclass
import re

from markupsafe import Markup

try:
    import bleach  # type: ignore
except Exception:  # pragma: no cover
    bleach = None  # type: ignore

try:
    from markdown_it import MarkdownIt  # type: ignore
    from mdit_py_plugins.anchors import anchors_plugin  # type: ignore
    from mdit_py_plugins.table import table_plugin  # type: ignore
except Exception:  # pragma: no cover
    MarkdownIt = None  # type: ignore
    anchors_plugin = None  # type: ignore
    table_plugin = None  # type: ignore


_ALLOWED_TAGS = [
    "p","br","hr","pre","code",
    "strong","em","b","i","u","s",
    "blockquote",
    "ul","ol","li",
    "h1","h2","h3","h4","h5","h6",
    "a",
    "table","thead","tbody","tr","th","td",
    "span","div",
]

_ALLOWED_ATTRS = {
    "*": ["class", "id", "title", "aria-label"],
    "a": ["href", "title", "rel", "target"],
    "code": ["class"],
    "pre": ["class"],
}

_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


@dataclass(frozen=True)
class TocItem:
    level: int
    slug: str
    title: str


def sanitize_html(html: str) -> str:
    raw = str(html or "")
    if bleach is None:
        import html as _html
        return _html.escape(raw)

    cleaned = bleach.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
    cleaned = bleach.linkify(
        cleaned,
        callbacks=[bleach.callbacks.nofollow, bleach.callbacks.target_blank],
        skip_tags=["pre", "code"],
    )
    return cleaned


def _build_md() -> MarkdownIt:
    if MarkdownIt is None:
        raise RuntimeError("markdown renderer not installed")
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "breaks": False})
    if table_plugin is not None:
        md.use(table_plugin)
    if anchors_plugin is not None:
        md.use(anchors_plugin, permalink=False)
    return md


_HEADING_RE = re.compile(r"<h([1-6])\s+[^>]*id=\"([^\"]+)\"[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)


def markdown_to_safe_html(md_text: str) -> tuple[str, list[TocItem]]:
    text = str(md_text or "")
    if not text.strip():
        return "", []

    if MarkdownIt is None:
        import html as _html
        esc = _html.escape(text)
        return f"<pre><code>{esc}</code></pre>", []

    md = _build_md()
    html = md.render(text)
    html = sanitize_html(html)

    toc: list[TocItem] = []
    for m in _HEADING_RE.finditer(html):
        level = int(m.group(1))
        slug = m.group(2)
        title_raw = re.sub(r"<[^>]+>", "", m.group(3)).strip()
        if title_raw:
            toc.append(TocItem(level=level, slug=slug, title=title_raw))
    return html, toc


def safe_markup(html: str) -> Markup:
    return Markup(html)
