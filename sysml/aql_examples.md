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

`sysml_Entities` -- one element each, 2291 of them.
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
     than a value. 176 elements carry one, but the map is present and empty on
     many more, so test `LENGTH(ATTRIBUTES(e.attributes)) > 0` rather than
     `e.attributes != null`. Units are reduced to the bare symbol,
     so `[kg]`, `[SI::kg]` and `['kg']` all read `kg`. Common names: `dryMass`,
     `propellantMass`, `powerLoad`, `failureRate`, `mass`, and the four
     `...Cost` attributes on `APOLLO11MISSION`.
  `short_name` *(stated)* the identifier the element was declared under, e.g.
     `DE-REQ-1` for `POWER` or `CLR-R083` for `SEPARATIONTIMERPRECISION`. 320
     elements have one, nearly all of them requirements. An element has ONE row
     whichever of its two names is used, so match on `short_name` -- never expect
     `entity_name == 'DE-REQ-1'`.
  `source_file` / `source_line` *(stated)* where the element is declared. Present
     on the 1851 elements the lexer matched; null for one the LLM named but no
     declaration does.
  `files` a LIST of the source files the element was found in. An element found in
     two files has two entries.
  `models` a LIST. One name per top-level entry under `models/`, here
     `apollo-11-sysml-v2`, `DroneModelLogical` and `Drone_BaseArchitecture`.
     Almost every element belongs to exactly one.
  `clusters` the Leiden cluster assignments backing the community layer.

`sysml_Documents` -- one per source file, 30 of them.
  `file_name` e.g. `apollo-11-sysml-v2/Requirements/TechnicalRequirementsPackage.sysml`
  `citable_url`, `file_ids`, `content` (the whole file), plus `files` and `models`.

`sysml_Chunks` -- 114 windows of source text.
  `content`, `tokens`, `chunk_order_index`, plus `files` and `models`.

`sysml_Communities` -- 280 Leiden clusters with an LLM-written report.
  `title`, `report_string`, `level` (0 to 3), `occurrence`, `sub_communities`,
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

## The three things most likely to go wrong

**Counting rows the files do not declare.** The graph holds two layers. An element
with a `source_file` is one a file declares; an element without one is a name the
extraction step read in prose, and no declaration backs it. Likewise an edge with
`stated: true` is one the syntax states, and an edge without it is the LLM's reading.

Any question that counts, ranks, or asks what is *missing* is answered over a set,
and the set it means is the declared one. So put `e.source_file != null` on the
elements and `r.stated == true` on the relations, on **every** such query, exactly as
you would for a lookup:

```aql
FOR e IN sysml_Entities
  FILTER e.entity_type == @kind AND @model IN e.models
  FILTER e.source_file != null
  LET covered = COUNT(FOR r IN sysml_Relations
                        FILTER r._to == e._id AND r.relationship_type == @relation
                        AND r.stated == true
                        RETURN 1)
  FILTER covered == 0
  RETURN {element: e.entity_name, at: CONCAT(e.source_file, ":", e.source_line)}
```

Leaving either filter off does not merely add noise -- it changes the answer, because
an undeclared row can never be the subject of a stated relation and so lands in every
"nothing satisfies it" result. If a question deliberately wants the prose layer too,
say so in the answer.

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
  FILTER 'DroneModelLogical' IN e.models
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

A bare name is not unique. SysML lets any number of declarations share one -- a
feature called `power` on several ports, a part called `spacecraft` in each
snapshot of a timeline -- so where that happens each declaration is stored under a
name prefixed with enough of its owner to tell them apart: `CONTROLPORT_POWER`,
`MISSIONSYSTEMATLOI_SPACECRAFT`. The unprefixed name may also exist as a row, but
that one is the concept extraction read in the prose, and it has no `source_file`.

So a question about a *declared* element should match the end of the name and take
the declared ones, rather than testing the bare name for equality:

```aql
FOR e IN sysml_Entities
  FILTER e.source_file != null
  FILTER e.entity_name == UPPER(@wanted) OR LIKE(e.entity_name, CONCAT("%_", UPPER(@wanted)))
  RETURN {name: e.entity_name, owner_hint: e.entity_name,
          at: CONCAT(e.source_file, ":", e.source_line)}
```

Reaching one through the containment tree is better still when the question names
a context ("the spacecraft at LOI"): start at the context and walk `owns`.

**The same filter belongs on a population, not only on a lookup.** Counting,
ranking and coverage questions -- *how many X*, *which X have no Y*, *the top ten X
by Y* -- are answered over a set, and a set of elements means the declared ones. A
row without a `source_file` is a name the extraction step read in prose and no
declaration backs; it can never be the subject of a relation the file states, so it
lands in every *"nothing satisfies it"* and *"has no owner"* answer and inflates it.
Filter both sides:

