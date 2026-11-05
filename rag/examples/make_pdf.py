"""Generate examples/docs/houseplant-care.pdf (2 pages, Helvetica text) with the stdlib only.

    uv run python examples/make_pdf.py
"""

from pathlib import Path

TITLE = "Houseplant Care Guide"
PAGES = [
    [
        "Houseplant Care Guide",
        "",
        "Watering",
        "Most houseplants die from too much water, not too little. Push a finger",
        "two centimetres into the soil: water only when it feels dry at that depth.",
        "Succulents and cacti need water every two to three weeks in summer and",
        "almost none in winter. Always use pots with a drainage hole.",
        "",
        "Light",
        "A monstera or a fiddle leaf fig wants bright indirect light near an east",
        "or west window. Snake plants and ZZ plants tolerate low light corners.",
    ],
    [
        "Repotting",
        "Repot in spring when roots grow out of the drainage hole. Choose a pot",
        "only two to four centimetres wider than the old one and use fresh soil.",
        "",
        "Pests",
        "Fungus gnats live in soil that stays wet; let the top layer dry out and",
        "use yellow sticky traps. Spider mites show up as fine webbing under the",
        "leaves; shower the plant and wipe the leaves weekly until they are gone.",
    ],
]


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build() -> bytes:
    objs: list[bytes] = []
    n_pages = len(PAGES)
    # 1 catalog, 2 pages, 3 font, 4 info, then (page, content) pairs
    page_ids = [5 + 2 * i for i in range(n_pages)]
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objs.append(f"<< /Title ({esc(TITLE)}) /Producer (stack-rag examples) >>".encode())
    for i, lines in enumerate(PAGES):
        ops = ["BT", "/F1 12 Tf", "16 TL", "72 760 Td"]
        for line in lines:
            ops.append(f"({esc(line)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode()
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_ids[i] + 1} 0 R >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 4 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


if __name__ == "__main__":
    target = Path(__file__).parent / "docs" / "houseplant-care.pdf"
    target.write_bytes(build())
    print(f"wrote {target}")
