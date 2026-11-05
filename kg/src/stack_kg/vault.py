"""Markdown folder -> graph. Incremental (content hash in SQLite), idempotent, rule-tagged.

Per note:
  (:Note {id: relative path, title, aliases, type, status, headings, body, ...})
  (:Note)-[:TAGGED {by, confidence, source}]->(:Tag {name, facet})
  (:Note)-[:LINKS_TO]->(:Note | :Ref)      [[wikilinks]]; Ref = target that does not exist (yet)
  (:Fact {kind, text, why, created})-[:IN_NOTE]->(:Note)
      from list lines "- 2026-01-31: Decision: text — why: reason" (see remember.py)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .db import KG
from .taxonomy import Hit, Taxonomy

FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\|#\^]+)(?:[#\^][^\]\|]*)?(?:\|[^\]]*)?\]\]")
INLINE_TAG_RE = re.compile(r"(?<![\w&/#])#([A-Za-z][\w/\-]*)")
CODE_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
HEX_RE = re.compile(r"[0-9a-fA-F]{3,8}")
NON_NOTE_RE = re.compile(r"\.(?!md$)[A-Za-z0-9]{2,5}$")  # [[image.png]], [[file.pdf]]
FACT_KINDS = {"decision": "Decision", "gotcha": "Gotcha", "fact": "Fact", "todo": "Todo", "question": "Question"}
FACT_RE = re.compile(
    r"^\s*[-*]\s+(?:\[(.)\]\s+)?(?:(\d{4}-\d{2}-\d{2}):\s+)?(Decision|Gotcha|Fact|Todo|Question):\s+(.+?)"
    r"(?:\s+(?:—|--)\s+why:\s+(.+?))?\s*$", re.M | re.I)
OWNED_EDGES = ["LINKS_TO", "TAGGED"]


@dataclass
class Note:
    id: str  # vault-relative path
    path: Path
    title: str
    folder: str
    fm: dict
    body: str
    hash: str
    mtime: str
    aliases: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    tags: list[Hit] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)


def _clean(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:10]
    s = str(v).strip()
    return s or None


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [x for x in re.split(r"[,\s]+", v) if x]
    if not isinstance(v, (list, tuple)):
        v = [v]
    return [s for s in (_clean(x) for x in v) if s]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), text[m.end():]


def scan(vault: Path, tax: Taxonomy) -> list[Note]:
    notes = []
    if not vault.is_dir():
        return notes
    for p in sorted(vault.rglob("*.md")):
        rel = p.relative_to(vault)
        if any(part in tax.skip_folders or part.startswith(".") for part in rel.parts[:-1]):
            continue
        raw = p.read_bytes()
        fm, body = parse_frontmatter(raw.decode("utf-8", errors="replace"))
        folder = str(rel.parent) if str(rel.parent) != "." else ""
        notes.append(Note(
            id=rel.as_posix(), path=p, title=_clean(fm.get("title")) or p.stem, folder=folder, fm=fm,
            body=body[: tax.body_limit],
            hash=hashlib.sha1(raw + tax.version.encode()).hexdigest(),
            mtime=dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            aliases=_as_list(fm.get("aliases") or fm.get("alias")),
        ))
    return notes


def fact_id(kind: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha1(f"{kind.lower()}|{norm}".encode()).hexdigest()[:16]


def parse_facts(body: str, default_date: str) -> list[dict]:
    out = {}
    for box, date, kind, text, why in FACT_RE.findall(body):
        kind = kind.lower()
        text = text.strip()
        fid = fact_id(kind, text)
        out[fid] = {"id": fid, "kind": kind, "text": text, "why": (why or "").strip() or None,
                    "created": date or default_date, "done": box.lower() == "x"}
    return list(out.values())


def extract(n: Note, tax: Taxonomy) -> None:
    hits: dict[str, Hit] = {}

    def add(h: Hit):
        cur = hits.get(h.tag)
        if cur is None or h.confidence > cur.confidence:
            hits[h.tag] = h

    prose = CODE_RE.sub(" ", n.body)
    # 1. your own tags: frontmatter + inline #tags
    manual = _as_list(n.fm.get("tags")) + [t for t in INLINE_TAG_RE.findall(prose) if not HEX_RE.fullmatch(t)]
    for t in manual:
        name = tax.resolve(t)
        if name:
            add(Hit(name, tax.facet_of(name), "you", 1.0, "tag"))
    # 2. structured properties -> their facet
    for key in ("type", "status"):
        v = _clean(n.fm.get(key))
        if v:
            name = tax.resolve(v)
            add(Hit(name, tax.facet_of(name, key), "frontmatter", 1.0, key))
    # 3. regex rules on title/body (a note keeps its explicit type)
    has_type = any(h.facet == "type" for h in hits.values())
    for h in tax.rule_hits(n.title, prose):
        if not (h.facet == "type" and has_type):
            add(h)
    n.tags = list(hits.values())

    n.headings = [m.group(2).strip() for m in HEADING_RE.finditer(prose)]
    n.links = sorted({t.strip() for bang, t in WIKILINK_RE.findall(prose)
                      if not bang and t.strip() and not NON_NOTE_RE.search(t.strip())})
    n.facts = parse_facts(n.body, _clean(n.fm.get("created")) or n.mtime[:10])


def _props(n: Note) -> dict:
    fm = n.fm
    body_wo_headings = re.sub(r"^#.*$", "", n.body, flags=re.M)
    excerpt = re.sub(r"\s+", " ", body_wo_headings).strip()[:300]
    updated = _clean(fm.get("updated")) or n.mtime[:10]
    return {
        "id": n.id, "title": n.title, "title_lc": n.title.lower(), "path": str(n.path), "folder": n.folder,
        "aliases": n.aliases, "aliases_lc": [a.lower() for a in n.aliases], "aliases_text": " ".join(n.aliases),
        "type": _clean(fm.get("type")), "status": _clean(fm.get("status")),
        "created": _clean(fm.get("created")), "updated": updated, "mtime": n.mtime,
        "hash": n.hash, "body": n.body, "headings": " · ".join(n.headings), "excerpt": excerpt,
    }


def resolver(notes: list[Note]):
    """Obsidian-style link resolution: by file name, title or alias; or by path suffix."""
    by_name: dict[str, list[Note]] = {}
    for n in notes:
        for key in {n.path.stem.lower(), n.title.lower(), *(a.lower() for a in n.aliases)}:
            by_name.setdefault(key, []).append(n)

    def resolve(target: str) -> Note | None:
        t = target.strip().removesuffix(".md").lower()
        if "/" in t:
            for n in notes:
                if n.id.lower().removesuffix(".md").endswith(t):
                    return n
            t = t.rsplit("/", 1)[1]
        cands = by_name.get(t, [])
        return sorted(cands, key=lambda c: len(c.id))[0] if cands else None

    return resolve


def write_note(kg: KG, n: Note, resolve) -> None:
    kg.write("MERGE (x:Note {id: $id}) SET x += $props", {"id": n.id, "props": _props(n)})
    kg.write("MATCH (x:Note {id: $id})-[r]->() WHERE type(r) IN $owned DELETE r",
             {"id": n.id, "owned": OWNED_EDGES})
    if n.tags:
        kg.write("""MATCH (x:Note {id: $id}) UNWIND $tags AS t
            MERGE (g:Tag {name: t.name}) ON CREATE SET g.facet = t.facet, g.vocab = false, g.aliases = []
            MERGE (x)-[r:TAGGED]->(g) SET r.by = t.by, r.confidence = t.confidence, r.source = t.source""",
                 {"id": n.id, "tags": [h.__dict__ | {"name": h.tag} for h in n.tags]})
    local, refs = [], []
    for t in n.links:
        m = resolve(t)
        if m is None:
            refs.append({"id": t.lower(), "title": t})
        elif m.id != n.id:
            local.append(m.id)
    if local:
        kg.write("""MATCH (x:Note {id: $id}) UNWIND $ids AS tid
            MERGE (y:Note {id: tid}) MERGE (x)-[:LINKS_TO]->(y)""", {"id": n.id, "ids": local})
    if refs:
        kg.write("""MATCH (x:Note {id: $id}) UNWIND $refs AS r
            MERGE (y:Ref {id: r.id}) SET y.title = r.title MERGE (x)-[:LINKS_TO]->(y)""",
                 {"id": n.id, "refs": refs})
    write_facts(kg, n)


def write_facts(kg: KG, n: Note) -> None:
    kg.write("MATCH (:Note {id: $id})<-[e:IN_NOTE]-(:Fact) DELETE e", {"id": n.id})
    if not n.facts:
        return
    tax = kg.tax
    facts = []
    for f in n.facts:
        tags = {f["kind"]: (tax.facet_of(f["kind"], "type"), "rule", 1.0, "kind")}
        for h in tax.rule_hits(f["text"] + " " + (f["why"] or ""), ""):
            tags.setdefault(h.tag, (h.facet, h.by, h.confidence, "text"))
        facts.append({**f, "tags": [{"name": k, "facet": v[0], "by": v[1], "confidence": v[2], "source": v[3]}
                                    for k, v in tags.items()]})
    kg.write("""MATCH (x:Note {id: $id}) UNWIND $facts AS f
        MERGE (k:Fact {id: f.id})
        SET k.kind = f.kind, k.text = f.text, k.why = f.why, k.done = f.done, k.title = f.text,
            k.created = coalesce(k.created, f.created), k.updated = f.created
        MERGE (k)-[:IN_NOTE]->(x)
        WITH k, f
        OPTIONAL MATCH (k)-[old:TAGGED]->() DELETE old
        WITH DISTINCT k, f UNWIND f.tags AS t
        MERGE (g:Tag {name: t.name}) ON CREATE SET g.facet = t.facet, g.vocab = false, g.aliases = []
        MERGE (k)-[r:TAGGED]->(g) SET r.by = t.by, r.confidence = t.confidence, r.source = t.source""",
             {"id": n.id, "facts": facts})


def delete_note(kg: KG, note_id: str) -> None:
    """Remove a note; links pointing at it turn back into Refs (the target may reappear)."""
    kg.write("""MATCH (a:Note)-[:LINKS_TO]->(n:Note {id: $id})
        MERGE (ref:Ref {id: n.title_lc}) SET ref.title = n.title MERGE (a)-[:LINKS_TO]->(ref)""", {"id": note_id})
    kg.write("MATCH (n:Note {id: $id}) DETACH DELETE n", {"id": note_id})


def cleanup(kg: KG) -> None:
    # Refs whose target now exists (by title or alias) become real links
    kg.write("""MATCH (a:Note)-[r:LINKS_TO]->(ref:Ref), (n:Note)
        WHERE (n.title_lc = ref.id OR ref.id IN n.aliases_lc OR toLower(n.id) = ref.id + '.md') AND n <> a
        DELETE r MERGE (a)-[:LINKS_TO]->(n)""")
    kg.write("MATCH (ref:Ref) WHERE NOT ()-->(ref) DELETE ref")
    kg.write("MATCH (f:Fact) WHERE NOT (f)-[:IN_NOTE]->() DETACH DELETE f")
    kg.write("MATCH (g:Tag) WHERE NOT coalesce(g.vocab, false) AND NOT ()-->(g) DELETE g")
