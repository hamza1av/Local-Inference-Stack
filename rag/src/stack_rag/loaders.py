"""Light loaders: markdown/text, PDF (pypdf), HTML (stdlib parser).

Every loader returns a `Loaded`: a title, a list of sections (heading, body) and metadata.
Markdown/HTML sections are split later by the heading-aware chunker; PDF pages become
sections headed `p. N`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

MD_EXT = {".md", ".markdown", ".mdx"}
TXT_EXT = {".txt", ".text", ".rst", ".log", ".csv"}
HTML_EXT = {".html", ".htm"}
PDF_EXT = {".pdf"}
SUPPORTED = MD_EXT | TXT_EXT | HTML_EXT | PDF_EXT


@dataclass
class Loaded:
    title: str
    kind: str  # markdown | text | pdf
    text: str = ""  # markdown / text body
    pages: list[str] = field(default_factory=list)  # pdf only
    meta: dict = field(default_factory=dict)


def supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED


def load(path: Path) -> Loaded:
    ext = path.suffix.lower()
    if ext in PDF_EXT:
        return load_pdf(path)
    raw = path.read_text(encoding="utf-8", errors="replace")
    if ext in HTML_EXT:
        return load_html(raw, fallback_title=path.stem)
    if ext in MD_EXT:
        return load_markdown(raw, fallback_title=path.stem)
    return Loaded(title=_nice(path.stem), kind="text", text=raw)


def _nice(stem: str) -> str:
    return re.sub(r"[_-]+", " ", stem).strip() or stem


_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.M)


def load_markdown(raw: str, fallback_title: str = "untitled") -> Loaded:
    meta: dict = {}
    m = _FRONTMATTER.match(raw)
    if m:
        raw = raw[m.end() :]
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep and k.strip() and not k.startswith((" ", "\t", "-")):
                meta[k.strip()] = v.strip().strip("\"'")
    title = meta.get("title") or ((h := _H1.search(raw)) and h.group(1).strip()) or _nice(fallback_title)
    return Loaded(title=title, kind="markdown", text=raw, meta=meta)


def load_pdf(path: Path) -> Loaded:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:  # broken page streams should not kill the whole document
            pages.append("")
    title = ""
    try:
        title = (reader.metadata and reader.metadata.title) or ""
    except Exception:
        pass
    return Loaded(title=title.strip() or _nice(path.stem), kind="pdf", pages=pages, meta={"pages": len(pages)})


class _HTMLToMarkdown(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head", "nav", "footer"}
    BLOCK = {"p", "div", "section", "article", "li", "tr", "br", "pre", "blockquote", "table", "ul", "ol", "dd", "dt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False
        self._heading: int | None = None
        self.pre = 0

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag == "pre":
            self.pre += 1
        if tag in self.SKIP:
            self.skip += 1
        elif m := re.fullmatch(r"h([1-6])", tag):
            self._heading = int(m.group(1))
            self.out.append("\n\n" + "#" * self._heading + " ")
        elif tag in self.BLOCK:
            self.out.append("\n- " if tag == "li" else "\n\n" if tag != "br" else "\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif re.fullmatch(r"h[1-6]", tag):
            self._heading = None
            self.out.append("\n\n")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.skip:
            return
        if not self.pre:
            data = re.sub(r"\s+", " ", data)
            if self._heading is not None:
                data = data.strip()
        self.out.append(data)


def load_html(raw: str, fallback_title: str = "untitled") -> Loaded:
    p = _HTMLToMarkdown()
    p.feed(raw)
    p.close()
    text = "".join(p.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\n\n(?=- )", "\n", text).strip()
    md = load_markdown(text, fallback_title)
    if p.title.strip():
        md.title = " ".join(p.title.split())
    return md
