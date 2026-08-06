# AQL examples for the SysML corpus

This file is the only place domain knowledge about the graph is written down. It is
passed verbatim to `ReadOnlyArangoGraphQAChain.from_llm(aql_examples=...)`, which is
the service's supported way to teach it a schema. There are no hand-written query
functions anywhere in this project; if AQLizer gets something wrong, the fix belongs
here.

Everything below was run against the graph before being written down.

## What the collections hold

The corpus is three SysML v2 models projected into the graphrag_importer schema.

`sysml_Entities` -- one SysML element each.
  `entity_name` full qualified name, e.g. `TechnicalComponentsPackage::SaturnV`
  `name` the last segment; `short_name` the `<'HLR-R001'>` alias, or null
  `entity_type` the SysML metatype: `RequirementUsage`, `RequirementDefinition`,
     `PartUsage`, `PartDefinition`, `ActionUsage`, `ActionDefinition`, `StateUsage`,
     `StateDefinition`, `AttributeUsage`, `Package`, `LibraryStub`, ...
     There is no metatype called `Requirement`, `Part` or `Function`.
  `model` one of `apollo-11`, `drone-logical`, `drone-base`
  `layer` the source folder: `Requirements`, `Technical`, `Logical`, `Function`,
     `Purpose`, `Program`, `Execution`, `Analysis`, `CoSMA`, `Drone`, `Library`
  `source_file` / `source_line` where it is declared -- always return these
  `doc` the authored documentation text
  `attributes` a MAP of name -> `{value, unit, raw}`; a computed attribute has
     `{expression: "..."}` and no `value`
  `constraints` a LIST of constraint strings
  `is_variation` / `is_variant` variation points and their alternatives
  `is_library` true for a name referenced by the model but declared outside it

`sysml_Relations` is the edge collection and holds every edge kind.
  `type` is the importer's own closed vocabulary:
     `RELATED_TO`, `MENTIONED_IN`, `PART_OF`, `IN_COMMUNITY`, `SUB_COMMUNITY_OF`
  `relationship_type` on a `RELATED_TO` edge is the **authored SysML relation**:
     `owns`, `typedBy`, `specializes`, `redefines`, `satisfies`, `refines`,
     `derives`, `performs`, `subject`, `exhibits`, `connects`, `transitionsTo`,
     `variantOf`, `valueRef`, `imports`, `sliceOf`
  Filtering `type == 'RELATED_TO'` alone gives every SysML relation mixed together.
  A question about a specific relation must also filter `relationship_type`.
  The structural edges have no `relationship_type`, so grouping by it counts SysML
  relations only. Group by `type` to count the structural edges as well.

Names are stored exactly as the model declares them and matching is case-sensitive.
`Apollo11Mission`, `SaturnV` and `forestFireObservationDrone` will not match a
lower-cased literal. When the question names a specific element but the case is
uncertain, compare with `LOWER(e.name) == LOWER(@wanted)`.

That is for an exact identifier. A vague or plural question -- *"tell me about
engines"*, *"anything to do with batteries"* -- is not naming one element and must
not be turned into an equality test, which will match nothing. Use containment and
search the description too:

```aql
FOR e IN sysml_Entities
  FILTER CONTAINS(LOWER(e.entity_name), "engine")
      OR CONTAINS(LOWER(e.description), "engine")
  LIMIT 25
  RETURN {entity_name: e.entity_name, entity_type: e.entity_type,
          at: CONCAT(e.source_file, ":", e.source_line)}
```

`attributes` is a **map**, not a list, so `e.attributes[*].unit` does not iterate it.
To filter or list attributes across elements, expand the map with `ATTRIBUTES()` and
index back into it:

```aql
FOR e IN sysml_Entities
  FILTER e.attributes != null
  FOR attrName IN ATTRIBUTES(e.attributes)
    LET a = e.attributes[attrName]
    FILTER a.unit == "kg" AND a.value != null
    SORT a.value DESC
    RETURN {element: e.entity_name, attribute: attrName, value: a.value, unit: a.unit,
            at: CONCAT(e.source_file, ":", e.source_line)}
```

`sysml_Chunks` source text windows (`content`, `file_name`, `start_line`, `end_line`).
`sysml_Documents` one per `.sysml` file (`file_name`, `file_ids`).
`sysml_Communities` clusters with a generated `report_string`, `level` 0 or 1.

## Directions that are easy to get backwards

`satisfy R by S` is stored as an edge **from the satisfier to the requirement**.
So the satisfiers of a requirement are its INBOUND `satisfies` edges, and a
requirement nothing satisfies is one with no inbound `satisfies` edge.

