# AQL examples for the SysML corpus

This file is the only place domain knowledge about the graph is written down. It is
passed verbatim to `ReadOnlyArangoGraphQAChain.from_llm(aql_examples=...)`, which is
the service's supported way to teach it a schema. There are no hand-written query
functions anywhere in this project; if AQLizer gets something wrong, the fix belongs
here.

Everything below was run against the graph before being written down.

## What the collections hold

Three SysML v2 models, read by graphrag_importer's extraction pipeline and written
by its own ArangoDB writer. Every field below is one the importer writes, except
`files` and `models`, which the load step adds.

`sysml_Entities` -- one extracted element each, 2503 of them.
  `entity_name` the element's name **in upper case**: `SATURNV`, `HLR-R060`,
     `COMMAND/SERVICE MODULE`. Extraction upper-cases every name, so a comparison
     against a mixed-case literal matches nothing.
  `entity_type` one of the 27 SysML kinds, **lower case**: `requirement`, `part`,
     `action`, `attribute`, `package`, `state`, `port`, `item`, `constraint`,
     `calc`, `analysis`, `connection`, `interface`, `view`, `viewpoint`,
     `enumeration`, `concern`, `flow`, `allocation`, `event`, `metadata`,
     `usecase`, `rendering`, `verification`, `snapshot`, `timeslice`, `occurrence`.
     There is no type called `Function`, `RequirementUsage` or `PartDefinition`.
  `description` prose describing the element, written by the extraction step from
     the source text. Numbers, units and rationale live in here as words -- there
     is no separate attributes map.
  `files` a LIST of the source files the element was found in. An element found in
     two files has two entries.
  `models` a LIST, each one of `apollo-11`, `drone-logical`, `drone-base`.
  `clusters` the Leiden cluster assignments backing the community layer.

`sysml_Documents` -- one per source file, 30 of them.
  `file_name` e.g. `apollo-11-sysml-v2/Requirements/TechnicalRequirementsPackage.sysml`
  `citable_url`, `file_ids`, `content` (the whole file), plus `files` and `models`.

`sysml_Chunks` -- 114 windows of source text.
  `content`, `tokens`, `chunk_order_index`, plus `files` and `models`.

`sysml_Communities` -- 138 Leiden clusters with an LLM-written report.
  `title`, `report_string`, `level` (0, 1 or 2), `occurrence`, `sub_communities`,
  and `report_json` with `title`, `summary`, `findings`, `rating`,
  `rating_explanation`.

`sysml_Relations` is the edge collection and holds every edge kind.
  `type` is the importer's own closed vocabulary:
     `RELATED_TO`, `MENTIONED_IN`, `PART_OF`, `IN_COMMUNITY`, `SUB_COMMUNITY_OF`,
     plus `SIMILAR_TO` for the analogy layer.
  `relationship_type` on a `RELATED_TO` edge is the SysML relation, **lower case**:
     `refines`, `satisfies`, `typedby`, `imports`, `dependson`, `owns`, `performs`,
     `connects`, `exhibits`, `transitionsto`, `sliceof`, `specializes`,
     `variantof`, `derives`, `subject`, `valueref`, `redefines`.
     Note `typedby`, `transitionsto`, `dependson` and `variantof` have no capital
     letter in the middle -- they are stored lower-cased.
  Filtering `type == 'RELATED_TO'` alone gives every SysML relation mixed together.
  A question about a specific relation must also filter `relationship_type`.
  The structural edges have no `relationship_type`, so grouping by it counts SysML
  relations only. Group by `type` to count the structural edges as well.

## The two things most likely to go wrong

**Case.** Names are upper case, types are lower case. Compare a name the model
supplies with `UPPER(@wanted)`, or use `CONTAINS(e.entity_name, UPPER(@wanted))`.

**`files` and `models` are lists, not strings.** Use `IN`:

```aql
FOR e IN sysml_Entities
  FILTER 'drone-logical' IN e.models
  FILTER e.entity_type == 'part'
  RETURN {name: e.entity_name, files: e.files}
```

A vague or plural question -- *"tell me about engines"*, *"anything to do with
batteries"* -- is not naming one element and must not be turned into an equality
test. Use containment, and search the description as well as the name:

```aql
FOR e IN sysml_Entities
  FILTER CONTAINS(e.entity_name, "ENGINE")
      OR CONTAINS(LOWER(e.description), "engine")
  LIMIT 25
  RETURN {name: e.entity_name, type: e.entity_type, files: e.files,
          description: e.description}
```

Numbers are in the prose, not in a structured field. A question about a value has
to return the description and let the summary read it out; do not try to arrive at
a total by arithmetic over fields that do not exist:

```aql
FOR e IN sysml_Entities
  FILTER CONTAINS(LOWER(e.description), "dry mass")
  LIMIT 15
  RETURN {name: e.entity_name, files: e.files, description: e.description}
```

## Directions

The edge always points from the thing that acts to the thing acted on.

  `A satisfies B` -- A is the design element, B is the requirement it meets.
  `A refines B` -- A is the more detailed statement, B the one being refined.
  `A performs B` -- A is the part, B the action.
  `A owns B` -- A is the container, B the contained.
  `A typedby B` -- A is the usage, B is the definition typing it.

So "which requirements does nothing satisfy" looks for requirements with no
**incoming** `satisfies` edge:

