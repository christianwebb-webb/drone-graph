"""Two ways to ask the graph a question in English. Both are stock read paths.

`aqlizer(question)` -- Arango's natural-language-to-AQL service, the `txt2aql`
package from natural-language-service, imported unmodified. It inspects the
collections, writes AQL, runs it and explains the rows. The only thing this project
gives it is `aql_examples`, read from `aql_examples.md` -- the argument
`ReadOnlyArangoGraphQAChain.from_llm` has always accepted and the deployed service
never passes. There are no hand-written query functions here: if an answer is wrong
the fix goes in that file, not into Python.

`graphrag(question)` -- the retrieval path a GraphRAG reader takes over an importer
corpus: embed the question, vector-search the entities, the chunks and the community
reports, expand one hop over the typed edges, and answer from what came back. This
is the path that handles descriptive questions; AQLizer handles analytical ones.

Every AQLizer answer carries the AQL that produced it, because a generated query
that is subtly wrong returns no rows, and a fluent sentence over no rows is
indistinguishable from a correct answer about something genuinely absent.

    python -m sysml.nl "which requirements does nothing satisfy?"
    python -m sysml.nl --graphrag "what does the drone battery do?"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from openai import OpenAI

from . import config
from .pipeline.enrich import embed_query

SERVICE_REPO = config.ROOT.parent / "natural-language-service"

# The service's read-only check (WRITE_OPERATIONS) does not include TRUNCATE, so a
# generated `FOR c IN [...] TRUNCATE c` passes it. Nothing reaches the database
# without clearing this first.
MUTATION = re.compile(r"\b(INSERT|UPDATE|REPLACE|REMOVE|UPSERT|TRUNCATE)\b", re.I)

ANSWER_RULES = """Answer the question from the context below and nothing else.

- Synthesising across the context is expected. Grouping, comparing and summarising
  what is there -- including the community summaries -- is answering, not guessing.
  What is forbidden is a fact the context does not contain.
- Cite every specific claim with the file and line it came from, written like
  (Drone_BaseArchitecture.sysml:22). Copy the real path and number out of the
  context -- never write the words "source_file" or "source_line" in the answer.
- If the context genuinely lacks what was asked, say "the model does not say".
  Never fall back on general knowledge about Apollo, spacecraft or drones.
- A number must come from an attribute value in the context. If an attribute is an
  expression rather than a value, say it is computed and give the expression.
