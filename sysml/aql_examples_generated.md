# AQL examples and graph-specific rules

## Collections and fields

`sysml_Entities` has 2,291 rows. It is the main element collection.

- `_id`, `_key`, `_rev`: exact database identifiers.
- `entity_name`: upper-case stored name. Treat it as an identifier, not display text. Always compare with `UPPER(@name)` or search with `CONTAINS(..., UPPER(@name))`.
- `entity_type`: lower-case type string. Values present here are only: `requirement`, `part`, `action`, `attribute`, `item`, `snapshot`, `package`, `state`, `port`, `enumeration`, `timeslice`, `calc`, `interface`, `analysis`, `connection`, `constraint`, `view`. Do not invent filters such as `component`, `system`, `element`, `function`, or `module`.
- `description`: LLM reading of prose. Useful for vague searches and context, not as exact structure.
- `attributes`: exact SysML values read from syntax. Present on 176 entities. It is a map from attribute name to either `{value, unit}` or `{expression}`. It is not a list.
- `short_name`: exact declaration identifier, present on 320 entities. Use this for requirement identifiers such as `HLR-R001`, `FLR-R001`, and unit symbols such as `kN`, `kPa`, `gn`.
- `source_file`, `source_line`: exact declaration location. Present on declared lexer-read elements; absent on prose-only concept rows.
- `files`, `models`: exact provenance lists. Use `IN`, not equality. Models present are `apollo-11-sysml-v2`, `DroneModelLogical`, and `Drone_BaseArchitecture`.
- `import_number`, `partition_id`: importer metadata.

`sysml_Relations` has 11,984 rows and is the only edge collection.

- `_from`, `_to`, `_id`, `_key`, `_rev`: exact edge identifiers.
- `type`: importer edge kind. Values present are `IN_COMMUNITY`, `RELATED_TO`, `MENTIONED_IN`, `SUB_COMMUNITY_OF`, `PART_OF`, `SIMILAR_TO`.
- `relationship_type`: only meaningful on `RELATED_TO`. Values present are `owns`, `typedby`, `specializes`, `satisfies`, `refines`, `performs`, `imports`, `redefines`, `connects`, `transitionsto`, `subject`, `dependson`.
- `stated`: exact lexer provenance when present and `true`. Inferred LLM relations do not have `stated: false`; the field is absent.
- `description`: edge description, often LLM/importer text.
- `source_file`, `source_line`: exact source location when the lexer read the relation.
- `weight`, `order`: importer metadata.
- On `SIMILAR_TO`, extra fields present include `cosine`, `rrf_score`, `analogy_role`, and `rank`. `analogy_role` values present are `requirement`, `part`, and `port`.

`sysml_Documents` has 30 rows.

- Fields shown in this graph are `models`, `files`, `file_name`, `file_ids`, `import_number`, `citable_url`, `_id`, `_key`, `_rev`.
- `models` and `files` are lists. `file_name` is the source path/name, such as `apollo-11-sysml-v2/Requirements/MissionRequirementsPackage.sysml`, `DroneModelLogical.sysml`, or `Drone_BaseArchitecture.sysml`.

`sysml_Chunks` has 114 rows.

- Fields shown in this graph are `models`, `files`, `chunk_order_index`, `import_number`, `tokens`, `_id`, `_key`, `_rev`.
- Chunks are importer text segments. Use them only when the question asks about source-document context; most engineering questions should use entities and relations.

`sysml_Communities` has 280 rows.

- Communities exist at levels 0, 1, 2, and 3.
- The report JSON has keys `findings`, `rating_explanation`, `rating`, `summary`, and `title`.
- Community membership is through `IN_COMMUNITY`; hierarchy is through `SUB_COMMUNITY_OF`.

## Resolve a name before using it

A bare English name may correspond to an exact upper-case row, an owner-prefixed row, or only a description hit. Prefer exact match, then owner-prefixed suffix, then containment search.

```aql
LET q = UPPER(@name)

LET exact = (
  FOR e IN sysml_Entities
    FILTER e.entity_name == q
    RETURN e
)

LET owner_prefixed = (
  FOR e IN sysml_Entities
    FILTER LIKE(e.entity_name, CONCAT("%_", q))
    RETURN e
)

LET contains_match = (
  FOR e IN sysml_Entities
    FILTER CONTAINS(e.entity_name, q)
       OR CONTAINS(LOWER(NOT_NULL(e.description, "")), LOWER(@name))
    LIMIT 25
    RETURN e
)

LET candidates =
  LENGTH(exact) > 0 ? exact :
  (LENGTH(owner_prefixed) > 0 ? owner_prefixed : contains_match)

FOR e IN candidates
  LIMIT 25
  RETURN {
    id: e._id,
    name: e.entity_name,
    type: e.entity_type,
    short_name: e.short_name,
    source_file: e.source_file,
    source_line: e.source_line,
    models: e.models
  }
```

