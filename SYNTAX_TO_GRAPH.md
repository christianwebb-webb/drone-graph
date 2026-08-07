# SysML syntax -> what it becomes in the graph

A `.sysml` file is read twice. The extraction step asks an LLM what the text means;
`structure` reads the same file with a lexer for what the syntax states outright.
Both write into the same collections, so a single declaration can produce a row from
one pass, a field from the other, and edges from both.

Three destinations, and which one a piece of syntax lands in is the thing this file
is about:

- **an entity** -- a row in `sysml_Entities` with an `entity_type`
- **a field** -- something written onto an entity that already exists
  (`attributes`, `short_name`, `source_file`)
- **an edge** -- a `RELATED_TO` row in `sysml_Relations` with a `relationship_type`

Names are stored upper case; types and relation labels are stored lower case.

## Declarations that become entities

The keyword decides the type. `def` and its absence do not: `part def Pump` and
`part coolantPump : Pump` are both `part`, and they are two separate entities.

| Syntax | `entity_type` |
| --- | --- |
| `package P { ... }` | `package` |
| `part def P` / `part p : P` | `part` |
| `action def A` / `action a` | `action` |
| `state def S` / `state s`, and `exhibit state s : S` | `state` |
| `port def P` / `port p : P` | `port` |
| `item def I` / `item i : I` | `item` |
| `attribute a : Real` (no value -- see below) | `attribute` |
| `requirement def <'REQ-4'> R` / `requirement r : R` | `requirement` |
| `calc def C` / `calc c` | `calc` |
| `analysis def A` / `analysis case a` | `analysis` |
| `connection def C` / `connection c` | `connection` |
| `interface def I` / `interface i : I` | `interface` |
| `view V` | `view` |
| `viewpoint V` | `viewpoint` |
| `enum def K { enum high; enum low; }` -- the definition **and** each literal | `enumeration` |
| `concern C` | `concern` |
| `constraint def C` / `constraint c` (named only) | `constraint` |
| `flow f` | `flow` |
| `allocation def A` / `allocation a` | `allocation` |
| `event e` | `event` |
| `metadata def M` | `metadata` |
| `use case def U` / `use case u` -- `case` belongs to the keyword | `usecase` |
| `rendering r` | `rendering` |
| `verification def V` | `verification` |
| `snapshot s { ... }` | `snapshot` |
| `timeslice t { ... }` | `timeslice` |
| `occurrence def O` / `occurrence o` | `occurrence` |

That is the closed set. There is no `component`, `system`, `function`,
`partDefinition` or `requirementUsage` type, and a question that uses one of those
words is not naming a type.

### Modifiers and prefixes are stripped, not typed

Everything in front of the keyword is dropped and the keyword still decides:

```
individual part def Apollo11MissionIndividual :> Apollo11Mission   -> part
ref part capability[1] : Capability                                -> part
private part def Pump                                              -> part
#Approved part x                                                   -> part
abstract requirement def R                                         -> requirement
standard library package ScalarValues                              -> package
exhibit state phases : StateAction                                 -> state
perform action load                                                -> action
assert constraint isSafe { ... }                                   -> constraint
```

The full modifier set is `private public protected abstract ref in out inout
readonly derived end nonunique ordered library individual variation variant standard
constant portion`. A relationship keyword in front of a declaration
(`exhibit`, `perform`, `assert`, `require`) is also dropped: the thing being declared
is what follows it.

A multiplicity (`[1..*]`) after the name is dropped too -- it is not part of the
name and it is not stored.

## Syntax that becomes a field rather than an entity

**`attribute name = value` is the big one.** An attribute with a value is a property
of whatever declares it, not an element of its own, because that is where a rollup
expects to find it. `part def 'S-IC' { attribute dryMass = 137000 [kg]; }` puts

```json
"attributes": { "dryMass": { "value": 137000, "unit": "kg" } }
```

on `S-IC` and creates no `dryMass` row. Three value shapes:

| Written | Stored |
| --- | --- |
| `= 137000 [kg]`, `= 4.5 [SI::m/s]`, `= 3 ['s']` | `{value: <number>, unit: "kg"}` -- the unit reduced to the bare symbol |
| `= "Skylab"` | `{value: "Skylab"}` |
| `= mass + sum(subcomponents.totalMass)` | `{expression: "mass + sum(...)"}` -- never a number, never summed |

An attribute with **no** value (`attribute ratedFlow : Real;`) has nothing to put in
the map, so it stays an `attribute` entity with a `typedby` edge instead. An
attribute declared with a value at the top of a file, owned by nothing, also becomes
its own entity carrying its own map.

**`<'DE-REQ-1'>` is a short name, not an element.** It lands in `short_name` on the
element it was declared on, and is appended to the description so lexical search can
find it. An element written both ways is one row -- the duplicate the extraction
pass made under the other name is merged away.

**`doc /* ... */`, `//` and `/* */`** declare nothing. Their text feeds the LLM's
`description`, and nothing in them is ever an element -- a concept a comment names,
an identifier series a comment implies, and a section heading are all excluded on
purpose.

**Where the declaration is** goes into `source_file` and `source_line`. A row with no
`source_file` is a name the extraction read in prose and no declaration backs; that
is the filter to reach for on any question that counts or ranks.

## Syntax that becomes an edge

