"""
Introspects the compiled LangGraph pipeline (app/graph.py::build_graph) and
prints its real node/edge structure -- Mermaid source plus a flat JSON
manifest -- so the architecture diagram shown to customers can be
regenerated from the actual code instead of redrawn by hand whenever
app/graph.py changes.

Usage (from the repo root, with the project's venv active):
    python scripts/render_graph_diagram.py

Uses an in-memory checkpointer (MemorySaver) purely to satisfy
build_graph()'s signature -- no Cosmos DB connection is made, so this runs
anywhere with no env vars configured.
"""

from __future__ import annotations

import json

from langgraph.checkpoint.memory import MemorySaver

from app.graph import build_graph


def main() -> None:
    graph = build_graph(MemorySaver())
    g = graph.get_graph()

    print("=== Mermaid ===")
    print(g.draw_mermaid())

    manifest = {
        "nodes": list(g.nodes.keys()),
        "edges": [
            {
                "source": e.source,
                "target": e.target,
                "label": e.data,
                "conditional": e.conditional,
            }
            for e in g.edges
        ],
    }
    print("=== JSON manifest ===")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
