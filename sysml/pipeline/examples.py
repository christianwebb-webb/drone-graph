"""An `aql_examples.md` written by a strong model from the graph it will be used on.

`sysml/aql_examples.md` is hand-written. Every line of it was learned by asking a
question, watching AQLizer get it wrong, and writing down the reason -- which works,
and does not survive contact with a model nobody has seen. A corpus imported
tomorrow has different attribute names, different identifiers, different snapshots,
and nobody to spend a week on it.

So this step writes one instead, from two inputs and nothing else:

`GUIDE` below -- what is true of *any* graph this pipeline builds, because the
    pipeline builds it that way: names upper-cased, types lower-cased and closed,
    `attributes` a map of `{value, unit}`, `files` and `models` lists, `stated` on
    what the lexer read, containment as `owns` + `typedby`, time-varying values on
    snapshots rather than on the static part. None of it mentions a drone or a
    Saturn V, because none of it is about them.

`survey` -- the same facts read off the live graph: which entity types actually
    occur and how often, which attribute names exist with their units, real short
    names, real snapshot names, how names were disambiguated, which relations are
    read and which inferred. This is what the hand-written file learned by hand,
    and it is a dozen AQL queries.

The generated file then goes through the check the hand-written one claims in its
own first paragraph -- every query in it was run against the graph. Each ```aql
block is parsed, executed and counted, and anything that fails goes back to the
model with its error for one repair round. A worked example that does not run is
worse than no example: the chain copies its shape.

Nothing here touches `aql_examples.md`. The output is a second file, and the read
side takes the hand-written one unless it is told otherwise.

    python -m sysml.pipeline.examples
    python -m sysml.pipeline.examples --model gpt-5.4 --out out/other.md
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from .. import config

# What the model is told the file is for, and what it may assume. Everything here
# is a property of the pipeline -- of extract.py, structure.py and load.py -- not
# of any particular model, so it holds for a corpus this project has never seen.
GUIDE = f"""You are writing `aql_examples.md` for one specific ArangoDB graph, built
from a set of SysML v2 models. The file is passed verbatim as the `aql_examples`
argument of LangChain's `ReadOnlyArangoGraphQAChain.from_llm`, so its only reader is
the LLM that turns an engineer's English question into AQL and then runs it. It sees
the collection schema already. What it does not see, and what your file is the only
source of, is what the fields mean, which ones are trustworthy, and the specific
shapes a query has to take here to return the right rows.

You are not writing documentation for a person. Assume the reader is competent at
AQL and knows nothing about this graph. Write down what it would otherwise get
wrong.

## How the graph was built, which is why its fields look the way they do

Every model under `models/` is read twice.

1. An LLM extraction pipeline reads the source text and writes the entities, their
   `description`s, and the relations the prose implies. It upper-cases every entity
   name it keeps and lower-cases every type.
2. A lexer then reads the same files for what the SysML syntax states outright, and
   overwrites nothing the first pass wrote. It supplies:
   - `attributes` -- a MAP from attribute name to `{{value, unit}}` when the file
     assigns a number, or `{{expression}}` when it assigns a formula. Never both.
     Units are reduced to the bare symbol, so `[kg]`, `[SI::kg]` and `['kg']` all
     read `kg`.
   - `short_name` -- the identifier an element was declared under,
     `requirement def <'XYZ-R001'> Something`. The element is ONE row, stored under
     its written name, with the identifier in this field.
   - `source_file` / `source_line` -- where the declaration is. An element the LLM
     named but no declaration does has neither.
   - edges for `owns`, `typedby`, `specializes`, `redefines` and `satisfy X by Y`,
     each carrying `stated: true`. An edge the LLM inferred has no `stated` field at
     all -- it is ABSENT, not false.

So a field the lexer supplied is exact and a field the extraction supplied is a
reading. Anything numeric or structural is the lexer's; anything descriptive is the
extraction's.

## The invariants a query here has to respect

- `entity_name` is UPPER CASE. A comparison against a mixed-case literal matches
  nothing. Compare with `UPPER(@x)` or use `CONTAINS(e.entity_name, UPPER(@x))`.
- `entity_type` is a single lower-case string from a closed list of 27:
  {", ".join(config.ENTITY_TYPES)}.
  There is no `component`, `system`, `element`, `function` or `module` type. A noun
  in the question is not automatically a type -- if it is not on that list, do not
  filter on `entity_type` at all; filter on what is actually being asked for.
