"""out/model.json -> the graphrag_importer schema in ArangoDB.

The importer's schema is five collections and a closed vocabulary of five edge types
(graphrag/naming.py, graphrag/import_graph_to_adb.py). Rather than invent a schema,
each SysML concept is placed in the importer slot that plays the same role for a
reader:

  Document   one .sysml file
  Chunk      a window of source text cut at declaration boundaries -- the retrievable
             unit, and the thing that carries provenance back to a Document
  Entity     one SysML element, described in prose so it can be embedded
  Relation   RELATED_TO for an authored SysML relation, with the relation name in
             `relationship_type` (the importer's own field for typed edges), plus
             MENTIONED_IN, PART_OF, IN_COMMUNITY and SUB_COMMUNITY_OF for structure

`type` is closed and exists for validation, so inventing a value is not an option.
`relationship_type` is open, which is why the SysML relation lives there. The
vocabulary is imported in config rather than copied -- see the note there.

The two fields hold two vocabularies and are never mixed: `type` is only ever one
of the importer's five constants, and `relationship_type` is only ever a SysML
relation. The importer leaves it unset on its structural edges, so a query that
collapses it sees SysML relations and nothing else.

    python -m sysml.pipeline.project
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .. import config

CHUNK_TARGET = 1400          # characters; declarations are never split across chunks
BATCH = 1000

# How a relation reads in prose. The description is embedded, so an edge is
# retrievable by meaning and not only by traversal.
#
# ONTOLOGY (2 of 3): the relation half. Its keys are every `relationship_type` a
# RELATED_TO edge can carry -- the counterpart of the `relationship_types` list
# GraphRAG's extraction path constrains an LLM with. Here they come out of the
# parser, so this dict has to be kept in step with what parse.py emits: a relation
# missing from here still becomes an edge, but its description falls back to the
# raw keyword.
PHRASING = {
    "owns": "contains", "typedBy": "is typed by", "specializes": "specializes",
    "redefines": "redefines", "satisfies": "satisfies", "refines": "refines",
    "derives": "derives", "performs": "performs", "subject": "has as its subject",
    "exhibits": "exhibits", "connects": "connects to", "transitionsTo": "transitions to",
    "variantOf": "is a variant of", "imports": "imports", "sliceOf": "is a time slice of",
    "sends": "sends to", "dependsOn": "depends on", "valueRef": "takes as its value",
}


def key_of(text: str) -> str:
    """A stable _key. Names carry `::`, spaces and quotes, none of which are legal."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", text)[:180].strip("_")
    return f"{slug}_{hashlib.sha1(text.encode()).hexdigest()[:10]}"


# ---------------------------------------------------------------------- chunking


def chunk_file(path: Path, text: str) -> list[dict]:
    """Cut a file into windows that end on a declaration boundary.

    A chunk boundary is only taken at brace depth 0 or 1, so a `part def { ... }`
    is never split in half. Chunks tile the file with no gaps, which is what makes
    "the chunk containing line N" a total function.
    """
    lines = text.splitlines()
    chunks: list[dict] = []
    start, depth, size = 0, 0, 0
    for i, line in enumerate(lines):
        size += len(line) + 1
        depth += line.count("{") - line.count("}")
        at_boundary = depth <= 1 and (line.rstrip().endswith(("}", ";")) or not line.strip())
        if size >= CHUNK_TARGET and at_boundary:
            chunks.append({"start": start + 1, "end": i + 1,
                           "content": "\n".join(lines[start:i + 1])})
            start, size = i + 1, 0
    if start < len(lines):
        chunks.append({"start": start + 1, "end": len(lines),
                       "content": "\n".join(lines[start:])})
    return [c for c in chunks if c["content"].strip()]


# ------------------------------------------------------------------- description


