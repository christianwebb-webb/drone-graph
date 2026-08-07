"""The steps that build the graph, in the order they run.

  extract   models/*.sysml -> out/kg, GraphRAG's own LLM extraction
  load      out/kg -> the graphrag_importer collections in ArangoDB, then
            `structure` over the same sources for the parts that must be exact
  analogy   cross-model SIMILAR_TO edges, found by autograph's SimilarityFinder
  examples  the finished graph -> aql_examples_generated.md, the AQLizer primer,
            written by a strong model from a survey of what was built

`structure` is not a step of its own because it has to run inside `load`, between
the import and the vector indexes: it creates an entity for anything a file
declares that the extraction missed, and ArangoDB will not accept a document
without a value in an indexed vector field. It is still runnable on its own
(`python -m sysml.pipeline.structure`) for iterating on the lexer.

`examples` is the only step that reads the database rather than the sources, and it
is last for that reason: it describes the finished graph, analogy edges included.
Nothing else depends on it, and the read side uses the hand-written primer unless
asked for the generated one, so `build.py --no-examples` changes no answer.

Each step reads only what the step before it left behind, so a rerun can start
anywhere. `build.py` runs all of them. Reading the graph afterwards is `sysml.nl`,
which is not part of this.
"""
