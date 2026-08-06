# drone-graph

Three SysML v2 models turned into an ArangoDB graph you can ask questions about in
English.

```bash
docker run -d --name christian-webb-drone-arango -p 8529:8529 \
  -e ARANGO_ROOT_PASSWORD=testpass arangodb:3.12.9.4 \
  arangod --experimental-vector-index=true

export CHAT_API_KEY=sk-...      # OpenAI
python build.py                 # parse -> project -> enrich -> analogy
```

Then open `simple-demo.ipynb`, which does the whole thing end to end, and
`analogy-demo.ipynb`, which is about relating the models to each other.

You must have the 4 requisite Arango repos cloned adjacent to this one.

## The pipeline

`build.py` runs four steps in order. Each one is also runnable on its own
(`python -m sysml.pipeline.parse`) and reads only what the step before it left
behind, so a rerun can start anywhere.

### 1. parse — `sysml/pipeline/parse.py`

NOTE: for simplicity for now I just threw all 3 models in that folder and we build a graph of them together, since this is just a POC. We would change this for any bigger project.

Reads every `.sysml` file under `models/` and writes `out/model.json`: a list of
elements and the relations between them, with every element carrying the file and
line it was declared on.

**Why:** SysML already states its structure exactly — `satisfy R by S` is a fact, not
a hint. The normal GraphRAG path would hand that text to an LLM and ask it to guess
the edges. Parsing instead means no edge is ever invented, and every answer can be
quoted back to a line of source.

### 2. project — `sysml/pipeline/project.py`

Loads `out/model.json` and writes it into ArangoDB in the `graphrag_importer` schema:
Documents (one per file), Chunks (windows of source text), Entities (one per SysML
element, described in prose), and Relations (every edge).

**Why:** using the importer's own schema instead of inventing one means anything
built to read a GraphRAG corpus can read this graph too. The prose description on
each entity is what gets embedded, so retrieval matches on meaning rather than on
name spelling.

### 3. enrich — `sysml/pipeline/enrich.py`

Adds the three things a GraphRAG corpus needs beyond the raw graph: communities
(clusters of related elements), one LLM-written report per community, and embeddings
plus vector indexes on everything embeddable.

**Why:** communities are what a question about the model *as a whole* gets answered
from — otherwise a broad question has to be assembled out of two thousand individual
elements. Embeddings are what make any of it searchable by meaning. This is the only
step that costs money, and it caches to `out/`, so a second run is free.

### 4. analogy — `sysml/pipeline/analogy.py`

Adds cross-model `SIMILAR_TO` edges between elements of the same kind in different
models, using autograph's own `SimilarityFinder`.

**Why:** parsing only relates elements a file says are related, and no Apollo file
mentions a drone — so without this nothing crosses a model boundary, and "what plays
the drone battery's role in Apollo?" has no path to walk.

## Asking questions

`sysml/nl.py` is the read side, and it offers two paths:

- **AQLizer** — Arango's natural-language-to-AQL service, given one extra thing:
  `sysml/aql_examples.md`, which teaches it how SysML concepts are laid out in this
  graph. Best at analytical questions ("which requirements does nothing satisfy?").
  Every answer comes back with the AQL that produced it.
- **GraphRAG** — the retriever service, run against this graph. `local` does hybrid
  vector + BM25 search over the entities, fuses the two rankings, and expands over
  the relations it lands on; `global` answers from the community reports; `unified`
  searches the source text and the entity graph in parallel and answers from both,
  which reaches facts stated in a `doc` comment that no element name resembles.
  Answers carry `[CITE:n]` markers that resolve to the source file. Best at
  descriptive and whole-model questions.

```python
from sysml import nl
nl.graphrag("what does the drone battery do?").show()
nl.graphrag("what does this model cover?", scope="global").show()
```

## How the Arango repos are used

Four repos, cloned next to this one.

The importer builds a graph in two stages: first on a workbench (a NetworkX graph and
JSON files, where an LLM extracts entities and Leiden clusters them), then it copies
the result into ArangoDB. This project has no first stage — SysML already states its
structure — so it parses and writes straight into the second. That is what "the schema,
not the pipeline" means below, and it is why anything typed against the workbench
(BaseGraphStorage, BaseKVStorage) is unreachable from here.

The two services normally run as pods beside the platform. Running them here means
supplying what a pod gets from its surroundings: a database JWT (ArangoDB issues one at
/_open/auth), a progress sink, and a token validator. Everything below those is the
services' own code, unmodified.

### graphrag_importer

Used — all of it from graphrag.naming, so an importer-side reader finds things where it
expects them:

- CollectionNames for the five collection names, derived from the GenAI project name
- IndexNames.EMBEDDING_FIELD, the field it vector-indexes: "embedding", singular.
  Writing anything else is silently broken — rows there, vectors there, every vector
  query empty