## `models` and `files` are lists

Use `IN` against `models` and `files`. Equality against a string silently misses rows.

```aql
FOR e IN sysml_Entities
  FILTER "apollo-11-sysml-v2" IN e.models
  FILTER e.entity_type == "requirement"
  COLLECT source = e.source_file WITH COUNT INTO n
  SORT n DESC
  LIMIT 20
  RETURN {source_file: source, requirements: n}
```

## Count in AQL for “how many”

Do not return one row per match and count outside the query; result limits can cap the apparent answer.

```aql
FOR e IN sysml_Entities
  COLLECT type = e.entity_type WITH COUNT INTO n
  SORT n DESC
  RETURN {entity_type: type, count: n}
```

## Read a known attribute from the map

`attributes` is a map. Reach a known attribute by name. Numeric values use `value`; formulas use `expression`.

```aql
LET q = UPPER(@name)
LET qFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(q, " ", ""), "-", ""), "_", ""), "'", "")

FOR e IN sysml_Entities
  LET nameFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(e.entity_name, " ", ""), "-", ""), "_", ""), "'", "")
  FILTER e.entity_name == q
     OR LIKE(e.entity_name, CONCAT("%_", q))
     OR CONTAINS(e.entity_name, q)
     OR CONTAINS(nameFlat, qFlat)
     OR CONTAINS(LOWER(NOT_NULL(e.description, "")), LOWER(@name))
  FILTER HAS(e.attributes, "dryMass")
  LET a = e.attributes.dryMass
  RETURN {
    name: e.entity_name,
    type: e.entity_type,
    dryMass: a.value,
    unit: a.unit,
    expression: a.expression,
    source_file: e.source_file,
    source_line: e.source_line
  }
```

## Discover attribute names when the question does not know them

Use `ATTRIBUTES(e.attributes)` to iterate map keys.

```aql
LET q = UPPER(@name)
LET qFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(q, " ", ""), "-", ""), "_", ""), "'", "")

FOR e IN sysml_Entities
  LET nameFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(e.entity_name, " ", ""), "-", ""), "_", ""), "'", "")
  FILTER e.entity_name == q
     OR LIKE(e.entity_name, CONCAT("%_", q))
     OR CONTAINS(e.entity_name, q)
     OR CONTAINS(nameFlat, qFlat)
     OR CONTAINS(LOWER(NOT_NULL(e.description, "")), LOWER(@name))
  FILTER e.attributes != null
  FOR attrName IN ATTRIBUTES(e.attributes)
    LET a = e.attributes[attrName]
    RETURN {
      element: e.entity_name,
      type: e.entity_type,
      attribute: attrName,
      value: a.value,
      unit: a.unit,
      expression: a.expression
    }
```

## Rank elements by an attribute

Only sort numeric `value`s. Do not sort formulas as if they were numbers.

```aql
FOR e IN sysml_Entities
  FILTER HAS(e.attributes, "powerLoad")
  LET a = e.attributes.powerLoad
  FILTER IS_NUMBER(a.value)
  SORT a.value DESC
  LIMIT 20
  RETURN {
    name: e.entity_name,
    type: e.entity_type,
    powerLoad: a.value,
    unit: a.unit,
    source_file: e.source_file,
    source_line: e.source_line
  }
```

## Formulas live under `expression`, not `value`

Some attributes such as `mass`, `engines`, `stages`, `conversionFactor`, `softLandingAchieved`, and `instrumentsDeployed` are formulas or expressions.

```aql
FOR e IN sysml_Entities
  FILTER e.attributes != null
  FOR attrName IN ATTRIBUTES(e.attributes)
    LET a = e.attributes[attrName]
    FILTER HAS(a, "expression")
    SORT e.entity_name, attrName
    LIMIT 50
    RETURN {
      element: e.entity_name,
      type: e.entity_type,
      attribute: attrName,
      expression: a.expression,
      source_file: e.source_file,
      source_line: e.source_line
    }
```

