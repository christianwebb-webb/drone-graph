"""The three steps that build the graph, in the order they run.

  parse     .sysml sources -> out/model.json, a resolved {elements, relations}
  project   out/model.json -> the graphrag_importer collections in ArangoDB
  enrich    communities, community reports, embeddings and vector indexes

Each step is runnable on its own (`python -m sysml.pipeline.parse`) and reads only
what the step before it left behind, so a rerun can start anywhere. `build.py` runs
all three. Reading the graph afterwards is `sysml.nl`, which is not part of this.
"""
