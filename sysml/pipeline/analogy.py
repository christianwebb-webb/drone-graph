"""Cross-model analogy edges, found by autograph's own SimilarityFinder.

Extraction relates elements a file talks about. It cannot relate two vehicles to
each other, because no Apollo file mentions a drone -- so nothing in the graph
crosses a model boundary except where the two happen to use the same word for the
same thing. Everything shares one embedding space, which is enough for a `unified`
question to pull both models into one answer, but there is nothing to query and
nothing to rank: "what plays the drone battery's role in Apollo?" has no path to
walk.

autograph already solves "which of these resemble each other" in
`corpus_graph.similarity_finding.SimilarityFinder`: semantic search and BM25 over
the same corpus, fused with reciprocal rank, top_k kept. That class runs here
unmodified, against the local container.

Two things are arranged around it rather than changed inside it.

**Granularity.** autograph's corpus layer compares whole documents, and reads only
the first `CorpusGraphConfig.CHUNK_MAX_CHARS` of each. So the corpus handed to it
is one document per *entity*, not per file -- every entity is far inside the
truncation bound, and the edges land where the question is. The descriptions and
the vectors are the ones extraction already wrote, so this step buys no embeddings.

**Direction.** `SimilarityFinder` restricts candidates with `module_doc_ids`,
which it applies before fusion so a foreign document cannot consume the top_k
window -- its purpose upstream is to keep edges *inside* a module. The same
argument runs backwards: pass the entities of the *other* models and the only
edges it can build are the ones that cross.

Entities are compared only against their own `entity_type` -- a part against a
part, a requirement against a requirement. Without that gate the nearest neighbour
of a part is regularly a requirement that talks about the part, which is a topical
match and not an analogy.

    python -m sysml.pipeline.analogy
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from .. import config

# The corpus autograph is handed, and the edges it writes into. Both are staging:
# they are rebuilt from scratch on every run and nothing outside this file reads
# them. The result is promoted into the importer's own edge collection at the end.
STAGING = f"{config.PROJECT}_AnalogyCorpus"
STAGING_EDGES = f"{config.PROJECT}_AnalogySimilarTo"

# Entity types worth comparing. Attribute, Package, Enumeration, Metadata,
# Snapshot and Timeslice are left out: an attribute's whole description is often a
# number and a unit, so every mass resembles every other mass; a package is a
# container and resembles whatever it contains; a snapshot is an occurrence of an
# element already compared on its own.
ROLES = {
    "part", "action", "requirement", "state", "port", "item", "interface",
    "connection", "constraint", "calc", "analysis", "flow", "allocation",
    "usecase", "verification", "concern", "view", "viewpoint", "event",
}

# How wide autograph searches. This is not the number of edges kept: the module
# restriction is applied to the raw search results before fusion, so a narrow
# top_k would be spent almost entirely on same-model neighbours -- an entity's
# closest matches are overwhelmingly its own siblings -- and nothing would survive
# to fuse. Searching wide and cutting afterwards is what leaves real cross-model
# candidates in the window.
SEARCH_TOP_K = 50

# What survives. The floor is the point of the exercise: most entities have no
# counterpart in another vehicle, and a layer that always finds three is not
# reporting a resemblance, it is reporting a sort order.
#
# The cap on the receiving end matters as much as the one on the asking end. Small
# models produce hubs: a drone-base entity described in terms of everything it
# contains comes back as the counterpart of a dozen unrelated ones. An entity that
# is the analogue of twelve things is the analogue of nothing.
MAX_PER_SOURCE = 3
MAX_PER_TARGET = 2
MIN_COSINE = 0.55


def key_of(text: str) -> str:
    """A stable _key. Entity names carry characters a key may not."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", text)[:180].strip("_")
    return f"{slug}_{hashlib.sha1(text.encode()).hexdigest()[:10]}"


# ------------------------------------------------------------------- the corpus

