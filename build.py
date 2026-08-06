"""Run the whole pipeline: parse -> project -> enrich -> analogy.

    python build.py            # everything
    python build.py --reset    # drop the database first

Needs a local ArangoDB with the experimental vector index enabled:

    docker run -d --name christian-webb-drone-arango -p 8529:8529 \
      -e ARANGO_ROOT_PASSWORD=testpass arangodb:3.12.9.4 \
      arangod --experimental-vector-index=true

and CHAT_API_KEY set to an OpenAI key. Embeddings are cached in out/, so a re-run
after the first costs nothing.
"""

from __future__ import annotations

import argparse
import time

from sysml import config
from sysml.pipeline import analogy, enrich, parse, project


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true", help="drop the database first")
    args = ap.parse_args()

    if args.reset and config.drop_database():
        print(f"dropped {config.DB_NAME}")

    started = time.time()
    print("\n== parse ==")
    parse.main()

    print("\n== project ==")
    project.main()
    
    print("\n== enrich ==")
    enrich.main()

    print("\n== analogy ==")
    analogy.main()
    print(f"\ndone in {time.time() - started:.0f}s -- database {config.DB_NAME}")


if __name__ == "__main__":
    main()
