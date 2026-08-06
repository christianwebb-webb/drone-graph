"""The part of a SysML file that must not be guessed: values and containment.

Extraction reads the prose and is good at it. It is unreliable at the two things
SysML states mechanically -- `attribute dryMass = 137000 [kg]` and
`part stage1 : 'S-IC'` -- and those are exactly what an analytical question needs.
On this corpus it produced 68 `owns` edges out of roughly two thousand written
down, left `S-IC` with no relations at all, and put every number inside a
sentence. A rollup over a containment subtree has nothing to walk.

So this step reads the same files with a lexer and writes down only what the
syntax says outright:

  attributes   a typed {value, unit} or {expression} map on the element that owns
               the declaration
  short_name   the `<'DE-REQ-1'>` an element was declared under
  owns         the enclosing element to the one declared inside it
  typedby      a usage to the definition after its `:`
  specializes  a definition to what follows `:>`
  redefines    a redefinition to the feature after `:>>`

Reading the short name is also what lets this step clean up after the other one.
A SysML element has two written names -- `requirement def <'DE-REQ-1'> Power` is
addressed as either -- and extraction keeps whichever the sentence it read
happened to use, so the same requirement arrives as `POWER` and again as
`DE-REQ-1`. Only the declaration says they are one element, so only this step can
say so: `merge` folds the short-name node into the named one.

Nothing here is specific to these three models. It recognises SysML v2's
declaration grammar -- any keyword in `KEYWORDS`, optionally `def`, an optional
`<shortName>`, a name, then any combination of `:`, `:>`, `:>>` and `=` -- so a
file this project has never seen parses on the same rules. What it deliberately
does not do is resolve names across files, infer anything, or read a `doc`
comment: that is extraction's half, and it is better at it.

    python -m sysml.pipeline.structure
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from .. import config

# Written to out/ so the step can be inspected on its own, and so `load` can apply
# it without re-reading the sources.
STRUCTURE_JSON = config.OUT / "structure.json"

# Declaration keywords, mapped to the entity_type extraction stores for the same
# concept -- lower case, because `_merge_nodes_then_upsert` lower-cases the type it
# keeps. The keys are SysML's spelling and the values are this project's ontology,
# which is why `enum` and `use` do not match their own labels.
KEYWORDS = {
    "package": "package", "part": "part", "action": "action", "state": "state",
    "port": "port", "item": "item", "attribute": "attribute",
    "requirement": "requirement", "calc": "calc", "analysis": "analysis",
    "connection": "connection", "interface": "interface", "view": "view",
    "viewpoint": "viewpoint", "enum": "enumeration", "concern": "concern",
    "constraint": "constraint", "flow": "flow", "allocation": "allocation",
    "event": "event", "metadata": "metadata", "use": "usecase",
    "rendering": "rendering", "verification": "verification",
    "snapshot": "snapshot", "timeslice": "timeslice", "occurrence": "occurrence",
}

# Anything that can stand in front of the keyword. Missing one is not a missing
# modifier, it is a missing declaration: `standard library package ScalarValues`
# would not be recognised as a package at all, and everything inside it would lose
# its owner and be recorded at the top level.
MODIFIERS = {
    "private", "public", "protected", "abstract", "ref", "in", "out", "inout",
    "readonly", "derived", "end", "nonunique", "ordered", "library", "individual",
    "variation", "variant", "standard", "constant", "portion",
}

# A `doc /* ... */` and its relatives are closed by their own comment rather than
# by a terminator, so the scanner has to end the statement when the comment ends.
COMMENT_HEADS = {"doc", "comment", "rep", "language"}

SHORTNAME = re.compile(r"<\s*('[^']*'|\"[^\"]*\"|[\w.\-]+)\s*>")
IDENT = re.compile(r"^(?:'[^']*'|\"[^\"]*\"|[A-Za-z_][\w.\-]*)")
QUANTITY = re.compile(r"^(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*(?:\[\s*([^\]]+?)\s*\])?$")


def unquote(token: str) -> str:
    return token[1:-1] if len(token) > 1 and token[0] in "\"'" and token[-1] == token[0] else token


# --------------------------------------------------------------------------- scan


def scan(src: str) -> Iterator[tuple[str, int, str]]:
    """Yield (statement text, 1-based line, terminator) for every statement.

    The terminator is `{` (opens a body), `}` (closes one) or `;`. Comments are
    dropped, string and quoted-name literals are passed through intact, and a
    `doc /* ... */` is closed by its own comment because nothing else closes it.
    """
    out, line, start, i, n = [], 1, 1, 0, len(src)
    # `start` has to be the line of the statement's first real character, not the
    # line the previous statement ended on. A newline puts a space in the buffer,
    # so "buffer is empty" stops being a usable test for "nothing seen yet".
    started = False

    def flush(term: str) -> Iterator[tuple[str, int, str]]:
        nonlocal started
        text = "".join(out).strip()
        out.clear()
        started = False
        if text or term in "{}":
            yield text, start, term

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            out.append(" ")
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            head = "".join(out).strip()
            line += src[i:j].count("\n")
            i = j
            # Leaving the head in the buffer would prefix it onto the next
            # declaration, which is then never recognised.
            if head.split(" ")[0] in COMMENT_HEADS:
                yield from flush("")
                start = line
            continue
        if c in "\"'":
            if not started:
                start, started = line, True
            j, esc = i + 1, False
            while j < n and (esc or src[j] != c):
                esc = not esc and src[j] == "\\"
                j += 1
            out.append(src[i:j + 1])
            i = j + 1
            continue
        if c in "{};":
            yield from flush(c)
            i += 1
            start = line
            continue
        if not started and not c.isspace():
            start, started = line, True
        out.append(c)
        i += 1
    yield from flush("")


# ------------------------------------------------------------------- declarations


def take_ref(text: str) -> tuple[str, str]:
    """Take one qualified reference off the front, e.g. `A::B::c[2]` -> `A::B::c`."""
    m = re.match(r"(?:'[^']*'|[A-Za-z_][\w.\-]*)(?:\s*::\s*(?:'[^']*'|[\w.\-]+))*", text)
    if not m:
        return "", text
    ref = re.sub(r"\s*::\s*", "::", m.group(0))
    rest = text[m.end():].strip()
    if rest.startswith("["):                       # multiplicity, not part of the name
        rest = rest[rest.find("]") + 1:].strip() if "]" in rest else ""
    return "::".join(unquote(p) for p in ref.split("::")), rest


def split_head(text: str) -> dict[str, Any]:
    """Pull a declaration apart into modifiers, kind, name, typing and value."""
    tokens = re.sub(r"\s+", " ", text).strip().split(" ")
    # Modifiers stack in front of the keyword, and a metadata annotation prefixes
    # the declaration it applies to: `#Approved part x` declares a part.
    while tokens and (tokens[0] in MODIFIERS or tokens[0].startswith("#")):
        tokens.pop(0)
    if not tokens or tokens[0] not in KEYWORDS:
        return {}
    kind = tokens.pop(0)
    # `use case def X` and `analysis case def X` -- `case` belongs to the keyword
    # and `def` marks a definition rather than a usage. Neither is the name, and
    # taking `case` for one is how `use case` silently became an element called
    # "case" with the real name thrown away.
    is_def = False
    while tokens and tokens[0] in ("case", "def"):
        is_def = tokens.pop(0) == "def" or is_def
    rest = " ".join(tokens).strip()

    short = ""
    m = SHORTNAME.search(rest)
    if m:
        short = unquote(m.group(1))
        rest = (rest[:m.start()] + " " + rest[m.end():]).strip()

    name = ""
    m = IDENT.match(rest)
    if m and not rest.startswith((":", "=")):
        name = unquote(m.group(0))
        rest = rest[m.end():].strip()

    typed, specializes, redefines, value = "", [], "", ""
    while rest:
        if rest.startswith(":>>"):
            redefines, rest = take_ref(rest[3:].strip())
        elif rest.startswith(":>"):
            rest = rest[2:].strip()
            while rest:
                ref, rest = take_ref(rest)
                if ref:
                    specializes.append(ref)
                if not rest.startswith(","):
                    break
                rest = rest[1:].strip()
        elif rest.startswith(":"):
            typed, rest = take_ref(rest[1:].strip())
        elif rest.startswith("="):
            value, rest = rest[1:].strip(), ""
        else:
            rest = rest[1:].strip()
    # `attribute :>> dryMass = 137000` names the feature it redefines, and that is
    # the name the value belongs under.
    return {"kind": KEYWORDS[kind], "is_def": is_def, "short": short,
            "name": name or redefines, "typed": typed,
            "specializes": specializes, "redefines": redefines, "value": value}


def parse_value(raw: str) -> dict[str, Any]:
    """`2 [m/s]` -> number + unit; anything else is kept as an expression."""
    raw = raw.strip().rstrip(";").strip()
    if not raw:
        return {}
    m = QUANTITY.match(raw)
    if m:
        num = float(m.group(1))
        # The same unit is written `[kg]`, `[SI::kg]` and `['s']`. Reduce to the
        # bare symbol, or a rollup treats them as different units and either
        # refuses to sum or sums things it should not.
        unit = m.group(2)
        if unit:
            unit = unquote(unit.split("::")[-1].strip())
        return {"value": int(num) if num.is_integer() else num, "unit": unit or None}
    if raw[0] in "\"'":
        return {"value": unquote(raw)}
    return {"expression": raw}


# ---------------------------------------------------------------------- the walk


def loose_relations(statement: str, owner: dict | None, path: str, line: int) -> list[dict]:
    """Relations a statement states without declaring an element of its own.

    `satisfy REQ by DESIGN` is a standalone statement joining two elements, and
    it is worth reading for the same reason `owns` is: extraction found 99 of the
    266 these files state, and invented twelve that relate an element to itself.

    A body can also continue the declaration that encloses it -- a bare `:> Base`
    or `:>> feature` inside `part def X { ... }` specializes or redefines from X
    rather than from anything named on the line.
    """
    out: list[dict] = []
    satisfy = re.match(r"^satisfy\s+(.+?)\s+by\s+(.+)$", statement)
    if satisfy:
        target = satisfy.group(1).strip()
        # `satisfy requirement r : T by x` names the requirement it satisfies
        # after the keyword rather than instead of it.
        if target.startswith("requirement "):
            target = target[len("requirement "):].strip()
        requirement, _ = take_ref(target)
        design, _ = take_ref(satisfy.group(2).strip())
        if requirement and design:
            out.append({"from": design, "to": requirement, "type": "satisfies",
                        "file": path, "line": line})
        return out
    if not owner:
        return out
    for prefix, kind in ((":>>", "redefines"), (":>", "specializes")):
        if not statement.startswith(prefix):
            continue
        rest = statement[len(prefix):].strip()
        while rest:
            ref, rest = take_ref(rest)
            if ref:
                out.append({"from": owner["qualified"], "to": ref, "type": kind,
                            "file": path, "line": line})
            if not rest.startswith(","):
                break
            rest = rest[1:].strip()
        break
    return out


def walk(relative_path: str, text: str, elements: dict, relations: list) -> None:
    """Read one file into elements and the relations its syntax states.

    The stack is what makes ownership work: every `{` pushes, every `}` pops, and
    a declaration belongs to whatever is on top. Blocks that are not declarations
    push `None` so the depths still line up.
    """
    stack: list[dict | None] = []
    for statement, line, terminator in scan(text):
        if terminator == "}":
            if stack:
                stack.pop()
            continue
        owner = next((e for e in reversed(stack) if e), None)
        head = split_head(statement) if statement else {}
        if not head or not head.get("name"):
            if statement:
                relations.extend(loose_relations(statement, owner, relative_path, line))
            if terminator == "{":
                stack.append(None)
            continue

        qualified = f"{owner['qualified']}::{head['name']}" if owner else head["name"]
        element = {
            "name": head["name"], "qualified": qualified, "kind": head["kind"],
            "short": head["short"], "file": relative_path, "line": line,
        }
        # An `attribute` with a value is not really an element of its own -- it is
        # a property of whatever declares it, which is how a rollup expects to
        # find `S-IC.dryMass`. Everything else becomes an element.
        value = parse_value(head["value"]) if head["value"] else {}
        if head["kind"] == "attribute" and value and owner:
            elements.setdefault(owner["qualified"], owner)
            owner.setdefault("attributes", {})[head["name"]] = value
        else:
            if value:
                element["attributes"] = {head["name"]: value}
            elements[qualified] = element
            if owner:
                relations.append({"from": owner["qualified"], "to": qualified,
                                  "type": "owns", "file": relative_path, "line": line})
            # Lower case, because `_merge_edges_then_upsert` lower-cases the type
            # extraction keeps -- so `typedby` from either half is one value and a
            # query does not have to know which wrote the edge.
            # `attribute :>> dryMass = ...` took its name from what it redefines,
            # so an edge there would point the element at itself.
            redefines = head["redefines"] if head["name"] != head["redefines"] else ""
            for kind, target in (("typedby", head["typed"]), ("redefines", redefines)):
                if target:
                    relations.append({"from": qualified, "to": target, "type": kind,
                                      "file": relative_path, "line": line})
            for target in head["specializes"]:
                relations.append({"from": qualified, "to": target, "type": "specializes",
                                  "file": relative_path, "line": line})
        if terminator == "{":
            stack.append(element)


def read() -> dict:
    """Every .sysml file under models/, as {elements, relations}."""
    elements: dict[str, dict] = {}
    relations: list[dict] = []
    for path in sorted(config.MODELS.rglob("*.sysml")):
        walk(path.relative_to(config.MODELS).as_posix(),
             path.read_text(encoding="utf-8"), elements, relations)
    return {"elements": list(elements.values()), "relations": relations}


# --------------------------------------------------------------------- the apply


def index_of(db) -> dict[str, list[dict]]:
    """Every way an entity can be addressed, upper-cased -> the candidates for it.

    Extraction upper-cases the names it keeps, and sometimes keeps a qualified one
    (`DRONE_SYSTEMREQUIREMENTS::BATTERY`). Both forms are indexed, and the last
    segment of a qualified name too, so a declaration that names an element plainly
    still finds it.

    A name can have several candidates, which is the whole reason this returns a
    list: `power` is declared on five Apollo ports and twice in the drone model.
    `resolve` is what picks between them.
    """
    lookup: dict[str, list[dict]] = {}
    for row in db.aql.execute(
            f"FOR e IN {config.ENTITIES} "
            "RETURN {k: e._key, n: e.entity_name, m: e.models}"):
        entry = {"key": row["k"], "models": row["m"] or []}
        name = (row["n"] or "").upper()
        lookup.setdefault(name, []).append(entry)
        if "::" in name:
            lookup.setdefault(name.split("::")[-1], []).append(entry)
    return lookup


def resolve(lookup: dict[str, list[dict]], reference: str, model: str,
            same_model_only: bool = False) -> str | None:
    """A declared name -> the _key of the entity for it, resolved within a model.

    Scoping by model is not a refinement, it is the difference between right and
    wrong. `power` names five Apollo ports and two drone elements, so a global
    name match binds an Apollo declaration to a drone requirement and invents a
    relation between two vehicles that share nothing but a word.

    `same_model_only` is set for relations, where a wrong edge is a false claim
    about the model. It is left off when attaching attributes and provenance to an
    element, where the fallback is the only candidate there is and the worst case
    is a value on the wrong row of the same name.
    """
    name = reference.upper()
    candidates = lookup.get(name) or lookup.get(name.split("::")[-1]) or []
    within = [c for c in candidates if model in c["models"]]
    if within:
        return within[0]["key"]
    return None if same_model_only or not candidates else candidates[0]["key"]


def alias_key(name: str) -> str:
    """The comparable form of a name: last segment, upper, letters and digits only.

    Extraction is inconsistent about how it writes a short name -- `DE-REQ-3`,
    `DE-REQ-3 DURABILITY`, and `FUNCTIONALREQUIREMENTSPACKAGE::'FLR-R002` with the
    opening quote of the declaration still attached. Dropping punctuation makes
    those one string, and the declaration is the only thing being matched against,
    so nothing is matched loosely against another guess.
    """
    return re.sub(r"[^A-Z0-9]", "", name.split("::")[-1].upper())


def plan_merges(db, structure: dict, lookup: dict[str, list[dict]]) -> dict[str, str]:
    """Short-name duplicate -> the entity it is a second name for.

    A row is only a duplicate if no declaration anywhere resolves to it. That is
    what keeps the real ones: Apollo declares
    `requirement 'flr-R001' : PropellantLoadingRequirement` -- a usage whose name
    genuinely is the short name of a definition elsewhere -- and merging it into
    that definition would erase a distinct element. The test is against the
    declarations rather than against `source_file`, which is only written further
    down and so is absent from every row on the run that matters.

    A short name is only followed if exactly one declaration in the model claims
    it, and the entity it merges into has to exist already. Both are true
    throughout this corpus; on one where they are not, the pair is left alone,
    which is the same graph as before this step.
    """
    declared: set[str] = set()
    claimed: dict[tuple[str, str], set[str]] = {}
    for element in structure["elements"]:
        model = config.model_of(element["file"])
        canonical = resolve(lookup, element["qualified"], model)
        if not canonical:
            continue
        declared.add(canonical)
        for written in (element["short"], f"{element['short']} {element['name']}"):
            alias = alias_key(written) if element.get("short") else ""
            # A one-character alias is a unit symbol (`$`, `%`) reduced to nothing
            # much, and would match far too widely.
            if len(alias) > 1:
                claimed.setdefault((model, alias), set()).add(canonical)

    rows = db.aql.execute(
        f"FOR e IN {config.ENTITIES} RETURN {{k: e._key, n: e.entity_name, m: e.models}}")
    merges = {}
    for row in rows:
        if row["k"] in declared:
            continue
        alias = alias_key(row["n"] or "")
        for model in row["m"] or []:
            canonical = claimed.get((model, alias)) or set()
            if len(canonical) == 1:
                merges[row["k"]] = next(iter(canonical))
    return merges


def merge(db, merges: dict[str, str], lookup: dict[str, list[dict]]) -> int:
    """Move every edge off the duplicates, then delete them.

    An edge that ends up joining an entity to itself is dropped rather than kept:
    it was only ever the two names of one element being related to each other.
    Two edges that become identical are collapsed for the same reason -- otherwise
    anything that counts relations counts the duplication that was just removed.

    Where the surviving entity is one this step created, its description is the
    sentence `describe` generated and the duplicate's is what extraction actually
    read out of the file. Keeping the generated one would make the merge a net
    loss, so the real prose and its vector are taken across first.
    """
    if not merges:
        return 0
    db.aql.execute(
        f"""FOR pair IN @pairs
              LET dup = DOCUMENT("{config.ENTITIES}", pair[0])
              LET keep = DOCUMENT("{config.ENTITIES}", pair[1])
              FILTER dup != null AND keep != null
                 AND keep.stated == true AND dup.description != null
              UPDATE keep WITH {{description: dup.description,
                                 {config.EMBEDDING_FIELD}: dup.{config.EMBEDDING_FIELD}}}
              IN {config.ENTITIES}""",
        bind_vars={"pairs": [list(p) for p in merges.items()]})
    bind = {"@rel": config.RELATIONS, "map": merges, "ents": config.ENTITIES}
    endpoints = """
      LET nf = @map[PARSE_IDENTIFIER(r._from).key]
      LET nt = @map[PARSE_IDENTIFIER(r._to).key]
      FILTER nf != null OR nt != null
      LET f = nf == null ? r._from : CONCAT(@ents, "/", nf)
      LET t = nt == null ? r._to : CONCAT(@ents, "/", nt)"""
    db.aql.execute(f"FOR r IN @@rel {endpoints} FILTER f == t REMOVE r IN @@rel", bind_vars=bind)
    db.aql.execute(f"FOR r IN @@rel {endpoints} UPDATE r WITH {{_from: f, _to: t}} IN @@rel",
                   bind_vars=bind)
    db.aql.execute(
        """FOR r IN @@rel
              COLLECT f = r._from, t = r._to, ty = r.type, rt = r.relationship_type INTO group
              FILTER LENGTH(group) > 1
              FOR extra IN SLICE(group[*].r, 1) REMOVE extra IN @@rel""",
        bind_vars={"@rel": config.RELATIONS})
    db.collection(config.ENTITIES).delete_many([{"_key": k} for k in merges])

    # `resolve` reads this afterwards, and a deleted key would come back as the
    # target of an edge that then has no such vertex.
    for entries in lookup.values():
        entries[:] = [e for e in entries if e["key"] not in merges]
    return len(merges)


def describe(element: dict) -> str:
    """A plain sentence for an element extraction did not find, so it can be
    embedded and retrieved like any other."""
    where = f"{element['file']}:{element['line']}"
    typed = f", typed by {element['typed']}" if element.get("typed") else ""
    return (f"{element['name']} is a {element['kind']} declared in "
            f"{element['qualified']}{typed}, at {where}.")


def embed(texts: list[str]) -> list[list[float]]:
    """The same embedding the extraction half uses, called synchronously.

    `openai_embedding` is what wrote every other vector in the collection
    (text-embedding-3-small, no `dimensions` argument), so a vector made any other
    way would not be in the same space as the rows it is compared against.
    """
    from openai import OpenAI

    client = OpenAI(api_key=config.openai_key())
    out: list[list[float]] = []
    for i in range(0, len(texts), 128):
        batch = client.embeddings.create(model=config.EMBED_MODEL, input=texts[i:i + 128])
        out.extend(d.embedding for d in batch.data)
    return out


def key_of(entity_name: str) -> str:
    """The importer's own key scheme, so a later re-import updates rather than
    duplicates: farmhash of the name, then the import number."""
    import farmhash

    return f"{farmhash.Fingerprint64(entity_name)}_0"


def apply(db, structure: dict | None = None, create_missing: bool = True) -> dict[str, int]:
    """Write the attributes and the stated relations onto the extracted graph.

    Most of it lands on entities extraction already made. The rest are elements a
    file declares and the extraction did not report -- roughly one in seven here,
    and the proportion is a property of the file, not of this corpus. Those are
    created rather than dropped, so the containment tree has no holes on a model
    nobody has run before. They are marked `stated` and given a deterministic
    description so they embed and retrieve like the others.

    `create_missing=False` skips that and only annotates what is already there.
    Creating needs the Entities vector index to be absent -- ArangoDB rejects a
    document with no value in an indexed vector field -- which is why `load` calls
    this before it builds the indexes.

    The edges are `RELATED_TO` with the SysML relation in `relationship_type`,
    the same shape extraction writes, so nothing downstream has to know which of
    the two put an edge there. `stated` marks what came from here, for anyone who
    does want to tell them apart.
    """
    structure = structure or read()
    lookup = index_of(db)
    coll = db.collection(config.ENTITIES)

    # Before anything is resolved, so that an element with two names is one row by
    # the time attributes and edges are attached to it.
    merged = merge(db, plan_merges(db, structure, lookup), lookup)

    updates, missing = [], []
    for element in structure["elements"]:
        key = resolve(lookup, element["qualified"], config.model_of(element["file"]))
        if key:
            row = {"_key": key, "source_file": element["file"], "source_line": element["line"]}
            if element.get("attributes"):
                row["attributes"] = element["attributes"]
            if element.get("short"):
                row["short_name"] = element["short"]
            updates.append(row)
        else:
            missing.append(element)

    created = []
    if create_missing and missing:
        vectors = embed([describe(e) for e in missing])
        for element, vector in zip(missing, vectors):
            name = element["qualified"].upper()
            models = [config.model_of(element["file"])]
            created.append({
                "_key": key_of(name), "entity_name": name,
                "entity_type": element["kind"], "description": describe(element),
                config.EMBEDDING_FIELD: vector, "clusters": [], "import_number": 0,
                "files": [element["file"]], "models": models,
                "source_file": element["file"], "source_line": element["line"],
                "short_name": element["short"] or None,
                "attributes": element.get("attributes") or {}, "stated": True,
            })
            entry = {"key": created[-1]["_key"], "models": models}
            lookup.setdefault(name, []).append(entry)
            lookup.setdefault(element["name"].upper(), []).append(entry)
        for i in range(0, len(created), 500):
            coll.import_bulk(created[i:i + 500], on_duplicate="update")

    for i in range(0, len(updates), 500):
        coll.import_bulk(updates[i:i + 500], on_duplicate="update")

    # Again, because a short name whose element extraction never reported had
    # nothing to merge into until the loop above created it.
    merged += merge(db, plan_merges(db, structure, lookup), lookup)

    # `short_name` is where the short name belongs and where a query should read
    # it, but the lexical half of `local` search only ever looks at `entity_name`
    # and `description`. Merging the `DE-REQ-1` row away would otherwise make the
    # identifier an engineer actually types unfindable, so the name goes into the
    # searchable text as well. Guarded on CONTAINS, so a re-run adds nothing.
    db.aql.execute(
        f"""FOR e IN {config.ENTITIES}
              FILTER e.short_name != null AND !CONTAINS(e.description, e.short_name)
              UPDATE e WITH {{description: CONCAT(e.description, " Also written ",
                                                  e.short_name, ".")}} IN {config.ENTITIES}""")

    edges, dropped = {}, 0
    for relation in structure["relations"]:
        # Both ends inside the model that states the relation. No file here
        # references another model, so an edge that leaves one is a name
        # collision rather than a fact -- `power` alone would produce seven.
        model = config.model_of(relation["file"])
        source = resolve(lookup, relation["from"], model, same_model_only=True)
        target = resolve(lookup, relation["to"], model, same_model_only=True)
        if not source or not target or source == target:
            dropped += 1
            continue
        edges[(source, target, relation["type"])] = {
            "_from": f"{config.ENTITIES}/{source}", "_to": f"{config.ENTITIES}/{target}",
            "type": "RELATED_TO", "relationship_type": relation["type"],
            "description": f"{relation['from']} {relation['type']} {relation['to']}",
            "weight": 1.0, "order": 1, "stated": True,
            "source_file": relation["file"], "source_line": relation["line"],
        }

    # Re-runnable: drop what a previous run of this step wrote, then insert. The
    # extraction's own edges have no `stated` flag and are left alone.
    db.aql.execute(f"FOR r IN {config.RELATIONS} FILTER r.stated == true "
                   f"REMOVE r IN {config.RELATIONS}")
    rel = db.collection(config.RELATIONS)
    rows = list(edges.values())
    for i in range(0, len(rows), 1000):
        rel.import_bulk(rows[i:i + 1000])

    # A SysML relation joins two elements, so an edge from a row to itself is not
    # a weak fact, it is not a fact. Extraction writes a few -- twelve of them
    # `satisfies`, which is enough to report twelve requirements as met by
    # themselves and leave them out of the list of what nothing satisfies.
    reflexive = db.aql.execute(
        f"""FOR r IN {config.RELATIONS}
              FILTER r.type == "RELATED_TO" AND r._from == r._to
              REMOVE r IN {config.RELATIONS} COLLECT WITH COUNT INTO n RETURN n""")

    # Where extraction guessed a relation the syntax also states, the two edges
    # are one fact written twice, and anything grouping by relationship_type
    # counts it twice. The read one is kept: it carries the file and line.
    superseded = db.aql.execute(
        f"""FOR r IN {config.RELATIONS}
              FILTER r.type == "RELATED_TO" AND r.stated != true
              LET twin = FIRST(FOR s IN {config.RELATIONS}
                FILTER s._from == r._from AND s._to == r._to
                   AND s.relationship_type == r.relationship_type AND s.stated == true
                RETURN 1)
              FILTER twin != null
              REMOVE r IN {config.RELATIONS}
              COLLECT WITH COUNT INTO n RETURN n""")

    return {"elements": len(structure["elements"]), "created": len(created),
            "with_attributes": sum(1 for e in structure["elements"] if e.get("attributes")),
            "merged": merged, "edges": len(rows), "dropped": dropped,
            "superseded": next(iter(superseded), 0) + next(iter(reflexive), 0)}


def main(create_missing: bool = True) -> None:
    structure = read()
    config.OUT.mkdir(parents=True, exist_ok=True)
    STRUCTURE_JSON.write_text(json.dumps(structure, indent=1), encoding="utf-8")
    counts = apply(config.db(), structure, create_missing=create_missing)
    print(f"  read {counts['elements']} declared elements, "
          f"{counts['with_attributes']} of them carrying values")
    print(f"  {counts['created']:>6}  entities extraction missed, created here")
    print(f"  {counts['merged']:>6}  short-name duplicates folded into the element they name")
    print(f"  {counts['edges']:>6}  stated relations written")
    print(f"  {counts['superseded']:>6}  LLM edges dropped (stated twin, or reflexive)")
    print(f"  {counts['dropped']:>6}  relations dropped (an end is outside the models)")


if __name__ == "__main__":
    main()
