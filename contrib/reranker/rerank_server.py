#!/usr/bin/env python3
"""Local cross-encoder rerank service (retrieval experiment, 2026-10-25).

    /usr/bin/python3 contrib/reranker/rerank_server.py [--host 172.17.0.1] [--port 8091] [--model BAAI/bge-reranker-v2-m3]

POST /rerank {"query": str, "documents": [str, ...]} -> {"scores": [float, ...]}
(sigmoid relevance in [0,1], same order as documents). GET /health -> {"ok": true, "model": ...}.

Runs on sage's system python (torch + sentence-transformers, CUDA). Prod binds
the docker0 bridge address (172.17.0.1) so the junto container reaches it as
host.docker.internal while the LAN cannot (interface:rerank-v0). Unit file:
contrib/reranker/junto-reranker.service. Stdlib HTTP server, one request at a time: a GPU cross-encoder
scores ~150 pairs/s, so serialising is fine for an experiment.
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

MODEL = None
NAME = None
MAX_DOC_CHARS = 2000
MAX_QUERY_CHARS = 2500


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "model": NAME})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/rerank":
            return self._send(404, {"error": "not found"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            q = (req.get("query") or "")[-MAX_QUERY_CHARS:]
            docs = [(d or "")[:MAX_DOC_CHARS] for d in req.get("documents") or []]
            scores = [float(s) for s in MODEL.predict([(q, d) for d in docs], batch_size=32)] if docs else []
            self._send(200, {"scores": scores})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})

    def log_message(self, *a):
        pass


def main():
    global MODEL, NAME
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--model", default="BAAI/bge-reranker-v2-m3")
    a = ap.parse_args()
    from sentence_transformers import CrossEncoder
    NAME = a.model
    MODEL = CrossEncoder(a.model, max_length=1024, device="cuda", model_kwargs={"dtype": "float16"})
    print(f"rerank service on {a.host}:{a.port} model={a.model}", flush=True)
    HTTPServer((a.host, a.port), H).serve_forever()


if __name__ == "__main__":
    main()