- `files` and `models` are LISTS. Use `IN`, never `==`.
- `attributes` is a MAP, so `e.attributes[*].unit` does not iterate it. Reach a
  known attribute by name; use `ATTRIBUTES(e.attributes)` when the name is not known
  in advance.
- The edge collection holds every edge kind. `type` is the importer's own closed
  vocabulary: `RELATED_TO` for a SysML relation, `MENTIONED_IN`, `PART_OF`,
  `IN_COMMUNITY`, `SUB_COMMUNITY_OF` for the structure the importer builds, and
  `SIMILAR_TO` for computed cross-model resemblances. Only `RELATED_TO` carries
  `relationship_type`, which is the SysML relation in lower case with no capital in
  the middle: `typedby`, `dependson`, `transitionsto`, `variantof`.
- A bare name is not unique. SysML lets many declarations share one, so where that
  happened the name is prefixed with enough of its owner to tell them apart
  (`OWNER_NAME`, `OUTER_INNER_NAME`). The unprefixed row may also exist -- that one
  is the concept the extraction read in the prose, and it has no `source_file`.
- Direction: the edge points from the thing that acts to the thing acted on.
  `A satisfies B` -- A is the design, B the requirement. `A refines B` -- A is the
  more detailed statement. `A performs B` -- A is the part, B the action.
  `A owns B` -- A is the container. `A typedby B` -- A is the usage, B the
  definition. So "what owns X" and "what satisfies X" are INBOUND from X; "what does
  X contain" and "what does X specialize" are OUTBOUND. Picking one direction
  because the wording sounds one-way is the most common way to get an empty result
  from a question that has an answer.
- A multi-hop traversal must constrain EVERY edge. `FOR v, e IN 1..6 OUTBOUND x
  edges FILTER e.relationship_type == 'owns'` filters only the LAST edge of each
  path and over six hops reaches most of the graph. Filter the path instead:
  `FILTER p.edges[*].relationship_type ALL IN [...]`.
- A containment rollup follows `owns` AND `typedby` together and nothing else. A
  part *usage* carries no values -- `part stage1 : 'S-IC'` is an occurrence of a
  definition, and the numbers are declared on the definition. Letting `specializes`
  into the walk climbs to an abstract supertype and back down into everything else
  that specializes it, which is a different vehicle's parts.
- Time-varying values live on `snapshot` and `timeslice` elements, which redeclare
  the parts they are about with the values held at that moment. A question about a
  moment starts at the occurrence and walks down; reading the static part answers a
  different question. Occurrence names are declaration identifiers with the case
  flattened, so they carry no spaces and often an abbreviation.
- "How many" means COUNT computed in AQL. The result set is capped, so a query that
  returns one row per match reports the cap as the answer.
- A vague or plural question ("anything about batteries") is not naming one element.
  Use `CONTAINS` over `entity_name` AND `LOWER(description)`, and `LIMIT`.
- Coverage questions ("which requirements does nothing satisfy") are anti-joins on
  an absent incoming edge, and they are the questions an engineer actually asks.

## What to write

A markdown file, and nothing else -- no preamble, no sign-off, no outer code fence.

- Start with what each collection holds, field by field, saying for each field
  whether it is exact (read from the syntax) or a reading (written by the LLM), and
  what a query has to know to use it. Use the real counts and the real values from
  the survey below, not invented ones.
- Then the traps, each as a short heading, the reason in one or two sentences, and a
  worked ```aql block that gets it right.
- Between 18 and 30 ```aql blocks in total. Every one must be valid AQL for THIS
  graph: real collection names, real field names, and literals that exist in the
  survey. Where a value comes from the question -- the element being asked about, the
  identifier, the moment -- write a bind parameter (`UPPER(@name)`), not a literal:
  the reader copies the shape of an example, so an example that matches one long name
  exactly teaches it to guess long names. Reserve literals for what is fixed about the
  graph: a model name, an entity type, an attribute name. And show how a name from a
  question is resolved -- exact match, then the owner-prefixed form, then containment
  -- rather than assuming the wording matches a stored name.
- Every query must be READ-ONLY. The reader answers questions; it never writes.
  No `INSERT`, `UPDATE`, `REPLACE`, `REMOVE`, `UPSERT` or `TRUNCATE` anywhere in
  the file, not even in an example of what not to do.
