"""out/kg -> ArangoDB, with graphrag_importer's own writer.

`ImportGraphToADB` is the second half of the importer: it creates the graph and
the five collections, reads the workbench artifacts and writes Documents, Chunks,
Entities, Communities and every edge between them, then builds the vector indexes.
It is the same class the platform's importer pods run, given three things a pod
gets from its surroundings instead: a database JWT (ArangoDB issues one at
/_open/auth), a no-op progress sink, and a local URL.

Two things are added afterwards, both before the vector indexes are built --
creating an entity is impossible once the index exists.

`label` writes down which file and model each row came from. The importer already
connects Entity -> Chunk -> Document, so that is answerable by walking two hops;
resolving the walk once means an analytical question is a filter instead.

`structure` reads the sources again with a lexer, for the two things extraction is
not reliable about: attribute values, and the containment and typing the syntax
states outright.

    python -m sysml.pipeline.load
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import sys

from .. import config
from . import structure


def importer(system: str):
    """graphrag_importer's writer, pointed at the local container.

    Two shims, both planted as module globals because that is what the import
    path resolves against.

    `update_service_status` is a gRPC call to the platform's metadata service
    announcing progress. There is nothing here to announce it to, and left alone
    it retries against an address that does not resolve.

    `open` is replaced because the two halves disagree about encoding on Windows.
    The extraction half writes its artifacts as UTF-8 with `ensure_ascii=False`
    (_utils.py:142) and the writer reads them back with a bare `open(path)`, which
    takes the platform default -- cp1252 here, UTF-8 on the Linux pods this
    normally runs on, which is why it has never shown up. The first non-ASCII
    character in a source file stops the import.
    """
    for name in ("graphrag", "arango-graphrag", "vectordb"):
        log = logging.getLogger(name)
        log.handlers = [logging.StreamHandler(sys.stderr)]
        log.propagate = False

    import graphrag.importer.import_graph_to_adb as writer

    async def no_status(*_args, **_kwargs) -> None:
        """Progress goes to the GenAI metadata store; nothing here watches it."""

    def utf8_open(file, mode="r", *args, **kwargs):
        if "b" not in mode:
            kwargs.setdefault("encoding", "utf-8")
        return builtins.open(file, mode, *args, **kwargs)

    writer.update_service_status = no_status
    writer.open = utf8_open

    from graphrag.graph_builder.builder._llm import openai_embedding

    config.openai_key()
    return writer.ImportGraphToADB(
        path_to_files=str(config.kg(system)),
        arangodb_url=config.ARANGO_URL,
        db_name=config.DB_NAME,
        # In every document key the writer generates
        # (graphrag/importer/import_graph_to_adb.py:391), which is what keeps two
        # systems that use the same word from becoming one row.
        import_number=config.import_number(system),
        project_name=config.PROJECT,
        # Communities are embedded so `global` can search them, and edges so a
        # question can be matched against the relations themselves.
        embedding_func=openai_embedding,
        enable_edge_embeddings=True,
        enable_community_embeddings=True,
    )


def reset(db) -> None:
    """Empty the collections this import is about to fill.

    The importer inserts rather than replaces, so without this a second run adds a
    second copy of every community and every structural edge. The vector indexes
    have to go first: ArangoDB refuses a document with no value in an indexed
    vector field, which is every row at the moment it is inserted.
    """
    for name in config.ALL_COLLECTIONS:
        config.drop_vector_indexes(db, name)
        if db.has_collection(name):
            db.collection(name).truncate()


async def load() -> dict:
    db = config.db(create=True)
    reset(db)

    # One import per system, in SYSTEMS order so the import numbers are the ones
    # `config.import_number` promises. Each reads its own workbench and writes into
    # the same collections; the keys cannot collide because the number is in them.
    imp = None
    for system in config.SYSTEMS:
        imp = importer(system)
        await imp.initialize(config.token())
        await imp.import_documents(config.ARTIFACTS.FULL_DOCS)
        # Deliberately without the chunk-embedding file -- see `chunk_vectors`.
        await imp.import_text_chunks(config.ARTIFACTS.TEXT_CHUNKS)
        chunk_vectors(db, imp)
        await imp.import_entities(config.ARTIFACTS.ENTITIES)
        await imp.import_relationships(config.ARTIFACTS.RELATIONSHIPS)
        await imp.import_community_reports(config.ARTIFACTS.COMMUNITY_REPORTS)

    label(db)
    # Before the indexes, not after: `structure` creates an entity for anything a
    # file declares that the extraction did not report, and ArangoDB rejects a
    # document with no value in an indexed vector field.
    counts = structure.apply(db)

    # Not the edge collection. ArangoDB requires the indexed vector field on every
    # document in the collection, and only RELATED_TO edges carry one -- the
    # structural edges have no text to embed. Exact COSINE_SIMILARITY is used for
    # those instead, which is also the only form that can be filtered by
    # relationship_type in the same query.
    for name in (config.ENTITIES, config.CHUNKS, config.COMMUNITIES):
        await imp.create_vector_index(collection_name=name,
                                      field=config.EMBEDDING_FIELD,
                                      index_name=config.VECTOR_INDEX)
    return {**{name: db.collection(name).count() for name in config.ALL_COLLECTIONS},
            "stated relations": counts["edges"],
            "entities created by structure": counts["created"],
            "short-name duplicates merged": counts["merged"]}


def chunk_vectors(db, imp) -> int:
    """Attach the chunk embeddings, matched by id rather than by position.

    `import_text_chunks` will do this itself if handed `vdb_chunks.json`, but it
    indexes the embedding matrix with the chunk's `chunk_order_index`
    (import_graph_to_adb.py:1892). That counter restarts at 0 in every document,
    while the matrix is in one flat vdb order -- so with more than one input file
    every chunk after the first document gets some other chunk's vector. It is
    silent: the rows are there, the vectors are there, and chunk search returns
    confident nonsense.

    The vdb records carry `__id__`, which is the same "chunk-<hash>" the importer
    derives the _key from, so matching on that is exact regardless of order.
    """
    rows, matrix = imp.get_data_and_embeddings("vdb_chunks.json")
    updates = [{"_key": imp._generate_key(row["__id__"].split("-")[1], apply_hash=False),
                config.EMBEDDING_FIELD: matrix[i].tolist()}
               for i, row in enumerate(rows)]
    coll = db.collection(config.CHUNKS)
    for i in range(0, len(updates), 500):
        coll.import_bulk(updates[i:i + 500], on_duplicate="update")
    return len(updates)


# Documents already carry the file they came from. Chunks reach one through
# PART_OF and entities reach it through MENTIONED_IN and then PART_OF, so both
# hops are resolved here and written down.
STAMP_DOCUMENTS = f"""
FOR d IN {config.DOCUMENTS}
  UPDATE d WITH {{files: [d.file_name], models: [@models[d.file_name]]}}
  IN {config.DOCUMENTS}"""

STAMP_CHUNKS = f"""
FOR c IN {config.CHUNKS}
  LET docs = (FOR d IN 1..1 OUTBOUND c {config.RELATIONS}
                FILTER IS_SAME_COLLECTION('{config.DOCUMENTS}', d) RETURN d)
  UPDATE c WITH {{files: SORTED(UNIQUE(docs[*].file_name)),
                  models: SORTED(UNIQUE(FLATTEN(docs[*].models)))}}
  IN {config.CHUNKS}"""

STAMP_ENTITIES = f"""
FOR e IN {config.ENTITIES}
  LET chunks = (FOR c IN 1..1 OUTBOUND e {config.RELATIONS}
                  FILTER IS_SAME_COLLECTION('{config.CHUNKS}', c) RETURN c)
  UPDATE e WITH {{files: SORTED(UNIQUE(FLATTEN(chunks[*].files))),
                  models: SORTED(UNIQUE(FLATTEN(chunks[*].models)))}}
  IN {config.ENTITIES}"""

# An entity found in several chunks keeps one description per chunk, joined with
# GRAPH_FIELD_SEP. Nothing downstream splits on it -- not the retrievers, not the
# AQLizer -- so it survives only as a literal "<SEP>" in the middle of every
# answer built from a description.
UNJOIN = """
FOR d IN @@collection
  FILTER CONTAINS(d.description, '<SEP>')
  UPDATE d WITH {description: SUBSTITUTE(d.description, '<SEP>', ' ')}
  IN @@collection"""


def label(db) -> None:
    """Write `files` and `models` onto every Document, Chunk and Entity."""
    names = db.aql.execute(f"FOR d IN {config.DOCUMENTS} RETURN d.file_name")
    db.aql.execute(STAMP_DOCUMENTS,
                   bind_vars={"models": {n: config.model_of(n) for n in names}})
    db.aql.execute(STAMP_CHUNKS)
    db.aql.execute(STAMP_ENTITIES)
    for name in (config.ENTITIES, config.RELATIONS):
        db.aql.execute(UNJOIN, bind_vars={"@collection": name})
    for name in (config.DOCUMENTS, config.CHUNKS, config.ENTITIES):
        db.collection(name).add_index(
            {"type": "persistent", "fields": ["models[*]"], "name": "models_idx"})
    db.collection(config.ENTITIES).add_index(
        {"type": "persistent", "fields": ["entity_type"], "name": "entity_type_idx"})
    db.collection(config.RELATIONS).add_index(
        {"type": "persistent", "fields": ["relationship_type"], "name": "relationship_type_idx"})


def main() -> None:
    counts = asyncio.run(load())
    print(f"  loaded into {config.DB_NAME}")
    for name, n in counts.items():
        print(f"  {n:>6}  {name}")


if __name__ == "__main__":
    main()
