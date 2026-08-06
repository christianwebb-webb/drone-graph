"""SysML v2 textual notation -> {elements, relations}.

A hand-written scanner rather than a grammar. SysML v2 text is brace-nested and
statement-oriented, so a character scanner that tracks `{`, `}` and `;` recovers the
containment tree directly, and every relation this model uses is announced by a
keyword at the head of a statement (`satisfy`, `perform`, `#refinement dependency`,
`:>`, ...). That fits in one readable file with no grammar dependency and no
recovery pass for the constructs a grammar mis-parses.

Name lookup is scoped the way SysML scopes it: a reference is matched against the
enclosing declaration first, then the file's own declarations, then whatever that
file imports. Without the import step two packages that both declare
`apollo11MissionSystem` are indistinguishable and half the references land on the
wrong one.

What it does not do: resolve against the SysML standard library, follow
specialization when matching a redefined feature, evaluate expressions, or type
check. A reference that resolves nowhere in the corpus becomes a stub element
flagged `isLibrary` -- reported, never dropped.

    python -m sysml.pipeline.parse            # writes out/model.json
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator

from .. import config

# Words that qualify a declaration without being its kind.
MODIFIERS = {
    "private", "public", "protected", "abstract", "ref", "in", "out", "inout",
    "readonly", "derived", "end", "nonunique", "ordered", "library", "individual",
    "variation", "variant",
}

# Statement kinds that become elements. The metatype is this name plus Definition or
# Usage depending on whether `def` follows; Package has neither form.
#
# ONTOLOGY (1 of 3): the entity half. These are the `entity_type` values that end up
# on every Entity. GraphRAG's extraction path takes the equivalent list as its
# `entity_types` argument and prompts an LLM to use only those; here the same
# vocabulary is enforced by recognising the keyword in the grammar, so an element is
# whatever SysML says it is. Nothing declares this list as data -- the other two
# places have to agree with it by hand.
KINDS = {
    "package": "Package", "part": "Part", "action": "Action", "state": "State",
    "port": "Port", "item": "Item", "attribute": "Attribute", "requirement": "Requirement",
    "calc": "Calc", "analysis": "Analysis", "connection": "Connection",
    "interface": "Interface", "view": "View", "viewpoint": "Viewpoint",
    "enum": "Enumeration", "concern": "Concern", "constraint": "Constraint",
    "flow": "Flow", "allocation": "Allocation", "event": "Event", "metadata": "Metadata",
    "use": "UseCase", "rendering": "Rendering", "verification": "Verification",
    "snapshot": "Snapshot", "timeslice": "Timeslice", "occurrence": "Occurrence",
}

SLICE_KINDS = {"snapshot": "sliceOf", "timeslice": "sliceOf"}

# The SysML library's implicit start/end of a sequence, not steps in it.
FLOW_PSEUDO_NODES = {"start", "done"}

# Prefixes that both declare what follows and draw a relation to it:
#   `perform action checkStatus { ... }` declares checkStatus and performs it.
#   `do action recoveryOps { ... }` is a state's do-behaviour, same shape.
PREFIX_RELATION = {"perform": "performs", "exhibit": "exhibits", "do": "performs"}

# Statement heads whose payload is a block comment, so the comment terminates them.
COMMENT_HEADS = {"doc", "comment", "rep", "language"}

# Kinds whose bodies are opaque text rather than nested declarations.
OPAQUE = {"constraint", "metadata", "doc", "comment", "rep", "language", "assume", "require", "assert"}

SHORTNAME = re.compile(r"<\s*('[^']*'|\"[^\"]*\"|[\w.\-]+)\s*>")
IDENT = re.compile(r"^(?:'[^']*'|\"[^\"]*\"|[A-Za-z_][\w.\-]*)")
QUANTITY = re.compile(r"^(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*(?:\[\s*([^\]]+?)\s*\])?$")
# A value that is a plain reference to another element rather than a number or an
# expression: `= FlightControlVariation::droneFlightControl4Engines`. See the
# `valueRef` note in `declare` for why these become edges.
REFERENCE_VALUE = re.compile(r"^[A-Za-z_][\w.\-]*(?:::[\w.\-']+)+$")


def unquote(token: str) -> str:
    return token[1:-1] if len(token) > 1 and token[0] in "'\"" and token[-1] == token[0] else token


# --------------------------------------------------------------------------- scan


def scan(src: str) -> Iterator[tuple[str, int, str]]:
    """Yield (statement text, 1-based line, terminator) for every statement.

    The terminator is `{` (opens a body), `}` (closes one) or `;`. Comments are
    dropped, string and quoted-name literals are passed through intact, and a
    `doc /* ... */` is closed by its own comment because nothing else closes it.
    """
    out, line, start, i, n = [], 1, 1, 0, len(src)
    # `start` has to be the line of the statement's first real character, not the
    # line the previous statement ended on. A newline puts a space in the buffer, so
    # "buffer is empty" stops being a usable test for "nothing seen yet" after the
    # first line break -- which silently shifted every citation up by a line or two.
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
            body = src[i:j]
            head = "".join(out).strip()
            line += body.count("\n")
            i = j
            # `doc /* ... */` and `comment source /* ... */` carry no terminator of
            # their own -- the comment ends them. Leaving the head in the buffer
            # prefixes it onto the next declaration, which is then never recognised:
            # one `comment source` cost the model a whole part usage.
            if head.split(" ")[0] in COMMENT_HEADS:
                if head == "doc":
                    out.append(body)
                yield from flush("")
                start = line
            # Any other block comment is just a comment and is dropped.
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


def split_head(text: str) -> dict[str, Any]:
    """Pull a declaration apart into modifiers, kind, names, typing and value."""
    text = re.sub(r"\s+", " ", text).strip()
    mods: list[str] = []
    tokens = text.split(" ")
    while tokens and tokens[0] in MODIFIERS:
        mods.append(tokens.pop(0))
    if not tokens:
        return {"mods": mods, "kind": "", "rest": ""}
    kind = tokens.pop(0)
    is_def = bool(tokens) and tokens[0] == "def"
    if is_def:
        tokens.pop(0)
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

    typed, specials, redefines, value = "", [], "", ""
    while rest:
        if rest.startswith(":>>"):
            rest = rest[3:].strip()
            redefines, rest = take_ref(rest)
        elif rest.startswith(":>"):
            rest = rest[2:].strip()
            while rest:
                ref, rest = take_ref(rest)
                if ref:
                    specials.append(ref)
                if rest.startswith(","):
                    rest = rest[1:].strip()
                else:
                    break
        elif rest.startswith(":"):
            rest = rest[1:].strip()
            typed, rest = take_ref(rest)
        elif rest.startswith("="):
            value, rest = rest[1:].strip(), ""
        else:
            rest = rest[1:].strip() if rest else ""
    return {
        "mods": mods, "kind": kind, "is_def": is_def, "short": short, "name": name,
        "typed": typed, "specializes": specials, "redefines": redefines, "value": value,
    }


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


def parse_value(raw: str) -> dict[str, Any]:
    """`2 [m/s]` -> number + unit; anything else is kept as an expression."""
    raw = raw.strip().rstrip(";").strip()
    if not raw:
        return {}
    m = QUANTITY.match(raw)
    if m:
        num = float(m.group(1))
        # The corpus writes the same unit three ways -- `[kg]`, `[SI::kg]`, `['s']`.
        # Reduce to the bare symbol, or a rollup treats them as different units and
        # either refuses to sum or sums things it should not.
        unit = m.group(2)
        if unit:
            unit = unquote(unit.split("::")[-1].strip())
        return {"value": int(num) if num.is_integer() else num, "unit": unit or None, "raw": raw}
    if raw[0] in "\"'":
        return {"value": unquote(raw), "raw": raw}
    return {"expression": raw, "raw": raw}


# ------------------------------------------------------------------------- parse


class FileParser:
    """One .sysml file -> elements and unresolved relation references."""

    def __init__(self, path: Path, model: str, layer: str):
        self.path, self.model, self.layer = path, model, layer
        self.elements: list[dict] = []
        self.refs: list[dict] = []
        self.scope: list[dict | None] = []          # None = an opaque or unnamed body
        self.anon = Counter()
        self.flow: list[str | None] = []            # `first`/`then` sequencing per body

    # -- helpers ----------------------------------------------------------------

    @property
    def owner(self) -> dict | None:
        for frame in reversed(self.scope):
            if frame is not None:
                return frame
        return None

    def ref(self, source: str | None, target: str, rtype: str, **extra) -> None:
        if source and target:
            self.refs.append({"from": source, "to": target, "type": rtype,
                              "scope": self.owner["qualifiedName"] if self.owner else "",
                              "file": str(self.path), **extra})

    def add(self, decl: dict, line: int, metatype: str) -> dict:
        parent = self.owner
        # `attribute :>> dryMass = 137000 [kg];` is anonymous but redefines a named
        # feature, and in SysML it takes that feature's name. Without this the value
        # lands on a generated name and no question about dryMass can find it.
        name = decl.get("name") or decl.get("short")
        if not name and decl.get("redefines"):
            name = decl["redefines"].split("::")[-1].split(".")[-1]
        if not name:
            key = decl["kind"]
            self.anon[key] += 1
            name = f"{key}{self.anon[key]}"
        prefix = parent["qualifiedName"] + "::" if parent else ""
        element = {
            "qualifiedName": prefix + name,
            "name": name,
            "shortName": decl.get("short") or None,
            "metatype": metatype,
            "model": self.model,
            "layer": self.layer,
            "sourceFile": self.relpath,
            "sourceLine": line,
            "doc": "",
            "parent": parent["qualifiedName"] if parent else None,
            "attributes": {},
            "constraints": [],
            "isVariation": "variation" in decl["mods"],
            "isVariant": "variant" in decl["mods"],
            "isLibrary": False,
        }
        self.elements.append(element)
        if parent:
            self.ref(parent["qualifiedName"], element["qualifiedName"], "owns", resolved=True)
        if decl.get("typed"):
            self.ref(element["qualifiedName"], decl["typed"], "typedBy")
        for sup in decl.get("specializes", []):
            self.ref(element["qualifiedName"], sup, "specializes")
        if decl.get("redefines"):
            self.ref(element["qualifiedName"], decl["redefines"], "redefines")
        if "variant" in decl["mods"] and parent:
            self.ref(element["qualifiedName"], parent["qualifiedName"], "variantOf", resolved=True)
        if decl["kind"] in SLICE_KINDS:
            for sup in decl.get("specializes", []):
                self.ref(element["qualifiedName"], sup, SLICE_KINDS[decl["kind"]])
        if decl.get("value"):
            element["attributes"]["value"] = parse_value(decl["value"])
            # A value can be a literal (`= 750 [kg]`), an expression
            # (`= dryMass + propellantMass`) or a reference to another element
            # (`= FlightControlVariation::droneFlightControl4Engines`). Only the
            # third kind names something that exists elsewhere in the model, so
            # only it gets an edge as well as the stored value.
            #
            # The test is on the shape of the value, not on what it turns out to
            # point at: the parser does not know whether the target is a variant, an
            # enumeration literal or anything else, and does not look. That keeps one
            # rule instead of a list of cases, and it is why the edge is called
            # `valueRef` rather than something that describes only one use of it. A
            # consumer that wants variant selections filters on `is_variant` on the
            # target; a consumer that wants enumeration bindings filters on the
            # target's kind. Naming the edge after either would misdescribe the rest.
            if REFERENCE_VALUE.match(decl["value"].strip().rstrip(";")):
                self.ref(element["qualifiedName"], decl["value"].strip().rstrip(";"),
                         "valueRef", line=line)
        return element

    @property
    def relpath(self) -> str:
        return self.path.relative_to(config.MODELS).as_posix()

    # -- statements -------------------------------------------------------------

    def run(self) -> None:
        self.last_closed: dict | None = None
        self.in_constraint = False
        self.after_close = False
        self.ends: dict | None = None
        for text, line, term in scan(self.path.read_text(encoding="utf-8")):
            if term == "}":
                # The body text of a constraint arrives here, not through handle():
                # `require constraint { a <= b }` is two statements and the second
                # one is the closing brace.
                if text and self.in_constraint and self.owner:
                    self.owner["constraints"].append(re.sub(r"\s+", " ", text).strip())
                self.in_constraint = False
                # `#derivation connection { end #original ::> A; end #derive ::> B;
                # end #derive ::> C; }` derives both B and C from A.
                if self.ends and len(self.ends["refs"]) >= 2:
                    first, *rest = self.ends["refs"]
                    for other in rest:
                        self.ref(first, other, self.ends["type"], line=self.ends["line"])
                self.ends = None
                if self.scope:
                    self.last_closed = self.scope.pop()
                    self.flow.pop()
                self.after_close = True
                continue
            opened = self.handle(text, line) if text else None
            self.after_close = False
            if term == "{":
                self.scope.append(opened)
                self.flow.append(None)
            elif opened:
                self.last_closed = opened

    def handle(self, text: str, line: int) -> dict | None:
        if text[0] in ":=":
            # Two different statements start the same way. Right after a `}` this is
            # a suffix on the element that body closed:
            #     requirement deploy : Goal { ... } :> goals;
            # anywhere else it is an anonymous feature that redefines an inherited
            # one, which is how a variant selection is written:
            #     :>> flightControl = FlightControlVariation::droneFlightControl4Engines;
            # An `=` settles it: a suffix never assigns a value, a redefinition
            # almost always does. Without this check the selection that follows a
            # variant with its own body is read as a suffix on that variant.
            if self.after_close and self.last_closed and "=" not in text:
                self.apply_suffix(self.last_closed, text)
                return None
            return self.declare(split_head("item " + text), line)

        head = text.split(" ", 1)[0].strip()

        if head == "doc":
            body = re.sub(r"^doc\s*/\*|\*/$", "", text).strip()
            body = re.sub(r"\s*\n\s*", " ", re.sub(r"\s+", " ", body)).strip()
            if self.owner:
                self.owner["doc"] = (self.owner["doc"] + " " + body).strip()
            return None

        if head in ("require", "assert", "assume") and " constraint" in text:
            self.in_constraint = True
            return None

        # A connection declares its endpoints in its body rather than inline:
        #     connection : CapabilityToGoalDerivation {
        #         end capa ::> heavyLiftLaunch;
        #         end goal ::> goToMoon;
        #     }
        # and `#derivation connection { ... }` is the same shape with a metadata tag
        # naming the relation. Without collecting these the endpoints are dropped and
        # the connection element sits in the graph attached to nothing.
        m = re.match(r"#(\w+)\s+(?:connection|dependency)\b", text)
        if m and " to " not in text:
            tag = {"derivation": "derives", "refinement": "refines"}.get(m.group(1), "dependsOn")
            self.ends = {"type": tag, "refs": [], "line": line}
            return None
        if self.ends is not None:
            # Only an end that *references* something is an endpoint. An interface
            # definition declares abstract ends instead -- `end source : StagingPort`
            # names a port type, not a participant, and treating it as one produced
            # an edge from the word "end" to itself.
            m = re.match(r"end\s+(?:#\w+\s*)?[\w']*\s*(?:::>|:>>|:>|=)\s*(.+)$", text)
            if m:
                target, _ = take_ref(m.group(1).strip())
                if target:
                    self.ends["refs"].append(target)
                return None

        if head in ("first", "then"):
            return self.handle_flow(text, line)

        if head in OPAQUE or text.startswith("@") or head == "accept":
            return None

        if head == "subject":                       # `subject apollo11Mission : Mission`
            decl = split_head("item " + text.split(" ", 1)[1])
            element = self.add(decl, line, "SubjectUsage")
            target = decl["typed"] or decl["redefines"]
            if target and element["parent"]:
                self.ref(element["parent"], target, "subject", line=line)
            return element

        if head == "satisfy":                       # satisfy R by S  ->  S --satisfies--> R
            m = re.match(r"satisfy\s+(.+?)\s+by\s+(.+)$", text)
            if m:
                req, _ = take_ref(m.group(1).strip())
                sat, _ = take_ref(m.group(2).strip())
                self.ref(sat, req, "satisfies", line=line)
            return None

        if "dependency" in text.split("{")[0] and " to " in text:
            m = re.match(r"(?:#(\w+)\s+)?dependency\s+(?:<[^>]*>\s*)?(.+?)\s+to\s+(.+)$", text)
            if m:
                kind = {"refinement": "refines", "derivation": "derives"}.get(m.group(1), "dependsOn")
                src, _ = take_ref(m.group(2).strip())
                for part in m.group(3).split(","):
                    tgt, _ = take_ref(part.strip())
                    self.ref(src, tgt, kind, line=line)
            return None

        if head == "connect":
            m = re.match(r"connect\s+(.+?)\s+to\s+(.+)$", text)
            if m:
                a, _ = take_ref(m.group(1).strip())
                b, _ = take_ref(m.group(2).strip())
                self.ref(a, b, "connects", line=line)
            return None

        if head == "send":
            m = re.match(r"send\s+(.+?)\s+to\s+(.+)$", text)
            if m:
                a, _ = take_ref(m.group(1).strip())
                b, _ = take_ref(m.group(2).strip())
                self.ref(a, b, "sends", line=line)
            return None

        if head == "transition":
            return self.handle_transition(text, line)

        if head == "import" or (head in MODIFIERS and " import " in f" {text} "):
            target, _ = take_ref(re.sub(r"^.*?\bimport\s+", "", text).replace("::*", ""))
            if self.owner:
                self.ref(self.owner["qualifiedName"], target, "imports", line=line)
            return None

        decl = split_head(text)
        prefix_rel = PREFIX_RELATION.get(decl["kind"])
        if prefix_rel:
            tail = text.split(" ", 1)[1] if " " in text else ""
            inner = split_head(tail)
            if inner["kind"] not in KINDS:            # `perform LaunchSystem::guideAscent`
                target, _ = take_ref(tail)
                if self.owner and target:
                    self.ref(self.owner["qualifiedName"], target, prefix_rel, line=line)
                return None
            element = self.declare(inner, line)       # `perform action X` / `do action Y`
            if element and element["parent"]:
                self.ref(element["parent"], element["qualifiedName"], prefix_rel, resolved=True)
            return element

        element = self.declare(decl, line)
        if decl["kind"] in ("connection", "interface"):
            # `interface lvToPayload : LVPayloadInterface connect a.port to b.port;`
            # states its endpoints inline; the body form is collected by self.ends.
            m = re.search(r"\bconnect\s+(.+?)\s+to\s+(.+)$", text)
            if m:
                a, _ = take_ref(m.group(1).strip())
                b, _ = take_ref(m.group(2).strip())
                self.ref(a, b, "connects", line=line)
            else:
                self.ends = {"type": "connects", "refs": [], "line": line}
        return element

    def apply_suffix(self, element: dict, text: str) -> None:
        decl = split_head("item _suffix " + text)
        for sup in decl["specializes"]:
            self.ref(element["qualifiedName"], sup, "specializes")
        if decl["typed"]:
            self.ref(element["qualifiedName"], decl["typed"], "typedBy")
        if decl["redefines"]:
            self.ref(element["qualifiedName"], decl["redefines"], "redefines")

    def declare(self, decl: dict, line: int) -> dict | None:
        kind = decl.get("kind", "")
        if kind not in KINDS:
            return None
        base = KINDS[kind]
        if base == "Package":
            metatype = "Package"
        elif "variant" in decl["mods"] and not decl["is_def"]:
            metatype = base + "Usage"
        else:
            metatype = base + ("Definition" if decl["is_def"] else "Usage")
        return self.add(decl, line, metatype)

    def handle_transition(self, text: str, line: int) -> None:
        m = re.search(r"first\s+(.+?)(?:\s+accept\s+(.+?))?\s+then\s+(.+)$", text)
        if not m:
            return None
        src, _ = take_ref(m.group(1).strip())
        tgt, _ = take_ref(m.group(3).strip())
        trigger = take_ref(m.group(2).strip())[0] if m.group(2) else None
        self.ref(src, tgt, "transitionsTo", line=line, trigger=trigger)
        return None

    def handle_flow(self, text: str, line: int) -> dict | None:
        """`first a; then b; then c;` inside an action body is a sequence.

        `then action retrieveCrewAndCM : RetrieveCrewAndCM;` both declares the step
        and sequences to it, so this has to be able to return a new element.
        """
        keyword, _, tail = text.partition(" ")
        element, decl = None, split_head(tail)
        if decl["kind"] in KINDS:
            element = self.declare(decl, line)
            target = element["qualifiedName"] if element else ""
        else:
            target, _ = take_ref(tail.strip())
        # `start` and `done` are the library's flow pseudo-nodes, not steps. They say
        # "this is the first/last one", which the sequence already says, and treating
        # them as elements makes one shared node that every state machine in every
        # model transitions through -- a false path between unrelated models.
        if target in FLOW_PSEUDO_NODES:
            return element
        if not target:
            return element
        if keyword == "then" and self.flow and self.flow[-1]:
            self.ref(self.flow[-1], target, "transitionsTo", line=line)
        if self.flow:
            self.flow[-1] = target
        return element


# ---------------------------------------------------------------------- resolve


def resolve_all(elements: list[dict], refs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Turn name references into edges, inventing stubs for unresolved library types."""
    by_qn = {e["qualifiedName"]: e for e in elements}
    by_short: dict[str, list[dict]] = defaultdict(list)
    by_simple: dict[str, list[dict]] = defaultdict(list)
    by_tail: dict[str, list[dict]] = defaultdict(list)
    for e in elements:
        if e["shortName"]:
            by_short[e["shortName"]].append(e)
        by_simple[e["name"]].append(e)
        parts = e["qualifiedName"].split("::")
        for i in range(len(parts)):
            by_tail["::".join(parts[i:])].append(e)

    # SysML resolves a name against what the file imports. Two packages can both
    # declare `apollo11MissionSystem`; the one the referencing file imported is the
    # one meant. Without this a snapshot of the mission system was recorded as a
    # slice of an unrelated analysis subject that happened to share the name.
    imported: dict[str, set[str]] = defaultdict(set)
    for r in refs:
        if r["type"] == "imports":
            imported[r["file"]].add(r["to"].split("::")[0])

    stubs: dict[str, dict] = {}
    relations, unresolved = [], []

    def best(candidates: list[dict], scope: str, model: str,
             exclude: dict | None = None, ref_file: str = "") -> dict | None:
        # Nothing types, specializes or redefines itself. Without this an anonymous
        # `attribute :>> dryMass = 137000` -- which takes its name from the feature
        # it redefines -- resolves the reference straight back to the element it
        # just created, and the graph fills with self-loops.
        candidates = [c for c in candidates if c is not exclude]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        visible = imported.get(ref_file, set())

        def score(e: dict) -> tuple:
            shared = 0
            a, b = scope.split("::"), e["qualifiedName"].split("::")
            for x, y in zip(a, b):
                if x != y:
                    break
                shared += 1
            return (shared, e["model"] == model, e["sourceFile"] == ref_file,
                    b[0] in visible, -len(b))

        return max(candidates, key=score)

    def lookup(ref: str, scope: str, model: str, exclude: dict | None = None,
               ref_file: str = "") -> dict | None:
        if ref in by_qn and by_qn[ref] is not exclude:
            return by_qn[ref]
        for table in (by_tail, by_short, by_simple):
            hit = best(table.get(ref, []), scope, model, exclude, ref_file)
            if hit:
                return hit
        # `a.b.c` navigation and `A::b::c` both end at the last segment. `by_short`
        # has to be in here: a reference is routinely written qualified,
        # `FunctionalRequirementsPackage::'FLR-R008'`, while the element carries
        # FLR-R008 as a short name and something else entirely as its name. Leaving
        # it out sent 214 of 368 refines edges to invented stubs.
        tail = re.split(r"[.:]+", ref)[-1]
        candidates = by_tail.get(tail, []) or by_simple.get(tail, [])
        seen = {id(e) for e in candidates}
        candidates = candidates + [e for e in by_short.get(tail, []) if id(e) not in seen]
        # The three models are independent namespaces, so a bare last-segment match
        # across them is always an accident: `ISQ::length` found a `length` attribute
        # in the drone model and gave Apollo's SaturnV a drone part as its supertype.
        if model:
            candidates = [c for c in candidates if c["model"] == model]
        if "::" in ref:
            # The namespace was written down. Require it to appear in the candidate's
            # path; if it does not, the name is external and belongs in a stub.
            qualifier = ref.split("::")[-2]
            candidates = [c for c in candidates
                          if qualifier in c["qualifiedName"].split("::")]
        return best(candidates, scope, model, exclude, ref_file)

    def stub(ref: str, model: str, source_file: str, line: int) -> dict:
        """A name that resolves nowhere in the corpus comes from outside it -- the
        SysML standard library, almost always (`Integer`, `ISQ`, the implicit
        `start` of a state machine). Stub it and flag it rather than dropping the
        edge, so the reference stays visible and countable."""
        return stubs.setdefault(ref, {
            "qualifiedName": ref, "name": ref.split("::")[-1], "shortName": None,
            "metatype": "LibraryStub", "model": model, "layer": "Library",
            "sourceFile": source_file, "sourceLine": line, "doc": "", "parent": None,
            "attributes": {}, "constraints": [], "isVariation": False,
            "isVariant": False, "isLibrary": True,
        })

    for r in refs:
        source = by_qn.get(r["from"])
        scope, model = r.get("scope", ""), source["model"] if source else ""
        if source is None:
            source = lookup(r["from"], scope, model, ref_file=r["file"])
        if source is None:
            unresolved.append({**r, "missing": "source"})
            continue
        target = (by_qn.get(r["to"]) if r.get("resolved")
                  else lookup(r["to"], scope, model, exclude=source, ref_file=r["file"]))
        if target is None:
            target = stub(r["to"], source["model"], source["sourceFile"], r.get("line") or 0)
        relations.append({
            "from": source["qualifiedName"], "to": target["qualifiedName"], "type": r["type"],
            "sourceFile": source["sourceFile"], "sourceLine": r.get("line") or source["sourceLine"],
            "trigger": r.get("trigger"),
        })

    return relations, unresolved, list(stubs.values())


