#!/usr/bin/env python3
"""Host-side ops for the chunk index (interface:chunk-index-v0).

    .venv/bin/python scripts/chunk_index.py status
    .venv/bin/python scripts/chunk_index.py sync [--source proj_cars]    # build/refresh
    .venv/bin/python scripts/chunk_index.py drop --source proj_cars

Same module the server runs (src/shared_memory/chunk_index.py), pointed at the
host ports: Chroma localhost:8001, Ollama on sage.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
os.environ.setdefault("JUNTO_EMBED_URL", "http://192.168.15.240:11434")

from shared_memory import chunk_index as ci  # noqa: E402

HOST, PORT = "localhost", 8001


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "sync", "drop"])
    ap.add_argument("--source", default=None)
    a = ap.parse_args()
    import chromadb
    chroma = await chromadb.AsyncHttpClient(host=HOST, port=PORT)
    cclient = await ci.get_chunk_client(HOST, PORT)
    if a.cmd == "status":
        v = await ci.ollama_version(force=True)
        print(f"embedder {ci.EMBED_URL} ollama={v}")
        for col in sorted(await cclient.list_collections(), key=lambda c: c.name):
            m = col.metadata or {}
            print(f"  {col.name:40s} chunks={await col.count():6d} current={ci.is_current(m, v)} "
                  f"ollama={m.get('ollama_version')} complete={m.get('complete')}")
    elif a.cmd == "sync":
        v = await ci.ollama_version(force=True)
        if not v:
            sys.exit(f"embedder unreachable at {ci.EMBED_URL}")
        if a.source:
            print(json.dumps(await ci.sync_source(chroma, a.source, v, print)), flush=True)
        else:
            for col in sorted(await chroma.list_collections(), key=lambda c: c.name):
                if ci.is_chunk_source(col.name):
                    print(json.dumps(await ci.sync_source(chroma, col.name, v, print)), flush=True)
    elif a.cmd == "drop":
        await cclient.delete_collection(ci.PREFIX + a.source)
        print("dropped", ci.PREFIX + a.source)


if __name__ == "__main__":
    asyncio.run(main())
