"""Controlled vocabulary: alias resolution and rule-based (regex) tagging."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

FACETS = ("topic", "tech", "type", "status")


def slug(value: str) -> str:
    """Normalise a tag: lowercase, spaces/underscores -> '-', strip '#'."""
    s = str(value).strip().lstrip("#").lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9/.\-]", "", s)
    return s.strip("-")


@dataclass
class TagDef:
    name: str
    facet: str
    aliases: list[str] = field(default_factory=list)
    patterns: list[re.Pattern] = field(default_factory=list)


@dataclass
class Hit:
    tag: str
    facet: str
    by: str  # you | frontmatter | rule
    confidence: float
    source: str  # what triggered it: "tag", "type", "title", "body x3"


class Taxonomy:
    def __init__(self, path: Path):
        text = path.read_text() if path.is_file() else ""
        self.version = hashlib.sha1(text.encode()).hexdigest()[:12]
        raw = yaml.safe_load(text) or {}
        self.vault_cfg = raw.get("vault") or {}
        self.skip_folders = set(self.vault_cfg.get("skip_folders") or [])
        self.body_limit = int(self.vault_cfg.get("body_limit", 50000))
        self.max_body_tags = int(self.vault_cfg.get("max_body_tags", 5))
        self.tags: dict[str, TagDef] = {}
        self.alias: dict[str, str] = {}
        for facet, entries in (raw.get("tags") or {}).items():
            for name, spec in (entries or {}).items():
                spec = spec or {}
                td = TagDef(
                    name=slug(name),
                    facet=facet,
                    aliases=[slug(a) for a in spec.get("aliases", [])],
                    patterns=[re.compile(p, re.IGNORECASE) for p in spec.get("match", [])],
                )
                self.tags[td.name] = td
                self.alias[td.name] = td.name
                for a in td.aliases:
                    self.alias.setdefault(a, td.name)

    def resolve(self, value: str) -> str:
        """Alias -> canonical tag name. Accepts 'facet:name' too."""
        v = str(value)
        s = slug(v.split(":", 1)[1] if ":" in v else v)
        return self.alias.get(s, s)

    def facet_of(self, name: str, default: str = "topic") -> str:
        td = self.tags.get(name)
        return td.facet if td else default

    def rule_hits(self, title: str, body: str) -> list[Hit]:
        hits: dict[str, Hit] = {}
        body_hits: list[tuple[int, Hit]] = []
        for td in self.tags.values():
            if not td.patterns:
                continue
            if any(p.search(title) for p in td.patterns):
                hits[td.name] = Hit(td.name, td.facet, "rule", 0.9, "title")
                continue
            n = sum(len(p.findall(body)) for p in td.patterns)
            if n >= 2:
                body_hits.append((n, Hit(td.name, td.facet, "rule", 0.6, f"body x{n}")))
        # long notes mention everything; keep only the strongest body signals
        for _, h in sorted(body_hits, key=lambda x: -x[0])[: self.max_body_tags]:
            hits.setdefault(h.tag, h)
        return list(hits.values())
