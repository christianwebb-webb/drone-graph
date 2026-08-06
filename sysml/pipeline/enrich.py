"""Embeddings, vector indexes, communities and community reports.

Three things the projection needs before it can be read the way a GraphRAG corpus
is read:

  embeddings   into `embedding` -- singular, which is the field the importer
               vector-indexes (IndexNames.EMBEDDING_FIELD). Writing autograph's
               `embeddings` instead is invisible: the rows are there, the vectors
               are there, and every vector query returns nothing.
  communities  the importer runs Leiden because an LLM-extracted graph has no other
               structure to go on. A SysML model has two: engineers already grouped
               it into packages, and the authored traceability edges cut across
               those groups. Level 1 is the package structure; level 0 is label
               propagation over the meaning-bearing edges only, which finds the
               requirement -> capability -> function -> component clusters.
  reports      one LLM summary per community, from counted facts and member names.
               This is the only place in the pipeline an LLM writes anything that
               is stored.

    python -m sysml.pipeline.enrich
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from openai import OpenAI

from .. import config
from .project import key_of

CACHE = config.OUT / "embeddings.npz"
REPORTS = config.OUT / "reports.json"

# autograph, cloned next to this project. Only DataStorage is used, and only to
# build the vector indexes -- see `ensure_vector_index`.
AUTOGRAPH_REPO = config.ROOT.parent / "autograph"

# Relations that carry engineering meaning rather than containment. Communities are
# found over these alone: `owns` connects the entire model into one blob, and a
# community that contains everything explains nothing.
MEANING_EDGES = {"satisfies", "refines", "performs", "specializes", "subject",
                 "derives", "variantOf", "transitionsTo", "connects", "exhibits",
                 "redefines", "sliceOf"}

# Below this a "community" is a stray pair or triple, and a report on it says
# nothing. Elements in one are simply not in any community -- which is honest, and
# they are still reachable by every other retrieval path.
MIN_COMMUNITY = 10
BATCH = 128


# ------------------------------------------------------------------- embeddings


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def load_cache() -> dict[str, list[float]]:
    if not CACHE.exists():
        return {}
    blob = np.load(CACHE, allow_pickle=False)
    if blob["vecs"].shape[1] != config.EMBED_DIM:
        return {}
    return dict(zip(blob["keys"].tolist(), blob["vecs"].tolist()))


def save_cache(vectors: dict[str, list[float]]) -> None:
    config.OUT.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(CACHE, keys=np.array(list(vectors)),
                        vecs=np.array(list(vectors.values()), dtype=np.float32))


def embed_texts(texts: list[str], vectors: dict[str, list[float]]) -> None:
    """Embed everything not already cached, in parallel batches."""
    todo = sorted({t for t in texts if _hash(t) not in vectors})
    if not todo:
        return
    client = OpenAI(api_key=config.openai_key())
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]

    def run(batch: list[str]) -> list[tuple[str, list[float]]]:
        resp = client.embeddings.create(model=config.EMBED_MODEL, input=batch,
                                        dimensions=config.EMBED_DIM)
        return [(_hash(t), d.embedding) for t, d in zip(batch, resp.data)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        for pairs in pool.map(run, batches):
            vectors.update(pairs)
    print(f"  embedded {len(todo)} new texts ({len(batches)} batches)")


def embed_query(text: str) -> list[float]:
    client = OpenAI(api_key=config.openai_key())
    return client.embeddings.create(model=config.EMBED_MODEL, input=[text],
                                    dimensions=config.EMBED_DIM).data[0].embedding


def write_vectors(db, collection: str, field: str, vectors: dict[str, list[float]]) -> int:
    """Attach `embedding` to every row whose text field is non-empty."""
    rows = list(db.aql.execute(
        f"FOR d IN @@c FILTER d.{field} != null AND d.{field} != '' "
        f"RETURN {{_key: d._key, text: d.{field}}}", bind_vars={"@c": collection}))
    updates = [{"_key": r["_key"], config.EMBEDDING_FIELD: vectors[_hash(r["text"])]}
               for r in rows if _hash(r["text"]) in vectors]
    coll = db.collection(collection)
    for i in range(0, len(updates), 500):
        coll.import_bulk(updates[i:i + 500], on_duplicate="update")
    return len(updates)


def data_storage(db):
    """autograph's DataStorage, pointed at the local database.

    Its config classes read the environment in their class bodies, so
    EMBEDDING_DIM has to be set before `corpus_graph` is imported: the index is
    built for `EmbeddingConfig.DIMENSION` rather than for the length of the
    vectors it finds, and a late setenv leaves that at autograph's default of 512
    while ours are 768. Both read the same variable, so setting it from
    config.EMBED_DIM keeps the two definitions of "how wide is a vector" in step.

    DataStorage takes a plain password-authed database -- its documented legacy
    usage. The platform's connection manager only matters when a JWT has to be
    renewed mid-run, which is not a thing that happens here.
    """
    os.environ.setdefault("EMBEDDING_DIM", str(config.EMBED_DIM))
    if str(AUTOGRAPH_REPO) not in sys.path:
        sys.path.insert(0, str(AUTOGRAPH_REPO))
    try:
        from corpus_graph.datastorage import DataStorage
    except ImportError as exc:
        raise RuntimeError(
            f"cannot import corpus_graph ({exc}). Clone autograph next to this project."
        ) from exc
    # corpus_graph/logger.py attaches a StreamHandler to stdout at import time, and
    # the index build is chatty. Same treatment as txt2aql gets in nl.py: keep the
    # logs, move them off stdout.
    log = logging.getLogger("corpus_graph")
    for handler in list(log.handlers):
        if getattr(handler, "stream", None) is sys.stdout:
            log.removeHandler(handler)
    if not log.handlers:
        log.addHandler(logging.StreamHandler(sys.stderr))
    return DataStorage(db)


def ensure_vector_index(db, collection: str) -> str:
    """Build the vector index with autograph's DataStorage.

    It sizes nLists itself, waits out the background training, and -- the part
    worth having -- writes `defaultNProbe` into the index. nProbe is how many of
    the index's partitions a search opens, and left at the default of 1 a search
    reads a sliver of the collection and silently returns fewer rows than the
    LIMIT asked for. Carrying the setting on the index means every query gets it
    without having to remember to pass it.

    The existing index is dropped first rather than adopted: autograph keeps a
    ready index as-is, which is right for its own pipeline but wrong here, where
    this runs directly after the vectors were rewritten.
    """
    coll = db.collection(collection)
    config.drop_vector_indexes(db, collection)
    # ArangoDB refuses a vector index unless every document carries the field.
    missing = next(iter(db.aql.execute(
        f"RETURN LENGTH(FOR d IN @@c FILTER !IS_LIST(d.{config.EMBEDDING_FIELD}) RETURN 1)",
        bind_vars={"@c": collection})))
    if missing:
        return f"no index: {missing} rows have no {config.EMBEDDING_FIELD}"
    data_storage(db).create_vector_index_on_field(coll, config.EMBEDDING_FIELD)
    index = next((i for i in coll.indexes() if i.get("type") == "vector"), None)
    if index is None:
        return "no index: creation failed"
    params = index.get("params", {})
    return (f"vector index, nLists={params.get('nLists')}, "
            f"defaultNProbe={params.get('defaultNProbe')}")


# ------------------------------------------------------------------ communities


def label_propagation(nodes: list[str], edges: list[tuple[str, str]],
                      rounds: int = 20) -> dict[str, str]:
    """Deterministic label propagation: every node takes its neighbours' commonest
    label, ties broken lexicographically so a rerun gives the same answer."""
    neighbours: dict[str, list[str]] = defaultdict(list)
    for a, b in edges:
        neighbours[a].append(b)
        neighbours[b].append(a)
    label = {n: n for n in nodes}
    order = sorted(nodes)
    for _ in range(rounds):
        changed = 0
        for n in order:
            if not neighbours[n]:
                continue
            counts = Counter(label[m] for m in neighbours[n])
            best = min(sorted(counts), key=lambda lab: (-counts[lab], lab))
            if best != label[n]:
                label[n] = best
                changed += 1
        if not changed:
            break
    return label


def detect_communities(db) -> tuple[list[dict], list[dict]]:
    """Level 0 from the traceability edges, level 1 from the package structure."""
    entities = list(db.aql.execute(
        f"FOR e IN {config.ENTITIES} FILTER e.is_library != true "
        "RETURN {name: e.entity_name, model: e.model, layer: e.layer, type: e.entity_type}"))
    edges = list(db.aql.execute(
        f"FOR r IN {config.RELATIONS} FILTER r.type == 'RELATED_TO' "
        "AND r.relationship_type IN @kinds "
        f"LET a = DOCUMENT(r._from), b = DOCUMENT(r._to) "
        "FILTER a != null AND b != null "
        "RETURN [a.entity_name, b.entity_name]",
        bind_vars={"kinds": sorted(MEANING_EDGES)}))

    names = [e["name"] for e in entities]
    inside = set(names)          # library stubs are endpoints but not members
    labels = label_propagation(names, [(a, b) for a, b in edges
                                       if a in inside and b in inside])
    members: dict[str, list[dict]] = defaultdict(list)
    for e in entities:
        members[labels[e["name"]]].append(e)

    info = {e["name"]: e for e in entities}
    level0: list[dict] = []
    for label, group in sorted(members.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(group) < MIN_COMMUNITY:
            continue
        model = Counter(m["model"] for m in group).most_common(1)[0][0]
        layer = Counter(m["layer"] for m in group).most_common(1)[0][0]
        level0.append({
            "id": f"c0_{key_of(label)}", "level": 0, "seed": label,
            "title": f"{info[label]['name']} cluster",
            "model": model, "layer": layer,
            "members": [m["name"] for m in group],
            "occurrence": len(group),
        })

    parents: dict[tuple[str, str], dict] = {}
    for c in level0:
        pk = (c["model"], c["layer"])
        parent = parents.setdefault(pk, {
            "id": f"c1_{key_of(f'{pk[0]}::{pk[1]}')}", "level": 1,
            "title": f"{pk[1]} of the {pk[0]} model", "model": pk[0], "layer": pk[1],
            "members": [], "sub_communities": [], "occurrence": 0,
        })
        parent["members"].extend(c["members"])
        parent["sub_communities"].append(c["id"])
        parent["occurrence"] += c["occurrence"]
        c["parent"] = parent["id"]
    return level0, sorted(parents.values(), key=lambda c: -c["occurrence"])


def community_facts(db, community: dict) -> str:
    """Counted facts about a community -- the only input the report writer gets."""
    rows = list(db.aql.execute(
        f"FOR e IN {config.ENTITIES} FILTER e.entity_name IN @names "
        "RETURN {name: e.name, type: e.entity_type, doc: SUBSTRING(e.doc, 0, 220), "
        "at: CONCAT(e.source_file, ':', e.source_line)}",
        bind_vars={"names": community["members"]}))
    rels = list(db.aql.execute(
        f"FOR r IN {config.RELATIONS} FILTER r.type == 'RELATED_TO' "
        "AND DOCUMENT(r._from).entity_name IN @names "
        "AND DOCUMENT(r._to).entity_name IN @names "
        "COLLECT t = r.relationship_type WITH COUNT INTO n SORT n DESC RETURN {t, n}",
        bind_vars={"names": community["members"]}))
    kinds = Counter(r["type"] for r in rows)
    lines = [
        f"Community: {community['title']}",
        f"Model: {community['model']}   Layer: {community['layer']}   Members: {len(rows)}",
        "Element kinds: " + ", ".join(f"{k}={v}" for k, v in kinds.most_common()),
        "Internal relations: " + (", ".join(f"{r['t']}={r['n']}" for r in rels) or "none"),
        "",
        "Members:",
    ]
    for r in sorted(rows, key=lambda r: r["name"])[:60]:
        lines.append(f"  {r['name']} ({r['type']}, {r['at']})"
                     + (f" -- {r['doc']}" if r["doc"] else ""))
    if len(rows) > 60:
        lines.append(f"  ... and {len(rows) - 60} more")
    return "\n".join(lines)


PROMPT = """You are summarising one cluster of a SysML v2 systems-engineering model
for an engineer browsing the model.

