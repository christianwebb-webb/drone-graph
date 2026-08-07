# drone-graph

Three SysML v2 models turned into an ArangoDB graph you can ask questions about in
English.

The sources are read twice. graphrag_importer's own extraction pipeline -- the same
code the platform's importer pods run, called in-process against a local container
-- asks an LLM what the text means. A small lexer then reads the same files for
what the syntax states outright: attribute values, containment and typing. The
first pass is good at prose and hopeless at arithmetic; the second is the reverse.

```bash
docker run -d --name christian-webb-drone-arango -p 8529:8529 \
  -e ARANGO_ROOT_PASSWORD=testpass arangodb:3.12.9.4 \
  arangod --experimental-vector-index=true

python build.py                 # extract -> load -> analogy -> examples
```

The OpenAI key is read from `CHAT_API_KEY` (or `OPENAI_API_KEY`) in the `env` file
one directory up, beside the cloned repos; an exported variable of either name is
used when the file has neither.

Four notebooks:

- `nl-demo.ipynb` -- the two ways of asking, side by side. AQLizer for anything with
  a shape (count, rollup, ranking, what is *missing*), GraphRAG for anything without
  one. Ends on a question asked both ways, to show where the line falls.
- `extraction-demo.ipynb` -- how the graph is built, and what the change to LLM
  extraction cost and bought.
- `analytics-demo.ipynb` -- the quantitative half: rollups, values, gaps, and the
  lexer run live on a SysML file it has never seen.
- `analogy-demo.ipynb` -- relating the two models to each other.
- `bespoke-aql-examples.ipynb` -- the hand-written AQLizer primer against one the
  pipeline generates for itself, scored on ten questions with the answers computed
  from the graph.

You must have the 4 requisite Arango repos cloned adjacent to this one.

## Brainstorming Questions

- Currently, we just import all models in models/. If this project ever becomes more than a POC, we should change this.

## The pipeline

`build.py` runs four steps in order. Each one is also runnable on its own
(`python -m sysml.pipeline.extract`) and reads only what the step before it left
behind, so a rerun can start anywhere.

### 1. extract -- `sysml/pipeline/extract.py`

Reads every `.sysml` file under `models/` and hands the text to
`graphrag_importer`'s `GraphRAG`, which chunks it, asks an LLM for the entities and
the relations between them, clusters the result with Leiden and writes one report
per cluster.

One model at a time, each on its own workbench under `out/kg/<model>`. Extraction
merges entities by name within a run, so this is the boundary that decides what may
merge: run together, Apollo's `control` action and the drone's `Control` requirement
become one row. A model is a top-level entry under `models/` -- a directory, or a
loose `.sysml` file -- derived rather than configured, so adding one is dropping a
folder in. Each is imported under its own `import_number`, which the writer puts in
every document key.

**Why:** the alternative -- and what this project used to do -- is a hand-written
SysML parser. That was 791 lines of grammar, and it could only ever report a
relation some statement spelled out. Extraction reads the `doc` comments and the
rationale too, and it is not specific to one input language. The two lists in
`config.KINDS` and `config.RELATIONS_ONTOLOGY` are the only thing this project tells
it about SysML; `enable_strict_types=True` makes them closed, so an entity or edge
typed outside them is dropped rather than renamed.

**Cost:** entity names come back upper-cased, and the LLM is still not exhaustive
about anything the syntax states mechanically -- on this corpus it produced 944
`owns` edges where the files state 1,793, and 221 `satisfies` where the files state
265. Of the 2,158 edges it reports, 1,587 coincide with an edge the syntax states
and 571 do not, and there is no way to tell which is which from the edge itself.
That is what `structure` exists to fix: it reads the same files with a lexer and
marks every edge the syntax states with `stated: true`, so a question that needs
the declared model can filter for it. Every LLM answer is cached in `out/kg`, so a
second run over unchanged sources is free.

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
question is a filter instead. Then it runs `structure`, below.

### 2a. structure -- `sysml/pipeline/structure.py`

Reads every `.sysml` file again with a lexer and writes down only what the syntax
says outright: an `attributes` map of `{value, unit}` or `{expression}` on the
element that declares it, its `short_name`, `source_file` / `source_line`, and the
`owns`, `typedby`, `specializes`, `redefines` and `satisfies` edges. Elements a
file declares that the extraction did not report are created, so the containment
tree has no holes.

**Why:** the numbers and the tree have to be exact, and an LLM is not. Without this,
"sum the dry mass of the Saturn V from its stages" returns nothing -- not because
the masses are missing but because there is no path from the vehicle to its stages
to walk. With it the answer is 188,650 kg from the four things the Saturn V
declares -- S-IC, S-II, S-IVB and the instrument unit -- and the four `...Cost`
attributes on the mission add to $11bn.

