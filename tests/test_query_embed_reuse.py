"""One embedding per request, computed off the event loop (backlog_c5dba6d13707).

MEASURED PROBLEM (learning_faca6ab430b48cbc, 2026-08-11): the query text was
embedded once PER COLLECTION — project + shared patterns + shared context — by
a CLIENT-SIDE ONNX model at ~96-138ms a call, ON the asyncio event loop. One
request therefore burned ~300-400ms of duplicate CPU and blocked every other
agent's request while it ran. memory_query measured 1-2ms at rest and 167ms
median under 6 concurrent /recall clients.

Pins:
- the text is embedded ONCE even when three collections are scanned;
- the vector actually reaches chroma as `query_embeddings`, not `query_texts`;
- the embed happens off the event loop;
- FAIL-OPEN: if embedding breaks for any reason the query still runs via
  `query_texts` — a perf optimisation must never cost a result.
"""

import asyncio

import pytest

from shared_memory.helpers import embed_query_once, query_kwargs


class FakeEF:
    """Stands in for chroma's DefaultEmbeddingFunction."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.thread_names = []

    def __call__(self, texts):
        import threading
        self.thread_names.append(threading.current_thread().name)
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("model exploded")
        return [[0.5, 0.25, 0.125]]


class FakeCollection:
    def __init__(self, name, ef):
        self.name = name
        self._embedding_function = ef
        self.query_calls = []

    async def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}


@pytest.mark.asyncio
async def test_embeds_once_and_returns_a_plain_list():
    ef = FakeEF()
    col = FakeCollection("proj_nimbus", ef)
    vec = await embed_query_once(col, "stripe billing webhook")
    assert vec == [0.5, 0.25, 0.125]
    assert ef.calls == [["stripe billing webhook"]], "exactly one embedding call"


@pytest.mark.asyncio
async def test_embed_runs_off_the_event_loop():
    """The whole point of part 2: ONNX inference is a blocking C call, so on the
    loop it stalls every other agent's request."""
    ef = FakeEF()
    col = FakeCollection("proj_nimbus", ef)
    loop_thread = asyncio.current_task().get_coro().cr_frame  # noqa: F841 - clarity only
    import threading
    main_name = threading.current_thread().name
    await embed_query_once(col, "text")
    assert ef.thread_names[0] != main_name, "embedding must not run on the loop thread"


@pytest.mark.asyncio
async def test_one_vector_serves_three_collections():
    """The measured waste: three collections, three identical embeds."""
    ef = FakeEF()
    cols = [FakeCollection(n, ef) for n in ("proj_nimbus", "shared_patterns", "shared_context")]
    vec = await embed_query_once(cols[0], "one text")
    for c in cols:
        await c.query(**query_kwargs("one text", vec), n_results=5, where=None)
    assert len(ef.calls) == 1, "must embed once for all three scans"
    for c in cols:
        assert c.query_calls[0]["query_embeddings"] == [vec]
        assert "query_texts" not in c.query_calls[0]


class TestFailOpen:
    @pytest.mark.asyncio
    async def test_embedding_error_degrades_to_query_texts(self):
        ef = FakeEF(fail=True)
        col = FakeCollection("proj_nimbus", ef)
        assert await embed_query_once(col, "text") is None

    @pytest.mark.asyncio
    async def test_missing_embedding_function_degrades(self):
        """`_embedding_function` is private chroma API — it may vanish on upgrade."""
        col = FakeCollection("proj_nimbus", None)
        assert await embed_query_once(col, "text") is None

    def test_none_vector_falls_back_to_the_previous_behaviour(self):
        kw = query_kwargs("some text", None)
        assert kw == {"query_texts": ["some text"]}

    def test_vector_is_used_when_present(self):
        kw = query_kwargs("some text", [0.1, 0.2])
        assert kw == {"query_embeddings": [[0.1, 0.2]]}


@pytest.mark.asyncio
async def test_numpy_style_vectors_are_converted():
    """Chroma's real EF returns numpy arrays; the JSON path needs plain lists."""

    class NpLike:
        def tolist(self):
            return [1.0, 2.0]

    class NpEF:
        def __call__(self, texts):
            return [NpLike()]

    col = FakeCollection("proj_nimbus", NpEF())
    assert await embed_query_once(col, "t") == [1.0, 2.0]