## Sum over a containment subtree with `owns` and `typedby` only

For rollups, follow only `owns` and `typedby`. Part usages often have no values; the definition reached by `typedby` carries the declared numbers. Do not include `specializes` in a mass/power rollup.

```aql
LET q = UPPER(@name)

LET roots = (
  FOR e IN sysml_Entities
    FILTER e.entity_name == q
       OR LIKE(e.entity_name, CONCAT("%_", q))
       OR CONTAINS(e.entity_name, q)
    LIMIT 5
    RETURN e
)

FOR root IN roots
  LET carriers = UNIQUE(
    FOR v, edge, path IN 0..6 OUTBOUND root sysml_Relations
      FILTER path.edges[*].type ALL == "RELATED_TO"
      FILTER path.edges[*].relationship_type ALL IN ["owns", "typedby"]
      FILTER HAS(v.attributes, "dryMass")
      LET a = v.attributes.dryMass
      FILTER IS_NUMBER(a.value)
      RETURN {
        id: v._id,
        name: v.entity_name,
        dryMass: a.value,
        unit: a.unit
      }
  )
  RETURN {
    root: root.entity_name,
    totalDryMass: SUM(carriers[*].dryMass),
    unit: "kg",
    countedElements: carriers
  }
```

## Multi-hop traversals must constrain every edge in the path

Filtering only the last edge lets unrelated paths leak in. Use `path.edges[*] ... ALL ...`.

```aql
LET q = UPPER(@name)
LET qFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(q, " ", ""), "-", ""), "_", ""), "'", "")

FOR root IN sysml_Entities
  LET nameFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(root.entity_name, " ", ""), "-", ""), "_", ""), "'", "")
  FILTER root.entity_name == q
     OR LIKE(root.entity_name, CONCAT("%_", q))
     OR CONTAINS(root.entity_name, q)
     OR CONTAINS(nameFlat, qFlat)
     OR CONTAINS(LOWER(NOT_NULL(root.description, "")), LOWER(@name))
  FOR v, edge, path IN 1..6 OUTBOUND root sysml_Relations
    FILTER path.edges[*].type ALL == "RELATED_TO"
    FILTER path.edges[*].relationship_type ALL IN ["owns"]
    RETURN DISTINCT {
      owner: root.entity_name,
      contained: v.entity_name,
      contained_type: v.entity_type,
      depth: LENGTH(path.edges)
    }
```

## Show an element’s relations in both directions

Direction matters: `A satisfies B` points from design to requirement; `A owns B` points from container to contained; `A typedby B` points from usage to definition.

```aql
LET q = UPPER(@name)

LET targets = (
  FOR e IN sysml_Entities
    FILTER e.entity_name == q
       OR LIKE(e.entity_name, CONCAT("%_", q))
       OR CONTAINS(e.entity_name, q)
    LIMIT 10
    RETURN e
)

FOR x IN targets
  LET outgoing = (
    FOR v, edge IN 1..1 OUTBOUND x sysml_Relations
      FILTER edge.type == "RELATED_TO"
      SORT edge.relationship_type, v.entity_name
      RETURN {
        direction: "OUTBOUND",
        relationship_type: edge.relationship_type,
        other: v.entity_name,
        other_type: v.entity_type,
        stated: HAS(edge, "stated")
      }
  )
  LET incoming = (
    FOR v, edge IN 1..1 INBOUND x sysml_Relations
      FILTER edge.type == "RELATED_TO"
      SORT edge.relationship_type, v.entity_name
      RETURN {
        direction: "INBOUND",
        relationship_type: edge.relationship_type,
        other: v.entity_name,
        other_type: v.entity_type,
        stated: HAS(edge, "stated")
      }
  )
  RETURN {
    element: x.entity_name,
    type: x.entity_type,
    relations: APPEND(outgoing, incoming)
  }
```

## “What satisfies this requirement?” is inbound

A satisfier points to the requirement. For a requirement target, look `INBOUND` on `satisfies`.

