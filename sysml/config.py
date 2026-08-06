"""Paths, connection settings and the collection names the importer schema uses.

The names and the edge vocabulary are imported from graphrag_importer rather than
restated here. Restating them is how they drift: this file used to claim
`HAS_PARENT` was part of the importer's closed set, and it never was -- the
importer's community-hierarchy edge is `SUB_COMMUNITY_OF`, so every parent edge
written under the old name was invisible to an importer-side reader.

Everything points at a local ArangoDB container. Nothing here touches a shared or
hosted deployment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from arango import ArangoClient
from arango.database import StandardDatabase

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
OUT = ROOT / "out"
MODEL_JSON = OUT / "model.json"
AQL_EXAMPLES = Path(__file__).resolve().parent / "aql_examples.md"

# The three Arango repos this project reads, cloned next to it.
IMPORTER_REPO = ROOT.parent / "graphrag_importer"
AUTOGRAPH_REPO = ROOT.parent / "autograph"
SERVICE_REPO = ROOT.parent / "natural-language-service"

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
    from graphrag.naming import CollectionNames, IndexNames, RelationshipTypes
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

# The field the importer vector-indexes: "embedding", singular. autograph writes
# "embeddings". Writing the wrong one is invisible -- the rows are there, the
# vectors are there, and every vector query returns nothing.
EMBEDDING_FIELD = IndexNames.EMBEDDING_FIELD
EMBED_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
# autograph's EmbeddingConfig reads this same variable, and the vector index is
# built for whatever it says rather than for the width of the vectors it finds.
EMBED_DIM = int(os.environ.get("EMBEDDING_DIM", "768"))
CHAT_MODEL = os.environ.get("CHAT_MODEL", "gpt-4o")

# `type` on an edge is closed, which is why the authored SysML relation lives in
# `relationship_type` instead -- the schema's own field for exactly that.
# SUB_COMMUNITY_OF is left out of the validation set upstream but is written by
# import_graph_to_adb and walked by the delete engine, so it counts as in-schema.
SUB_COMMUNITY_OF = RelationshipTypes.SUB_COMMUNITY_OF
EDGE_TYPES = tuple(sorted(RelationshipTypes.get_expected_types() | {SUB_COMMUNITY_OF}))

# Which source tree each file belongs to, and the label used everywhere downstream.
MODELS_INDEX = {
    "apollo-11-sysml-v2": "apollo-11",
    "DroneModelLogical.sysml": "drone-logical",
    "Drone_BaseArchitecture.sysml": "drone-base",
}


def openai_key() -> str:
    key = os.environ.get("CHAT_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("set CHAT_API_KEY (or OPENAI_API_KEY) to an OpenAI key")
    return key


def client() -> ArangoClient:
    return ArangoClient(hosts=ARANGO_URL)


def db(create: bool = False) -> StandardDatabase:
    c = client()
    if create:
        sys_db = c.db("_system", username=ARANGO_USER, password=ARANGO_PASS)
        if not sys_db.has_database(DB_NAME):
            sys_db.create_database(DB_NAME)
    return c.db(DB_NAME, username=ARANGO_USER, password=ARANGO_PASS)


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