```aql
FOR e IN sysml_Entities
  FILTER e.entity_type == @kind AND @model IN e.models
  FILTER e.source_file != null                      // a declared element
  LET covered = COUNT(FOR r IN sysml_Relations
                        FILTER r._to == e._id AND r.relationship_type == @relation
                        AND r.stated == true        // a relation the file states
                        RETURN 1)
  FILTER covered == 0
  RETURN {element: e.entity_name, at: CONCAT(e.source_file, ":", e.source_line)}
```

Dropping `stated` from the inner count answers a different and weaker question --
"is there any evidence at all", including the LLM's reading -- so say which one was
asked. Counting the same element twice is the other half of this: an element the
prose names several ways can produce several unstated edges from what is really one
source, so a count of *satisfiers* should be over `DISTINCT r._from`, not over rows.

An identifier like `DE-REQ-1`, `CLR-R083` or `FLR-R046` is usually a `short_name`
rather than an `entity_name`, so it has to be looked up in the field that holds it.
But SysML lets the same identifier be *both*: a definition declared as
`requirement def <'HLR-R001'> CrewReturnSafetyRequirement` carries it as a short
name, while a usage of that definition can be declared as
`requirement 'HLR-R001' : CrewReturnSafetyRequirement`, where it is the element's
actual name. They are two elements, and the edges divide between them in a way that
matters: the definition takes the `refines` from other requirements, and the usage
is what `satisfy 'HLR-R001' by ...` names.

So match both fields and return both rows, rather than taking `FIRST` of either:

```aql
FOR e IN sysml_Entities
  FILTER e.short_name == UPPER(@id) OR e.entity_name == UPPER(@id)
  RETURN {name: e.entity_name, short: e.short_name, type: e.entity_type,
          role: e.short_name == UPPER(@id) ? "declared with this identifier"
                                           : "named by this identifier",
          description: e.description,
          at: CONCAT(e.source_file, ":", e.source_line)}
```

`FIRST(...)` on an identifier is the trap: it silently picks one of the two and the
answer looks complete. Walking outward from only that one is how a requirement with
twenty relations comes back with one.

**`attributes` is a map, not a list**, so `e.attributes[*].unit` does not iterate
it. Reach a known attribute by name, and expand the map with `ATTRIBUTES()` when
the name is not known in advance:

```aql
FOR e IN sysml_Entities
  FILTER LENGTH(ATTRIBUTES(e.attributes)) > 0
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
FOR e IN sysml_Entities
  FILTER e.short_name == UPPER(@id) OR e.entity_name == UPPER(@id)
  FOR v, r IN 1..1 ANY e sysml_Relations
    FILTER r.type == 'RELATED_TO'
    RETURN {of: e.entity_name, relation: r.relationship_type, other: v.entity_name,
            direction: r._from == e._id ? 'outgoing' : 'incoming',
            stated: r.stated == true}
```

Picking one direction because the wording sounds one-way is the most common way to
get an empty result from a question that has an answer.

## A multi-hop walk must constrain EVERY edge, not the last one

`FOR v, e IN 1..6 OUTBOUND x edges FILTER e.relationship_type == 'owns'` does not
do what it looks like. `e` is the **last** edge of each path, so the filter admits
any path whose final step is `owns` no matter what the earlier steps were. Over six
hops that reaches most of the graph.

To constrain the whole path, filter the path:

```aql
FOR v, e, p IN 1..6 OUTBOUND @start sysml_Relations
  OPTIONS {uniqueVertices: "path"}
  FILTER p.edges[*].relationship_type ALL IN ["owns", "typedby"]
  RETURN DISTINCT v
```

A traversal starting at `1` excludes the element you started from. When the question
is about *those elements themselves* -- "give each stage's dry mass" -- do not
traverse at all, just read their attributes. Traversing instead returns a row per
element with every value null, which reads as "the model does not record this" when
the truth is that the walk stepped straight past it:

```aql
FOR e IN sysml_Entities
  FILTER e.entity_name IN @names AND e.source_file != null
  RETURN {element: e.entity_name,
          dryMass: e.attributes.dryMass.value,
          propellantMass: e.attributes.propellantMass.value,
          at: CONCAT(e.source_file, ":", e.source_line)}
```

Traverse only when the question is about what those elements contain, and use `0..N`
when it is about both.

This matters most for containment rollups. A part's subtree is reached by `owns`
to each usage and `typedby` from a usage to the definition that carries the values
-- and by nothing else. Let one `specializes` in and the walk climbs into an
abstract supertype, then back down into every other thing that specializes it,
which is a different vehicle's parts.

## A question about a moment in time starts at the occurrence, not the part

