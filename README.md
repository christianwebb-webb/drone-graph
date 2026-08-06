# drone-graph

Three SysML v2 models turned into an ArangoDB graph you can ask questions about in
English. The graph is built by graphrag_importer's own extraction pipeline -- the
same code the platform's importer pods run -- called in-process against a local
container.

```bash
docker run -d --name christian-webb-drone-arango -p 8529:8529 \
  -e ARANGO_ROOT_PASSWORD=testpass arangodb:3.12.9.4 \
  arangod --experimental-vector-index=true

export CHAT_API_KEY=sk-...      # OpenAI
python build.py                 # extract -> load -> analogy
```

Then open `extraction-demo.ipynb`, which is about how the graph is built and what
that choice costs; `simple-demo.ipynb`, which asks it questions; and
`analogy-demo.ipynb`, which is about relating the models to each other.

You must have the 4 requisite Arango repos cloned adjacent to this one.

## The pipeline

`build.py` runs three steps in order. Each one is also runnable on its own
(`python -m sysml.pipeline.extract`) and reads only what the step before it left
behind, so a rerun can start anywhere.

### 1. extract -- `sysml/pipeline/extract.py`

Reads every `.sysml` file under `models/` and hands the text to
`graphrag_importer`'s `GraphRAG`, which chunks it, asks an LLM for the entities and
the relations between them, clusters the result with Leiden and writes one report
per cluster. The artifacts land in `out/kg`.

**Why:** the alternative -- and what this project used to do -- is a hand-written
SysML parser. That was 791 lines of grammar, and it could only ever report a
relation some statement spelled out. Extraction reads the `doc` comments and the
rationale too, and it is not specific to one input language. The two lists in
`config.KINDS` and `config.RELATIONS_ONTOLOGY` are the only thing this project tells
it about SysML; `enable_strict_types=True` makes them closed, so an entity or edge
typed outside them is dropped rather than renamed.

**Cost:** the LLM writes the descriptions, so numbers live in prose rather than in a
typed field, entity names come back upper-cased, and there are no line numbers --
an entity traces to the files it appears in, not to a declaration. Every LLM answer
is cached in `out/kg`, so a second run over unchanged sources is free.

### 2. load -- `sysml/pipeline/load.py`

Hands `out/kg` to the importer's own `ImportGraphToADB`, which creates the graph and
the five collections, writes Documents, Chunks, Entities, Communities and every edge
between them, and builds the vector indexes.

**Why:** using the importer's writer rather than imitating its schema means the
graph is the shape a platform-built graph has, by construction rather than by
agreement. The retrievers and the AQLizer were built to read that; before, they were
reading a hand-made copy of it that this project had to keep in step by hand.

Afterwards it writes `files` and `models` onto every Document, Chunk and Entity. The
importer already connects Entity -> Chunk -> Document, so "which model is this in"
is answerable by walking two hops; resolving the walk once means an analytical
question is a filter instead.

### 3. analogy -- `sysml/pipeline/analogy.py`

Adds cross-model `SIMILAR_TO` edges between entities of the same `entity_type` in
different models, using autograph's own `SimilarityFinder`.

**Why:** extraction relates what a text talks about, and no Apollo file mentions a
drone -- so nothing crosses a model boundary except where the two happen to use the
same word, and "what plays the drone battery's role in Apollo?" has no path to walk.

## Asking questions

`sysml/nl.py` is the read side, and it offers two paths:

- **AQLizer** -- Arango's natural-language-to-AQL service, given one extra thing:
  `sysml/aql_examples.md`, which teaches it how SysML concepts are laid out in this
  graph. Best at analytical questions ("which requirements does nothing satisfy?").
  Every answer comes back with the AQL that produced it.
- **GraphRAG** -- the retriever service, run against this graph. `local` does hybrid
  vector + BM25 search over the entities, fuses the two rankings, and expands over
  the relations it lands on; `global` answers from the community reports; `unified`
  searches the source text and the entity graph in parallel and answers from both,
  which reaches a figure that survived into a chunk but not into any entity
  description. Answers carry `[CITE:n]` markers that resolve to the source file.

```python
from sysml import nl
nl.graphrag("what does the drone battery do?").show()
nl.graphrag("what does this model cover?", scope="global").show()
```

## How the Arango repos are used

Four repos, cloned next to this one.

Both halves of the importer run here, unmodified, with three things supplied that a
pod would get from its surroundings: a database JWT (ArangoDB issues one at
`/_open/auth`), a progress sink, and a token validator.

### graphrag_importer

Used -- the pipeline, both halves:

- `GraphRAG` and `ainsert`, for the extraction. `metadata_list` is what carries
  provenance: each dict is merged into that document's record and read back out by
  `import_documents` as `file_name`, `citable_url` and `file_ids`
- `ImportGraphToADB`: `initialize`, the five `import_*` methods in the order
  `server.py` calls them, and `create_vector_index`
