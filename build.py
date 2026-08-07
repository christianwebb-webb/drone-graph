"""Run the whole pipeline: extract -> load (+ structure) -> analogy -> examples.

    python build.py            # everything
    python build.py --reset    # drop the database first
    python build.py --no-examples

Needs a local ArangoDB with the experimental vector index enabled:

    docker run -d --name christian-webb-drone-arango -p 8529:8529 \
      -e ARANGO_ROOT_PASSWORD=testpass arangodb:3.12.9.4 \
      arangod --experimental-vector-index=true

and an OpenAI key as CHAT_API_KEY in the `env` file one directory up (an exported
CHAT_API_KEY / OPENAI_API_KEY also works). Extraction caches every LLM answer in
out/kg, so a re-run over unchanged sources costs nothing.

The graph is built from the sources twice, deliberately. `extract` asks an LLM
what the text means; `structure`, inside `load`, reads the same files with a lexer
for what the syntax states outright -- attribute values, containment and typing.
The second pass is what makes a question like "sum the dry mass of the Saturn V"
answerable, because that needs exact numbers on an exact tree.

The last step writes `sysml/aql_examples_generated.md`, the AQLizer primer, from
the finished graph. It is a second file: the read side still takes the hand-written
`sysml/aql_examples.md` unless asked for the other one, so the step can be added,
skipped or rerun without changing any existing answer.
"""

from __future__ import annotations

import argparse
import time

from sysml import config
from sysml.pipeline import analogy, examples, extract, load


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true", help="drop the database first")
    ap.add_argument("--no-examples", dest="examples", action="store_false",
                    help="skip writing the generated AQLizer primer")
    args = ap.parse_args()

    if args.reset and config.drop_database():
        print(f"dropped {config.DB_NAME}")

    started = time.time()
    print("\n== extract ==")
    extract.main()

    print("\n== load ==")
    load.main()

    print("\n== analogy ==")
    analogy.main()

    # Last, and reading the database rather than the sources: the primer describes
    # the graph, so every earlier step has to have finished writing it -- the
    # analogy edges included, or the file it writes will not know they exist.
    if args.examples:
        print("\n== examples ==")
        examples.main()

    print(f"\ndone in {time.time() - started:.0f}s -- database {config.DB_NAME}")


if __name__ == "__main__":
    main()