```aql
LET q = UPPER(@requirementName)
LET qFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(q, " ", ""), "-", ""), "_", ""), "'", "")

FOR req IN sysml_Entities
  FILTER req.entity_type == "requirement"
  LET nameFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(req.entity_name, " ", ""), "-", ""), "_", ""), "'", "")
  LET shortFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(UPPER(NOT_NULL(req.short_name, "")), " ", ""), "-", ""), "_", ""), "'", "")
  FILTER req.entity_name == q
     OR UPPER(NOT_NULL(req.short_name, "")) == q
     OR LIKE(req.entity_name, CONCAT("%_", q))
     OR CONTAINS(req.entity_name, q)
     OR CONTAINS(nameFlat, qFlat)
     OR shortFlat == qFlat
     OR CONTAINS(LOWER(NOT_NULL(req.description, "")), LOWER(@requirementName))
  FOR design, edge IN 1..1 INBOUND req sysml_Relations
    FILTER edge.type == "RELATED_TO"
    FILTER edge.relationship_type == "satisfies"
    SORT HAS(edge, "stated") DESC, design.entity_name
    RETURN {
      requirement: req.entity_name,
      requirement_short_name: req.short_name,
      satisfier: design.entity_name,
      satisfier_type: design.entity_type,
      stated: HAS(edge, "stated"),
      source_file: edge.source_file,
      source_line: edge.source_line
    }
```

## Use `short_name` for requirement IDs and unit symbols

Declared identifiers such as `HLR-R001`, `FLR-R001`, `kN`, `kPa`, `gn`, `%`, and `yr` are in `short_name`, not necessarily in `entity_name`.

```aql
FOR e IN sysml_Entities
  FILTER e.short_name == @shortName
  RETURN {
    name: e.entity_name,
    type: e.entity_type,
    short_name: e.short_name,
    description: e.description,
    source_file: e.source_file,
    source_line: e.source_line,
    models: e.models
  }
```

## Vague noun searches should not invent a type

For “battery”, “system”, “function”, “module”, or other vague nouns, search names and descriptions. Do not filter by a non-existent type.

```aql
LET qUpper = UPPER(@topic)
LET qLower = LOWER(@topic)

FOR e IN sysml_Entities
  FILTER CONTAINS(e.entity_name, qUpper)
     OR CONTAINS(LOWER(NOT_NULL(e.description, "")), qLower)
  SORT e.entity_type, e.entity_name
  LIMIT 25
  RETURN {
    name: e.entity_name,
    type: e.entity_type,
    short_name: e.short_name,
    description: e.description,
    models: e.models,
    source_file: e.source_file,
    source_line: e.source_line
  }
```

## `stated` means lexer-read; absence means inferred

Do not test `stated == false`. Inferred edges lack the field.

```aql
LET q = UPPER(@name)

FOR x IN sysml_Entities
  FILTER x.entity_name == q
     OR LIKE(x.entity_name, CONCAT("%_", q))
     OR CONTAINS(x.entity_name, q)
  FOR v, edge IN 1..1 ANY x sysml_Relations
    FILTER edge.type == "RELATED_TO"
    COLLECT rel = edge.relationship_type,
            provenance = HAS(edge, "stated") ? "stated" : "inferred"
      WITH COUNT INTO n
    SORT rel, provenance
    RETURN {
      relationship_type: rel,
      provenance: provenance,
      count: n
    }
```

## Ask for stated SysML structure by requiring `stated`

The lexer-read structural relations in this graph include stated `owns`, `typedby`, `specializes`, `satisfies`, and `redefines`.

```aql
LET q = UPPER(@name)

FOR x IN sysml_Entities
  FILTER x.entity_name == q
     OR LIKE(x.entity_name, CONCAT("%_", q))
     OR CONTAINS(x.entity_name, q)
  FOR v, edge IN 1..1 OUTBOUND x sysml_Relations
    FILTER edge.type == "RELATED_TO"
    FILTER HAS(edge, "stated")
    FILTER edge.relationship_type IN ["owns", "typedby", "specializes", "satisfies", "redefines"]
    SORT edge.relationship_type, v.entity_name
    RETURN {
      from: x.entity_name,
      relationship_type: edge.relationship_type,
      to: v.entity_name,
      to_type: v.entity_type,
      source_file: edge.source_file,
      source_line: edge.source_line
    }
```

## Coverage: requirements with no satisfier

Unsatisfied-requirement questions are anti-joins on absent incoming `satisfies` edges.

```aql
FOR req IN sysml_Entities
  FILTER req.entity_type == "requirement"
  FILTER "apollo-11-sysml-v2" IN req.models

  LET satisfiers = (
    FOR design, edge IN 1..1 INBOUND req sysml_Relations
      FILTER edge.type == "RELATED_TO"
      FILTER edge.relationship_type == "satisfies"
      RETURN design._id
  )

  FILTER LENGTH(satisfiers) == 0
  SORT req.short_name, req.entity_name
  LIMIT 100
  RETURN {
    requirement: req.entity_name,
    short_name: req.short_name,
    source_file: req.source_file,
    source_line: req.source_line,
    description: req.description
  }
```

