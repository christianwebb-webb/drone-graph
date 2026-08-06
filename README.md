# drone-graph

Three SysML v2 models turned into an ArangoDB graph you can ask questions about in
English.

```bash
docker run -d --name christian-webb-drone-arango -p 8529:8529 \
  -e ARANGO_ROOT_PASSWORD=testpass arangodb:3.12.9.4 \
  arangod --experimental-vector-index=true

export CHAT_API_KEY=sk-...      # OpenAI
python build.py                 # parse -> project -> enrich
```

Then open `simple-demo.ipynb`, which does the whole thing end to end.

You must have the 3 requisite Arango repos cloned adjacent to this one.

## The pipeline

`build.py` runs three steps in order. Each one is also runnable on its own
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


`graphrag_importer` - The schema. Collection names, the `embedding` field and the edge vocabulary are imported from `graphrag.naming`, so an importer-side reader finds everything where it expects it 
`graphrag_retrievers` - The whole GraphRAG read path — `RetrievalService`, its local and global retrievers, the hybrid search indexes it builds for itself, and its citations 
`autograph` - `DataStorage` builds the vector indexes, including the `defaultNProbe` that keeps a search from silently reading part of the index 
`natural-language-service` - `txt2aql` writes and runs the AQL for the AQLizer path 

The two services normally run as pods beside the platform. Running them here means
supplying the few things a pod would get from its surroundings: a database JWT
(ArangoDB issues one at `/_open/auth`), a sink for progress reporting, and a token
validator. Everything under those is the services' own code, unmodified.
