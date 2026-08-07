"""The extraction prompts this project replaces, and why.

`GraphRAG` takes a `custom_prompts` mapping and falls back to its own for every
key that is absent (graph_builder/builder/prompt.py:406), so these two replace the
defaults and the rest are left alone.

The defaults come from Microsoft's GraphRAG and are written for narrative prose.
All three worked examples in the entity prompt are fiction -- characters in a room,
a mission with a code name -- and the community prompt asks for a group's "legal
compliance, technical capabilities, reputation". Handed a .sysml file, a model
following those examples does what they demonstrate: it reads for meaning and
reports what it understood. That is the right behaviour for an article and the
wrong one for a declaration, where the answer is already written down and reading
for meaning can only add to it.

What it added here was a layer of elements no file declares -- identifiers
continuing a series past its last member, section headings promoted to elements,
concepts named only in a comment, and one element under several composed names.
None of it is reachable from the syntax, so `structure` never stamps it with a
source file, and a query that filters on one cannot see it. A query that does not
filter cannot tell it from a real element.

The gleaning pass is switched off where these are set, and that is the larger half
of the fix. Its prompt opens "MANY entities were missed in the last extraction"
and asks for another round, with no way to answer that nothing was missed. On
prose that recovers real omissions. On a file whose declarations were all found the
first time, the only way to comply is to invent, and it invents in the rare types
because the prompt asks for those specifically.

Two things shape what is written below. The names have to survive: identity in
this graph is the name a file writes, so a composed or reworded name belongs to no
element and can never be matched to a declaration. And the syntax has a direction:
an inverted edge does not merely lose information, it states the reverse of what
the file says, which is worse than the edge being absent.
"""

from __future__ import annotations