- RelationshipTypes for the closed set of edge type values, plus SUB_COMMUNITY_OF,
  which validation omits but its writer and delete engine both use
- relationship_type for the authored SysML relation, since type is closed
- file_ids, the key it deletes on, and citable_url, which citations resolve through

Not used:

- The extraction pipeline, the point of the repo. It asks an LLM to guess entities and
  edges from text; parsing means no edge is ever invented
- Leiden. It runs Leiden because an LLM-extracted graph has no other structure. SysML
  has two: the packages engineers wrote, and the traceability edges cutting across them
- Its community report generator, which is a workbench function — it reads
  community_schema() off BaseGraphStorage and writes into a BaseKVStorage. Reports here
  are one OpenAI call per community over counted facts (member kinds, internal relation
  counts, members with file:line), cached to out/
- SemanticUnits, which nothing here fills, so vertex collections are named one by one
- The delete engine and anything partition-shaped. build.py rebuilds from scratch

### graphrag_retrievers

Used — the whole read path, in-process against the local container:

- RetrievalService and its three scopes: local (vector + BM25 over entities, fused by
  reciprocal rank, expanded over relations), global (from the community reports),
  unified (source chunks and entity graph in parallel — local reaches a chunk only
  through an entity that matched first)
- The inverted indexes and search-alias view retrieval_fusion builds for itself
- report_string and report_json, the community shape global reads. We write the
  reports; the field names are its
- response_instructions, its supported way to shape an answer
- citation_mapping and the [CITE:n] markers, and graph_metadata for the line reporting
  what was actually read — citation count alone made thirty documents look like three

Not used:

- use_llm_planner, which runs a global pass just to pick a retriever we already picked
- auto_select_partitions, which reads AutoGraph corpus collections a single-module
  graph does not have
- The response cache, so every question actually runs
- Its auth stack and update_service_status, replaced by the shims described above

### autograph

Used — both run unmodified:

- DataStorage.create_vector_index_on_field. It sizes nLists, waits out the training,
  and writes defaultNProbe onto the index. nProbe is how many index partitions a search
  opens; at the default of 1 a search reads a sliver and quietly returns fewer rows than
  the LIMIT asked for. On the index, every query gets it for free
- SimilarityFinder, for the cross-model analogy edges. Its module_doc_ids argument
  exists to keep edges inside a module; passing the other models' elements runs it
  backwards, so the only edges it can build are the ones that cross
- corpus_graph.naming EDGE_LABEL_SIMILAR_TO for those edges. An analogy is not
  something a file states, so it must not be a RELATED_TO with a relationship_type —
  that would make a resemblance we computed look like a relation an engineer wrote

Not used:

- The orchestrator and importer spawning. They start importer pods to run the LLM
  extraction this project replaces with a parser; a platform would not change that
- The strategizer. Its output is a strategy, a partition id and an entity_types hint,
  all three to feed and shard that extraction — and it samples every file through File
  Manager, so it could not run locally either
- The corpus build's own extraction and chunking: converters for PDF and Office, when
  these inputs are text. Its similarity granularity is wrong here too — it compares
  whole documents truncated to CHUNK_MAX_CHARS, about 8% of the largest file, so it is
  handed one document per SysML element instead
- The "embeddings" field name it writes, plural. The retrievers are the readers
- The platform connection manager, which only earns its keep renewing a JWT mid-run
- Its keep-a-ready-index behaviour, wrong when the index build follows a vector rewrite

### natural-language-service

Used — the AQLizer path. No hand-written query functions: if an answer is wrong the fix
goes in aql_examples.md, not into Python.

- Txt2AqlService for the LLM and database clients, and the LangChain ArangoGraph it
  builds its schema picture from
- ReadOnlyArangoGraphQAChain.from_llm with force_read_only_query, execute_aql_query,
  return_aql_query, return_aql_result, max_aql_generation_attempts and top_k
- aql_examples, which from_llm has always accepted and the deployed service never
  passes. Ours teaches it how SysML concepts are laid out in this graph
- qa_prompt — upstream's template plus three lines. Upstream asks for a summary "in the
  same language as the User Input" and has answered an English question in Spanish; the
  additions pin the language, ask for the file:line the rows already carry, and stop it
  reporting an empty result as "the model contains no such thing"

Not used:

- process_nl_query, its own entry point. It resolves the chain's mix of str and
  AIMessage with str(AIMessage), gluing token accounting onto the AQL; reading .content
  is the fix, so the chain is invoked directly
- Schema document sampling. Collection and field lists are what the model needs
- Its read-only check as the only gate. WRITE_OPERATIONS omits TRUNCATE, so a generated
  FOR c IN [...] TRUNCATE c passes it; nothing reaches the database without clearing a
  second check here