SysML models change over time with `snapshot` and `timeslice` declarations, each
redeclaring the parts it is about and giving them the values they hold at that
moment. The values are therefore on elements *inside* the snapshot, not on the
static component -- so start at the snapshot and walk down, collecting whatever
carries attributes:

Names are declaration identifiers with the case flattened, so they carry no
spaces: the snapshot for lunar orbit insertion is `ATLOI`, not
`AT LUNAR ORBIT INSERTION`. A phrase taken from the question will not match.
Strip the spaces out of it, and match an abbreviation too when the model uses one:

```aql
FOR occurrence IN sysml_Entities
  FILTER occurrence.entity_type IN ["snapshot", "timeslice"]
  FILTER CONTAINS(occurrence.entity_name, UPPER(SUBSTITUTE(@moment, " ", "")))
  FOR v, e, p IN 1..5 OUTBOUND occurrence sysml_Relations
    FILTER p.edges[*].relationship_type ALL IN ["owns", "redefines", "typedby"]
    FILTER LENGTH(ATTRIBUTES(v.attributes)) > 0
    RETURN DISTINCT {element: v.entity_name, values: v.attributes,
                     at: CONCAT(v.source_file, ":", v.source_line)}
```

Reading the static part instead answers with whatever the model says in general,
which is a different question and usually a different number.

## "How many" means COUNT, not a list

The result set is capped, so a query that returns one row per match and leaves the
counting to the reader reports the cap as the answer -- ask for a total over three
hundred requirements and get back "41", the size of the truncated list.

Deciding this from the wording is unreliable, so decide it from the answer instead:
**if the answer is a single number, the query must return a single row.** Compute it
in AQL. The safe form covers both readings at once and is the one to reach for
whenever a question is about a set rather than about one element -- the total is
taken over the whole set, and the examples are a sample of it:

```aql
LET unsatisfied = (
  FOR e IN sysml_Entities
    FILTER e.entity_type == 'requirement' AND 'apollo-11-sysml-v2' IN e.models
    FILTER e.source_file != null
    FILTER LENGTH(FOR r IN sysml_Relations
             FILTER r._to == e._id AND r.relationship_type == 'satisfies'
             AND r.stated == true
             RETURN 1) == 0
    RETURN e.entity_name)
RETURN {total: LENGTH(unsatisfied), examples: SLICE(unsatisfied, 0, 10)}
```

Return one row per match only when the rows themselves are the answer and the
question named no number -- "which requirements does nothing satisfy", where each
one has to be identified. It looks for requirements with no **incoming** `satisfies`
edge:

```aql
FOR e IN sysml_Entities
  FILTER e.entity_type == 'requirement' AND 'DroneModelLogical' IN e.models
  FILTER e.source_file != null
  LET satisfied = LENGTH(
    FOR r IN sysml_Relations
      FILTER r._to == e._id AND r.relationship_type == 'satisfies'
      AND r.stated == true
      RETURN 1)
  FILTER satisfied == 0
  RETURN {requirement: e.entity_name, at: CONCAT(e.source_file, ":", e.source_line)}
```

Both filters are load-bearing and neither is optional decoration. Without
`source_file != null` the answer also counts the names extraction read in prose,
which no declaration backs and no stated relation can reach, so every one of them
lands in the result. Without `stated == true` it counts the LLM's reading of the
prose as coverage.

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
  FILTER 'apollo-11-sysml-v2' IN e.models
  FILTER e.source_file != null
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

Everything a named element is attached to, in both directions. Note the outer `FOR`
where `FIRST(...)` would be shorter: a name or an identifier can belong to more than
one row -- a definition and a usage, a short name and a name -- and collapsing to one
of them drops the other's edges without saying so. Iterate, and let the rows show how
many were found:

```aql
FOR e IN sysml_Entities
  FILTER e.entity_name == UPPER(@name) OR e.short_name == UPPER(@name)
  FOR v, r IN 1..1 ANY e sysml_Relations
    FILTER r.type == 'RELATED_TO'
    RETURN {of: e.entity_name, relation: r.relationship_type, other: v.entity_name,
            direction: r._from == e._id ? 'outgoing' : 'incoming',
            stated: r.stated == true, description: r.description}
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
FOR e IN sysml_Entities
  FILTER e.entity_name == UPPER(@name) OR e.short_name == UPPER(@name)
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
  FILTER 'DroneModelLogical' IN e.models AND e.entity_type == 'part'
  FILTER e.source_file != null
  LET analogues = LENGTH(
    FOR v, r IN 1..1 ANY e sysml_Relations
      FILTER r.type == 'SIMILAR_TO' RETURN 1)
  FILTER analogues == 0
  RETURN {name: e.entity_name, at: CONCAT(e.source_file, ":", e.source_line)}
```