Write, from the facts below and nothing else:
  title      six words or fewer, naming what this cluster is about
  summary    two or three sentences: what part of the system this covers, and what
             the relations between its members establish
  findings   two to four short bullet observations an engineer would care about --
             a gap, a concentration, an inconsistency, a notable value. Cite
             file:line for anything specific.

Do not invent facts about Apollo, drones or spacecraft that are not in the input.
If the cluster looks incoherent, say so.

Return JSON: {"title": str, "summary": str, "findings": [str]}

FACTS
-----
%s"""


def write_reports(db, communities: list[dict]) -> dict[str, dict]:
    cached = json.loads(REPORTS.read_text(encoding="utf-8")) if REPORTS.exists() else {}
    todo = [c for c in communities if c["id"] not in cached]
    if todo:
        client = OpenAI(api_key=config.openai_key())

        def run(c: dict) -> tuple[str, dict]:
            resp = client.chat.completions.create(
                model=config.CHAT_MODEL, temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": PROMPT % community_facts(db, c)}])
            return c["id"], json.loads(resp.choices[0].message.content)

        with ThreadPoolExecutor(max_workers=6) as pool:
            for cid, report in pool.map(run, todo):
                cached[cid] = report
        REPORTS.write_text(json.dumps(cached, indent=1), encoding="utf-8")
        print(f"  generated {len(todo)} community reports")
    return cached


def write_communities(db, level0: list[dict], level1: list[dict],
                      reports: dict[str, dict]) -> int:
    config.drop_vector_indexes(db, config.COMMUNITIES)
    coll = db.collection(config.COMMUNITIES)
    coll.truncate()
    docs, edges = [], []
    for c in level0 + level1:
        report = reports.get(c["id"], {})
        findings = report.get("findings", [])
        report_string = "\n".join([
            report.get("title", c["title"]), "", report.get("summary", ""), "",
            *(f"- {f}" for f in findings)]).strip()
        docs.append({
            "_key": c["id"], "title": report.get("title", c["title"]),
            "report_string": report_string,
            "report_json": {**report, "member_count": len(c["members"])},
            "level": c["level"], "occurrence": c["occurrence"],
            "sub_communities": c.get("sub_communities", []),
            "model": c["model"], "layer": c["layer"], "members": c["members"],
        })
        for name in c["members"]:
            edges.append({
                "_key": key_of(f"incomm:{c['id']}:{name}"),
                "_from": f"{config.ENTITIES}/{key_of(name)}",
                "_to": f"{config.COMMUNITIES}/{c['id']}",
                "type": "IN_COMMUNITY",
                "description": f"{name} belongs to {c['title']}",
                "weight": 1.0, "source_id": c["id"], "order": c["level"],
            })
        if c.get("parent"):
            edges.append({
                "_key": key_of(f"subcommunity:{c['id']}"),
                "_from": f"{config.COMMUNITIES}/{c['id']}",
                "_to": f"{config.COMMUNITIES}/{c['parent']}",
                "type": config.SUB_COMMUNITY_OF,
                "description": f"{c['title']} is part of a larger community",
                "weight": 1.0, "source_id": c["id"], "order": 0,
            })
    coll.import_bulk(docs, on_duplicate="replace")
    rel = db.collection(config.RELATIONS)
    rel.delete_many([{"_key": e["_key"]} for e in edges], silent=True, sync=False)
    for i in range(0, len(edges), 1000):
        rel.import_bulk(edges[i:i + 1000], on_duplicate="replace")
    # A stale membership edge from a previous run would point at a deleted
    # community, so drop anything not written this time. HAS_PARENT is the name
    # this project used before the vocabulary came from the importer; a graph
    # built by the old code still has those edges and they are cleaned up here.
    db.aql.execute(
        f"FOR r IN {config.RELATIONS} "
        f"FILTER r.type IN ['IN_COMMUNITY', '{config.SUB_COMMUNITY_OF}', 'HAS_PARENT'] "
        "FILTER DOCUMENT(r._to) == null OR r.type == 'HAS_PARENT' "
        "REMOVE r IN " + config.RELATIONS)
    return len(docs)


# ------------------------------------------------------------------------- main


def main() -> None:
    db = config.db()
    print("communities")
    level0, level1 = detect_communities(db)
    print(f"  level 0: {len(level0)} clusters   level 1: {len(level1)} groups")
    reports = write_reports(db, level0 + level1)
    n = write_communities(db, level0, level1, reports)
    print(f"  wrote {n} communities")

    print("embeddings")
    vectors = load_cache()
    sources = [(config.ENTITIES, "description"), (config.CHUNKS, "content"),
               (config.COMMUNITIES, "report_string")]
    texts: list[str] = []
    for coll, field in sources:
        texts += [r for r in db.aql.execute(
            f"FOR d IN @@c FILTER d.{field} != null AND d.{field} != '' RETURN d.{field}",
            bind_vars={"@c": coll})]
    texts += list(db.aql.execute(
        f"FOR r IN {config.RELATIONS} FILTER r.type == 'RELATED_TO' RETURN r.description"))
    embed_texts(texts, vectors)
    save_cache(vectors)

    for coll, field in sources:
        written = write_vectors(db, coll, field, vectors)
        print(f"  {coll:22} {written:>6} vectors   {ensure_vector_index(db, coll)}")

    # Only RELATED_TO edges carry meaning worth embedding, which means the edge
    # collection cannot take an ANN index: ArangoDB requires the vector field on
    # every document, and then rejects inserts without it. Exact COSINE_SIMILARITY
    # is used instead -- and unlike APPROX_NEAR_COSINE it can be filtered by
    # relationship_type in the same query.
    rows = list(db.aql.execute(
        f"FOR r IN {config.RELATIONS} FILTER r.type == 'RELATED_TO' "
        "RETURN {_key: r._key, text: r.description}"))
    updates = [{"_key": r["_key"], config.EMBEDDING_FIELD: vectors[_hash(r["text"])]}
               for r in rows if _hash(r["text"]) in vectors]
    # import_bulk on an edge collection demands _from/_to even for an update, so
    # patch through AQL instead.
    for i in range(0, len(updates), 500):
        db.aql.execute(
            f"FOR u IN @rows UPDATE u._key WITH {{{config.EMBEDDING_FIELD}: u.{config.EMBEDDING_FIELD}}} "
            f"IN {config.RELATIONS}", bind_vars={"rows": updates[i:i + 500]})
    print(f"  {config.RELATIONS:22} {len(updates):>6} vectors   exact cosine, no ANN index")


if __name__ == "__main__":
    main()