## Snapshot and timeslice questions start at the occurrence

Time-varying values are under `snapshot` and `timeslice` elements such as liftoff, crew ingress, translunar injection, powered descent, lunar surface ops, and reentry. Resolve the occurrence name, then walk down through containment/typing.

```aql
LET q = UPPER(@moment)

FOR occurrence IN sysml_Entities
  FILTER occurrence.entity_type IN ["snapshot", "timeslice"]
  FILTER occurrence.entity_name == q
     OR LIKE(occurrence.entity_name, CONCAT("%_", q))
     OR CONTAINS(occurrence.entity_name, q)

  FOR v, edge, path IN 0..6 OUTBOUND occurrence sysml_Relations
    FILTER path.edges[*].type ALL == "RELATED_TO"
    FILTER path.edges[*].relationship_type ALL IN ["owns", "typedby"]
    FILTER v.attributes != null

    FOR attrName IN ATTRIBUTES(v.attributes)
      LET a = v.attributes[attrName]
      SORT v.entity_name, attrName
      RETURN {
        moment: occurrence.entity_name,
        element: v.entity_name,
        element_type: v.entity_type,
        attribute: attrName,
        value: a.value,
        unit: a.unit,
        expression: a.expression,
        source_file: v.source_file,
        source_line: v.source_line
      }
```

## Read one value at a moment

Use the occurrence subtree for questions such as cabin pressure, oxygen level, altitude, velocity, engine status, status, or location at a mission moment.

```aql
LET momentQ = UPPER(@moment)
LET momentFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(momentQ, " ", ""), "-", ""), "_", ""), "'", "")
LET attrQ = LOWER(@attributeName)
LET attrFlatQ = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(attrQ, " ", ""), "-", ""), "_", ""), "'", "")

FOR occurrence IN sysml_Entities
  FILTER occurrence.entity_type IN ["snapshot", "timeslice"]
  LET occurrenceFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(occurrence.entity_name, " ", ""), "-", ""), "_", ""), "'", "")
  FILTER occurrence.entity_name == momentQ
     OR LIKE(occurrence.entity_name, CONCAT("%_", momentQ))
     OR CONTAINS(occurrence.entity_name, momentQ)
     OR CONTAINS(occurrenceFlat, momentFlat)

  FOR v, edge, path IN 0..6 OUTBOUND occurrence sysml_Relations
    FILTER path.edges[*].type ALL == "RELATED_TO"
    FILTER path.edges[*].relationship_type ALL IN ["owns", "typedby"]
    FILTER v.attributes != null

    FOR attrName IN ATTRIBUTES(v.attributes)
      LET attrFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(LOWER(attrName), " ", ""), "-", ""), "_", ""), "'", "")
      FILTER attrFlat == attrFlatQ
         OR CONTAINS(attrFlat, attrFlatQ)
         OR CONTAINS(attrFlatQ, attrFlat)

      LET a = v.attributes[attrName]
      SORT v.entity_name
      RETURN {
        moment: occurrence.entity_name,
        element: v.entity_name,
        attribute: attrName,
        value: a.value,
        unit: a.unit,
        expression: a.expression
      }
```

## Community membership is not a SysML relation

Use `IN_COMMUNITY` for the community layer, not `RELATED_TO`.

```aql
LET q = UPPER(@name)
LET qFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(q, " ", ""), "-", ""), "_", ""), "'", "")

FOR e IN sysml_Entities
  LET nameFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(e.entity_name, " ", ""), "-", ""), "_", ""), "'", "")
  FILTER e.entity_name == q
     OR LIKE(e.entity_name, CONCAT("%_", q))
     OR CONTAINS(e.entity_name, q)
     OR CONTAINS(nameFlat, qFlat)
     OR CONTAINS(LOWER(NOT_NULL(e.description, "")), LOWER(@name))

  FOR c, edge IN 1..1 ANY e sysml_Relations
    FILTER edge.type == "IN_COMMUNITY"
    SORT c.level, NOT_NULL(c.report_json.title, c.title, "")
    RETURN {
      element: e.entity_name,
      community_id: c._id,
      level: c.level,
      title: NOT_NULL(c.report_json.title, c.title),
      summary: NOT_NULL(c.report_json.summary, c.summary),
      rating: NOT_NULL(c.report_json.rating, c.rating)
    }
```