- `graphrag.naming` for the collection names, the artifact file names, the closed
  edge vocabulary and `IndexNames.EMBEDDING_FIELD` -- "embedding", singular. Writing
  anything else is silently broken: rows there, vectors there, every vector query
  empty

Two things in the local path need working around, both planted as module globals
because that is what the import path resolves against:

- `update_service_status` is a gRPC call to the platform's metadata service. There
  is nothing here to announce progress to, and left alone it retries against an
  address that does not resolve
- `open`. The extraction half writes its artifacts as UTF-8 with
  `ensure_ascii=False` and the writer reads them back with a bare `open(path)`,
  which takes the platform default. That is UTF-8 on the Linux pods this normally
  runs on and cp1252 here, so the import stops at the first non-ASCII character

And one that has to be done differently rather than shimmed: `import_text_chunks`
indexes the chunk-embedding matrix with `chunk_order_index`, which restarts at 0 in
every document while the matrix is in one flat order. With more than one input file
every chunk after the first document gets some other chunk's vector. `load` passes
no embedding file and attaches the vectors itself, matched on the vdb's `__id__`.

Not used:

- The gRPC server, the File Manager, the job tracker and the tracing plane
- Partitions and the delete engine. `build.py` rebuilds from scratch
- SemanticUnits, which nothing here fills

### graphrag_retrievers

Used -- the whole read path, in-process against the local container:

- `RetrievalService` and its three scopes
- The inverted indexes and search-alias view `retrieval_fusion` builds for itself
- `report_string` and `report_json`, the community shape `global` reads
- `response_instructions`, its supported way to shape an answer
- `citation_mapping` and the `[CITE:n]` markers, and `graph_metadata` for the line
  reporting what was actually read

One setting is changed, through the config object rather than the code:
`global_min_community_similarity`, the cosine below which `global` discards a
community report before the map-reduce. The default of 0.45 is unreachable against
reports of the length the extraction step writes -- measured here, an on-topic
question tops out at 0.33-0.38 and an off-topic one at 0.05-0.07, so it is set to
0.25, which is in the gap. Left alone, every global question answers "No relevant
information found."

Not used:

- `use_llm_planner`, which runs a global pass just to pick a retriever we already
  picked
- `auto_select_partitions`, which reads AutoGraph corpus collections a single-module
  graph does not have
- The response cache, so every question actually runs
- Its auth stack and `update_service_status`, replaced by the shims above

### autograph

Used -- both run unmodified:

- `SimilarityFinder`, for the cross-model analogy edges. Its `module_doc_ids`
  argument exists to keep edges inside a module; passing the other models' entities
  runs it backwards, so the only edges it can build are the ones that cross
- `DataStorage.create_vector_index` and `create_arangosearch_view` over the analogy
  staging corpus. The view name is the one `LexicalSearch` queries, so the BM25 half
  of the search works without being told where to look
- `corpus_graph.naming.EDGE_LABEL_SIMILAR_TO` for those edges. An analogy is not
  something a file states, so it must not be a `RELATED_TO` with a
  `relationship_type` -- that would make a resemblance we computed look like a
  relation the source asserted

Not used:

- The orchestrator and importer spawning, which start pods to run the extraction
  this project runs in-process
- The strategizer. Its output is a strategy, a partition id and an `entity_types`
  hint, all three to feed and shard that extraction -- and it samples every file
  through File Manager, so it could not run locally either
- The corpus build's own extraction and chunking. Its similarity granularity is
  wrong here too: it compares whole documents truncated to `CHUNK_MAX_CHARS`, about
  8% of the largest file, so it is handed one document per entity instead
- The "embeddings" field name it writes, plural. The retrievers are the readers

### natural-language-service

Used -- the AQLizer path. No hand-written query functions: if an answer is wrong the
fix goes in `aql_examples.md`, not into Python.

- `Txt2AqlService` for the LLM and database clients, and the LangChain `ArangoGraph`
  it builds its schema picture from
- `ReadOnlyArangoGraphQAChain.from_llm` with `force_read_only_query`,
  `execute_aql_query`, `return_aql_query`, `return_aql_result`,
  `max_aql_generation_attempts` and `top_k`
- `aql_examples`, which `from_llm` has always accepted and the deployed service
  never passes. Ours teaches it how SysML concepts are laid out in this graph
- `qa_prompt` -- upstream's template plus three lines. Upstream asks for a summary
  "in the same language as the User Input" and has answered an English question in
  Spanish; the additions pin the language, ask for the files the rows already carry,
  and stop it reporting an empty result as "the model contains no such thing"

Not used:

- `process_nl_query`, its own entry point. It resolves the chain's mix of `str` and
  `AIMessage` with `str(AIMessage)`, gluing token accounting onto the AQL; reading
  `.content` is the fix, so the chain is invoked directly
- Schema document sampling. Collection and field lists are what the model needs
- Its read-only check as the only gate. `WRITE_OPERATIONS` omits TRUNCATE, so a
  generated `FOR c IN [...] TRUNCATE c` passes it; nothing reaches the database
  without clearing a second check here
