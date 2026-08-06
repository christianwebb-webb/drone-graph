"""Paths, connection settings and the collection names the importer schema uses.

Everything points at a local ArangoDB container. Nothing here touches a shared or
hosted deployment.
"""

from __future__ import annotations

import os
from pathlib import Path

from arango import ArangoClient
from arango.database import StandardDatabase

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
OUT = ROOT / "out"
MODEL_JSON = OUT / "model.json"
AQL_EXAMPLES = Path(__file__).resolve().parent / "aql_examples.md"

ARANGO_URL = os.environ.get("ARANGO_URL", "http://localhost:8529")
ARANGO_USER = os.environ.get("ARANGO_USER", "root")
ARANGO_PASS = os.environ.get("ARANGO_PASSWORD", "testpass")
DB_NAME = os.environ.get("SYSML_DB", "dronegraph")

# graphrag_importer builds every collection name from the GenAI project name
# (graphrag/naming.py: CollectionNames). Using its convention means an
# importer-side reader finds the collections where it expects them.
PROJECT = os.environ.get("SYSML_PROJECT", "sysml")
DOCUMENTS = f"{PROJECT}_Documents"
CHUNKS = f"{PROJECT}_Chunks"
ENTITIES = f"{PROJECT}_Entities"
COMMUNITIES = f"{PROJECT}_Communities"
RELATIONS = f"{PROJECT}_Relations"
GRAPH = f"{PROJECT}_kg"

VERTEX_COLLECTIONS = (DOCUMENTS, CHUNKS, ENTITIES, COMMUNITIES)
ALL_COLLECTIONS = VERTEX_COLLECTIONS + (RELATIONS,)

# The importer vector-indexes IndexNames.EMBEDDING_FIELD, which is "embedding" --
# singular. autograph writes "embeddings". Writing the wrong one is invisible: the
# rows are there, the vectors are there, and every vector query returns nothing.
EMBEDDING_FIELD = "embedding"
EMBED_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBED_DIM = int(os.environ.get("EMBEDDING_DIM", "768"))
# How many index partitions an APPROX_NEAR_COSINE search opens. Left at the default
# of 1 the search misses most of the collection and silently returns fewer rows
# than the LIMIT asked for.
N_PROBE = int(os.environ.get("VECTOR_N_PROBE", "24"))
CHAT_MODEL = os.environ.get("CHAT_MODEL", "gpt-4o")

# The importer's closed edge vocabulary (RelationshipTypes.get_expected_types).
# A sixth value is not allowed, which is why the authored SysML relation lives in
# relationship_type instead -- the schema's own field for exactly that.
EDGE_TYPES = ("PART_OF", "MENTIONED_IN", "RELATED_TO", "IN_COMMUNITY", "HAS_PARENT")

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
