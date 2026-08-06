# AQL examples for the SysML corpus

This file is the only place domain knowledge about the graph is written down. It is
passed verbatim to `ReadOnlyArangoGraphQAChain.from_llm(aql_examples=...)`, which is
the service's supported way to teach it a schema. There are no hand-written query
functions anywhere in this project; if AQLizer gets something wrong, the fix belongs
here.

Everything below was run against the graph before being written down.

## What the collections hold

Three SysML v2 models, read two ways. An LLM extraction pipeline reads the prose
and supplies the entities, the descriptions and the relations the text implies. A
lexer then reads the same files for the things the syntax states outright --
attribute values, containment and typing -- because those must be exact. Fields
from the second pass are marked *(stated)* below and are always trustworthy;
everything else is the LLM's reading.

`sysml_Entities` -- one element each, 2751 of them.
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
     the source text.
  `attributes` *(stated)* a MAP of attribute name -> `{value, unit}` for a number
     the file assigns, or `{expression}` when the file assigns a formula rather
     than a value. 162 elements carry one. Units are reduced to the bare symbol,
     so `[kg]`, `[SI::kg]` and `['kg']` all read `kg`. Common names: `dryMass`,
     `propellantMass`, `powerLoad`, `failureRate`, `mass`, and the four
     `...Cost` attributes on `APOLLO11MISSION`.
  `short_name` *(stated)* the identifier the element was declared under, e.g.
     `DE-REQ-1` for `POWER` or `CLR-R083` for `SEPARATIONTIMERPRECISION`. 320
     elements have one, nearly all of them requirements. An element has ONE row
     whichever of its two names is used, so match on `short_name` -- never expect
     `entity_name == 'DE-REQ-1'`.
  `source_file` / `source_line` *(stated)* where the element is declared. Present
     on the 1531 elements the lexer matched; null for one the LLM named but no
     declaration does.
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
     `owns`, `typedby`, `specializes`, `refines`, `satisfies`, `imports`,
     `dependson`, `performs`, `connects`, `exhibits`, `transitionsto`, `sliceof`,
     `variantof`, `derives`, `subject`, `valueref`, `redefines`.
     Note `typedby`, `transitionsto`, `dependson` and `variantof` have no capital
     letter in the middle -- they are stored lower-cased.
  `stated` is `true` on an edge the lexer read out of the syntax (`owns`,
     `typedby`, `specializes`, `redefines`, `satisfies`) and ABSENT -- not false,
     not null-checked some other way -- on one the LLM inferred. A question about
     where a relation came from is `r.stated == true` versus `r.stated != true`;
     `r.type` says nothing about it, and every `RELATED_TO` has a `type`.
     Both kinds fill `relationship_type`, so a query never has to care which; the
     flag is there for when you do.
  Filtering `type == 'RELATED_TO'` alone gives every SysML relation mixed together.
  A question about a specific relation must also filter `relationship_type`.
  The structural edges have no `relationship_type`, so grouping by it counts SysML
  relations only. Group by `type` to count the structural edges as well.

## The two things most likely to go wrong

**Case.** Names are upper case, types are lower case. Compare a name the model
supplies with `UPPER(@wanted)`, or use `CONTAINS(e.entity_name, UPPER(@wanted))`.

**`entity_type` is a single string; `files` and `models` are lists.** Compare
`entity_type` with `==`, never with `IN`. And a noun from the question is not
necessarily a type: there is no `component`, `system`, `element` or `function`
type. If the question's noun is not one of the 27 above, do not filter on
`entity_type` at all -- filter on what is actually being asked for (an attribute,
a name, a relation) and let that select the rows.

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

Provenance is a property of the edge, so split on `stated` and never on `type`.
An element belongs to a model through its own `models` list, not through the
edge's:

```aql
FOR r IN sysml_Relations
  FILTER r.type == "RELATED_TO"
  LET from = DOCUMENT(r._from)
  FILTER from != null
  FOR model IN from.models
    COLLECT m = model, read = r.stated == true WITH COUNT INTO n
    RETURN {model: m, source: read ? "read from the syntax" : "inferred by the LLM",
            relations: n}
```

An identifier like `DE-REQ-1`, `CLR-R083` or `FLR-R046` is a `short_name`, not an
`entity_name`. The element is one row, stored under its written name, so look the
identifier up in the field that holds it:

```aql
FOR e IN sysml_Entities
  FILTER e.short_name == "DE-REQ-1"
  RETURN {name: e.entity_name, short: e.short_name, type: e.entity_type,
          description: e.description,
          at: CONCAT(e.source_file, ":", e.source_line)}
```

**`attributes` is a map, not a list**, so `e.attributes[*].unit` does not iterate
it. Reach a known attribute by name, and expand the map with `ATTRIBUTES()` when
the name is not known in advance:

```aql
FOR e IN sysml_Entities
  FILTER e.attributes != null
  FOR name IN ATTRIBUTES(e.attributes)
    LET a = e.attributes[name]
    FILTER a.unit == "kg" AND a.value != null
    SORT a.value DESC LIMIT 15
    RETURN {element: e.entity_name, attribute: name, value: a.value, unit: a.unit,
            at: CONCAT(e.source_file, ":", e.source_line)}
```