```aql
FOR e IN sysml_Entities
  FILTER e.entity_type == 'requirement' AND 'drone-logical' IN e.models
  LET satisfied = LENGTH(
    FOR r IN sysml_Relations
      FILTER r._to == e._id AND r.relationship_type == 'satisfies'
      RETURN 1)
  FILTER satisfied == 0
  RETURN {requirement: e.entity_name, files: e.files}
```

## Examples

Count the elements of each kind in one model:

```aql
FOR e IN sysml_Entities
  FILTER 'apollo-11' IN e.models
  COLLECT type = e.entity_type WITH COUNT INTO n
  SORT n DESC LIMIT 10
  RETURN {type, n}
```

What satisfies what, with the sentence the extraction wrote for the edge:

```aql
FOR r IN sysml_Relations
  FILTER r.relationship_type == 'satisfies'
  LET a = DOCUMENT(r._from), b = DOCUMENT(r._to)
  FILTER a != null AND b != null
  LIMIT 20
  RETURN {satisfier: a.entity_name, requirement: b.entity_name,
          how: r.description, files: a.files}
```

Everything one named element is attached to, in both directions:

```aql
LET e = FIRST(FOR x IN sysml_Entities
               FILTER x.entity_name == UPPER(@name) RETURN x)
FOR v, r IN 1..1 ANY e sysml_Relations
  FILTER r.type == 'RELATED_TO'
  RETURN {relation: r.relationship_type, other: v.entity_name,
          direction: r._from == e._id ? 'outgoing' : 'incoming',
          description: r.description}
```

Which elements appear in more than one source file -- extraction merges elements by
name, so this finds the concepts that several files talk about:

```aql
FOR e IN sysml_Entities
  FILTER LENGTH(e.files) > 1
  SORT LENGTH(e.files) DESC
  LIMIT 20
  RETURN {name: e.entity_name, type: e.entity_type, files: e.files}
```

Which elements are shared between two of the three models -- the same merge, seen
across model boundaries:

```aql
FOR e IN sysml_Entities
  FILTER LENGTH(e.models) > 1
  RETURN {name: e.entity_name, type: e.entity_type, models: e.models}
```

The community layer. Level 0 is the coarsest, level 2 the finest; `occurrence` is
how much of the corpus the cluster covers:

```aql
FOR c IN sysml_Communities
  FILTER c.level == 0
  SORT c.occurrence DESC
  LIMIT 5
  RETURN {title: c.report_json.title, summary: c.report_json.summary,
          findings: c.report_json.findings, occurrence: c.occurrence}
```

Which elements are in a community, through the `IN_COMMUNITY` edge:

```aql
FOR c IN sysml_Communities
  FILTER c.level == 0
  SORT c.occurrence DESC LIMIT 1
  FOR e IN 1..1 INBOUND c sysml_Relations
    FILTER IS_SAME_COLLECTION('sysml_Entities', e)
    LIMIT 30
    RETURN {community: c.report_json.title, member: e.entity_name,
            type: e.entity_type}
```

Which file an element came from, walking it rather than reading `files` -- useful
when the question is about the source text rather than the element:

```aql
LET e = FIRST(FOR x IN sysml_Entities
               FILTER x.entity_name == UPPER(@name) RETURN x)
FOR chunk IN 1..1 OUTBOUND e sysml_Relations
  FILTER IS_SAME_COLLECTION('sysml_Chunks', chunk)
  FOR doc IN 1..1 OUTBOUND chunk sysml_Relations
    FILTER IS_SAME_COLLECTION('sysml_Documents', doc)
    RETURN DISTINCT {file: doc.file_name, url: doc.citable_url,
                     text: chunk.content}
```

## Analogies between models

`SIMILAR_TO` edges are the only ones in the graph that were **computed** rather
than read out of a source file. Every `RELATED_TO` edge is something the
extraction found stated in the text; a `SIMILAR_TO` edge means two elements of the
same kind in different models resemble each other. Keep them apart: a question
about what the model *says* must filter `type == 'RELATED_TO'`.

  `analogy_role` the shared `entity_type`, e.g. `requirement`, `part`
  `cosine` how close the two descriptions are; nothing below 0.55 is kept
  `weight` the same number, so the retriever ranks on it

What plays a given element's role in another model:

```aql
LET e = FIRST(FOR x IN sysml_Entities
               FILTER x.entity_name == UPPER(@name) RETURN x)
FOR v, r IN 1..1 ANY e sysml_Relations
  FILTER r.type == 'SIMILAR_TO'
  SORT r.cosine DESC
  RETURN {counterpart: v.entity_name, models: v.models,
          role: r.analogy_role, cosine: r.cosine, why: r.description}
```

Every analogy, strongest first:

```aql
FOR r IN sysml_Relations
  FILTER r.type == 'SIMILAR_TO'
  LET a = DOCUMENT(r._from), b = DOCUMENT(r._to)
  FILTER a != null AND b != null
  SORT r.cosine DESC
  LIMIT 25
  RETURN {a: a.entity_name, a_model: a.models, b: b.entity_name,
          b_model: b.models, role: r.analogy_role, cosine: r.cosine}
```

Which elements of one model have **no** counterpart in another -- the more useful
question, because it names what the smaller model does not cover:

```aql
FOR e IN sysml_Entities
  FILTER 'drone-logical' IN e.models AND e.entity_type == 'part'
  LET analogues = LENGTH(
    FOR v, r IN 1..1 ANY e sysml_Relations
      FILTER r.type == 'SIMILAR_TO' RETURN 1)
  FILTER analogues == 0
  RETURN {name: e.entity_name, files: e.files}
```
