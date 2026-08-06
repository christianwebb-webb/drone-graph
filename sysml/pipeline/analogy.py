"""Cross-model analogy edges, found by autograph's own SimilarityFinder.

Parsing relates elements that a file says are related. It cannot relate two
vehicles to each other, because no Apollo file mentions a drone -- so the graph
this pipeline built through `enrich` has no edge and no community that crosses a
model boundary. Everything shares one embedding space, which is enough for a
`unified` question to pull both models into one answer, but there is nothing to
query and nothing to rank: "what plays the drone battery's role in Apollo?" has
no path to walk.

autograph already solves "which of these resemble each other" in
`corpus_graph.similarity_finding.SimilarityFinder`: semantic search and BM25 over
the same corpus, fused with reciprocal rank, top_k kept. That class runs here
unmodified, against the local container, exactly as `enrich` already runs
autograph's DataStorage.

Two things had to be arranged around it rather than changed inside it.

**Granularity.** autograph's corpus layer compares whole documents, and it reads
only the first `CorpusGraphConfig.CHUNK_MAX_CHARS` (1200 tokens x 4 = 4800
characters) of each. The largest file here is 58,902 bytes, so a document-level
run would be comparing 8% of it, and the answer would be "these two files
resemble each other" when the question is "which part plays which part's role".
So the corpus handed to it is one document per SysML *element*. Every element is
far inside the truncation bound, and the edges land where the question is.

**Direction.** `SimilarityFinder` restricts candidates with `module_doc_ids`,
which it applies before fusion so that a foreign document cannot consume the
top_k window -- its purpose upstream is to keep edges *inside* a module. The same
argument runs backwards: pass the elements of the *other* models and the only
edges it can build are the ones that cross. It is run once per (role, model)
group, with `module` stamped on every edge so the group is recoverable
afterwards.

Elements are compared only against the same role -- a part against a part, a
requirement against a requirement. Without that gate the nearest neighbour of a
part is regularly a requirement that talks about the part, which is a topical
match and not an analogy.

    python -m sysml.pipeline.analogy
"""

from __future__ import annotations

import json
from collections import defaultdict

from .. import config
from . import enrich
from .project import describe, key_of

# The corpus autograph is handed, and the edges it writes into. Both are staging:
# they are rebuilt from scratch on every run and nothing outside this file reads
# them. The result is promoted into the importer's own edge collection at the end.
STAGING = f"{config.PROJECT}_AnalogyCorpus"
STAGING_EDGES = f"{config.PROJECT}_AnalogySimilarTo"

# Metatypes are compared with their own kind, after dropping the Definition/Usage
# suffix so `PartDefinition` and `PartUsage` count as one role.
#
# Attribute, Package, Enumeration, Metadata, Snapshot and Timeslice are left out.
# An attribute's whole description is often a number and a unit, so every mass
# resembles every other mass; a package is a container and resembles whatever it
# contains; a snapshot is an occurrence of an element already compared on its own.
ROLES = {
    "Part", "Action", "Requirement", "State", "Port", "Item", "Interface",
    "Connection", "Constraint", "Calc", "Analysis", "Flow", "Allocation",
    "UseCase", "Verification", "Concern", "View", "Viewpoint", "Event",
}

# How wide autograph searches. This is not the number of edges kept: the module
# restriction is applied to the raw search results before fusion, so a narrow
# top_k would be spent almost entirely on same-model neighbours -- an element's
# closest matches are overwhelmingly its own siblings -- and nothing would
# survive to fuse. Searching wide and cutting afterwards is what leaves real
# cross-model candidates in the window. `rank` on the resulting edge is already a
# rank among valid candidates, because the restriction ran first.
SEARCH_TOP_K = 50

# What survives. The floor is the point of the exercise: most elements have no
# counterpart in another vehicle, and a layer that always finds three is not
# reporting a resemblance, it is reporting a sort order.
#
# The cap on the receiving end matters as much as the one on the asking end.
# Small models produce hubs: drone-base has seven comparable elements, its `drone`
# is described in terms of everything it contains, and so it comes back as the
# counterpart of a dozen unrelated drone-logical elements. An element that is the
# analogue of twelve things is the analogue of nothing.
MAX_PER_SOURCE = 3
MAX_PER_TARGET = 2
MIN_COSINE = 0.55