These five come out of the lexer, so they carry `stated: true` and a file and line.
The LLM's guesses at the first four are deleted after the lexer runs -- it reads them
exhaustively and correctly, and an inferred one points backwards often enough to
break a multi-hop walk.

| Syntax | `relationship_type` | Direction |
| --- | --- | --- |
| declared inside another element's body | `owns` | container -> contained |
| `part p : Pump` | `typedby` | usage -> definition |
| `part def Boost :> Pump` (each target of a comma list) | `specializes` | specific -> general |
| `attribute :>> dryMass = 40` | `redefines` | redefinition -> feature redefined |
| `satisfy REQ by DESIGN` | `satisfies` | design -> requirement |

A bare `:>` or `:>>` on its own line inside a body continues the enclosing
declaration, and is read from that element rather than from anything named on the
line.

`satisfies` is the one relation both passes write, so it appears with and without
`stated`.

## Relations only the LLM produces

These are read out of prose and out of syntax the lexer does not resolve, so they
have no `stated` flag and no file and line:

`refines` `derives` `performs` `subject` `exhibits` `connects` `transitionsto`
`variantof` `imports` `sliceof` `sends` `dependson` `valueref`

Which means a question that needs one of them is answered from the LLM's reading. A
question about containment, typing, specialisation or redefinition is answered from
the syntax, and the two should not be mixed in a count.

## Syntax that produces nothing

Worth knowing, because the absence looks like a bug from the outside:

| Written | Why nothing |
| --- | --- |
| `subject sys : System;` | `subject` is not a declaration keyword. The LLM's `subject` edges are the only trace. |
| `assert constraint { x < 5 }`, `require constraint { y > 1 }` | anonymous -- no name, so no identity to store. A *named* `assert constraint c { ... }` does become a `constraint`. |
| `text = "shall stay cool";` | no `attribute` keyword in front, so it is not read as a value. Requirement text survives only in the description. |
| `private import MissionPackage::*;` | `import` is not a declaration. The `imports` edges come from the LLM. |
| `#refinement dependency A to B;` | `dependency` is not a declaration keyword. `refines` / `dependson` come from the LLM. |
| `transition first a accept Ev then b;` | not a declaration; `transitionsto` comes from the LLM. |
| `perform Other::doThing :>> doThing;` | no keyword after the prefix, so nothing is declared. `perform action load` does declare an action. |
| `interface i : IFace connect a to b;` | the interface and its `typedby` are stored; the two `connect` ends are not. `connects` comes from the LLM. |
| `individual part : 'Gus Grissom' :> crew;` | anonymous -- the name slot holds a type reference, not a name. |
| a type from an imported library (`Real`, `String`, an SI unit) | referenced, never declared here. The `typedby` edge is dropped when its far end resolves to nothing in the model. |

## Names, and why one element can be two rows

A bare name is not unique. `spacecraft` is declared once per mission snapshot and
`umbilicalPort` on five different parts, so a contested name is stored with as much
of its owner prefixed as it takes to separate it: `CONTROLPORT_POWER`,
`MISSIONSYSTEMATLOI_SPACECRAFT`. An uncontested name is left alone.

Separately, SysML lets one identifier be both a short name and a name --
`requirement def <'HLR-R001'> CrewReturnSafety` and `requirement 'HLR-R001' :
CrewReturnSafety` are two elements. The definition takes the `refines` edges and the
usage is what `satisfy 'HLR-R001' by ...` names, so match `short_name` and
`entity_name` both and return both rows.

## What the current corpus actually contains

Three models, read by the lexer into 1,855 declared elements and 3,576 stated
relations. Eleven of the 27 types have no members here, which is a property of these
files and not of the mapping:

| Present | Absent |
| --- | --- |
| `requirement` `part` `action` `attribute` `item` `snapshot` `package` `state` `port` `enumeration` `timeslice` `calc` `interface` `analysis` `connection` `view` | `viewpoint` `concern` `constraint` `flow` `allocation` `event` `metadata` `usecase` `rendering` `verification` `occurrence` |

`concern` is worth a note: the Apollo model declares `item def Concern`, which is an
`item` called Concern and not the `concern` keyword. `constraint` is absent because
every constraint in the corpus is anonymous.

Relations, by where they came from:

| From the syntax (`stated: true`) | From the LLM |
| --- | --- |
| `owns` `typedby` `specializes` `satisfies` `redefines` | `refines` `satisfies` `performs` `connects` `imports` `subject` `transitionsto` `exhibits` `derives` `dependson` |

Both lists are re-derivable rather than taken on trust:

```aql
FOR r IN sysml_Relations
  COLLECT type = r.type, relation = r.relationship_type, stated = r.stated == true
  WITH COUNT INTO n
  SORT n DESC
  RETURN {type, relation, stated, n}
```

```aql
FOR e IN sysml_Entities
  FILTER e.source_file != null
  COLLECT type = e.entity_type WITH COUNT INTO n
  SORT n DESC
  RETURN {type, n}
```

## Non-SysML edges in the same collection

`sysml_Relations` holds more than the SysML relations, and only `RELATED_TO` rows
carry a `relationship_type`:

- `MENTIONED_IN` entity -> chunk, `PART_OF` chunk -> document -- the provenance chain
- `IN_COMMUNITY` entity -> community, `SUB_COMMUNITY_OF` -- the Leiden layer
- `SIMILAR_TO` -- the analogy layer, computed rather than read out of any file. A
  question about what a model *states* has to filter `type == 'RELATED_TO'`.
