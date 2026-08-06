"""Run the whole pipeline: extract -> load -> analogy.

    python build.py            # everything
    python build.py --reset    # drop the database first

Needs a local ArangoDB with the experimental vector index enabled:

    docker run -d --name christian-webb-drone-arango -p 8529:8529 \
      -e ARANGO_ROOT_PASSWORD=testpass arangodb:3.12.9.4 \
      arangod --experimental-vector-index=true

and CHAT_API_KEY set to an OpenAI key. Extraction caches every LLM answer in
out/kg, so a re-run over unchanged sources costs nothing.
"""

from __future__ import annotations

import argparse
import time

from sysml import config
from sysml.pipeline import analogy, extract, load


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true", help="drop the database first")
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
    print(f"\ndone in {time.time() - started:.0f}s -- database {config.DB_NAME}")


if __name__ == "__main__":
    main()
