# drone-graph

```bash
docker run -d --name christian-webb-drone-arango -p 8529:8529 \
  -e ARANGO_ROOT_PASSWORD=testpass arangodb:3.12.9.4 \
  arangod --experimental-vector-index=true

python build.py                 # extract -> load -> analogy -> examples
```

The OpenAI key is read from `CHAT_API_KEY` (or `OPENAI_API_KEY`) in the `env` file
one directory up, beside the cloned repos; an exported variable of either name is
used when the file has neither.

You must have the 4 requisite Arango repos cloned adjacent to this one.

## The pipeline

### 1. extract -- `sysml/pipeline/extract.py` - graphrag_importer

Reads every `.sysml` file under `models/` and hands the text to
`graphrag_importer`'s `GraphRAG`, which chunks it, asks an LLM for the entities and
the relations between them, clusters the result with Leiden and writes one report
per cluster.

One model at a time, each on its own dir under `out/kg/<model>`. 

- We enable strict types, and pass in an ontology that is defined in config.py
- This includes 27 kinds and 18 relationship types

### 2. load -- `sysml/pipeline/load.py` - graphrag_importer

Hands `out/kg` to the importer's `ImportGraphToADB`, which creates the graph and
the five collections, writes Documents, Chunks, Entities, Communities and every edge
between them, and builds the vector indexes.

- On purpose we set enable_edge_embeddings=False here, since the next step will change
the edges, and then get the embeddings itself afterwards.

### 2a. structure -- `sysml/pipeline/structure.py`

Reads every `.sysml` file again with a deterministic parser and make fields for what the syntax
says outright: 
- an `attributes` map of `{value, unit}` or `{expression}` on the element that declares it
- its `short_name`, `source_file` / `source_line`
- the `owns`, `typedby`, `specializes`, `redefines` and `satisfies` edges

**Why:** the numbers and the tree have to be exact, and an LLM is not. Without this,
AQLizer questions like "sum the dry mass of the Saturn V from its stages" return nothing. 
With it the answer is 188,650 kg from the four things the Saturn V
declares -- S-IC, S-II, S-IVB and the instrument unit -- and the four `...Cost`
attributes on the mission add to $11bn.

It knows SysML v2's declaration grammar, not specifically this corpus: any modifier or `#`
metadata annotation, a keyword in `KEYWORDS`, optionally `case` and `def`, an
optional `<shortName>`, a name, then any combination of `:`, `:>`, `:>>` and `=`.
A body can continue its enclosing declaration with a bare `:>` or `:>>`, and
`satisfy REQ by DESIGN` is read as the relation it states. A model it has never
seen parses on the same rules -- `analytics-demo.ipynb` runs it on one to show
that, using forms no file here contains.

Edges it writes are `RELATED_TO` with the relation in `relationship_type`, the same
shape extraction writes, so nothing downstream has to know which pass produced an
edge. 

`stated: true` marks the ones that came from the parse (as opposed to from the LLM), for when you do want to
tell them apart.

It runs inside `load` rather than as a step of its own because creating an entity
is impossible once the Entities vector index exists.

### 3. analogy -- `sysml/pipeline/analogy.py`

Adds cross-model `SIMILAR_TO` edges between entities of the same `entity_type` in
different models, using autograph's own `SimilarityFinder`.

**Why:** extraction relates what a text talks about, and no Apollo file mentions a
drone -- so nothing crosses a model boundary except where the two happen to use the
same word, and "what plays the drone battery's role in Apollo?" has no path to walk.

### 4. examples -- `sysml/pipeline/examples.py`

Writes `out/aql_examples_generated.md`, the context AQLizer is given, from the
finished graph.

AQLizer 

It takes general info about converting SysML to Arango, then adds some syntax from the conversion thus far, and explains in depth what the examples file should be.


## Asking questions

`sysml/nl.py` is the read side, and it offers two paths:

- **AQLizer** -- Arango's natural-language-to-AQL service, given one extra thing:
  `sysml/aql_examples.md`, which teaches it how SysML concepts are laid out in this
  graph. Best at analytical questions ("which requirements does nothing satisfy?").
  Every answer comes back with the AQL that produced it. `nl.instance(path)` primes
  it with a different file instead -- step 4 writes one.
- **GraphRAG** -- the retriever service, run against this graph. `local` does hybrid
  vector + BM25 search over the entities, fuses the two rankings, and expands over
  the relations it lands on; `global` answers from the community reports; `unified`
  searches the source text and the entity graph in parallel and answers from both,
  which reaches a figure that survived into a chunk but not into any entity
  description. Answers carry `[CITE:n]` markers that resolve to the source file,
  and `Answer.evidence(find=...)` prints the retrieved text around a figure, so an
  answer can be checked against what was read rather than taken on trust.

`unified` is also the one scope that does not get the chat key through the service
object: it asks the retriever for a `chat_api_key` attribute, `UnifiedRetriever`
keeps its constructor arguments in a kwargs dict, and the lookup falls through to
the environment. The retriever bootstrap sets `CHAT_API_KEY` for it. Left alone the
retrieval succeeds and only the answer fails, reported as "response generation
failed".

graphrag with scope
.\.venv\Scripts\python.exe -m sysml.nl --graphrag --scope global "what does the drone battery do?"

graphrag with default (local) scope
.\.venv\Scripts\python.exe -m sysml.nl --graphrag --scope global "what does the drone battery do?"

AQLizer query
.\.venv\Scripts\python.exe -m sysml.nl "what does the drone battery do?"

## How the Arango repos are used

### graphrag_importer

- `GraphRAG` and `ainsert`, for the extraction. `metadata_list` is what carries
  provenance: each dict is merged into that document's record and read back out by
  `import_documents` as `file_name`, `citable_url` and `file_ids`
- `ImportGraphToADB`: `initialize`, the five `import_*` methods in the order
  `server.py` calls them, and `create_vector_index`
- `graphrag.naming` for the collection names, the artifact file names, the closed
  edge vocabulary and `IndexNames.EMBEDDING_FIELD` -- "embedding", singular. Writing
  anything else is silently broken: rows there, vectors there, every vector query
  empty

And one that has to be done differently rather than shimmed: `import_text_chunks`
indexes the chunk-embedding matrix with `chunk_order_index`, which restarts at 0 in
every document while the matrix is in one flat order. With more than one input file
every chunk after the first document gets some other chunk's vector. `load` passes
no embedding file and attaches the vectors itself, matched on the vdb's `__id__`.

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
