"""The steps that build the graph, in the order they run.

  extract   models/*.sysml -> out/kg, GraphRAG's own LLM extraction
  load      out/kg -> the graphrag_importer collections in ArangoDB, then
            `structure` over the same sources for the parts that must be exact
  analogy   cross-model SIMILAR_TO edges, found by autograph's SimilarityFinder

`structure` is not a step of its own because it has to run inside `load`, between
the import and the vector indexes: it creates an entity for anything a file
declares that the extraction missed, and ArangoDB will not accept a document
without a value in an indexed vector field. It is still runnable on its own
(`python -m sysml.pipeline.structure`) for iterating on the lexer.

Each step reads only what the step before it left behind, so a rerun can start
anywhere. `build.py` runs all of them. Reading the graph afterwards is `sysml.nl`,
which is not part of this.
"""
