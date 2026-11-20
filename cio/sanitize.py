"""Sanitização de HTML de e-mail (conteúdo NÃO confiável) e extração de texto pesquisável."""
from __future__ import annotations

import hashlib
import html
import re
from html.parser import HTMLParser

import nh3

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "col", "colgroup", "dd", "div", "dl", "dt",
    "em", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "li", "ol", "p", "pre", "s", "small",
    "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
_GENERIC = {"style", "align", "valign", "width", "height", "colspan", "rowspan", "border",
            "cellpadding", "cellspacing", "bgcolor", "color", "title", "dir", "lang"}
ALLOWED_ATTRS = {t: set(_GENERIC) for t in ALLOWED_TAGS}
ALLOWED_ATTRS["a"] |= {"href"}
# Propriedades CSS permitidas no atributo style (sem url(), sem position)
ALLOWED_CSS = {
    "color", "background-color", "font-family", "font-size", "font-weight", "font-style", "text-align",
    "text-decoration", "vertical-align", "padding", "padding-top", "padding-bottom", "padding-left",
    "padding-right", "margin", "margin-top", "margin-bottom", "margin-left", "margin-right", "border",
    "border-top", "border-bottom", "border-left", "border-right", "border-collapse", "border-radius",
    "width", "max-width", "min-width", "height", "line-height", "white-space", "display",
}


_DATA_IMG = re.compile(r"^data:image/(png|jpe?g|gif|webp);base64,[a-z0-9+/=\s]+$", re.I)
MAX_INLINE_IMAGE = 3 * 1024 * 1024


def _attribute_filter(tag: str, attr: str, value: str):
    """Imagens: só embutidas (data:image/...;base64), nunca externas (rastreio/SSRF).
    Links: só https/mailto; data: e javascript: são descartados."""
    if tag == "img" and attr == "src":
        return value if (_DATA_IMG.match(value or "") and len(value) <= MAX_INLINE_IMAGE) else None
    if attr == "href":
        v = (value or "").strip().lower()
        return value if v.startswith(("https://", "mailto:")) else None
    return value


def sanitize_html(raw: str) -> str:
    """Remove scripts, handlers, iframes, imagens externas, formulários e URLs perigosas."""
    cleaned = nh3.clean(
        raw or "",
        tags=ALLOWED_TAGS | {"img"},
        attributes={**ALLOWED_ATTRS, "img": set(_GENERIC) | {"src", "alt"}},
        url_schemes={"https", "mailto", "data"},
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
        filter_style_properties=ALLOWED_CSS,
        attribute_filter=_attribute_filter,
        set_tag_attribute_values={"a": {"target": "_blank"}},
    )
    # Defesa adicional: nenhuma referência url() pode sobreviver em estilos.
    return re.sub(r"url\s*\(", "blocked(", cleaned, flags=re.I)


def inline_cid_images(html: str, eml: bytes, max_bytes: int = MAX_INLINE_IMAGE) -> str:
    """Troca <img src="cid:..."> pelas imagens anexas ao próprio e-mail (data: URI)."""
    if not eml or "cid:" not in (html or ""):
        return html
    import base64
    from email import policy
    from email.parser import BytesParser
    try:
        msg = BytesParser(policy=policy.default).parsebytes(eml)
    except Exception:
        return html
    images = {}
    for part in msg.walk():
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        ctype = part.get_content_type()
        if cid and ctype in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            data = part.get_payload(decode=True) or b""
            if 0 < len(data) <= max_bytes:
                images[cid.lower()] = f"data:{ctype};base64,{base64.b64encode(data).decode()}"
    if not images:
        return html
    return re.sub(r'cid:([^"\'\s>]+)', lambda m: images.get(m.group(1).lower(), m.group(0)), html)


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head", "title"}:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head", "title"} and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    p = _TextExtractor()
    p.feed(raw or "")
    p.close()
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"(\s*\|\s*)+", " | ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def content_hash(raw: str) -> str:
    """Hash do conteúdo normalizado (espaços colapsados) para idempotência/versão."""
    norm = re.sub(r"\s+", " ", raw or "").strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, max_len: int = 120) -> str:
    """Nome de arquivo sem path traversal nem caracteres especiais."""
    import unicodedata

    base = unicodedata.normalize("NFKD", name or "arquivo").encode("ascii", "ignore").decode()
    base = base.replace("/", "_").replace("\\", "_")
    base = _SAFE_NAME.sub("-", base).strip(".-_") or "arquivo"
    return base[:max_len]