# The placeholders are filled by `extract_entities` (_op.py:483) and every one of
# them has to survive: the template is passed through `str.format`, so a missing
# name raises and a stray brace is read as a field. Braces inside the SysML
# examples are doubled for that reason.
ENTITY_EXTRACTION = """-Goal-
You are reading the source text of a SysML v2 model. Report the elements it
declares and the relationships its syntax states between them.

This is source code in a formal modelling language, not prose. Everything the
model contains is written down, so nothing has to be inferred and nothing that is
absent belongs in the output. Extraction here is transcription. An element you add
that the text does not declare does not make the model more complete -- it makes
it wrong, and afterwards nothing can tell it apart from a real one.

-Steps-
1. Find every declaration in the text. A declaration is a keyword, optionally a
short name in angle brackets, then the element's name:

    part def Pump;
    part coolantPump : Pump;
    requirement def <'REQ-4'> ThermalMargin;
    action circulate;

For each declaration, extract:
- entity_name: the name exactly as written. Strip surrounding single quotes and
  nothing else. Do not prefix it with its package, its owner or its keyword, do
  not append a description, do not expand an abbreviation, do not change its
  spelling or its capitalisation. The name in the file is the element's identity;
  a name you compose is the identity of nothing.
- entity_type: MUST be exactly one of [{entity_types}], taken from the keyword
  that declares it. A definition and a usage written with the same keyword
  (`part def` and `part`) are two separate elements of the same type.
- entity_description: what this text says about the element -- what types it, what
  it specializes, what it declares inside it, the values written on it. Quote
  identifiers as they appear. Do not say what the element is for, do not add
  engineering knowledge the file does not state, do not speculate about intent.

Format each entity as ("entity"{tuple_delimiter}<entity_name>{tuple_delimiter}<entity_type>{tuple_delimiter}<entity_description>)

2. Report the relationships the syntax states. Each one must be traceable to
something written in this text, and its direction runs from the element carrying
the syntax to the element that syntax names:

- an element declared inside another's body: the enclosing element -> the inner one
- `name : Type`: the usage -> the definition that types it
- `Def :> General`: the more specific definition -> the more general one
- `:>> feature`: the redefining feature -> the feature it redefines
- `satisfy R by E`: E -> R, because E is what satisfies
- a flow, connection, transition or allocation: the source end -> the target end

Use the label from [{relationship_types}] that names the relation.
{relationship_type_instruction}

Direction is not a detail. Reversed, `typed by` makes a definition an instance of
its own specialization and `owns` makes a part the container of its own container,
and a query walking the result reaches the opposite of what the model says. When
you cannot tell which end carries the syntax, leave the relationship out.

For each relationship, extract:
- source_entity: name of the source entity, as identified in step 1
- target_entity: name of the target entity, as identified in step 1
- relationship_type: the label from the list above
- relationship_description: the syntax it is read from, quoted
- relationship_strength: a numeric score indicating strength of the relationship

Format each relationship as ("relationship"{tuple_delimiter}<source_entity>{tuple_delimiter}<target_entity>{tuple_delimiter}<relationship_type>{tuple_delimiter}<relationship_description>{tuple_delimiter}<relationship_strength>)

3. Return output in English as a single list of all the entities and relationships
identified in steps 1 and 2. Use **{record_delimiter}** as the list delimiter.

4. When finished, output {completion_delimiter}

-Rules-
The first of these decides whether the result is usable at all:

- Extract only what this text writes down. If it declares nothing of one of the
  entity types, extract none of that type -- a type with no elements is a correct
  answer, not a gap to be filled. Never continue an identifier series past the last
  one written. Never add an element because a model of this kind usually has one.
  Finding fewer elements than you expected is not a failure.
- A comment (`//`, `/* */`) and the body of a `doc` are not declarations. Neither
  is a heading, a unit, a literal value, or a term that appears only inside a
  sentence. A concept a comment names is not an element of the model.
- An element this text references but declares elsewhere -- the definition after
  `:`, the target of a `satisfy` -- may be extracted, under exactly the name the
  reference writes. Anything not present in this text at all may not.
- A type supplied by an imported library rather than by this model (`Real`,
  `String`, a quantity or unit from a standard package) need not be extracted.
- A short name in angle brackets (`<'REQ-4'>`) is an alternate identifier, not the
  element's name. Extract the element under its name.
- A qualified name (`SafetyPackage::ThermalMargin`) and a dotted path
  (`assembly.pump.outlet`) each name one element: the one in the last segment. Use
  that segment, both as an entity's name and at either end of a relationship. The
  rest of it is the route taken to reach the element, not part of what it is
  called, and an element written both ways is still one element.
- One element is one entity. An element declared once and referenced many times is
  a single entity under a single name, however many times it appears and however
  it is reached.
- A redefinition that writes no new name of its own (`attribute :>> mass = 40`)
  carries the name of the feature it redefines. Report the value it sets, but not
  a relationship, which would join a name to itself. Never relate an element to
  itself.

######################
-Examples-
######################
Example 1:

Entity_types: [Package, Part, Port, Attribute]
Relationship_types: [owns, typedBy, specializes]
Text:
package HydraulicsPackage {{
    part def Pump {{
        attribute ratedFlow : Real;
        port outlet : FluidPort;
    }}

    part def BoosterPump :> Pump;

    part primaryPump : BoosterPump;
}}
################
Output:
("entity"{tuple_delimiter}"HydraulicsPackage"{tuple_delimiter}"Package"{tuple_delimiter}"A package declaring two part definitions, Pump and BoosterPump, and one part usage, primaryPump."){record_delimiter}
("entity"{tuple_delimiter}"Pump"{tuple_delimiter}"Part"{tuple_delimiter}"A part definition declaring an attribute ratedFlow typed by Real and a port outlet typed by FluidPort."){record_delimiter}
("entity"{tuple_delimiter}"ratedFlow"{tuple_delimiter}"Attribute"{tuple_delimiter}"An attribute declared in Pump, typed by Real. No value is written."){record_delimiter}
("entity"{tuple_delimiter}"outlet"{tuple_delimiter}"Port"{tuple_delimiter}"A port declared in Pump, typed by FluidPort."){record_delimiter}
("entity"{tuple_delimiter}"BoosterPump"{tuple_delimiter}"Part"{tuple_delimiter}"A part definition that specializes Pump."){record_delimiter}
("entity"{tuple_delimiter}"primaryPump"{tuple_delimiter}"Part"{tuple_delimiter}"A part usage declared in HydraulicsPackage, typed by BoosterPump."){record_delimiter}
("relationship"{tuple_delimiter}"HydraulicsPackage"{tuple_delimiter}"Pump"{tuple_delimiter}"owns"{tuple_delimiter}"Pump is declared inside the body of HydraulicsPackage."{tuple_delimiter}9){record_delimiter}
("relationship"{tuple_delimiter}"HydraulicsPackage"{tuple_delimiter}"BoosterPump"{tuple_delimiter}"owns"{tuple_delimiter}"BoosterPump is declared inside the body of HydraulicsPackage."{tuple_delimiter}9){record_delimiter}
("relationship"{tuple_delimiter}"HydraulicsPackage"{tuple_delimiter}"primaryPump"{tuple_delimiter}"owns"{tuple_delimiter}"primaryPump is declared inside the body of HydraulicsPackage."{tuple_delimiter}9){record_delimiter}
("relationship"{tuple_delimiter}"Pump"{tuple_delimiter}"ratedFlow"{tuple_delimiter}"owns"{tuple_delimiter}"ratedFlow is declared inside the body of Pump."{tuple_delimiter}8){record_delimiter}
("relationship"{tuple_delimiter}"Pump"{tuple_delimiter}"outlet"{tuple_delimiter}"owns"{tuple_delimiter}"outlet is declared inside the body of Pump."{tuple_delimiter}8){record_delimiter}
("relationship"{tuple_delimiter}"BoosterPump"{tuple_delimiter}"Pump"{tuple_delimiter}"specializes"{tuple_delimiter}"part def BoosterPump :> Pump"{tuple_delimiter}9){record_delimiter}
("relationship"{tuple_delimiter}"primaryPump"{tuple_delimiter}"BoosterPump"{tuple_delimiter}"typedBy"{tuple_delimiter}"part primaryPump : BoosterPump"{tuple_delimiter}9)
# NOTE: Real and FluidPort are named only as types from outside this text and were
# not extracted. The direction of every relationship follows the element that
# carries the syntax: BoosterPump -> Pump, never Pump -> BoosterPump.
{completion_delimiter}
#############################
Example 2:

Entity_types: [Package, Part, Requirement, Action]
Relationship_types: [owns, satisfies, performs]
Text:
// Thermal limits are tracked as the SAF series and reviewed once a cycle.
package SafetyPackage {{
    requirement def <'SAF-1'> ThermalMargin {{
        doc /* The assembly shall stay below its rated temperature. */
    }}

    part coolingAssembly {{
        satisfy SafetyPackage::ThermalMargin by coolingAssembly.radiator;
    }}
}}
#############
Output:
("entity"{tuple_delimiter}"SafetyPackage"{tuple_delimiter}"Package"{tuple_delimiter}"A package declaring the requirement definition ThermalMargin and the part usage coolingAssembly."){record_delimiter}
("entity"{tuple_delimiter}"ThermalMargin"{tuple_delimiter}"Requirement"{tuple_delimiter}"A requirement definition carrying the short name SAF-1. Its documentation reads: the assembly shall stay below its rated temperature."){record_delimiter}
("entity"{tuple_delimiter}"coolingAssembly"{tuple_delimiter}"Part"{tuple_delimiter}"A part usage declared in SafetyPackage. Its body states that ThermalMargin is satisfied by coolingAssembly.radiator."){record_delimiter}
("entity"{tuple_delimiter}"radiator"{tuple_delimiter}"Part"{tuple_delimiter}"Named by the path coolingAssembly.radiator as what satisfies ThermalMargin. Its declaration is not in this text."){record_delimiter}
("relationship"{tuple_delimiter}"SafetyPackage"{tuple_delimiter}"ThermalMargin"{tuple_delimiter}"owns"{tuple_delimiter}"ThermalMargin is declared inside the body of SafetyPackage."{tuple_delimiter}9){record_delimiter}
("relationship"{tuple_delimiter}"SafetyPackage"{tuple_delimiter}"coolingAssembly"{tuple_delimiter}"owns"{tuple_delimiter}"coolingAssembly is declared inside the body of SafetyPackage."{tuple_delimiter}9){record_delimiter}
("relationship"{tuple_delimiter}"radiator"{tuple_delimiter}"ThermalMargin"{tuple_delimiter}"satisfies"{tuple_delimiter}"satisfy SafetyPackage::ThermalMargin by coolingAssembly.radiator"{tuple_delimiter}9)
# NOTE: SAF-1 is a short name and is not an element of its own. The comment
# mentions a SAF series, so SAF-2 and anything after it would be plausible -- they
# are not in this text and were NOT extracted, and neither were "thermal limits"
# or "cycle", which are words in a comment rather than declarations. Both
# references in the satisfy line were written as last segments: the requirement as
# ThermalMargin rather than SafetyPackage::ThermalMargin, and the part as radiator
# rather than coolingAssembly.radiator.
{completion_delimiter}
#############################
-Real Data-
######################
Entity_types: {entity_types}
Relationship_types: {relationship_types}
Text: {input_text}
######################
Output:
"""