**Identity is the qualified name.** A bare name is not unique -- SysML lets any
number of declarations share one, and this corpus has 139 that are shared, covering
390 declarations: `spacecraft` is declared 21 times, once per mission snapshot, and
`power` is a feature on five different ports. Keyed on the bare name they become
one row, and then a rollup walks into the wrong subtree, one declaration's
attributes overwrite another's, and a question about lunar orbit insertion is
answered with the command module's condition after it was recovered from the ocean.
So a contested name is stored with as much of its owner as it takes to be unique --
`MISSIONSYSTEMATLOI_SPACECRAFT`, `CONTROLPORT_POWER` -- and the three quarters of
names that nothing contests keep the short form that reads and searches well.

References are resolved the way SysML scopes them rather than by matching text: the
nearest enclosing declaration wins, then the shallowest, and a dotted path like
`performLunarMission.outbound.prep.load` is followed through typing and inheritance
rather than containment, because that is what the dots mean. Anything still
ambiguous is dropped instead of guessed. That is what lets all but one of the 273
`satisfy` statements resolve, where matching on the last name alone got 209 and
some of those were bound to the wrong element. The graph holds 265 `satisfies`
edges rather than 272 because seven of those statements are written twice, and an
edge is a pair rather than a line.

It knows SysML v2's declaration grammar, not this corpus: any modifier or `#`
metadata annotation, a keyword in `KEYWORDS`, optionally `case` and `def`, an
optional `<shortName>`, a name, then any combination of `:`, `:>`, `:>>` and `=`.
A body can continue its enclosing declaration with a bare `:>` or `:>>`, and
`satisfy REQ by DESIGN` is read as the relation it states. A model it has never
seen parses on the same rules -- `analytics-demo.ipynb` runs it on one to show
that, using forms no file here contains.

`KEYWORDS` is the same 27-kind vocabulary the extraction step is given, so the two
passes cannot disagree about what an element is. It is not derived from these
files: eleven of the 27 never appear in them. SysML v2 does have declaration kinds
outside that list -- `message`, `succession`, `alias` -- and those are left to
extraction, along with the relationship statements that are not `satisfy`
(`perform`, `connect`, `subject`, `import`, and the `#refinement dependency` form,
whose meaning is carried by an annotation rather than by a keyword). What it also
deliberately does not do is resolve names across files, infer anything, or read a
`doc` comment; that is extraction's half, and it is better at it.

Reading the short name is also what lets it clean up after extraction. A SysML
element has two written names -- `requirement def <'DE-REQ-1'> Power` is addressed
as either -- and extraction keeps whichever the sentence it read happened to use,
so the same requirement arrives twice, once as `POWER` and once as `DE-REQ-1`,
each with a share of the edges. Only the declaration says they are one element, so
only this step can: 127 duplicate rows are folded into the element they name, and
their edges moved across. A row is only a duplicate if no declaration resolves to
it -- Apollo's `requirement 'flr-R001' : PropellantLoadingRequirement` is a real
usage whose name happens to be another element's short name, and it survives.

The same reasoning applies to the edges, and more strongly. `owns`, `typedby`,
`specializes` and `redefines` are not things a text discusses, they are things a
declaration states, and the lexer reads all of them -- so an inferred one is a
guess at something already known. They are dropped. The guesses were wrong in a
particular way: they point backwards. `HARDWARECOMPONENT typedby
SATURNVINSTRUMENTUNIT` inverts a specialisation, and a six-hop containment walk
crossing one arrives at another vehicle's parts. Extraction keeps the relations
that live in prose -- `refines`, `dependson`, `performs`, and the `satisfies` it
infers rather than reads.

Also dropped: an edge duplicating one the syntax states, and any edge from an
element to itself. Twelve of the latter were `satisfies`, enough to report twelve
requirements as met by themselves and leave them out of the list of what nothing
satisfies.

Edges it writes are `RELATED_TO` with the relation in `relationship_type`, the same
shape extraction writes, so nothing downstream has to know which pass produced an
edge. `stated: true` marks the ones that came from here, for when you do want to
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

Writes `out/aql_examples_generated.md`, the primer AQLizer is given, from the
finished graph -- a fixed prompt holding what is true of any graph this pipeline
builds, plus a survey of this one: which entity types occur, which attribute names
exist and in what units, real short names, real snapshot names, which relations were
read and which inferred. A strong model (`EXAMPLES_MODEL`, default `gpt-5.5`) turns
the two into the file. Every ```aql block in what comes back is then parsed and run,
and anything that fails goes back with its error for a repair round; a query that
writes is refused rather than run.

**Why:** `sysml/aql_examples.md` is hand-written, and every paragraph of it was
learned by asking a question and working out why the answer was wrong. A corpus
imported next week gets none of that. This step is the same primer for the price of
a build.

It is a second file, not a replacement. `nl.instance()` still takes the hand-written
one; `nl.instance(config.AQL_EXAMPLES_GENERATED)` takes this one, and
`bespoke-aql-examples.ipynb` scores them against each other. `--no-examples` skips
the step.


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