# One row per comparable entity. Entities in more than one model are skipped:
# extraction merges by name, so those are already the same row in both models and
# there is nothing to draw an analogy between.
CORPUS = f"""
FOR e IN {config.ENTITIES}
  FILTER e.entity_type IN @roles
  FILTER LENGTH(e.models) == 1 AND IS_LIST(e.{config.EMBEDDING_FIELD})
  FILTER e.description != null AND e.description != ''
  RETURN {{key: e._key, entity_name: e.entity_name, role: e.entity_type,
           model: e.models[0], text: e.description,
           vector: e.{config.EMBEDDING_FIELD}}}"""


def corpus(db) -> list[dict]:
    return list(db.aql.execute(CORPUS, bind_vars={"roles": sorted(ROLES)}))


def stage(db, rows: list[dict]) -> int:
    """Write the corpus in the shape autograph's searches read.

    `embeddings` is plural here and `embedding` is singular in `all_docs` below,
    because that is how autograph has it: `SemanticSearch` reads `doc.embeddings`
    off the collection and `SimilarityFinder` reads `doc_data["embedding"]` off
    the dict it was passed. `content` is what `LexicalSearch` searches and BM25
    ranks, and `filename` is what an edge is labelled with.
    """
    config.drop_vector_indexes(db, STAGING)
    if db.has_collection(STAGING):
        db.collection(STAGING).truncate()
    else:
        db.create_collection(STAGING)
    coll = db.collection(STAGING)
    docs = [{"_key": r["key"], "filename": r["entity_name"], "content": r["text"],
             "embeddings": r["vector"], "name": r["entity_name"],
             "model": r["model"], "role": r["role"]} for r in rows]
    for i in range(0, len(docs), 500):
        coll.import_bulk(docs[i:i + 500], on_duplicate="replace")
    return coll.count()


def data_storage(db):
    """autograph's DataStorage, pointed at the local database.

    Its config classes read the environment in their class bodies, so
    EMBEDDING_DIM has to be set before `corpus_graph` is imported: the index is
    built for `EmbeddingConfig.DIMENSION` rather than for the length of the
    vectors it finds. `ARANGO_DEPLOYMENT_ENDPOINT` is assigned rather than
    defaulted -- it is the variable a shell is most likely to already hold,
    pointing at a hosted deployment, and deferring to it sends autograph's HTTP
    calls at a database this project has no business writing to.
    """
    import logging
    import os
    import sys

    os.environ["EMBEDDING_DIM"] = str(config.EMBED_DIM)
    os.environ["ARANGO_DEPLOYMENT_ENDPOINT"] = config.ARANGO_URL
    os.environ["db_name"] = config.DB_NAME
    os.environ["ARANGODB_USER"] = config.ARANGO_USER
    os.environ["ARANGODB_PASSWORD"] = config.ARANGO_PASS

    from corpus_graph.datastorage import DataStorage

    log = logging.getLogger("corpus_graph")
    log.handlers = [logging.StreamHandler(sys.stderr)]
    log.propagate = False
    return DataStorage(db)


def index(db) -> str:
    """autograph's own vector index and ArangoSearch view over the staging corpus.

    `create_arangosearch_view` builds `{collection}_search_view` on `content`,
    which is the exact name `LexicalSearch` queries, so the BM25 half of the
    search works without being told where to look. It addresses the database over
    HTTP with a bearer token rather than through the connection it was handed --
    ArangoDB mints an acceptable one itself at `/_open/auth`.
    """
    storage = data_storage(db)
    coll = db.collection(STAGING)
    storage.create_vector_index(coll)
    storage.create_arangosearch_view(coll, token=config.token())
    params = next((i.get("params", {}) for i in coll.indexes()
                   if i.get("type") == "vector"), {})
    return (f"nLists={params.get('nLists')}, "
            f"defaultNProbe={params.get('defaultNProbe')}, view {STAGING}_search_view")