def describe(element: dict, outgoing: list[tuple[str, str]], provenance: bool = True) -> str:
    """A deterministic prose rendering of an element. No LLM.

    This is the text that gets embedded, so it has to carry the facts a question
    might be about: what it is, where it is, what it says, what it is worth, and
    what it is attached to.

    `provenance=False` drops the opening sentence and the short name and keeps the
    rest. That form is what the analogy step embeds: the opening sentence names the
    model, the layer and the source file, so two elements out of the same file
    resemble each other on the strength of shared boilerplate, which is fatal to a
    comparison whose entire purpose is to cross models. Short names go with it --
    `flr-R073` identifies an element, it does not describe one.
    """
    parts = []
    if not provenance:
        parts.append(f"{element['name']}.")
    else:
        parts.append(
            f"{element['name']} is a {element['metatype']} in the {element['model']} model "
            f"({element['layer']} layer), declared at "
            f"{element['sourceFile']}:{element['sourceLine']}."
        )
        if element.get("shortName"):
            parts.append(f"Its short name is {element['shortName']}.")
    if element.get("doc"):
        parts.append(element["doc"])
    values = []
    for name, attr in (element.get("attributes") or {}).items():
        if name == "value" or not isinstance(attr, dict):
            continue
        if attr.get("value") is not None:
            values.append(f"{name} = {attr['value']}" + (f" {attr['unit']}" if attr.get("unit") else ""))
        elif attr.get("expression"):
            values.append(f"{name} is computed as {attr['expression']}")
    if values:
        parts.append("Attributes: " + "; ".join(values) + ".")
    if element.get("constraints"):
        parts.append("Constraints: " + "; ".join(element["constraints"]) + ".")
    grouped: dict[str, list[str]] = {}
    for rtype, target in outgoing:
        grouped.setdefault(PHRASING.get(rtype, rtype), []).append(target)
    for phrase, targets in grouped.items():
        shown = ", ".join(targets[:12]) + (f" and {len(targets) - 12} more" if len(targets) > 12 else "")
        parts.append(f"It {phrase} {shown}.")
    if element.get("isVariation"):
        parts.append("It is a variation point: its variants are alternative configurations.")
    if element.get("isLibrary"):
        parts.append("It is referenced by this model but declared outside it, in the SysML standard library.")
    return " ".join(parts)


# ---------------------------------------------------------------------- building


