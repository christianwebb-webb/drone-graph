"""models/*.sysml -> a knowledge graph on GraphRAG's workbench, in out/kg.

This is graphrag_importer's own extraction pipeline, run in-process. It reads the
source text, asks an LLM for the entities and the relations between them, clusters
the result with Leiden and writes one report per cluster. Nothing here parses
SysML. The only thing this project tells it about SysML is the ontology in
`config`: 27 entity types and 18 relation types, handed to `GraphRAG` as
`entity_types` and `relationship_types` with `enable_strict_types=True`, which
makes them a closed vocabulary rather than a suggestion -- an entity or edge whose
type is not on the list is dropped, not renamed.

Provenance survives, which is the part worth knowing. `ainsert` takes a
`metadata_list` alongside the texts and merges each dict into that document's
record; `import_documents` reads `name`, `citable_url` and `file_id` back out of
it (graphrag/importer/import_graph_to_adb.py:1783). Chunks carry `full_doc_id` and
entities carry the chunk ids they were found in, so every entity traces back to
the files it came from through the graph the importer already builds.

    python -m sysml.pipeline.extract
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from .. import config

# `print` is used by upstream's progress ticker, which is drawn with braille
# characters. A Windows console is cp1252 by default and raises on them, mid-run,
# after the LLM calls are already paid for. A notebook's stdout is already UTF-8
# and has no `reconfigure`, so both are checked before touching it.
if (sys.stdout.encoding or "").lower() not in ("utf-8", "utf8"):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def graphrag(model: str):
    """The extraction pipeline, configured and pointed at one model's workbench.

    `enable_chunk_embeddings` is on because the `unified` retriever searches the
    source chunks in parallel with the entity graph, and without vectors on the
    chunks that half of it has nothing to search.

    A workbench per model is what keeps entity names from merging across two
    unrelated vehicles -- the merge happens here, in the builder's own graph, long
    before ArangoDB sees anything, so it is the only place it can be scoped.

    The LLM cache lives in the working directory, so a second run over unchanged
    files re-reads the answers instead of re-buying them.
    """
    config.openai_key()
    config.kg(model).mkdir(parents=True, exist_ok=True)
    # corpus_graph and the builder both attach a stdout handler at import time,
    # which corrupts anything reading stdout. Keep the logs, move them to stderr.
    for name in ("arango-graphrag", "vectordb", "graphrag"):
        log = logging.getLogger(name)
        log.handlers = [logging.StreamHandler(sys.stderr)]
        log.propagate = False

    from graphrag.graph_builder.builder.graphrag import GraphRAG

    return GraphRAG(
        working_dir=str(config.kg(model)),
        entity_types=config.KINDS,
        relationship_types=config.RELATIONS_ONTOLOGY,
        enable_strict_types=True,
        enable_chunk_embeddings=True,
    )


def sources(model: str) -> list[tuple[str, str]]:
    """One model's .sysml files, as (path relative to models/, text)."""
    return [(rel, p.read_text(encoding="utf-8"))
            for p, rel in ((p, p.relative_to(config.MODELS).as_posix())
                           for p in sorted(config.MODELS.rglob("*.sysml")))
            if config.model_of(rel) == model]


def metadata(relative_path: str) -> dict:
    """What `import_documents` will read off the Document.

    `file_id` is the key the importer's delete engine matches on, `citable_url` is
    what a [CITE:n] in a retrieved answer resolves to, and `name` is the file name
    every downstream question about provenance goes through.
    """
    return {
        "name": relative_path,
        "citable_url": f"models/{relative_path}",
        "file_id": f"sysml:{relative_path}",
    }


async def run(model: str) -> dict:
    kg = graphrag(model)
    files = sources(model)
    print(f"  {model}: {len(files)} files, {sum(len(t) for _, t in files):,} characters")
    await kg.ainsert([text for _, text in files],
                     [metadata(rel) for rel, _ in files])
    return summary(model)


def summary(model: str) -> dict:
    """Count what landed on the workbench, without loading the graph twice."""
    import networkx as nx

    out = config.kg(model)
    graph = nx.read_graphml(out / config.ARTIFACTS.RELATIONSHIPS)
    chunks = json.loads((out / config.ARTIFACTS.TEXT_CHUNKS).read_text(encoding="utf-8"))
    reports = json.loads(
        (out / config.ARTIFACTS.COMMUNITY_REPORTS).read_text(encoding="utf-8"))
    return {"chunks": len(chunks), "entities": graph.number_of_nodes(),
            "relations": graph.number_of_edges(), "communities": len(reports)}


def main() -> None:
    for model in config.MODEL_NAMES:
        counts = asyncio.run(run(model))
        print(f"  extracted into {config.kg(model)}")
        for name, n in counts.items():
            print(f"  {n:>6}  {name}")


if __name__ == "__main__":
    main()