# Relations whose target's own words are folded into the source's text. A SysML
# usage carries almost nothing by itself -- `engines : RocketEngine` says that it
# exists and what it is, and everything about what a rocket engine *does* sits on
# the definition, which is a separate element with its own doc, attributes and
# constraints. Without this, 31% of the corpus is under 60 characters and matching
# falls back on names and on shared sentence shape. One hop is where a reader
# would look too.
INHERIT = ("typedBy", "specializes")


def role_of(metatype: str) -> str | None:
    for suffix in ("Definition", "Usage"):
        if metatype.endswith(suffix):
            metatype = metatype[: -len(suffix)]
            break
    return metatype if metatype in ROLES else None


# ------------------------------------------------------------------- the corpus


def corpus(model: dict) -> list[dict]:
    """One row per comparable element, described without its provenance.

    Every non-library element is described, not only the comparable ones, because
    a comparable element inherits the text of what it is typed by and that
    definition may be of a role nothing is ever compared against.
    """
    by_qn = {e["qualifiedName"]: e for e in model["elements"]}
    outgoing: dict[str, list[tuple[str, str]]] = defaultdict(list)
    inherits: dict[str, list[str]] = defaultdict(list)
    for r in model["relations"]:
        target = by_qn.get(r["to"])
        outgoing[r["from"]].append((r["type"], target["name"] if target else r["to"]))
        if r["type"] in INHERIT and target is not None:
            inherits[r["from"]].append(r["to"])

    described = {
        e["qualifiedName"]: describe(e, outgoing[e["qualifiedName"]], provenance=False)
        for e in model["elements"] if not e["isLibrary"]
    }

    rows = []
    for element in model["elements"]:
        qn = element["qualifiedName"]
        role = role_of(element["metatype"])
        if element["isLibrary"] or role is None:
            continue
        text = described[qn]
        for target in dict.fromkeys(inherits[qn]):   # one hop, each type once
            if target != qn and target in described:
                text += f" {by_qn[target]['name']} is: {described[target]}"
        rows.append({
            "key": key_of(qn), "entity_name": qn, "name": element["name"],
            "model": element["model"], "role": role, "text": text,
        })
    return rows


def stage(db, rows: list[dict], vectors: dict[str, list[float]]) -> int:
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
    docs = [{
        "_key": r["key"], "filename": r["entity_name"], "content": r["text"],
        "embeddings": vectors[enrich._hash(r["text"])],
        "name": r["name"], "model": r["model"], "role": r["role"],
    } for r in rows if enrich._hash(r["text"]) in vectors]
    for i in range(0, len(docs), 500):
        coll.import_bulk(docs[i:i + 500], on_duplicate="replace")
    return coll.count()


def index(db) -> str:
    """autograph's own vector index and ArangoSearch view over the staging corpus.

    `create_arangosearch_view` builds `{collection}_search_view` on `content`,
    which is the exact name `LexicalSearch` queries, so the BM25 half of the
    search works without being told where to look. It addresses the database over
    HTTP with a bearer token rather than through the connection it was handed --
    ArangoDB mints an acceptable one itself at `/_open/auth`.
    """
    from ..nl import token

    storage = enrich.data_storage(db)
    coll = db.collection(STAGING)
    storage.create_vector_index(coll)
    storage.create_arangosearch_view(coll, token=token())
    params = next((i.get("params", {}) for i in coll.indexes()
                   if i.get("type") == "vector"), {})
    return (f"nLists={params.get('nLists')}, "
            f"defaultNProbe={params.get('defaultNProbe')}, view {STAGING}_search_view")


# ------------------------------------------------------------------ the search


def edge_collection(db, name: str):
    if db.has_collection(name):
        db.collection(name).truncate()
    else:
        db.create_collection(name, edge=True)
    return db.collection(name)