def build(model: dict, source_root: Path) -> dict[str, list[dict]]:
    """Turn the parsed model into rows for each importer collection."""
    documents, chunks, entities, edges = [], [], [], []
    chunk_index: dict[str, list[dict]] = {}

    for order, rel in enumerate(model["files"]):
        doc_key = key_of(rel)
        documents.append({
            "_key": doc_key,
            "file_name": rel,
            # The importer keys deletes on file_ids, so a projection has to carry one.
            "file_ids": [f"sysml:{rel}"],
            "citable_url": f"models/{rel}",
            "import_number": order,
        })
        text = (source_root / rel).read_text(encoding="utf-8")
        placed = []
        for i, c in enumerate(chunk_file(source_root / rel, text)):
            ckey = key_of(f"{rel}#{i}")
            chunks.append({
                "_key": ckey, "content": c["content"],
                "tokens": len(c["content"]) // 4, "chunk_order_index": i,
                "file_name": rel, "start_line": c["start"], "end_line": c["end"],
            })
            edges.append({
                "_key": key_of(f"partof:{ckey}"),
                "_from": f"{config.CHUNKS}/{ckey}", "_to": f"{config.DOCUMENTS}/{doc_key}",
                "type": "PART_OF",
                "description": f"chunk {i} of {rel}", "weight": 1.0,
                "source_id": ckey, "order": i,
            })
            placed.append({"key": ckey, "start": c["start"], "end": c["end"]})
        chunk_index[rel] = placed

    def chunk_for(file_name: str, line: int) -> str | None:
        for c in chunk_index.get(file_name, []):
            if c["start"] <= line <= c["end"]:
                return c["key"]
        return chunk_index.get(file_name, [{}])[0].get("key")

    outgoing: dict[str, list[tuple[str, str]]] = {}
    by_qn = {e["qualifiedName"]: e for e in model["elements"]}
    for r in model["relations"]:
        target = by_qn.get(r["to"])
        outgoing.setdefault(r["from"], []).append((r["type"], target["name"] if target else r["to"]))

    for element in model["elements"]:
        ekey = key_of(element["qualifiedName"])
        entities.append({
            "_key": ekey,
            "entity_name": element["qualifiedName"],
            "entity_type": element["metatype"],
            "description": describe(element, outgoing.get(element["qualifiedName"], [])),
            "clusters": [],
            # Kept alongside the importer's own fields: unknown keys are ignored by
            # the importer and these are what a citation is built from.
            "name": element["name"], "short_name": element["shortName"],
            "model": element["model"], "layer": element["layer"],
            "source_file": element["sourceFile"], "source_line": element["sourceLine"],
            "doc": element["doc"], "attributes": element["attributes"],
            "constraints": element["constraints"],
            "is_variation": element["isVariation"], "is_variant": element["isVariant"],
            "is_library": element["isLibrary"], "parent": element["parent"],
        })
        ckey = chunk_for(element["sourceFile"], element["sourceLine"])
        if ckey and not element["isLibrary"]:
            edges.append({
                "_key": key_of(f"mention:{ekey}:{ckey}"),
                "_from": f"{config.ENTITIES}/{ekey}", "_to": f"{config.CHUNKS}/{ckey}",
                "type": "MENTIONED_IN",
                "description": f"{element['name']} is declared in {element['sourceFile']}",
                "weight": 1.0, "source_id": ckey, "order": 0,
            })

    for i, r in enumerate(model["relations"]):
        src, dst = by_qn.get(r["from"]), by_qn.get(r["to"])
        if not src or not dst:
            continue
        fk, tk = key_of(r["from"]), key_of(r["to"])
        phrase = PHRASING.get(r["type"], r["type"])
        description = f"{src['name']} {phrase} {dst['name']}"
        if r.get("trigger"):
            description += f" on {r['trigger']}"
        edges.append({
            "_key": key_of(f"rel:{r['from']}|{r['type']}|{r['to']}"),
            "_from": f"{config.ENTITIES}/{fk}", "_to": f"{config.ENTITIES}/{tk}",
            "type": "RELATED_TO",
            # The importer's own field for a typed relation. `type` cannot carry it:
            # RelationshipTypes.get_expected_types() is a closed set of five.
            "relationship_type": r["type"],
            "description": description,
            "weight": 1.0,
            "source_id": chunk_for(r["sourceFile"], r["sourceLine"]),
            "order": i,
            "source_file": r["sourceFile"], "source_line": r["sourceLine"],
            "trigger": r.get("trigger"),
        })

    return {config.DOCUMENTS: documents, config.CHUNKS: chunks,
            config.ENTITIES: entities, config.RELATIONS: edges}


# ------------------------------------------------------------------------ write


def ensure_schema(db) -> None:
    for name in config.VERTEX_COLLECTIONS:
        if not db.has_collection(name):
            db.create_collection(name)
    if not db.has_collection(config.RELATIONS):
        db.create_collection(config.RELATIONS, edge=True)
    if not db.has_graph(config.GRAPH):
        db.create_graph(config.GRAPH, edge_definitions=[{
            "edge_collection": config.RELATIONS,
            "from_vertex_collections": list(config.VERTEX_COLLECTIONS),
            "to_vertex_collections": list(config.VERTEX_COLLECTIONS),
        }])
    db.collection(config.DOCUMENTS).add_index(
        {"type": "persistent", "fields": ["file_ids[*]"], "name": "file_ids_idx"})
    db.collection(config.RELATIONS).add_index(
        {"type": "persistent", "fields": ["relationship_type"], "name": "relationship_type_idx"})
    db.collection(config.ENTITIES).add_index(
        {"type": "persistent", "fields": ["entity_type"], "name": "entity_type_idx"})


def write(db, rows: dict[str, list[dict]]) -> dict[str, int]:
    written = {}
    for name, docs in rows.items():
        config.drop_vector_indexes(db, name)
        coll = db.collection(name)
        coll.truncate()
        for i in range(0, len(docs), BATCH):
            coll.import_bulk(docs[i:i + BATCH], on_duplicate="replace")
        written[name] = coll.count()
    return written


def main() -> None:
    model = json.loads(config.MODEL_JSON.read_text(encoding="utf-8"))
    db = config.db(create=True)
    ensure_schema(db)
    counts = write(db, build(model, config.MODELS))
    print(f"projected into {config.DB_NAME}")
    for name, n in counts.items():
        print(f"  {n:>6}  {name}")


if __name__ == "__main__":
    main()