# ------------------------------------------------------------------ the search


def find(db, rows: list[dict]) -> int:
    """Run autograph's SimilarityFinder once per (role, source model) group.

    Only the models that are not the largest drive a search. Every cross-model
    pair has at least one endpoint outside the largest model -- a pair with both
    endpoints inside it would not cross a model at all -- so driving from there
    reaches every pair while searching from far fewer entities. It also keeps the
    layer readable: driving from the Apollo side would hang three Apollo entities
    off every drone entity, since there are ten times more of them to choose from.

    The count moves by an edge or two between runs. `SimilarityFinder` searches on
    a thread pool and claims each pair under a lock, so when two entities are
    near-tied for the same counterpart the one that gets there first takes it.
    """
    from corpus_graph.similarity_finding import SimilarityFinder

    if db.has_collection(STAGING_EDGES):
        db.collection(STAGING_EDGES).truncate()
    else:
        db.create_collection(STAGING_EDGES, edge=True)
    edges = db.collection(STAGING_EDGES)
    finder = SimilarityFinder(db, db.collection(STAGING), top_k=SEARCH_TOP_K)

    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["model"]] += 1
    largest = max(counts, key=lambda m: counts[m])

    by_role: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_role[r["role"]].append(r)

    total = 0
    for _role, group in sorted(by_role.items()):
        ids_by_model = defaultdict(set)
        for r in group:
            ids_by_model[r["model"]].add(f"{STAGING}/{r['key']}")
        for source_model in sorted(m for m in ids_by_model if m != largest):
            sources = {r["entity_name"]: {"id": f"{STAGING}/{r['key']}",
                                          "embedding": r["vector"],
                                          "content": r["text"]}
                       for r in group if r["model"] == source_model}
            allowed = {i for m, ids in ids_by_model.items() if m != source_model
                       for i in ids}
            if not sources or not allowed:
                continue
            total += finder.create_similarity_relationships(
                sources, edges, top_k=SEARCH_TOP_K,
                # Stamped on every edge. The pair is stored with the
                # lexicographically smaller id first, so without this there is no
                # way back to which side asked the question.
                module=f"{_role}:{source_model}", module_doc_ids=allowed)
    return total


# ---------------------------------------------------------------- the promotion


CANDIDATES = f"""
FOR e IN {STAGING_EDGES}
  LET a = DOCUMENT(e._from), b = DOCUMENT(e._to)
  FILTER a != null AND b != null
  LET source_model = SPLIT(e.module, ':')[1]
  LET src = a.model == source_model ? a : b
  LET dst = a.model == source_model ? b : a
  LET cosine = COSINE_SIMILARITY(a.embeddings, b.embeddings)
  FILTER cosine >= @floor
  SORT cosine DESC
  RETURN {{src: src._key, src_name: src.name, src_model: src.model,
           dst: dst._key, dst_name: dst.name, dst_model: dst.model,
           role: SPLIT(e.module, ':')[0], rank: e.rank,
           rrf_score: e.rrf_score, cosine: cosine}}"""