def find(db, rows: list[dict], vectors: dict[str, list[float]]) -> int:
    """Run autograph's SimilarityFinder once per (role, source model) group.

    Only the models that are not the largest drive a search. Every cross-model
    pair has at least one endpoint outside the largest model -- a pair with both
    endpoints inside it would not cross a model at all -- so driving from there
    reaches every pair while searching from 198 elements instead of 2,359. It
    also keeps the layer readable: driving from the Apollo side would hang three
    Apollo elements off every drone element, since there are ten times more of
    them to choose from.

    The count moves by an edge or two between runs. `SimilarityFinder` searches on
    a thread pool and claims each pair under a lock, so when two elements are
    near-tied for the same counterpart the one that gets there first takes it.
    Everything above the floor by a clear margin is stable; the last few rows are
    not.
    """
    from corpus_graph.similarity_finding import SimilarityFinder

    staging = db.collection(STAGING)
    edges = edge_collection(db, STAGING_EDGES)
    finder = SimilarityFinder(db, staging, top_k=SEARCH_TOP_K)

    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["model"]] += 1
    largest = max(counts, key=lambda m: counts[m])

    by_role: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_role[r["role"]].append(r)

    total = 0
    for role, group in sorted(by_role.items()):
        targets_by_model = defaultdict(set)
        for r in group:
            targets_by_model[r["model"]].add(f"{STAGING}/{r['key']}")
        for source_model in sorted(m for m in targets_by_model if m != largest):
            sources = {
                r["entity_name"]: {
                    "id": f"{STAGING}/{r['key']}",
                    "embedding": vectors[enrich._hash(r["text"])],
                    "content": r["text"],
                }
                for r in group
                if r["model"] == source_model and enrich._hash(r["text"]) in vectors
            }
            allowed = {i for m, ids in targets_by_model.items() if m != source_model
                       for i in ids}
            if not sources or not allowed:
                continue
            total += finder.create_similarity_relationships(
                sources, edges, top_k=SEARCH_TOP_K,
                # Stamped on every edge. The pair is stored with the
                # lexicographically smaller id first, so without this there is no
                # way back to which side asked the question.
                module=f"{role}:{source_model}", module_doc_ids=allowed)
    return total


# ----------------------------------------------------------------- the promotion


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

    Three cuts on the way. Reciprocal pairs go first: when neither model is the
    largest, both drive a search, so A finds B and B finds A and the same
    resemblance is stated twice in opposite directions. `SimilarityFinder`
    deduplicates within one call and these are two calls, so it cannot see them.
    An analogy is symmetric and the retriever matches an edge from either end, so
    one edge per unordered pair is the whole truth.

    Then elements sharing a name are collapsed: five distinct Apollo elements are
    named `power`, and without this they take every slot and say one thing five
    times. Then the floor and the per-element cap.

    `order` is 0 and `weight` is the cosine, which is how the local retriever
    sorts the relations it found (`order` ascending, then `weight` descending).
    That puts an analogy ahead of the authored relations of the element it hangs
    off -- deliberately, because those relations are already spelled out in the
    element's own description, and the analogy is the only thing in the context
    that came from another model.
    """
    ranked = list(db.aql.execute(CANDIDATES, bind_vars={"floor": floor}))

    seen: set[frozenset[str]] = set()
    best: dict[tuple[str, str], dict] = {}
    for row in ranked:                       # already sorted by cosine descending
        pair = frozenset((row["src"], row["dst"]))
        if pair in seen:
            continue
        seen.add(pair)
        best.setdefault((row["src"], row["dst_name"]), row)

    # One pass, strongest first, taking a pair only while both ends have room.
    # Both caps have to be applied together: cutting by source first and by target
    # afterwards throws away a weak element's only analogy to make room for a
    # strong element's third.
    out_degree: dict[str, int] = defaultdict(int)
    in_degree: dict[str, int] = defaultdict(int)
    rows = []
    for row in sorted(best.values(), key=lambda r: -r["cosine"]):
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
    for view in (f"{STAGING}_search_view",):
        if any(v["name"] == view for v in db.views()):
            db.delete_view(view)
    for name in (STAGING_EDGES, STAGING):
        if db.has_collection(name):
            db.delete_collection(name)


# ------------------------------------------------------------------------- main


def main(keep: bool = False) -> None:
    db = config.db()
    model = json.loads(config.MODEL_JSON.read_text(encoding="utf-8"))

    rows = corpus(model)
    by_model: dict[str, int] = defaultdict(int)
    for r in rows:
        by_model[r["model"]] += 1
    print(f"corpus  {len(rows)} comparable elements  "
          + "  ".join(f"{m}={n}" for m, n in sorted(by_model.items())))

    vectors = enrich.load_cache()
    enrich.embed_texts([r["text"] for r in rows], vectors)
    enrich.save_cache(vectors)
    print(f"  staged {stage(db, rows, vectors)} documents   {index(db)}")

    print(f"  autograph found {find(db, rows, vectors)} candidate pairs")
    kept = promote(db)
    print(f"  kept {len(kept)} analogy edges above cosine {MIN_COSINE}")
    for r in sorted(kept, key=lambda r: -r["cosine"])[:8]:
        print(f"    {r['cosine']:.3f}  {r['src_name']} ({r['src_model']})"
              f"  ~  {r['dst_name']} ({r['dst_model']})")
    if not keep:
        cleanup(db)


if __name__ == "__main__":
    main()