def fold_attributes(elements: list[dict], relations: list[dict]) -> None:
    """Copy each attribute element's value onto its owner under the attribute's name.

    An attribute is both an element (so `owns` stays uniform) and a field on its
    owner (so `attributes.totalMass.value` is one hop, which is what a question
    about a number actually wants).
    """
    by_qn = {e["qualifiedName"]: e for e in elements}
    for e in elements:
        if not e["metatype"].startswith("Attribute") or not e["attributes"].get("value"):
            continue
        owner = by_qn.get(e["parent"] or "")
        if owner is not None:
            owner["attributes"][e["name"]] = e["attributes"]["value"]


def layer_of(path: Path) -> str:
    """The folder a file sits in: Requirements, Technical, Purpose, ...

    A file at the root of a model tree has no folder to take a layer from, so it
    gets `Model` -- Apollo11Model.sysml is the top-level assembly, not a layer.
    """
    parts = path.relative_to(config.MODELS).parts
    if len(parts) == 1:
        return "Drone"
    return parts[1] if len(parts) > 2 else "Model"


def parse_all(models_dir: Path | None = None) -> dict[str, Any]:
    models_dir = models_dir or config.MODELS
    files = sorted(models_dir.rglob("*.sysml"))
    elements: list[dict] = []
    refs: list[dict] = []
    for path in files:
        rel = path.relative_to(models_dir).as_posix()
        model = next((v for k, v in config.MODELS_INDEX.items() if rel.startswith(k)), "unknown")
        fp = FileParser(path, model, layer_of(path))
        fp.run()
        elements.extend(fp.elements)
        refs.extend(fp.refs)

    # A duplicated qualifiedName means two declarations of the same path; keep the
    # first and let the second's edges resolve onto it.
    seen, unique = set(), []
    for e in elements:
        if e["qualifiedName"] in seen:
            continue
        seen.add(e["qualifiedName"])
        unique.append(e)

    relations, unresolved, stubs = resolve_all(unique, refs)
    unique.extend(stubs)
    fold_attributes(unique, relations)

    # A graph edge is (from, to, type); the same pair authored twice adds nothing.
    # Both counts are reported because the authored count is what a reader can
    # verify by grepping the sources.
    dedup = {}
    for r in relations:
        dedup[(r["from"], r["to"], r["type"])] = r

    return {
        "files": [p.relative_to(models_dir).as_posix() for p in files],
        "elements": unique,
        "relations": list(dedup.values()),
        "unresolved": unresolved,
        "authored_relation_counts": dict(Counter(r["type"] for r in relations)),
    }


def main() -> None:
    model = parse_all()
    config.OUT.mkdir(parents=True, exist_ok=True)
    config.MODEL_JSON.write_text(json.dumps(model, indent=1), encoding="utf-8")
    rel_types = Counter(r["type"] for r in model["relations"])
    meta = Counter(e["metatype"] for e in model["elements"])
    print(f"{len(model['files'])} files -> {len(model['elements'])} elements, "
          f"{len(model['relations'])} relations, {len(model['unresolved'])} unresolved")
    print("  relations :", ", ".join(f"{k}={v}" for k, v in rel_types.most_common()))
    print("  metatypes :", ", ".join(f"{k}={v}" for k, v in meta.most_common(8)))
    print(f"  written   : {config.MODEL_JSON}")


if __name__ == "__main__":
    main()
