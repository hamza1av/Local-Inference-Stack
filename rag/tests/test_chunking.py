"""Chunking, loaders and helpers (no network)."""

import math

from conftest import EXAMPLES

from stack_rag.chunking import chunk_markdown, chunk_pages, est_tokens, split_sections
from stack_rag.embed import format_doc, format_query, truncate
from stack_rag.loaders import load, load_html, load_markdown
from stack_rag.retrieval import fts_query


def test_heading_paths_and_code_fences():
    md = "# Guide\n\nintro\n\n## Install\n\n```sh\n# not a heading\n```\n\n### macOS\n\nbrew it\n\n## Use\n\nrun it\n"
    secs = split_sections(md)
    assert [p for p, _ in secs] == [["Guide"], ["Guide", "Install"], ["Guide", "Install", "macOS"], ["Guide", "Use"]]
    assert "# not a heading" in secs[1][1]


def test_tiny_sections_are_merged_without_losing_text():
    md = "# A\n\nshort\n\n## B\n\nalso short\n\n## C\n\n" + ("long text. " * 400)
    chunks = chunk_markdown(md, max_tokens=200, overlap=20)
    joined = "\n".join(c.text for c in chunks)
    assert "short" in joined and "also short" in joined and "## C" in joined
    assert chunks[0].heading == "A"  # A absorbed its child B
    assert all(c.heading.startswith("A") for c in chunks)


def test_long_section_split_with_size_bound_and_overlap():
    para = " ".join(f"Sentence number {i} talks about topic {i % 7}." for i in range(300))
    chunks = chunk_markdown(f"# Long\n\n{para}\n", max_tokens=120, overlap=20)
    assert len(chunks) > 5
    assert all(est_tokens(c.text) <= 120 for c in chunks)
    # consecutive chunks share some words (overlap)
    for a, b in zip(chunks, chunks[1:]):
        tail = a.text.split()[-3:]
        assert " ".join(tail) in b.text


def test_example_markdown_chunks_have_headings():
    loaded = load(EXAMPLES / "home-network.md")
    assert loaded.title == "Home Network Handbook"
    assert loaded.meta["title"] == "Home Network Handbook"
    chunks = chunk_markdown(loaded.text)
    headings = [c.heading for c in chunks]
    assert any("NAS and backups" in h for h in headings)
    assert any("Wi-Fi" in h for h in headings)
    text = "\n".join(c.text for c in chunks)
    for needle in ("Firmware updates", "Choosing channels", "Restoring a file", "Troubleshooting"):
        assert needle in text


def test_pdf_loader_pages():
    loaded = load(EXAMPLES / "houseplant-care.pdf")
    assert loaded.kind == "pdf" and loaded.title == "Houseplant Care Guide"
    chunks = chunk_pages(loaded.pages)
    assert [c.heading for c in chunks] == ["p. 1", "p. 2"]
    assert "drainage hole" in chunks[1].text


def test_html_loader_strips_scripts_and_keeps_headings():
    loaded = load_html(
        "<html><head><title>T</title><style>x{}</style></head><body><nav>menu</nav>"
        "<h1>Top</h1><p>Hello\n   world</p><h2>Sub</h2><ul><li>one</li><li>two</li></ul>"
        "<script>evil()</script></body></html>"
    )
    assert loaded.title == "T"
    assert "evil" not in loaded.text and "menu" not in loaded.text
    assert "# Top" in loaded.text and "## Sub" in loaded.text and "Hello world" in loaded.text
    assert "- one" in loaded.text


def test_markdown_title_fallbacks():
    assert load_markdown("no heading here", fallback_title="my_notes-file").title == "my notes file"
    assert load_markdown("---\ntitle: FM\n---\n# H1\n").title == "FM"


def test_prompt_formats_and_matryoshka():
    assert format_query("x?") == "task: search result | query: x?"
    assert format_doc(None, "body") == "title: none | text: body"
    assert format_doc("T", "body") == "title: T | text: body"
    v = truncate([3.0, 4.0, 100.0], 2)
    assert v == [0.6, 0.8]
    assert math.isclose(sum(x * x for x in v), 1.0)


def test_fts_query_sanitises():
    assert fts_query('what is "the" NAS backup?') == '"nas" OR "backup"'
    assert fts_query("") == ""
    assert fts_query("the") == '"the"'  # only stopwords: keep them
