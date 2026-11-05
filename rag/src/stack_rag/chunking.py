"""Heading-aware chunking.

Markdown is split into sections at headings (code fences respected). Each chunk keeps the
heading path ("Guide > Install > macOS") so a search hit can be cited precisely. Sections
are packed into chunks of about `max_tokens` (estimated at 4 chars per token); long
sections are split at paragraph, then sentence, then word boundaries with `overlap`
tokens repeated from the previous chunk. Tiny neighbouring sections under the same parent
are merged so short headings do not produce useless one-line chunks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CHARS_PER_TOKEN = 4
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


@dataclass
class Chunk:
    heading: str
    text: str


def est_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def split_sections(md: str) -> list[tuple[list[str], str]]:
    """-> [(heading_path, body)] ; the heading line itself is kept at the top of the body."""
    sections: list[tuple[list[str], list[str]]] = []
    path: list[str] = []
    levels: list[int] = []
    cur: list[str] = []
    fence = None
    for line in md.splitlines():
        f = _FENCE.match(line)
        if f:
            fence = None if fence == f.group(1) else (fence or f.group(1))
        m = None if fence or f else _HEADING.match(line)
        if m:
            sections.append((list(path), cur))
            level, title = len(m.group(1)), m.group(2).strip()
            while levels and levels[-1] >= level:
                levels.pop()
                path.pop()
            levels.append(level)
            path.append(title)
            cur = [line]
        else:
            cur.append(line)
    sections.append((list(path), cur))
    return [(p, "\n".join(body).strip()) for p, body in sections if "\n".join(body).strip()]


def _pieces(text: str, max_chars: int) -> list[str]:
    """Split text into pieces <= max_chars at paragraph > line > sentence > word boundaries."""
    if len(text) <= max_chars:
        return [text]
    for splitter in (re.compile(r"\n\s*\n"), re.compile(r"\n"), _SENT, re.compile(r"\s+")):
        parts = [p for p in splitter.split(text) if p.strip()]
        if len(parts) > 1:
            out: list[str] = []
            for p in parts:
                out.extend(_pieces(p.strip(), max_chars))
            return out
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


def _tail(text: str, chars: int) -> str:
    if chars <= 0 or len(text) <= chars:
        return "" if chars <= 0 else text
    cut = text[-chars:]
    sp = cut.find(" ")
    return cut[sp + 1 :] if 0 <= sp < len(cut) // 2 else cut


def pack(text: str, max_tokens: int = 450, overlap: int = 60) -> list[str]:
    max_chars = max_tokens * CHARS_PER_TOKEN
    ov_chars = min(overlap * CHARS_PER_TOKEN, max_chars // 3)
    pieces = _pieces(text.strip(), max_chars - ov_chars)
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        sep = "\n\n" if cur else ""
        if cur and len(cur) + len(sep) + len(p) > max_chars:
            chunks.append(cur)
            tail = _tail(cur, ov_chars)
            cur = (tail + " … " + p) if tail else p
        else:
            cur = cur + sep + p
    if cur.strip():
        chunks.append(cur)
    return chunks


def chunk_markdown(md: str, max_tokens: int = 450, overlap: int = 60, min_tokens: int | None = None) -> list[Chunk]:
    min_tokens = max_tokens // 4 if min_tokens is None else min_tokens
    max_chars = max_tokens * CHARS_PER_TOKEN
    # merge tiny sections into a following sibling/child while they fit
    merged: list[tuple[list[str], str]] = []
    for path, body in split_sections(md):
        if merged:
            ppath, pbody = merged[-1]
            child = path[: len(ppath)] == ppath
            sibling = len(path) == len(ppath) and path[:-1] == ppath[:-1]
            if est_tokens(pbody) < min_tokens and (child or sibling) and len(pbody) + len(body) + 2 <= max_chars:
                # a parent absorbing its child keeps its own path; siblings share the parent's
                heading = ppath if child else (ppath[:-1] or ppath)
                merged[-1] = (heading or path, pbody + "\n\n" + body)
                continue
        merged.append((path, body))
    out: list[Chunk] = []
    for path, body in merged:
        heading = " > ".join(path)
        for piece in pack(body, max_tokens, overlap):
            out.append(Chunk(heading=heading, text=piece))
    return out


def chunk_pages(pages: list[str], max_tokens: int = 450, overlap: int = 60) -> list[Chunk]:
    out: list[Chunk] = []
    for i, page in enumerate(pages, 1):
        page = re.sub(r"[ \t]+", " ", page).strip()
        if not page:
            continue
        for piece in pack(page, max_tokens, overlap):
            out.append(Chunk(heading=f"p. {i}", text=piece))
    return out