- If the context shows two facts that contradict each other, report the conflict
  rather than choosing one."""


def _text(value: Any) -> str:
    """The chain hands back a mix of `str` and LangChain `AIMessage`.

    Upstream's `process_nl_query` has the same wrinkle and resolves it with
    `str(AIMessage)`, which glues the model's token accounting onto the AQL. Reading
    `.content` is the fix.
    """
    if value is None:
        return ""
    return str(getattr(value, "content", value)).strip()


@dataclass
class Answer:
    question: str
    answer: str
    aql: str | None = None
    rows: list = field(default_factory=list)
    context: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def show(self, row_limit: int = 6) -> None:
        print(f"Q  {self.question}")
        if self.aql:
            print("\nAQL")
            for line in self.aql.strip().splitlines():
                print(f"   {line}")
        if self.error:
            print(f"\n!! {self.error}")
        if self.rows:
            print(f"\nrows ({len(self.rows)}, first {min(row_limit, len(self.rows))})")
            for row in self.rows[:row_limit]:
                print("   " + json.dumps(row, default=str)[:300])
        elif not self.error:
            print("\nrows (0)")
        print(f"\nA  {self.answer}\n")


# ------------------------------------------------------------------- AQLizer


class Aqlizer:
    """The shipped Txt2AqlService, pointed at this database and given examples."""

    def __init__(self, examples_path: Path | None = None):
        self.examples_path = examples_path or config.AQL_EXAMPLES
        self._service = None
        self._graph = None
        self._bootstrap()

    def _bootstrap(self) -> None:
        if str(SERVICE_REPO) not in sys.path:
            sys.path.insert(0, str(SERVICE_REPO))
        os.environ["ARANGODB_ENDPOINT"] = config.ARANGO_URL
        os.environ["ARANGODB_NAME"] = config.DB_NAME
        os.environ["CHAT_API_KEY"] = config.openai_key()
        os.environ.setdefault("CHAT_MODEL", config.CHAT_MODEL)
        os.environ.setdefault("CHAT_API_PROVIDER", "openai")
        try:
            import txt2aql.service  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                f"cannot import txt2aql ({exc}). Clone natural-language-service next "
                "to this project and install its deps into this interpreter."
            ) from exc
        # txt2aql/logger.py attaches a StreamHandler to stdout at import time, which
        # corrupts anything parsing stdout. The logs are useful; move them to stderr.
        log = logging.getLogger("txt2aql")
        for handler in list(log.handlers):
            if getattr(handler, "stream", None) is sys.stdout:
                log.removeHandler(handler)
        if not log.handlers:
            log.addHandler(logging.StreamHandler(sys.stderr))

    @property
    def examples(self) -> str:
        return self.examples_path.read_text(encoding="utf-8")

    @property
    def service(self):
        if self._service is None:
            from txt2aql.service import Txt2AqlService
            self._service = Txt2AqlService()
        return self._service

    def token(self) -> str:
        r = requests.post(f"{config.ARANGO_URL}/_open/auth", timeout=15,
                          json={"username": config.ARANGO_USER, "password": config.ARANGO_PASS})
        r.raise_for_status()
        return r.json()["jwt"]

    def graph(self):
        """The LangChain ArangoGraph the service builds its schema picture from."""
        if self._graph is None:
            from langchain_arangodb import ArangoGraph
            db = self.service.get_db_client().db(name=config.DB_NAME, user_token=self.token())
            self._graph = ArangoGraph(db=db, generate_schema_on_init=True,
                                      schema_sample_ratio=0, schema_graph_name=None,
                                      schema_include_examples=True, schema_list_limit=32,
                                      schema_string_limit=256)
        return self._graph

    @property
    def schema(self) -> dict:
        return self.graph().schema

    def qa_prompt(self):
        """Upstream's summary prompt with two lines added.

        Upstream's says the summary must be "in the same language as the User Input",
        and on an English question about a Spanish-free graph it has answered in
        Spanish. Pinning the language, and asking for the citation the rows already
        carry, is a supported `from_llm` argument -- not a change to the service.
        """
        from langchain_arangodb.chains.graph_qa.prompts import AQL_QA_TEMPLATE
        from langchain_core.prompts import PromptTemplate
        return PromptTemplate(
            input_variables=["adb_schema", "user_input", "aql_query", "aql_result"],
            template=AQL_QA_TEMPLATE + (
                "\nWrite the Summary in English.\n"
                "If a row carries source_file and source_line, cite them as "
                "(file:line) next to the fact they support.\n"
                "If the AQL Result is empty, say the query returned no rows. Do not "
                "state that the model contains no such thing.\n"))

    def chain(self, primed: bool = True, top_k: int = 40):
        from txt2aql.read_only_chain import ReadOnlyArangoGraphQAChain
        kwargs: dict[str, Any] = {
            "qa_prompt": self.qa_prompt(),
            "graph": self.graph(), "verbose": False, "allow_dangerous_requests": True,
            "force_read_only_query": True, "return_aql_query": True,
            "return_aql_result": True, "execute_aql_query": True,
            "max_aql_generation_attempts": 5, "top_k": top_k,
        }
        if primed:
            kwargs["aql_examples"] = self.examples
        return ReadOnlyArangoGraphQAChain.from_llm(llm=self.service._llm, **kwargs)

    def ask(self, question: str, primed: bool = True, top_k: int = 40) -> Answer:
        try:
            result = self.chain(primed, top_k).invoke({"query": question})
        except Exception as exc:
            return Answer(question, "", error=f"{type(exc).__name__}: {exc}")
        aql = re.sub(r"^```(?:aql)?|```$", "", _text(result.get("aql_query")), flags=re.M).strip()
        if aql and MUTATION.search(aql):
            return Answer(question, "", aql=aql,
                          error="refused: the generated query mutates the graph")
        rows = result.get("aql_result") or []
        return Answer(question, _text(result.get("result")), aql=aql,
                      rows=rows if isinstance(rows, list) else [rows])


# ------------------------------------------------------------------- GraphRAG


ENTITY_SEARCH = f"""
FOR e IN {config.ENTITIES}
  LET score = APPROX_NEAR_COSINE(e.{config.EMBEDDING_FIELD}, @q, {{nProbe: {config.N_PROBE}}})
  SORT score DESC
  LIMIT @k
  RETURN {{name: e.entity_name, type: e.entity_type, at: CONCAT(e.source_file, ':', e.source_line),
           description: e.description, score}}"""

CHUNK_SEARCH = f"""
FOR c IN {config.CHUNKS}
  LET score = APPROX_NEAR_COSINE(c.{config.EMBEDDING_FIELD}, @q, {{nProbe: {config.N_PROBE}}})
  SORT score DESC
  LIMIT @k
  RETURN {{at: CONCAT(c.file_name, ':', c.start_line, '-', c.end_line),
           content: c.content, score}}"""

COMMUNITY_SEARCH = f"""
FOR c IN {config.COMMUNITIES}
  LET score = APPROX_NEAR_COSINE(c.{config.EMBEDDING_FIELD}, @q, {{nProbe: {config.N_PROBE}}})
  SORT score DESC
  LIMIT @k
  RETURN {{title: c.title, level: c.level, members: c.occurrence,
           report: c.report_string, score}}"""

# The edge collection has no ANN index -- ArangoDB requires the vector field on
# every document and only RELATED_TO edges carry one. Exact cosine over 5k edges is
# fast, and unlike APPROX_NEAR_COSINE it can be filtered by relationship_type.
RELATION_SEARCH = f"""
FOR r IN {config.RELATIONS}
  FILTER r.type == 'RELATED_TO' AND IS_LIST(r.{config.EMBEDDING_FIELD})
  FILTER @relation == null OR r.relationship_type == @relation
  LET score = COSINE_SIMILARITY(r.{config.EMBEDDING_FIELD}, @q)
  SORT score DESC
  LIMIT @k
  RETURN {{description: r.description, relation: r.relationship_type,
           at: CONCAT(r.source_file, ':', r.source_line), score}}"""

NEIGHBOURS = f"""
FOR e IN {config.ENTITIES}
  FILTER e.entity_name IN @names
  FOR other, edge IN 1..1 ANY e {config.RELATIONS}
    FILTER edge.type == 'RELATED_TO'
    RETURN DISTINCT {{from: DOCUMENT(edge._from).entity_name,
                      relation: edge.relationship_type,
                      to: DOCUMENT(edge._to).entity_name,
                      at: CONCAT(edge.source_file, ':', edge.source_line)}}"""


def search_entities(db, question: str, k: int = 10, vector=None) -> list[dict]:
    return list(db.aql.execute(ENTITY_SEARCH, bind_vars={"q": vector or embed_query(question), "k": k}))


def search_chunks(db, question: str, k: int = 4, vector=None) -> list[dict]:
    return list(db.aql.execute(CHUNK_SEARCH, bind_vars={"q": vector or embed_query(question), "k": k}))


def search_communities(db, question: str, k: int = 3, vector=None) -> list[dict]:
    return list(db.aql.execute(COMMUNITY_SEARCH, bind_vars={"q": vector or embed_query(question), "k": k}))


def search_relations(db, question: str, k: int = 8, relation: str | None = None,
                     vector=None) -> list[dict]:
    return list(db.aql.execute(RELATION_SEARCH, bind_vars={
        "q": vector or embed_query(question), "k": k, "relation": relation}))


def graphrag(db, question: str, entities: int = 10, chunks: int = 3,
             communities: int = 2, expand: bool = True) -> Answer:
    """Retrieve over the projection, then answer from what came back.

    Four sources, because the schema offers four and a question rarely needs the
    same one twice: entity descriptions for "what is X", chunks for the authored
    source text, community reports for questions about the model as a whole, and
    the typed edges for how things connect.
    """
    vector = embed_query(question)
    hits = search_entities(db, question, entities, vector)
    parts: list[str] = []

    if communities:
        for c in search_communities(db, question, communities, vector):
            parts.append(f"[community: {c['title']}, {c['members']} members]\n{c['report']}")
    for h in hits:
        parts.append(f"[entity: {h['name']} ({h['type']}) at {h['at']}]\n{h['description']}")
    if expand and hits:
        edges = list(db.aql.execute(NEIGHBOURS, bind_vars={"names": [h["name"] for h in hits]}))
        if edges:
            lines = [f"{e['from']} --{e['relation']}--> {e['to']}  ({e['at']})" for e in edges[:120]]
            parts.append("[typed relations touching those elements]\n" + "\n".join(lines))
    for c in search_chunks(db, question, chunks, vector):
        parts.append(f"[source text {c['at']}]\n{c['content']}")

    context = "\n\n".join(parts)
    client = OpenAI(api_key=config.openai_key())
    resp = client.chat.completions.create(
        model=config.CHAT_MODEL, temperature=0,
        messages=[{"role": "system", "content": ANSWER_RULES},
                  {"role": "user", "content": f"CONTEXT\n-------\n{context}\n\nQUESTION\n--------\n{question}"}])
    return Answer(question, resp.choices[0].message.content.strip(), context=context,
                  rows=[{"entity": h["name"], "score": round(h["score"], 3)} for h in hits])


# ----------------------------------------------------------------------- main


_SHARED: Aqlizer | None = None


def instance() -> Aqlizer:
    global _SHARED
    if _SHARED is None:
        _SHARED = Aqlizer()
    return _SHARED


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("question")
    ap.add_argument("--graphrag", action="store_true", help="retrieval path instead of AQLizer")
    ap.add_argument("--stock", action="store_true", help="AQLizer with no aql_examples")
    args = ap.parse_args()
    if args.graphrag:
        graphrag(config.db(), args.question).show()
    else:
        instance().ask(args.question, primed=not args.stock).show()


if __name__ == "__main__":
    main()
