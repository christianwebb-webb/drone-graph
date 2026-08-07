"""Paths, connection settings, the ontology, and the collection names.

Names, artifact file names and the edge vocabulary are imported from
graphrag_importer rather than restated here. Restating them is how they drift:
this file used to claim `HAS_PARENT` was part of the importer's closed set, and it
never was -- the importer's community-hierarchy edge is `SUB_COMMUNITY_OF`, so
every parent edge written under the old name was invisible to an importer-side
reader.

Everything points at a local ArangoDB container. Nothing here touches a shared or
hosted deployment.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from arango import ArangoClient
from arango.database import StandardDatabase

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
OUT = ROOT / "out"
KG = OUT / "kg"                  # GraphRAG's working_dir: extraction artifacts + LLM cache
AQL_EXAMPLES = Path(__file__).resolve().parent / "aql_examples.md"
# The same file written by `pipeline.examples` instead of by hand. It is a second
# file rather than a replacement: the read side takes AQL_EXAMPLES unless it is
# told otherwise, so generating one cannot change an answer by accident.
AQL_EXAMPLES_GENERATED = Path(__file__).resolve().parent / "aql_examples_generated.md"

# The four Arango repos this project reads, cloned next to it.
IMPORTER_REPO = ROOT.parent / "graphrag_importer"
AUTOGRAPH_REPO = ROOT.parent / "autograph"
SERVICE_REPO = ROOT.parent / "natural-language-service"
RETRIEVER_REPO = ROOT.parent / "graphrag_retrievers"

ARANGO_URL = os.environ.get("ARANGO_URL", "http://localhost:8529")
ARANGO_USER = os.environ.get("ARANGO_USER", "root")
ARANGO_PASS = os.environ.get("ARANGO_PASSWORD", "testpass")
DB_NAME = os.environ.get("SYSML_DB", "dronegraph")

# Every importer collection name is built from the GenAI project name, in a class
# body that reads the environment at import time -- so this has to be set first.
PROJECT = os.environ.get("SYSML_PROJECT", "sysml")
os.environ.setdefault("GENAI_PROJECT_NAME", PROJECT)
if str(IMPORTER_REPO) not in sys.path:
    sys.path.insert(0, str(IMPORTER_REPO))
try:
    from graphrag.naming import CollectionNames, FileNames, IndexNames, RelationshipTypes
except ImportError as exc:  # pragma: no cover - a missing clone, not a code path
    raise RuntimeError(
        f"cannot import graphrag.naming ({exc}). Clone graphrag_importer next to "
        "this project -- the collection names and edge vocabulary come from it."
    ) from exc

DOCUMENTS = CollectionNames.DOCUMENTS
CHUNKS = CollectionNames.CHUNKS
ENTITIES = CollectionNames.ENTITIES
COMMUNITIES = CollectionNames.COMMUNITIES
RELATIONS = CollectionNames.get_edge_collection()
GRAPH = f"{PROJECT}_kg"

# SemanticUnits is in the importer's list and nothing here fills it, so the
# vertex collections are named rather than taken wholesale.
VERTEX_COLLECTIONS = (DOCUMENTS, CHUNKS, ENTITIES, COMMUNITIES)
ALL_COLLECTIONS = VERTEX_COLLECTIONS + (RELATIONS,)

# The five artifacts extraction leaves in KG, named by the importer's own class so
# the two halves cannot disagree about what the files are called.
ARTIFACTS = FileNames

# The field the importer vector-indexes: "embedding", singular. autograph writes
# "embeddings". Writing the wrong one is invisible -- the rows are there, the
# vectors are there, and every vector query returns nothing.
EMBEDDING_FIELD = IndexNames.EMBEDDING_FIELD
VECTOR_INDEX = IndexNames.VECTOR_COSINE

# Fixed by the importer, not chosen here: `openai_embedding` calls
# text-embedding-3-small with no `dimensions` argument, so vectors are always
# 1536 wide (graphrag/graph_builder/builder/_llm.py). The retrievers have to be
# told the same number or their queries return nothing.
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536
CHAT_MODEL = os.environ.get("CHAT_MODEL", "gpt-4o")

# Only `pipeline.examples` uses this one, and it is deliberately not CHAT_MODEL.
# Writing the examples file happens once per import and is the one call whose
# output every later question depends on, so it is worth the strongest model
# available; answering a question is a per-question cost and stays on CHAT_MODEL.
EXAMPLES_MODEL = os.environ.get("EXAMPLES_MODEL", "gpt-5.5")

SUB_COMMUNITY_OF = RelationshipTypes.SUB_COMMUNITY_OF
EDGE_TYPES = tuple(sorted(RelationshipTypes.get_expected_types() | {SUB_COMMUNITY_OF}))

# The analogy layer's edge label, and it is deliberately not the importer's. An
# analogy is not something a SysML file states, so it cannot be a RELATED_TO
# carrying a `relationship_type` -- that would make a resemblance we computed
# indistinguishable from a relation an engineer wrote, and would land in every
# count that groups by `relationship_type`. SIMILAR_TO is autograph's own label
# for "these two were found to resemble each other", which is exactly this.
if str(AUTOGRAPH_REPO) not in sys.path:
    sys.path.insert(0, str(AUTOGRAPH_REPO))
try:
    from corpus_graph.naming import EDGE_LABEL_SIMILAR_TO
except ImportError as exc:  # pragma: no cover - a missing clone, not a code path
    raise RuntimeError(
        f"cannot import corpus_graph.naming ({exc}). Clone autograph next to this "
        "project -- the similarity edge label comes from it."
    ) from exc

SIMILAR_TO = EDGE_LABEL_SIMILAR_TO

# One model per top-level entry under models/: a directory is a model, and so is a
# loose .sysml file. Derived rather than listed, so adding a model is dropping a
# folder in rather than editing this file.
#
# The list is also the boundary that decides what may merge with what. Extraction
# merges entities by name within a run, so two models extracted together share a
# `Battery`; extracted apart they do not. Each model gets its own workbench and its
# own `import_number`, which `_generate_key` puts in every document key
# (graphrag/importer/import_graph_to_adb.py:391) so the same word in two models is
# two rows. Correspondence between models is what the analogy layer is for, and it
# says "resembles", not "is".
MODEL_NAMES = tuple(sorted(
    p.name if p.is_dir() else p.stem
    for p in MODELS.iterdir() if p.is_dir() or p.suffix == ".sysml"
))


def model_of(relative_path: str) -> str:
    head = relative_path.split("/")[0]
    return head[: -len(".sysml")] if head.endswith(".sysml") else head


def import_number(model: str) -> int:
    """A model's slot in every document key. Sorted, so it is stable across runs
    and only shifts if a model is added or removed -- which needs a fresh build."""
    return MODEL_NAMES.index(model)


def kg(model: str) -> Path:
    """One GraphRAG working directory per model: artifacts and LLM cache."""
    return KG / model


# --------------------------------------------------------------------- ontology

# The two lists handed to the extraction pipeline, and the only thing this project
# tells it about SysML. `entity_types` and `relationship_types` are constructor
# arguments on GraphRAG; with `enable_strict_types` an extracted entity or edge
# whose type is not on the matching list is dropped rather than renamed, so these
# are a closed vocabulary and not a hint.
#
# Both are the vocabulary the hand-written parser used to recognise in the
# grammar, moved out of Python and into the prompt. Nothing else changed about
# them: the same 27 declaration kinds, the same 18 relations.
KINDS = [
    "Package", "Part", "Action", "State", "Port", "Item", "Attribute",
    "Requirement", "Calc", "Analysis", "Connection", "Interface", "View",
    "Viewpoint", "Enumeration", "Concern", "Constraint", "Flow", "Allocation",
    "Event", "Metadata", "UseCase", "Rendering", "Verification", "Snapshot",
    "Timeslice", "Occurrence",
]

RELATIONS_ONTOLOGY = [
    "owns", "typedBy", "specializes", "redefines", "satisfies", "refines",
    "derives", "performs", "subject", "exhibits", "connects", "transitionsTo",
    "variantOf", "imports", "sliceOf", "sends", "dependsOn", "valueRef",
]

# What both come back as. `_merge_nodes_then_upsert` lowercases the winning type
# before it stores it, so `UseCase` is written as `usecase` and `typedBy` as
# `typedby` -- which is what a query has to match on.
ENTITY_TYPES = [k.lower() for k in KINDS]
RELATIONSHIP_TYPES = [r.lower() for r in RELATIONS_ONTOLOGY]


# ------------------------------------------------------------------ connections


def _env_file() -> dict[str, str]:
    """The `env` file sitting next to the cloned repos, parsed as KEY=VALUE lines.

    Nothing here is exported wholesale -- the file also carries a deployment
    password, and the only value wanted is the OpenAI key.
    """
    for name in ("env", ".env"):
        path = ROOT.parent / name
        if path.is_file():
            break
    else:
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip("\"'")
    return values


def openai_key() -> str:
    from_file = _env_file()
    key = (
        from_file.get("CHAT_API_KEY")
        or from_file.get("OPENAI_API_KEY")
        or os.environ.get("CHAT_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    if not key:
        raise RuntimeError(
            "no OpenAI key: put CHAT_API_KEY (or OPENAI_API_KEY) in the `env` file "
            "beside the cloned repos, or export it"
        )
    # The extraction half builds its own `AsyncOpenAI()` with no arguments, which
    # reads OPENAI_API_KEY and nothing else.
    os.environ["OPENAI_API_KEY"] = key
    return key


# Anything that writes. Two places run AQL that a language model wrote -- `nl`
# runs what AQLizer generated for a question, and `pipeline.examples` runs the
# worked examples out of a generated primer to check them -- and neither may be
# allowed to reach the database with a mutation. The service's own read-only gate
# is a substring test (`if op in query.upper()`), so it refuses "orbit INSERTion"
# and passes a `FOR c IN [...] TRUNCATE c`. This is the one that decides.
MUTATION = re.compile(r"\b(INSERT|UPDATE|REPLACE|REMOVE|UPSERT|TRUNCATE)\b", re.I)


def client() -> ArangoClient:
    return ArangoClient(hosts=ARANGO_URL)


def db(create: bool = False) -> StandardDatabase:
    c = client()
    if create:
        sys_db = c.db("_system", username=ARANGO_USER, password=ARANGO_PASS)
        if not sys_db.has_database(DB_NAME):
            sys_db.create_database(DB_NAME)
    return c.db(DB_NAME, username=ARANGO_USER, password=ARANGO_PASS)


def token() -> str:
    """A database JWT. Both the importer and the retrievers authenticate with one
    rather than a password, and ArangoDB issues an acceptable one itself."""
    import requests

    r = requests.post(f"{ARANGO_URL}/_open/auth", timeout=15,
                      json={"username": ARANGO_USER, "password": ARANGO_PASS})
    r.raise_for_status()
    return r.json()["jwt"]


def drop_vector_indexes(db, collection: str) -> int:
    """Remove any vector index on a collection before rewriting it.

    ArangoDB refuses to insert a document without the indexed vector field once a
    vector index exists, so a re-run of an earlier stage fails with a bare
    "bad parameter" unless the index is dropped first.
    """
    if not db.has_collection(collection):
        return 0
    coll = db.collection(collection)
    dropped = [i for i in coll.indexes() if i.get("type") == "vector"]
    for idx in dropped:
        coll.delete_index(idx["id"])
    return len(dropped)


def drop_database() -> bool:
    sys_db = client().db("_system", username=ARANGO_USER, password=ARANGO_PASS)
    if sys_db.has_database(DB_NAME):
        sys_db.delete_database(DB_NAME)
        return True
    return False