`owns` runs from container to contained. So to ask *"is this element inside
SaturnV?"* the hop is INBOUND `owns` from the element, or OUTBOUND from SaturnV.
Going OUTBOUND from the element reaches what it contains, not what contains it.

`typedBy` and `specializes` run from the usage to the type it is declared against.

**A definition and its usages often differ only in the first letter.**
`AstronautSafety` is the definition and `astronautSafety` is a usage of it;
`SaturnV` is a definition and `saturnV` a usage. They are different documents with
different edges. Traceability -- `satisfies`, `refines`, `performs`, `subject` --
is almost always authored on the **definition**, and attribute values are always on
the definition. So when a question names something in CamelCase, match it exactly;
do not silently lower-case the first letter. If which one is meant is genuinely
unclear, match both with `LOWER(e.name) == LOWER(@wanted)` and return whichever has
the edges.

```aql
FOR e IN sysml_Entities
  FILTER LOWER(e.name) == LOWER("AstronautSafety")
  LET refiners = (
    FOR x, edge IN 1..1 INBOUND e sysml_Relations
      FILTER edge.relationship_type == "refines"
      RETURN {name: x.entity_name, at: CONCAT(x.source_file, ":", x.source_line)})
  FILTER LENGTH(refiners) > 0
  RETURN {matched: e.entity_name, entity_type: e.entity_type, refiners}
```

## Examples

Find an element by name or short name. SysML names are CamelCase and never spaced.

```aql
FOR e IN sysml_Entities
  FILTER e.name == "SaturnV" OR e.short_name == "SaturnV"
  RETURN {entity_name: e.entity_name, entity_type: e.entity_type,
          at: CONCAT(e.source_file, ":", e.source_line)}
```

What satisfies a requirement -- inbound `satisfies`.

```aql
FOR e IN sysml_Entities
  FILTER e.short_name == "HLR-R001"
  FOR s, edge IN 1..1 INBOUND e sysml_Relations
    FILTER edge.type == "RELATED_TO" AND edge.relationship_type == "satisfies"
    RETURN {satisfier: s.entity_name, at: CONCAT(s.source_file, ":", s.source_line)}
```

Requirements that nothing satisfies -- the gap query.

```aql
FOR e IN sysml_Entities
  FILTER e.entity_type IN ["RequirementUsage", "RequirementDefinition"]
  FILTER e.model == "apollo-11"
  LET satisfiers = LENGTH(
    FOR r IN sysml_Relations
      FILTER r._to == e._id AND r.relationship_type == "satisfies"
      LIMIT 1 RETURN 1)
  FILTER satisfiers == 0
  RETURN {entity_name: e.entity_name,
          at: CONCAT(e.source_file, ":", e.source_line)}
```

Count relations by their authored SysML type.

```aql
FOR r IN sysml_Relations
  FILTER r.type == "RELATED_TO"
  COLLECT relation = r.relationship_type WITH COUNT INTO n
  SORT n DESC
  RETURN {relation, count: n}
```

"How many ..." wants one number. Aggregate in the query; do not return the matching
documents and leave the caller to count rows. `RETURN COUNT(e)` inside a `FOR` is
not a count of the matches -- it counts the attributes of one document.

```aql
FOR e IN sysml_Entities
  FILTER e.model == "apollo-11" AND e.entity_type == "RequirementDefinition"
  COLLECT WITH COUNT INTO n
  RETURN n
```

Read a numeric attribute. Note the `.value` hop, and that a computed attribute has
`.expression` instead -- report those as computed, not as a number.

```aql
FOR e IN sysml_Entities
  FILTER e.attributes.dryMass.value != null
  RETURN {entity_name: e.entity_name, value: e.attributes.dryMass.value,
          unit: e.attributes.dryMass.unit,
          at: CONCAT(e.source_file, ":", e.source_line)}
```

Sum a numeric attribute over a containment subtree. A part **usage** carries no
values of its own -- `part stage1 : 'S-IC'` is an occurrence of the part
**definition** `S-IC`, and the mass is declared there. So a rollup has to follow
`owns` and `typedBy` together, or it walks to the usages and finds nothing.

```aql
FOR e IN sysml_Entities
  FILTER e.name == "SaturnV"
  LET parts = (
    FOR child, edge IN 1..6 OUTBOUND e sysml_Relations
      FILTER edge.relationship_type IN ["owns", "typedBy"]
      FILTER child.attributes.dryMass.value != null
      RETURN DISTINCT {name: child.name, mass: child.attributes.dryMass.value,
                       unit: child.attributes.dryMass.unit,
                       at: CONCAT(child.source_file, ":", child.source_line)})
  RETURN {total: SUM(parts[*].mass), contributors: parts}
```