- Cover: reading an attribute; ranking by an attribute; summing over a containment
  subtree; an element's relations in both directions; the `short_name` lookup; a
  vague-noun search; provenance (`stated` vs inferred); counting by type; an
  anti-join; a snapshot or timeslice question; the community layer; the computed
  similarity layer; and any shape peculiar to the survey below.
- Prose is for the reason a query has to look the way it does. Do not explain AQL
  itself, do not pad, and do not repeat the survey as a table.

Ground every claim in the survey. If the survey does not show something, do not say
it exists."""

# One question each, and each answerable without knowing what the corpus is about.
# The whole point is that a corpus imported next week is surveyed by the same list.
PROBES: list[tuple[str, str]] = [
    # Named, not counted. The collection names are the one thing a query cannot be
    # written without, and a survey that says "3,137 entities" without saying what
    # the entities collection is called invites the model to invent a plausible
    # name -- the first run of this step wrote `sysml_TextChunks`, which does not
    # exist, into a heading.
    ("the collections, by name", """
     FOR row IN [%s] RETURN row""" % ", ".join(
        f'{{collection: "{n}", rows: LENGTH({n}), '
        f'edge_collection: {str(n == config.RELATIONS).lower()}}}'
        for n in config.ALL_COLLECTIONS)),

    ("models, and how many entities each one has", f"""
     FOR e IN {config.ENTITIES} FOR m IN e.models
       COLLECT model = m WITH COUNT INTO n SORT n DESC
       RETURN {{model, entities: n}}"""),

    ("entity_type, every value that occurs", f"""
     FOR e IN {config.ENTITIES}
       COLLECT entity_type = e.entity_type WITH COUNT INTO n SORT n DESC
       RETURN {{entity_type, n}}"""),

    ("which fields are filled in, out of all entities", f"""
     FOR e IN {config.ENTITIES}
       COLLECT AGGREGATE
           with_attributes = SUM(LENGTH(ATTRIBUTES(e.attributes or {{}})) > 0 ? 1 : 0),
           with_short_name = SUM(e.short_name != null ? 1 : 0),
                         declared = SUM(e.source_file != null ? 1 : 0),
                         read_by_lexer = SUM(e.stated == true ? 1 : 0),
                         in_two_models = SUM(LENGTH(e.models) > 1 ? 1 : 0),
                         in_two_files = SUM(LENGTH(e.files) > 1 ? 1 : 0)
       RETURN {{with_attributes, with_short_name, declared, read_by_lexer,
                in_two_models, in_two_files}}"""),

    ("attribute names, with their units and an element that carries one", f"""
     FOR e IN {config.ENTITIES}
       FILTER e.attributes != null
       FOR name IN ATTRIBUTES(e.attributes)
         LET a = e.attributes[name]
         COLLECT attribute = name INTO rows = {{owner: e.entity_name, a}}
         SORT LENGTH(rows) DESC LIMIT 30
         RETURN {{attribute, on_n_elements: LENGTH(rows),
                  units: UNIQUE(rows[*].a.unit), example_owner: rows[0].owner,
                  example_value: rows[0].a}}"""),

    ("attributes that hold a formula rather than a number", f"""
     FOR e IN {config.ENTITIES}
       FILTER e.attributes != null
       FOR name IN ATTRIBUTES(e.attributes)
         FILTER e.attributes[name].expression != null
         LIMIT 8
         RETURN {{element: e.entity_name, attribute: name,
                  expression: e.attributes[name].expression}}"""),

    ("short names, a sample", f"""
     FOR e IN {config.ENTITIES}
       FILTER e.short_name != null
       LIMIT 12
       RETURN {{short_name: e.short_name, entity_name: e.entity_name,
                entity_type: e.entity_type}}"""),

    ("names that were prefixed with their owner to keep them apart", f"""
     FOR e IN {config.ENTITIES}
       FILTER e.source_file != null AND CONTAINS(e.entity_name, "_")
       LET tail = LAST(SPLIT(e.entity_name, "_"))
       LET twin = FIRST(FOR x IN {config.ENTITIES}
                          FILTER x.entity_name == tail LIMIT 1 RETURN x.entity_name)
       FILTER twin != null
       LIMIT 8
       RETURN {{prefixed: e.entity_name, bare_row_also_exists: twin,
                type: e.entity_type}}"""),

    ("an entity as stored, with the vector dropped", f"""
     FOR e IN {config.ENTITIES}
       FILTER e.attributes != null AND e.short_name != null AND e.source_file != null
       LIMIT 1
       RETURN UNSET(e, "{config.EMBEDDING_FIELD}", "clusters", "_rev", "_id")"""),

    ("edge kinds", f"""
     FOR r IN {config.RELATIONS}
       COLLECT type = r.type WITH COUNT INTO n SORT n DESC
       RETURN {{type, n}}"""),

    ("SysML relations, split by whether the lexer read them or the LLM inferred them", f"""
     FOR r IN {config.RELATIONS}
       FILTER r.type == "RELATED_TO"
       COLLECT relationship_type = r.relationship_type, read_from_syntax = r.stated == true
       WITH COUNT INTO n SORT n DESC
       RETURN {{relationship_type, read_from_syntax, n}}"""),

    ("a RELATED_TO edge as stored, with the vector dropped", f"""
     FOR r IN {config.RELATIONS}
       FILTER r.type == "RELATED_TO" AND r.stated == true
       LIMIT 1
       RETURN UNSET(r, "{config.EMBEDDING_FIELD}", "_rev")"""),

    ("occurrences -- the elements that carry a moment in time", f"""
     FOR e IN {config.ENTITIES}
       FILTER e.entity_type IN ["snapshot", "timeslice", "occurrence"]
       LET valued = LENGTH(
           FOR v, x, p IN 1..5 OUTBOUND e {config.RELATIONS}
             FILTER p.edges[*].relationship_type
                    ALL IN ["owns", "redefines", "typedby"]
             FILTER LENGTH(ATTRIBUTES(v.attributes or {{}})) > 0
             RETURN DISTINCT v)
       SORT valued DESC LIMIT 10
       RETURN {{name: e.entity_name, type: e.entity_type,
                elements_carrying_values_below_it: valued}}"""),

    ("the community layer", f"""
     FOR c IN {config.COMMUNITIES}
       COLLECT level = c.level INTO rows
       RETURN {{level, communities: LENGTH(rows), example_title: rows[0].c.report_json.title,
                report_json_keys: ATTRIBUTES(rows[0].c.report_json)}}"""),

    ("the computed similarity layer", f"""
     FOR r IN {config.RELATIONS}
       FILTER r.type == "{config.SIMILAR_TO}"
       COLLECT role = r.analogy_role INTO rows
       LET a = DOCUMENT(rows[0].r._from), b = DOCUMENT(rows[0].r._to)
       SORT LENGTH(rows) DESC
       RETURN {{analogy_role: role, edges: LENGTH(rows),
                cosine_range: [MIN(rows[*].r.cosine), MAX(rows[*].r.cosine)],
                example: CONCAT(a.entity_name, " ~ ", b.entity_name),
                fields: ATTRIBUTES(rows[0].r)}}"""),

    ("documents and chunks", f"""
     LET docs = (FOR d IN {config.DOCUMENTS} SORT d.file_name
                   RETURN {{name: d.file_name, models: d.models,
                            fields: ATTRIBUTES(UNSET(d, "content", "{config.EMBEDDING_FIELD}"))}})
     RETURN {{document_fields: docs[0].fields, file_names: docs[*].name,
              chunk_fields: FIRST(
                  FOR c IN {config.CHUNKS}
                    RETURN ATTRIBUTES(
                        UNSET(c, "content", "{config.EMBEDDING_FIELD}")))}}"""),

    ("the elements with the most relations", f"""
     FOR e IN {config.ENTITIES}
       LET degree = LENGTH(FOR v, r IN 1..1 ANY e {config.RELATIONS}
                             FILTER r.type == "RELATED_TO" RETURN 1)
       SORT degree DESC LIMIT 8
       RETURN {{name: e.entity_name, type: e.entity_type, relations: degree}}"""),
]

FENCE = re.compile(r"```(?:aql|AQL)\s*\n(.*?)```", re.S)
BIND = re.compile(r"@@?([A-Za-z_][A-Za-z0-9_]*)")


# ------------------------------------------------------------------- the survey


def survey(db) -> str:
    """Run every probe and render the answers as the model's evidence.

    Truncated hard. A survey is a description of the graph, and one that runs to
    thirty pages buries the six numbers that matter -- the generated file starts
    quoting row counts back instead of writing queries.
    """
    import json

    out = []
    for heading, query in PROBES:
        try:
            rows = list(db.aql.execute(" ".join(query.split()), max_runtime=60))
        except Exception as exc:                # a probe is not worth failing over
            out.append(f"### {heading}\n\n(unavailable: {type(exc).__name__})")
            continue
        # One row per line rather than pretty-printed: the same facts in a third of
        # the tokens, and the survey has to leave room for the graph it describes.
        body = "\n".join(json.dumps(row, default=str) for row in rows)
        if len(body) > 4000:
            body = body[:body.rfind("\n", 0, 4000)] + "\n... truncated"
        out.append(f"### {heading}\n\n```json\n{body}\n```")
    return "\n\n".join(out)


# ---------------------------------------------------------------- the generation


def ask(prompt: str, model: str, previous: list[dict] | None = None) -> tuple[str, list[dict]]:
    """One turn with the strong model, carrying the conversation so far.

    The repair round has to see what it wrote and why it failed, and a chat history
    is the cheapest way to give it both without restating the guide.
    """
    from openai import OpenAI

    messages = list(previous or [{"role": "system", "content": GUIDE}])
    messages.append({"role": "user", "content": prompt})
    reply = OpenAI(api_key=config.openai_key()).chat.completions.create(
        model=model, messages=messages).choices[0].message.content or ""
    # A model asked for a markdown file sometimes hands back the whole file inside
    # one fence, which would put ``` around a document that itself contains fences.
    text = reply.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", text).strip()
    return text, messages + [{"role": "assistant", "content": reply}]


# ---------------------------------------------------------------------- the check


def bind_values(db) -> dict[str, Any]:
    """Plausible values for the bind parameters an example is likely to use.

    A query written as an example is a template, and a template cannot be run
    without values. These are read off the graph rather than invented, so a query
    that comes back with no rows has really come back with no rows.
    """
    def one(aql: str) -> Any:
        return next(iter(db.aql.execute(aql)), None)

    return {
        "name": one(f"FOR e IN {config.ENTITIES} FILTER e.source_file != null "
                    "LIMIT 1 RETURN e.entity_name"),
        "short": one(f"FOR e IN {config.ENTITIES} FILTER e.short_name != null "
                     "LIMIT 1 RETURN e.short_name"),
        "model": one(f"FOR e IN {config.ENTITIES} LIMIT 1 RETURN e.models[0]"),
        "type": one(f"FOR e IN {config.ENTITIES} LIMIT 1 RETURN e.entity_type"),
        "moment": one(f"FOR e IN {config.ENTITIES} "
                      "FILTER e.entity_type IN ['snapshot', 'timeslice'] "
                      "LIMIT 1 RETURN e.entity_name"),
        "id": one(f"FOR e IN {config.ENTITIES} FILTER e.source_file != null "
                  "LIMIT 1 RETURN e._id"),
        "file": one(f"FOR d IN {config.DOCUMENTS} LIMIT 1 RETURN d.file_name"),
    }


def fill(names: list[str], values: dict[str, Any]) -> dict[str, Any]:
    """Guess what each bind parameter wants from what it is called."""
    wanted = {}
    for name in names:
        lowered = name.lower().lstrip("@")
        if name.startswith("@"):                       # @@collection
            wanted[name] = config.ENTITIES
        elif any(w in lowered for w in ("start", "from", "root", "_id", "vertex")):
            wanted[name] = values["id"]
        elif any(w in lowered for w in ("short", "req_id", "identifier")):
            wanted[name] = values["short"]
        elif "model" in lowered:
            wanted[name] = values["model"]
        elif any(w in lowered for w in ("type", "kind", "role")):
            wanted[name] = values["type"]
        elif any(w in lowered for w in ("moment", "time", "snapshot", "phase")):
            wanted[name] = values["moment"]
        elif "file" in lowered:
            wanted[name] = values["file"]
        else:
            wanted[name] = values["name"]
    return wanted


def check(db, markdown: str) -> list[dict]:
    """Parse, run and count every ```aql block in the file.

    Three outcomes worth telling apart. A query that does not parse is a broken
    example and has to be repaired. One that parses, runs and returns nothing is
    usually a wrong assumption about a value -- a type that does not exist, a name
    in the wrong case -- and is exactly the mistake the file exists to prevent, so
    it is reported too. One that returns rows is what it claims to be.

    A query that writes is refused rather than run. This is not a formality: the
    first version of this step ran every block it found, a draft included one that
    wrote, and it emptied most of the graph it was describing. Nothing a model
    wrote reaches the database without clearing `config.MUTATION` first, here for
    the same reason `nl` does it for a generated answer.
    """
    values = bind_values(db)
    results = []
    for i, block in enumerate(FENCE.findall(markdown), start=1):
        query = block.strip()
        row = {"n": i, "query": query, "parses": False, "runs": False, "rows": 0,
               "error": None, "binds": sorted(set(BIND.findall(query)))}
        writes = config.MUTATION.search(query)
        if writes:
            row["error"] = (f"refused: writes to the graph ({writes.group(0).upper()}). "
                            "Every example must be a read-only query.")
            results.append(row)
            continue
        try:
            db.aql.validate(query)
            row["parses"] = True
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            results.append(row)
            continue
        binds = {name: v for name, v in fill(
            [("@" + n) if f"@@{n}" in query else n for n in row["binds"]], values).items()}
        try:
            rows = list(db.aql.execute(query, bind_vars=binds or None, max_runtime=45))
            row["runs"], row["rows"] = True, len(rows)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        results.append(row)
    return results


def invented(db, markdown: str) -> list[str]:
    """Collection names the file uses that the database does not have.

    The prose is not executable, so `check` cannot see it, and a wrong name written
    in a heading is followed just as readily as one written in a query. Every
    collection here is named `{project}_Something`, which is a shape a regular
    expression can find.
    """
    real = {c["name"] for c in db.collections()}
    used = set(re.findall(rf"\b{config.PROJECT}_[A-Za-z][A-Za-z0-9_]*", markdown))
    return sorted(used - real)


def report(results: list[dict], missing: list[str] | None = None) -> str:
    ran = sum(1 for r in results if r["runs"])
    empty = sum(1 for r in results if r["runs"] and not r["rows"])
    broken = [r for r in results if not r["parses"]]
    line = (f"{len(results)} queries, {ran} run, {len(broken)} do not parse, "
            f"{ran - empty} return rows, {empty} return nothing")
    if missing:
        line += f", {len(missing)} invented collection names ({', '.join(missing)})"
    return line


def complaints(results: list[dict], missing: list[str] | None = None) -> str:
    """What to hand back to the model: the failures, with the query and the error."""
    lines = []
    for name in missing or []:
        lines.append(f"`{name}` is not a collection in this database. Nothing may "
                     "refer to it -- not a query, not a heading, not a sentence.")
    for r in results:
        if r["runs"] and r["rows"]:
            continue
        why = r["error"] or "runs, but returns no rows -- an assumption in it is wrong"
        lines.append(f"Query {r['n']}:\n```aql\n{r['query']}\n```\n{why}")
    return "\n\n".join(lines)


# ------------------------------------------------------------------------- build


def build(db=None, model: str | None = None, rounds: int = 1,
          out: Path | None = None) -> dict:
    """Survey, generate, check, repair, write."""
    db = db if db is not None else config.db()
    model = model or config.EXAMPLES_MODEL
    out = out or config.AQL_EXAMPLES_GENERATED

    facts = survey(db)
    markdown, history = ask(
        "Here is the survey of the graph. Write the file.\n\n" + facts, model)
    results, missing = check(db, markdown), invented(db, markdown)
    history_of = [report(results, missing)]

    for _ in range(rounds):
        bad = complaints(results, missing)
        if not bad:
            break
        markdown, history = ask(
            "Every query in the file was run against the graph. These did not work:"
            f"\n\n{bad}\n\nReturn the WHOLE file again with those fixed -- same "
            "structure, same prose, only what is listed above changed. A query that "
            "returns no rows is filtering on something that is not in the graph; "
            "consult the survey for what is. Markdown only.",
            model, history)
        results, missing = check(db, markdown), invented(db, markdown)
        history_of.append(report(results, missing))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")
    return {"path": out, "model": model, "markdown": markdown, "survey": facts,
            "results": results, "invented": missing, "rounds": history_of}


def main(model: str | None = None, rounds: int = 1, out: Path | None = None) -> None:
    built = build(model=model, rounds=rounds, out=out)
    print(f"  {built['model']} wrote {len(built['markdown']):,} characters "
          f"from a {len(built['survey']):,}-character survey")
    for i, line in enumerate(built["rounds"]):
        print(f"  {'generated' if not i else f'repair {i}':>9}  {line}")
    print(f"  {built['path']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=None, help=f"default {config.EXAMPLES_MODEL}")
    ap.add_argument("--rounds", type=int, default=1, help="repair rounds after the check")
    ap.add_argument("--out", type=Path, default=None,
                    help=f"default {config.AQL_EXAMPLES_GENERATED}")
    args = ap.parse_args()
    main(model=args.model, rounds=args.rounds, out=args.out)
