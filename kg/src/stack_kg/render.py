"""Compact, token-cheap text for agents and the terminal."""

from __future__ import annotations

import json
import re


def _tags(ts) -> str:
    return " ".join(f"#{t}" for t in sorted(ts)) if ts else ""


def _one_line(s: str | None, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def find(rows: list[dict]) -> str:
    if not rows:
        return "No matches. Try other words, fewer words, or kg_tags to see the tag names."
    lines = []
    for r in rows:
        kind = {"Note": "", "Fact": f"[{r.get('type') or 'fact'}] ", "Commit": f"[commit {r.get('type') or ''}] "}.get(
            r["label"], f"[{r['label'].lower()}] ")
        meaning = " ~meaning-only" if r.get("via") == ["vec"] else ""
        meta = " · ".join(str(x) for x in [r.get("type") if r["label"] == "Note" else None,
                                           r.get("status"), r.get("updated")] if x)
        lines.append(f"- {kind}{r['title']}" + (f" ({meta})" if meta else "") +
                     (f" {_tags(r.get('tags'))}" if r.get("tags") else "") + meaning)
        if r.get("excerpt") and r["label"] != "Fact":
            lines.append(f"  {_one_line(r['excerpt'], 160)}")
        elif r["label"] == "Fact" and r.get("excerpt"):
            lines.append(f"  why: {_one_line(r['excerpt'], 160)}")
    lines.append("(~meaning-only = matched by meaning, not by the words; verify before relying on it)"
                 if any(r.get("via") == ["vec"] for r in rows) else "")
    return "\n".join(x for x in lines if x)


def context(c: dict) -> str:
    kind = c.get("kind")
    if kind == "none":
        return f"Nothing found for {c.get('target')!r}. Try kg_find with a few keywords."
    if kind == "tag":
        out = [f"# #{c['tag']} (facet: {c.get('facet') or '?'}, {c['count']} items)"]
        if c.get("aliases"):
            out.append("aliases: " + ", ".join(c["aliases"]))
        if c["co_tags"]:
            out.append("often with: " + " ".join(f"#{t['tag']}({t['c']})" for t in c["co_tags"]))
        out += [f"- {'' if i['label'] == 'Note' else '[' + (i.get('kind') or i['label'].lower()) + '] '}{i['title']}"
                f" ({i.get('updated') or '-'}, {i['by']} {i['confidence']:g})" for i in c["items"]]
        return "\n".join(out)
    if kind == "repo":
        r = c["repo"]
        out = [f"# repo {r['name']} [{r.get('branch')}]", f"path: {r['id']}"]
        if r.get("remote"):
            out.append(f"remote: {r['remote']}")
        if c["commits"]:
            out.append("\n## Recent commits")
            out += [f"- {x['date'][:10]} {x['sha']} {x['subject']} ({x['author']})" for x in c["commits"]]
        if c["notes"]:
            out.append("\n## Notes mentioning it")
            out += [f"- {n['title']}" for n in c["notes"]]
        return "\n".join(out)
    n = c["note"]
    out = []
    if c.get("guessed_from"):
        out.append(f"(no exact match for {c['guessed_from']!r}; closest note:)")
    out.append(f"# {n['title']}")
    meta = " · ".join(str(x) for x in [n.get("type"), n.get("status"), n.get("updated") and f"updated {n['updated']}",
                                       n.get("id")] if x)
    out.append(meta)
    if n.get("aliases"):
        out.append("aliases: " + ", ".join(n["aliases"]))
    if c["tags"]:
        out.append("tags: " + " ".join(f"#{t['tag']}" + ("" if t["confidence"] >= 1 else f"({t['confidence']:g})")
                                       for t in c["tags"]))
    if n.get("excerpt"):
        out.append(f"\n{_one_line(n['excerpt'], 300)}")
    if n.get("headings"):
        out.append(f"sections: {_one_line(n['headings'], 300)}")
    sections = [
        ("Links to", [f"{x['title']}" + (" (missing note)" if x["missing"] else "") for x in c["links"]]),
        ("Linked from", [x["title"] for x in c["backlinks"]]),
        ("Facts", [f"{x['created'] or ''} {x['kind']}: {x['text']}" + (f" (why: {x['why']})" if x.get("why") else "")
                   for x in c["facts"]]),
        ("Repos mentioned", [f"{x['name']} [{x['branch']}]" for x in c["repos"]]),
        ("Related (shared tags)", [f"{x['title']} — {', '.join('#' + s for s in x['shared'])}" for x in c["related"]]),
    ]
    for head, items in sections:
        if items:
            out.append(f"\n## {head}")
            out += [f"- {i}" for i in items]
    return "\n".join(out)


def tags(rows: list[dict]) -> str:
    if not rows:
        return "No tags yet (nothing ingested?)."
    out, facet = [], None
    for r in rows:
        if r["facet"] != facet:
            facet = r["facet"]
            out.append(f"{facet}:")
        alias = f" (= {', '.join(r['aliases'])})" if r.get("aliases") else ""
        out.append(f"  {r['name']} {r['uses']}{alias}")
    return "\n".join(out)


def stats(s: dict) -> str:
    out = [f"graph {s['graph']}"]
    out.append("nodes: " + (", ".join(f"{k} {v}" for k, v in sorted(s["nodes"].items())) or "none"))
    out.append("edges: " + (", ".join(f"{k} {v}" for k, v in sorted(s["edges"].items())) or "none"))
    out.append(f"embedded: {s['embedded']}")
    if "notes_without_topic" in s:
        out.append(f"notes without topic/tech tag: {s['notes_without_topic']}")
    lr = s.get("last_ingest")
    if lr:
        out.append(f"last ingest: {lr['started']} ({lr['seconds']:.1f}s)" +
                   (f" ERROR {lr['error']}" if lr.get("error") else f" {lr['stats']}"))
    return "\n".join(out)


def cypher(rows: list[dict]) -> str:
    if not rows:
        return "(no rows)"
    return "\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows)