Multi-hop traceability: a function, what performs it, and what specializes that.

Same usage-versus-definition rule as the rollup, in its other form. `perform
LaunchSystem::guideAscentTrajectory` attaches the `performs` edge to the **usage**,
and the usage is `typedBy` the `GuideAscentTrajectory` **definition**. Starting from
the definition, the first hop is INBOUND `typedBy` to reach its usages; only then
does INBOUND `performs` find the component.

```aql
FOR fn IN sysml_Entities
  FILTER fn.name == "GuideAscentTrajectory"
  FOR usage, typing IN 1..1 INBOUND fn sysml_Relations
    FILTER typing.relationship_type == "typedBy"
    FOR logical, performed IN 1..1 INBOUND usage sysml_Relations
      FILTER performed.relationship_type == "performs"
      FOR technical, spec IN 1..1 INBOUND logical sysml_Relations
        FILTER spec.relationship_type == "specializes"
        RETURN DISTINCT {function: fn.name, logical: logical.name,
                         technical: technical.name,
                         at: CONCAT(technical.source_file, ":", technical.source_line)}
```

A feature whose value is another element carries a `valueRef` edge to it, alongside
the stored value. That covers two different questions, told apart by the target and
not by the edge:

- the target `is_variant` -- a concrete part binding one alternative of a `variation`
  with `:>> feature = Variation::variant`
- the target is an `EnumerationUsage` -- a feature set to an enumeration literal

So a question about what a configuration selects filters `valueRef` **and**
`is_variant`. Comparing those selections against each other is how a configuration is
checked for internal consistency; nothing in SysML enforces it.

```aql
FOR e IN sysml_Entities
  FILTER e.name == "forestFireObservationDrone"
  FOR variant, edge IN 1..2 OUTBOUND e sysml_Relations
    FILTER edge.relationship_type == "valueRef" AND variant.is_variant == true
    RETURN {feature: DOCUMENT(edge._from).name, variant: variant.name,
            value: variant.attributes.value.value,
            at: CONCAT(variant.source_file, ":", variant.source_line)}
```

The same edge, read the other way -- which enumeration literal a feature is set to.

```aql
FOR e IN sysml_Entities
  FILTER e.name == "NASA"
  FOR lit, edge IN 1..2 OUTBOUND e sysml_Relations
    FILTER edge.relationship_type == "valueRef"
    FILTER lit.entity_type == "EnumerationUsage"
    RETURN {feature: DOCUMENT(edge._from).entity_name, literal: lit.name}
```

Variation points and the variants under them.

```aql
FOR v IN sysml_Entities
  FILTER v.is_variation == true AND v.model == "drone-logical"
  LET variants = (
    FOR child, edge IN 1..1 INBOUND v sysml_Relations
      FILTER edge.relationship_type == "variantOf"
      RETURN {name: child.name, value: child.attributes.value.value,
              at: CONCAT(child.source_file, ":", child.source_line)})
  RETURN {variation: v.entity_name, variants}
```

Community reports, for a question about the model as a whole rather than one element.

```aql
FOR c IN sysml_Communities
  FILTER c.level == 1
  SORT c.occurrence DESC
  RETURN {title: c.title, members: c.occurrence, report: c.report_string}
```

State machine order, following `transitionsTo`. Transitions are declared between
the state **usages** inside a state machine body, so start from a usage (lowercase
names like `poweredDescent`) rather than from the phase definition.

```aql
FOR e IN sysml_Entities
  FILTER e.name == "poweredDescent"
  FOR next, edge IN 1..3 OUTBOUND e sysml_Relations
    FILTER edge.relationship_type == "transitionsTo"
    RETURN {to: next.name, trigger: edge.trigger,
            at: CONCAT(next.source_file, ":", next.source_line)}
```

Note for query writers: the service's read-only guard tests whether the strings
INSERT, UPDATE, REPLACE, REMOVE or UPSERT appear **anywhere** in the query text, not
as keywords. An element name containing one of them -- `LunarOrbitInsertionPhase`
contains "INSERT" -- makes a perfectly ordinary read query be rejected as a write.
Where a question is about such an element, match it another way, for example
`FILTER e.short_name == ...` or `FILTER CONTAINS(e.name, "LunarOrbit")`.