A question that says *components*, *parts of the system*, *elements* or *items* is
describing things in general, not naming an `entity_type`. Filter on the attribute
being asked about and nothing else -- adding `entity_type == 'component'` matches
no row, because that is not one of the 27 types:

```aql
FOR e IN sysml_Entities
  FILTER e.attributes.failureRate.value != null
  SORT e.attributes.failureRate.value DESC
  RETURN {element: e.entity_name, value: e.attributes.failureRate.value,
          unit: e.attributes.failureRate.unit,
          at: CONCAT(e.source_file, ":", e.source_line)}
```

An attribute has either a `value` (with a `unit`) or an `expression`, never both.
`totalMass` on `MASSEDCOMPONENT` is `{expression: "mass + sum(subcomponents.totalMass)"}`
-- report that as computed rather than as a number, and never sum it.

## Directions

The edge always points from the thing that acts to the thing acted on.

  `A satisfies B` -- A is the design element, B is the requirement it meets.
  `A refines B` -- A is the more detailed statement, B the one being refined.
  `A performs B` -- A is the part, B the action.
  `A owns B` -- A is the container, B the contained.
  `A typedby B` -- A is the usage, B is the definition typing it.

So "what owns X" and "what satisfies X" are **INBOUND** from X, while "what does X
contain" and "what does X specialize" are OUTBOUND. A question that asks for both
sides at once wants `ANY`, and `ANY` with `r._from == e._id` tells the two apart:

```aql
LET e = FIRST(FOR x IN sysml_Entities FILTER x.short_name == @short RETURN x)
FOR v, r IN 1..1 ANY e sysml_Relations
  FILTER r.type == 'RELATED_TO'
  RETURN {relation: r.relationship_type, other: v.entity_name,
          direction: r._from == e._id ? 'outgoing' : 'incoming'}
```

Picking one direction because the wording sounds one-way is the most common way to
get an empty result from a question that has an answer.

## "How many" means COUNT, not a list

The result set is capped, so a query that returns one row per match and leaves the
counting to the reader reports the cap as the answer. Compute the number in AQL.
When the question wants both a number and examples, return both, and take the
number from the whole set rather than from the sample:

```aql
LET unsatisfied = (
  FOR e IN sysml_Entities
    FILTER e.entity_type == 'requirement' AND 'apollo-11' IN e.models
    FILTER LENGTH(FOR r IN sysml_Relations
             FILTER r._to == e._id AND r.relationship_type == 'satisfies'
             RETURN 1) == 0
    RETURN e.entity_name)
RETURN {total: LENGTH(unsatisfied), examples: SLICE(unsatisfied, 0, 10)}
```

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

**Summing a value over a containment subtree.** This is the shape most
quantitative questions take, and it has one trap: a part *usage* carries no values
of its own. `part stage1 : 'S-IC'` is an occurrence of the part *definition*
`S-IC`, and `dryMass` is declared on the definition. So the traversal has to follow
`owns` and `typedby` **together**, or it walks to the usages and finds nothing.

```aql
FOR e IN sysml_Entities
  FILTER e.entity_name == "SATURNV"
  LET parts = (
    FOR child, edge IN 1..6 OUTBOUND e sysml_Relations
      FILTER edge.relationship_type IN ["owns", "typedby"]
      FILTER child.attributes.dryMass.value != null
      RETURN DISTINCT {name: child.entity_name, mass: child.attributes.dryMass.value,
                       unit: child.attributes.dryMass.unit,
                       at: CONCAT(child.source_file, ":", child.source_line)})
  RETURN {total: SUM(parts[*].mass), unit: "kg", contributors: parts}
```

Summing several named attributes on one element -- the mission's costs:

```aql
FOR e IN sysml_Entities
  FILTER e.entity_name == "APOLLO11MISSION"
  LET costs = (
    FOR name IN ATTRIBUTES(e.attributes)
      FILTER LIKE(LOWER(name), "%cost%") AND e.attributes[name].value != null
      RETURN {name, value: e.attributes[name].value, unit: e.attributes[name].unit})
  RETURN {total: SUM(costs[*].value), currency: "$", parts: costs}
```

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

What plays a given element's role in another model. Only a minority of elements
have an analogy at all, so do **not** pick one candidate with `FIRST` and traverse
from it -- a fuzzy name match will usually land on one of the many that has none.
Iterate every candidate and let the `SIMILAR_TO` filter do the narrowing:

```aql
FOR e IN sysml_Entities
  FILTER CONTAINS(e.entity_name, UPPER(@name))
  FOR v, r IN 1..1 ANY e sysml_Relations
    FILTER r.type == 'SIMILAR_TO'
    SORT r.cosine DESC
    RETURN {element: e.entity_name, counterpart: v.entity_name,
            models: v.models, role: r.analogy_role, cosine: r.cosine,
            why: r.description}
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