# `generate_community_report` fills only `input_text` (_op.py:861), with a CSV of
# the group's entities and relationships and the reports of any groups nested
# inside it. The JSON keys are the contract: `title`, `summary` and `findings` are
# read back out (_op.py:808), and `rating` orders nested groups when a parent's
# context is packed (_op.py:665), so higher has to mean more important.
COMMUNITY_REPORT = """You are a systems engineer describing one part of a SysML v2 model.

The model has been divided into groups of elements that are densely connected to
each other. You are given one group: its elements, the relationships among them,
and the reports of any groups nested inside it. Write a report on that group for an
engineer who has not opened the files.

# Goal
Say what this group of the model is, what its elements are, and how they are held
together -- what contains what, what is typed by what, what satisfies which
requirement, what flows or acts on what. Report the values the model states and
name the elements that carry them. The report is read on its own, so an element
that matters to the group should be named in it.

# Report Structure

- TITLE: what this group is, named after its elements. Prefer the name of the
  element the others hang off -- an assembly, a package, a requirement set -- to a
  generic phrase. Short and specific.
- SUMMARY: what the group covers and how its elements are connected: the
  containment, the types, the requirements and what satisfies them, the behaviour.
  Say what kind of part of the model this is, not only what it contains.
- RATING: a float from 0-10 for how central this group is to the model as a whole.
  A group holding a top-level system and its main assemblies rates high; a group of
  leaf attributes or a single isolated definition rates low.
- RATING EXPLANATION: one sentence for the rating.
- DETAILED FINDINGS: 5-10 observations about the group. Each is a short summary
  followed by explanatory paragraphs. Prefer what an engineer would ask: the
  structure the elements form, the values stated and their units, requirements and
  their coverage, the behaviour and its ordering, and anything the model states
  that is unusual for its kind -- a requirement nothing satisfies, an element with
  no type, a value at odds with a neighbouring one.

Return output as a well-formed JSON-formatted string with the following format:
    {{
        "title": <report_title>,
        "summary": <executive_summary>,
        "rating": <centrality_rating>,
        "rating_explanation": <rating_explanation>,
        "findings": [
            {{
                "summary":<insight_1_summary>,
                "explanation": <insight_1_explanation>
            }},
            {{
                "summary":<insight_2_summary>,
                "explanation": <insight_2_explanation>
            }}
            ...
        ]
    }}

CRITICAL: Return ONLY a single JSON object.
- Do NOT wrap it in an array.
- The top-level JSON structure MUST be a JSON object (starting with {{), not a list (starting with [).
- Your response should start with {{ and end with }}.

# Grounding Rules
Report only what the rows below state. This is a model, so its content is written
down rather than described: a value, a type or a relationship that is not in the
rows is not in the model, and adding one from engineering knowledge makes the
report disagree with the files it claims to describe. Write element names exactly
as the rows spell them, so a reader can find them. Where the model says nothing --
a requirement with nothing satisfying it, an element with no attributes -- that
silence is a finding worth reporting, not a gap to fill in.

# Real Data
Text:
```
{input_text}
```
Output:
"""


CUSTOM_PROMPTS = {
    "entity_extraction": ENTITY_EXTRACTION,
    "community_report": COMMUNITY_REPORT,
}
