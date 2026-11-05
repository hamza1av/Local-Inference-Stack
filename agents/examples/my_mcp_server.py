"""Your own MCP tool server in ~40 lines (FastMCP, streamable HTTP).

Run:   just agents::mcp-demo                       (= uv run python examples/my_mcp_server.py)
       -> http://127.0.0.1:8099/mcp   (port: --port or $MY_MCP_PORT)

MCP ("Model Context Protocol") is a standard way to offer tools to *any* client. Write the tool
once, use it from:

  * this agent       add to agents/mcp_servers.toml:
                         [servers.units]
                         url = "http://127.0.0.1:8099/mcp"
                         route = "units"
                         description = "Converting units or date arithmetic"
                     -> the router gets a new `units` option, the route sees only these tools
  * example 03       uv run python examples/03_mcp_tools.py --url units=http://127.0.0.1:8099/mcp "..."
  * Claude Code      claude mcp add --transport http units http://127.0.0.1:8099/mcp
  * Open WebUI       Admin Settings -> External Tools -> + -> type "MCP (Streamable HTTP)",
                     URL http://host.docker.internal:8099/mcp (Open WebUI runs in a container;
                     start the server with --host 0.0.0.0 for that), then enable the tool in a chat.

What makes a good tool for a small (9B) model: one clear job, few parameters with obvious
names, a one-line docstring, short plain-text results, errors returned as text.
"""

from __future__ import annotations

import argparse
import os
from datetime import date

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("units")

# factor to the base unit of each dimension (m, kg, l)
_FACTORS = {
    "mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0, "in": 0.0254, "ft": 0.3048, "yd": 0.9144, "mi": 1609.344,
    "g": 0.001, "kg": 1.0, "lb": 0.45359237, "oz": 0.028349523125, "t": 1000.0,
    "ml": 0.001, "l": 1.0, "gal": 3.785411784, "cup": 0.2365882365,
}
_DIM = {**dict.fromkeys(["mm", "cm", "m", "km", "in", "ft", "yd", "mi"], "length"),
        **dict.fromkeys(["g", "kg", "lb", "oz", "t"], "mass"),
        **dict.fromkeys(["ml", "l", "gal", "cup"], "volume")}
_TEMP = {"c", "f", "k"}


@mcp.tool()
def convert_units(value: float, from_unit: str, to_unit: str) -> str:
    """Convert a value between units: mm cm m km in ft yd mi, g kg lb oz t, ml l gal cup, C F K."""
    f, t = from_unit.strip().lower().rstrip("°"), to_unit.strip().lower().rstrip("°")
    f, t = f.removeprefix("deg"), t.removeprefix("deg")
    if f in _TEMP and t in _TEMP:
        c = {"c": value, "f": (value - 32) * 5 / 9, "k": value - 273.15}[f]
        out = {"c": c, "f": c * 9 / 5 + 32, "k": c + 273.15}[t]
    elif f in _FACTORS and t in _FACTORS:
        if _DIM[f] != _DIM[t]:
            return f"error: cannot convert {_DIM[f]} ({from_unit}) to {_DIM[t]} ({to_unit})"
        out = value * _FACTORS[f] / _FACTORS[t]
    else:
        return f"error: unknown unit {from_unit if f not in _FACTORS and f not in _TEMP else to_unit}"
    return f"{value:g} {from_unit} = {out:.6g} {to_unit}"


@mcp.tool()
def days_between(start: str, end: str) -> str:
    """Number of days between two dates (YYYY-MM-DD). Use 'today' for the current date."""
    def parse(s: str) -> date:
        return date.today() if s.strip().lower() == "today" else date.fromisoformat(s.strip())

    try:
        d = (parse(end) - parse(start)).days
    except ValueError as e:
        return f"error: {e}"
    return f"{d} days from {start} to {end}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MY_MCP_PORT", "8099")))
    a = ap.parse_args()
    mcp.settings.host, mcp.settings.port = a.host, a.port
    mcp.settings.stateless_http = True  # no session state needed: simpler and restart-safe
    mcp.run(transport="streamable-http")
