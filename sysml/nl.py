"""Two ways to ask the graph a question in English. Both are stock read paths.

`aqlizer(question)` -- Arango's natural-language-to-AQL service, the `txt2aql`
package from natural-language-service, imported unmodified. It inspects the
collections, writes AQL, runs it and explains the rows. The only thing this project
gives it is `aql_examples`, read from `aql_examples.md` -- the argument
`ReadOnlyArangoGraphQAChain.from_llm` has always accepted and the deployed service
never passes. There are no hand-written query functions here: if an answer is wrong
the fix goes in that file, not into Python. `instance(path)` primes it with a
different file instead, which is how the generated one gets compared against it.

`graphrag(question)` -- the GraphRAG retriever service from graphrag_retrievers,
run in-process against the local database. `local` is hybrid vector + BM25 search
over the entities fused with reciprocal rank, then expanded over the relations;
`global` answers from the community reports; `unified` searches the source text and
the entity graph in parallel. All three come back with citations that resolve to a
source file. This is the path for descriptive and whole-model
questions; AQLizer handles analytical ones.

Every AQLizer answer carries the AQL that produced it, because a generated query
that is subtly wrong returns no rows, and a fluent sentence over no rows is
indistinguishable from a correct answer about something genuinely absent.

    python -m sysml.nl "which requirements does nothing satisfy?"
    python -m sysml.nl --graphrag "what does the drone battery do?"
    python -m sysml.nl --graphrag --scope global "what does this model cover?"
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arango import ArangoClient

from . import config

SERVICE_REPO = config.SERVICE_REPO
RETRIEVER_REPO = config.RETRIEVER_REPO
OPENAI_URL = "https://api.openai.com/v1"

# The service's read-only check (WRITE_OPERATIONS) does not include TRUNCATE, so a
# generated `FOR c IN [...] TRUNCATE c` passes it. Nothing reaches the database
# without clearing this first. It lives in `config` because the generated-primer
# step runs model-written AQL too, and one gate is easier to trust than two.
MUTATION = config.MUTATION

# Passed to the retriever as `response_instructions`, which is its supported way to
# shape an answer.
ANSWER_RULES = """You are answering questions about one specific set of SysML v2
models, from the retrieved context and nothing else. The context is the model. Your
own knowledge of Apollo, spacecraft or drones is not evidence and must not appear in
the answer, even when it agrees with the context and even when it would fill an
obvious gap.

- Answer about *these* models, not about the subject in general. Name the elements
  the context names, using the names the context gives them, and say which source
  file each came from where the context shows one. An answer that would read the
  same against any other drone or spacecraft model has not used the context.
- Synthesising across the context is expected. Grouping, comparing and summarising
  what is there -- including the community summaries -- is answering, not guessing.
  What is forbidden is a fact the context does not contain.
- If the context genuinely lacks what was asked, say "the model does not say".
  Never fall back on general knowledge to cover it.
- A number must appear in the context, quoted with its unit as the context gives
  it. Do not supply a real-world figure for something the context leaves
  unspecified, and do not compute a total from figures the context does not state.