def promote(db, floor: float = MIN_COSINE, per_source: int = MAX_PER_SOURCE,
            per_target: int = MAX_PER_TARGET) -> list[dict]:
    """Staging edges -> SIMILAR_TO edges between the real entities.

    Two cuts on the way. Reciprocal pairs go first: when neither model is the
    largest, both drive a search, so A finds B and B finds A and the same
    resemblance is stated twice in opposite directions. `SimilarityFinder`
    deduplicates within one call and these are two calls, so it cannot see them.
    An analogy is symmetric and the retriever matches an edge from either end, so
    one edge per unordered pair is the whole truth. Then the floor and the
    per-entity caps.

    `order` is 0 and `weight` is the cosine, which is how the local retriever
    sorts the relations it found (`order` ascending, then `weight` descending).
    That puts an analogy ahead of the authored relations of the entity it hangs
    off -- deliberately, because those relations are already spelled out in the
    entity's own description, and the analogy is the only thing in the context
    that came from another model.
    """
    ranked = list(db.aql.execute(CANDIDATES, bind_vars={"floor": floor}))

    seen: set[frozenset[str]] = set()
    best: list[dict] = []
    for row in ranked:                       # already sorted by cosine descending
        pair = frozenset((row["src"], row["dst"]))
        if pair not in seen:
            seen.add(pair)
            best.append(row)

    # One pass, strongest first, taking a pair only while both ends have room.
    # Both caps have to be applied together: cutting by source first and by target
    # afterwards throws away a weak entity's only analogy to make room for a
    # strong entity's third.
    out_degree: dict[str, int] = defaultdict(int)
    in_degree: dict[str, int] = defaultdict(int)
    rows = []
    for row in best:
        if out_degree[row["src"]] >= per_source or in_degree[row["dst"]] >= per_target:
            continue
        out_degree[row["src"]] += 1
        in_degree[row["dst"]] += 1
        rows.append(row)

    db.aql.execute(f"FOR r IN {config.RELATIONS} FILTER r.type == @t "
                   f"REMOVE r IN {config.RELATIONS}", bind_vars={"t": config.SIMILAR_TO})
    edges = [{
        "_key": key_of(f"analogy:{r['src']}:{r['dst']}"),
        "_from": f"{config.ENTITIES}/{r['src']}",
        "_to": f"{config.ENTITIES}/{r['dst']}",
        "type": config.SIMILAR_TO,
        "description": (
            f"{r['src_name']} in the {r['src_model']} model plays a role like "
            f"{r['dst_name']} in the {r['dst_model']} model: both are {r['role']} "
            f"elements and their descriptions match at cosine {r['cosine']:.2f}."),
        "weight": round(r["cosine"], 4), "order": 0,
        "analogy_role": r["role"], "cosine": r["cosine"],
        "rrf_score": r["rrf_score"], "rank": r["rank"],
    } for r in rows]
    coll = db.collection(config.RELATIONS)
    for i in range(0, len(edges), 500):
        coll.import_bulk(edges[i:i + 500], on_duplicate="replace")
    return rows


def cleanup(db) -> None:
    """Drop the staging corpus once its edges have been promoted.

    Not housekeeping. AQLizer writes its query against whatever collections the
    schema shows it, and `sysml_AnalogySimilarTo` reads like the obvious place to
    look for an analogy -- it picked it over the real edge collection and returned
    nothing. Scratch that outlives the step it belongs to is a decoy.

    Pass `keep=True` to `main` when tuning `promote`, which reads staging and is
    the one thing worth re-running without repeating the search.
    """
    view = f"{STAGING}_search_view"
    if any(v["name"] == view for v in db.views()):
        db.delete_view(view)
    for name in (STAGING_EDGES, STAGING):
        if db.has_collection(name):
            db.delete_collection(name)


# ------------------------------------------------------------------------- main


def main(keep: bool = False) -> None:
    db = config.db()
    rows = corpus(db)
    by_model: dict[str, int] = defaultdict(int)
    for r in rows:
        by_model[r["model"]] += 1
    print(f"  corpus  {len(rows)} comparable entities  "
          + "  ".join(f"{m}={n}" for m, n in sorted(by_model.items())))
    print(f"  staged {stage(db, rows)} documents   {index(db)}")
    print(f"  autograph found {find(db, rows)} candidate pairs")
    kept = promote(db)
    print(f"  kept {len(kept)} analogy edges above cosine {MIN_COSINE}")
    for r in kept[:8]:
        print(f"    {r['cosine']:.3f}  {r['src_name']} ({r['src_model']})"
              f"  ~  {r['dst_name']} ({r['dst_model']})")
    if not keep:
        cleanup(db)


if __name__ == "__main__":
    main()
