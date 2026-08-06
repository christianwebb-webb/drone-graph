"""The three steps that build the graph, in the order they run.

  extract   models/*.sysml -> out/kg, GraphRAG's own LLM extraction
  load      out/kg -> the graphrag_importer collections in ArangoDB
  analogy   cross-model SIMILAR_TO edges, found by autograph's SimilarityFinder

Each step is runnable on its own (`python -m sysml.pipeline.extract`) and reads
only what the step before it left behind, so a rerun can start anywhere.
`build.py` runs all three. Reading the graph afterwards is `sysml.nl`, which is
not part of this.
"""