- If the context shows two facts that contradict each other, report the conflict
  rather than choosing one."""


token = config.token


def embed_query(text: str) -> list[float]:
    """A query vector, made the way the stored ones were.

    The extraction half embeds with `openai_embedding`, which calls
    text-embedding-3-small with no `dimensions` argument. A query embedded any
    other way is not in the same space as the rows it is compared against.
    """
    from openai import OpenAI

    return OpenAI(api_key=config.openai_key()).embeddings.create(
        model=config.EMBED_MODEL, input=[text]).data[0].embedding


def logs_to_stderr(*names: str) -> None:
    """Move a service's logging off stdout.

    Both services attach a stdout handler at import time, which corrupts anything
    reading stdout. The logs are worth keeping -- the retriever narrates what it
    searched and why -- so they move rather than go away. The stream is reopened as
    UTF-8 because the retriever logs arrows, and those raise on a cp1252 console.
    A notebook's stderr has no file descriptor behind it, and there it is already
    unicode-safe, so it is used as-is.
    """
    try:
        stream: Any = open(sys.stderr.fileno(), "w", encoding="utf-8", closefd=False)
    except (AttributeError, OSError, io.UnsupportedOperation):
        stream = sys.stderr
    for name in names:
        log = logging.getLogger(name)
        log.handlers = [logging.StreamHandler(stream)]
        log.propagate = False


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
    retrieved: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None

    def evidence(self, chars: int = 700, find: str | None = None) -> None:
        """Print a window onto what was retrieved, so the answer can be checked.

        An answer alone cannot be told apart from something the model already knew.
        Printing the context beside it makes the difference visible: the facts in
        the answer are in the text below it, or they were invented. `find` moves the
        window to the first mention of something, which is how a specific number in
        an answer gets traced back to the text it came from.
        """
        if not self.context:
            print("evidence  (none retrieved)")
            return
        body = re.sub(r"\n{2,}", "\n", self.context.strip())
        start = 0
        if find:
            hit = body.find(find)
            if hit < 0:
                print(f"evidence  ({find!r} does not appear in the retrieved context)")
                return
            # Back up to a line start, so the window does not open mid-word.
            start = body.rfind("\n", 0, max(0, hit - chars // 4)) + 1
        window = body[start:start + chars]
        where = f"at char {start:,}" if start else "from the start"
        print(f"evidence  ({len(window):,} chars {where}, of {len(self.context):,} retrieved)")
        for line in window.splitlines():
            print(f"   {line}")
        if start + chars < len(body):
            print("   ...")

    def show(self, row_limit: int = 6) -> None:
        print(f"Q  {self.question}")
        if self.aql:
            print("\nAQL")
            for line in self.aql.strip().splitlines():
                print(f"   {line}")
        if self.error:
            print(f"\n!! {self.error}")
        # What the answer was built from. On the retrieval paths this is a summary
        # of the search, and `rows` is the citation map -- which is a subset of
        # what was read, not the whole of it. Printing only the citations made a
        # retrieval that read thirty documents look like it had found three.
        if self.retrieved:
            print(f"\nretrieved  {self.retrieved}")
        if self.rows:
            label = "cited" if self.retrieved else "rows"
            print(f"\n{label} ({len(self.rows)}, first {min(row_limit, len(self.rows))})")
            for row in self.rows[:row_limit]:
                print("   " + json.dumps(row, default=str)[:300])
        elif not self.error and not self.retrieved:
            print("\nrows (0)")
        print(f"\nA  {self.answer}\n")


# ------------------------------------------------------------------- AQLizer


_GRAPH = None


class Aqlizer:
    """The shipped Txt2AqlService, pointed at this database and given examples.

    `examples_path` is the one thing this project adds to the service, and it
    defaults to the hand-written `aql_examples.md`. Pass another file to ask the
    same questions primed with a different one -- `pipeline.examples` writes one
    from the graph itself, and comparing the two is what `bespoke-aql-examples.ipynb`
    does.
    """

    def __init__(self, examples_path: Path | None = None):
        self.examples_path = examples_path or config.AQL_EXAMPLES
        self._service = None
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
            from txt2aql.read_only_chain import ReadOnlyArangoGraphQAChain
        except ImportError as exc:
            raise RuntimeError(
                f"cannot import txt2aql ({exc}). Clone natural-language-service next "
                "to this project and install its deps into this interpreter."
            ) from exc

        # The read-only gate looks for its keywords with `if op in query.upper()`,
        # a substring test, so a perfectly ordinary read is refused for containing
        # the letters of a write. "What was the state at lunar orbit insertion"
        # produces AQL mentioning INSERTION and is rejected as an INSERT. On a
        # systems-engineering corpus that is not an edge case -- orbit insertion,
        # part replacement, requirement updates. Matching on word boundaries keeps
        # the gate and drops the false positives, and MUTATION below is still the
        # one that actually decides whether a query runs.
        def read_only(_self, aql_query: str):
            found = MUTATION.search(aql_query or "")
            return (False, found.group(0).upper()) if found else (True, None)

        ReadOnlyArangoGraphQAChain._is_read_only_query = read_only
        logs_to_stderr("txt2aql")

    @property
    def examples(self) -> str:
        return self.examples_path.read_text(encoding="utf-8")

    @property
    def service(self):
        if self._service is None:
            from txt2aql.service import Txt2AqlService
            self._service = Txt2AqlService()
        return self._service

    def graph(self):
        """The LangChain ArangoGraph the service builds its schema picture from.

        Module-level rather than per-instance: two AQLizers differ only in which
        examples file they were given, and the schema read is the slow part of
        building either one.
        """
        global _GRAPH
        if _GRAPH is None:
            from langchain_arangodb import ArangoGraph
            db = self.service.get_db_client().db(name=config.DB_NAME, user_token=token())
            _GRAPH = ArangoGraph(db=db, generate_schema_on_init=True,
                                 schema_sample_ratio=0, schema_graph_name=None,
                                 schema_include_examples=True, schema_list_limit=32,
                                 schema_string_limit=256)
        return _GRAPH

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
                "If a row carries `files` or `models`, name them in brackets next "
                "to the fact they support.\n"
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


class Retriever:
    """graphrag_retrievers' RetrievalService, pointed at the local database.

    `local` is entity-centred: hybrid vector + BM25 search over the entities,
    fused with reciprocal rank, then expanded over the relations touching what it
    found. `global` answers from the community reports, which is what a question
    about the model as a whole needs. `unified` searches the chunks of source text
    and the entity graph in parallel and answers from both -- `local` reaches a
    chunk only through an entity that matched first, so a fact written in a `doc`
    comment that no element name resembles is out of its reach.

    The service normally runs as a pod beside the platform, so three of the things
    it reaches for at startup are supplied here instead: a JWT, a service-status
    sink, and a token validator. Everything below those is the service's own code.
    """

    def __init__(self, level: int = 1):
        self.level = level
        self._service = None
        self._bootstrap()

    def _bootstrap(self) -> None:
        if str(RETRIEVER_REPO) not in sys.path:
            sys.path.insert(0, str(RETRIEVER_REPO))
        # Collection names are derived from the project name, and the retriever
        # reads its own database name from the environment.
        os.environ.setdefault("DB_NAME", config.DB_NAME)
        os.environ.setdefault("db_name", config.DB_NAME)
        # `unified` is the one scope that does not receive the key through the
        # service. It asks the retriever object for a `chat_api_key` attribute,
        # UnifiedRetriever keeps its constructor arguments in a kwargs dict rather
        # than as attributes, so the lookup misses and falls through to the
        # environment. Without this the retrieval succeeds and only the answer
        # fails, as "response generation failed".
        os.environ["CHAT_API_KEY"] = config.openai_key()
        try:
            import retrievers.global_retriever_ng.global_retriever_ng as global_ng
            import retrievers.local_retriever.local_retriever as local
            import retrievers.service as service
            import retrievers.unified_retriever.unified_retriever as unified
            import retrievers.utils.auth as auth
            import retrievers.utils.metadata_helpers as metadata
            import retrievers.utils.retriever_config as config_module
        except ImportError as exc:
            raise RuntimeError(
                f"cannot import retrievers ({exc}). Clone graphrag_retrievers next "
                "to this project and install its deps into this interpreter."
            ) from exc

        async def open_database(arangodb_url: str, db_name: str, **_):
            # ArangoDB issues an acceptable JWT itself, which is what the retriever
            # wants: it connects with auth_method="jwt".
            client = ArangoClient(hosts=arangodb_url)
            return token(), client, client.db(name=db_name, auth_method="jwt",
                                              user_token=token())

        async def no_status(*_args, **_kwargs) -> None:
            """Progress goes to the GenAI metadata store; nothing here watches it."""

        async def token_valid(_token):
            """Every token here was minted a moment earlier, so it is valid."""
            return True, config.ARANGO_USER

        auth._initialize_database_connection = open_database
        auth.is_valid_token = token_valid
        metadata.update_service_status = no_status

        # `global` drops any community whose report embedding is less similar to
        # the question than this, to skip the map-reduce on an off-topic query.
        # The default of 0.45 is unreachable here: the reports the extraction step
        # writes are a page of markdown each, and the cosine between a page and a
        # one-line question does not get that high. Measured on this graph, an
        # on-topic question tops out at 0.33-0.38 and an off-topic one
        # ("a recipe for sourdough bread") at 0.05-0.07, so 0.25 sits in the gap
        # and still throws out what the threshold exists to throw out. Left at
        # 0.45 every global question answers "No relevant information found."
        config_module.CONFIG.global_.global_min_community_similarity = 0.25
        # Each retriever imports these by name, so the shim has to be planted in
        # every module that will call one -- `unified` mid-query, when it renews.
        for module in (local, global_ng, unified, service):
            for name, shim in (("update_service_status", no_status),
                               ("is_valid_token", token_valid)):
                if hasattr(module, name):
                    setattr(module, name, shim)
        self._module = service
        logs_to_stderr("graphrag", "retrievers")

    @property
    def service(self):
        if self._service is None:
            self._service = self._module.RetrievalService(
                arangodb_url=config.ARANGO_URL, db_name=config.DB_NAME,
                level=self.level, chat_api_provider="openai",
                embedding_api_provider="openai",
                chat_api_url=OPENAI_URL, embedding_api_url=OPENAI_URL,
                chat_api_key=config.openai_key(), embedding_api_key=config.openai_key(),
                chat_model=config.CHAT_MODEL, embedding_model=config.EMBED_MODEL,
                embedding_dim=config.EMBED_DIM, username=config.ARANGO_USER)
        return self._service

    async def ask_async(self, question: str, scope: str = "local") -> Answer:
        kinds = {"local": self._module.QueryType.LOCAL,
                 "global": self._module.QueryType.GLOBAL,
                 "unified": self._module.QueryType.UNIFIED}
        if scope not in kinds:
            raise ValueError(f"scope must be one of {sorted(kinds)}, not {scope!r}")
        try:
            result = await self.service.process_query(
                query=question, query_type=kinds[scope], validated_token=token(),
                response_instructions=ANSWER_RULES, use_cache=False,
                # Straight to the retriever asked for. The planner would run a
                # global pass first to decide the route, and partition selection
                # reads the AutoGraph corpus collections, which a graph built
                # from a single module does not have.
                use_llm_planner=False, auto_select_partitions=False)
        except Exception as exc:
            return Answer(question, "", error=f"{type(exc).__name__}: {exc}")
        if not isinstance(result, dict):
            return Answer(question, str(result))
        if result.get("status") == "error":
            return Answer(question, "", error=str(result.get("error")))
        # `local` and `global` answer under `result` with everything else nested in
        # `metadata`; `unified` answers under `llm_response` and keeps its citations
        # at the top level.
        meta = result.get("metadata") or {}
        answer = result.get("result") or result.get("llm_response") or ""
        citations = meta.get("citation_mapping") or result.get("citation_mapping") or {}
        # A [CITE:n] in the answer indexes this map, and each entry names the file
        # the text came from -- so the citations resolve to real sources.
        rows = [{"cite": key, "source": value.get("citable_url")}
                for key, value in sorted(citations.items(), key=lambda kv: str(kv[0]))]
        context = meta.get("formatted_context") or result.get("formatted_context") or ""
        if not context:
            # Only `local` reports a context blob. `global`'s evidence is the points
            # its analyst pass distilled from the community reports; `unified` keeps
            # the text of what it read in the citation map. Both are the retrieved
            # text, just filed elsewhere.
            context = "\n".join(f"- {p.get('answer', '')}"
                                for p in meta.get("final_support_points") or [])
        if not context:
            context = "\n".join(str(v.get("content") or "").strip()
                                for _, v in sorted(citations.items(), key=lambda kv: str(kv[0])))
        return Answer(question, str(answer).strip(), rows=rows, context=str(context),
                      retrieved=self._summarise(result, meta, str(context)))

    @staticmethod
    def _summarise(result: dict, meta: dict, context: str) -> str:
        """One line naming what the search actually read.

        Each scope reports itself differently: `global` counts the community reports
        it summarised and the points it distilled from them, the other two count the
        documents and edges they walked. Without this the only visible number is the
        citation count, and a citation is a source the answer happens to quote --
        not a measure of what was searched.
        """
        graph = meta.get("graph_metadata") or result.get("graph_metadata") or {}
        docs = len(graph.get("documents") or [])
        edges = len(graph.get("edges") or [])
        points = len(meta.get("final_support_points") or [])
        if points:
            return f"{docs} community reports -> {points} points"
        parts = [f"{docs} documents"]
        if edges:
            parts.append(f"{edges} edges")
        if context:
            parts.append(f"{len(context):,} chars of context")
        return ", ".join(parts)

    def ask(self, question: str, scope: str = "local") -> Answer:
        return asyncio.run(self.ask_async(question, scope))


# The one retrieval this adds: the edge collection has no ANN index -- ArangoDB
# requires the vector field on every document and only RELATED_TO edges carry one.
# Exact cosine over 5k edges is fast, and unlike an index scan it can be filtered
# by relationship_type, so "the nearest `satisfies` edges to this phrase" is a
# query rather than a post-filter.
RELATION_SEARCH = f"""
FOR r IN {config.RELATIONS}
  FILTER r.type == 'RELATED_TO' AND IS_LIST(r.{config.EMBEDDING_FIELD})
  FILTER @relation == null OR r.relationship_type == @relation
  LET score = COSINE_SIMILARITY(r.{config.EMBEDDING_FIELD}, @q)
  SORT score DESC
  LIMIT @k
  RETURN {{description: r.description, relation: r.relationship_type,
           at: DOCUMENT(r._from).files, score}}"""


def search_relations(db, question: str, k: int = 8, relation: str | None = None,
                     vector=None) -> list[dict]:
    return list(db.aql.execute(RELATION_SEARCH, bind_vars={
        "q": vector or embed_query(question), "k": k, "relation": relation}))


# ----------------------------------------------------------------------- main


# Both are expensive to build -- schema generation for one, index checks and a
# database connection for the other -- and cheap to keep, so each is built once.
# The AQLizers are kept per examples file, because comparing two of them means
# holding both at once and the schema is the expensive half either way.
_AQLIZERS: dict[Path | None, Aqlizer] = {}
_RETRIEVER: Retriever | None = None


def instance(examples_path: Path | str | None = None) -> Aqlizer:
    """The AQLizer, primed with `sysml/aql_examples.md` unless another file is named.

    `pipeline.examples` writes a second examples file from the graph itself, and
    `nl.instance(config.AQL_EXAMPLES_GENERATED)` is how that one gets asked the same
    questions. The default is the hand-written file and does not change.
    """
    path = Path(examples_path) if examples_path is not None else None
    if path not in _AQLIZERS:
        _AQLIZERS[path] = Aqlizer(path)
    return _AQLIZERS[path]


def retriever() -> Retriever:
    global _RETRIEVER
    if _RETRIEVER is None:
        _RETRIEVER = Retriever()
    return _RETRIEVER


def graphrag(question: str, scope: str = "local") -> Answer:
    return retriever().ask(question, scope)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("question")
    ap.add_argument("--graphrag", action="store_true", help="retrieval path instead of AQLizer")
    ap.add_argument("--scope", default="local", choices=("local", "global", "unified"),
                    help="which retriever answers (--graphrag only)")
    ap.add_argument("--stock", action="store_true", help="AQLizer with no aql_examples")
    ap.add_argument("--examples", type=Path, default=None,
                    help=f"an examples file other than {config.AQL_EXAMPLES.name}")
    args = ap.parse_args()
    if args.graphrag:
        graphrag(args.question, args.scope).show()
    else:
        instance(args.examples).ask(args.question, primed=not args.stock).show()


if __name__ == "__main__":
    main()