## Search community reports directly

Community titles and summaries are useful for broad architectural questions.

```aql
LET q = LOWER(@topic)

FOR c IN sysml_Communities
  FILTER CONTAINS(LOWER(NOT_NULL(c.report_json.title, "")), q)
     OR CONTAINS(LOWER(NOT_NULL(c.report_json.summary, "")), q)
     OR CONTAINS(LOWER(NOT_NULL(c.report_json.findings, "")), q)
  SORT c.level, c.report_json.title
  LIMIT 20
  RETURN {
    community_id: c._id,
    level: c.level,
    title: c.report_json.title,
    summary: c.report_json.summary,
    rating: c.report_json.rating,
    rating_explanation: c.report_json.rating_explanation
  }
```

## Community hierarchy uses `SUB_COMMUNITY_OF`

Use the hierarchy edge separately from entity membership.

```aql
LET q = LOWER(@communityTitle)

FOR c IN sysml_Communities
  LET title = NOT_NULL(c.report_json.title, c.title, "")
  FILTER CONTAINS(LOWER(title), q)

  LET parents = (
    FOR p, edge IN 1..1 OUTBOUND c sysml_Relations
      FILTER edge.type == "SUB_COMMUNITY_OF"
      RETURN {
        id: p._id,
        level: p.level,
        title: NOT_NULL(p.report_json.title, p.title)
      }
  )

  LET children = (
    FOR child, edge IN 1..1 INBOUND c sysml_Relations
      FILTER edge.type == "SUB_COMMUNITY_OF"
      RETURN {
        id: child._id,
        level: child.level,
        title: NOT_NULL(child.report_json.title, child.title)
      }
  )

  RETURN {
    community: title,
    level: c.level,
    parents: parents,
    children: children
  }
```

## Computed similarity is `SIMILAR_TO`, not SysML

Similarity edges compare entities across embedding space. They carry `cosine`, `rrf_score`, `rank`, and `analogy_role`; they do not carry `relationship_type`.

```aql
LET q = UPPER(@name)
LET qFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(q, " ", ""), "-", ""), "_", ""), "'", "")

FOR e IN sysml_Entities
  LET nameFlat = SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(e.entity_name, " ", ""), "-", ""), "_", ""), "'", "")
  FILTER e.entity_name == q
     OR LIKE(e.entity_name, CONCAT("%_", q))
     OR CONTAINS(e.entity_name, q)
     OR CONTAINS(nameFlat, qFlat)
     OR CONTAINS(LOWER(NOT_NULL(e.description, "")), LOWER(@name))

  FOR other, edge IN 1..1 ANY e sysml_Relations
    FILTER edge.type == "SIMILAR_TO"
    FILTER edge.analogy_role IN ["requirement", "part", "port"]
    SORT edge.cosine DESC
    LIMIT 20
    RETURN {
      element: e.entity_name,
      similar_to: other.entity_name,
      similar_type: other.entity_type,
      analogy_role: edge.analogy_role,
      cosine: edge.cosine,
      rrf_score: edge.rrf_score,
      rank: edge.rank,
      description: edge.description
    }
```

## Use documents for file-level questions

Documents are source-file records. Their `models` and `files` fields are lists, and `file_name` is the path/name.

```aql
FOR d IN sysml_Documents
  FILTER "apollo-11-sysml-v2" IN d.models
  SORT d.file_name
  RETURN {
    document_id: d._id,
    file_name: d.file_name,
    files: d.files,
    models: d.models,
    citable_url: d.citable_url
  }
```

## Use source fields for declaration provenance

Declared entities have `source_file` and `source_line`; prose-only concept rows may not. Prefer these fields over description text when citing SysML declarations.

```aql
LET q = UPPER(@name)

FOR e IN sysml_Entities
  FILTER e.entity_name == q
     OR LIKE(e.entity_name, CONCAT("%_", q))
     OR CONTAINS(e.entity_name, q)
  SORT e.source_file, e.source_line
  LIMIT 25
  RETURN {
    name: e.entity_name,
    type: e.entity_type,
    short_name: e.short_name,
    declared: e.source_file != null,
    source_file: e.source_file,
    source_line: e.source_line,
    models: e.models,
    files: e.files
  }
```