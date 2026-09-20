import ast
import asyncio
import io
import inspect
import textwrap
import uuid

import pytest

from katzenqt import katzen, network

pytestmark = pytest.mark.asyncio


async def test_pause_upload_cancels_the_in_flight_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agg = uuid.uuid4()
    started = asyncio.Event()

    async def in_flight() -> None:
        started.set()
        await asyncio.Event().wait()

    class Sess:
        async def __aenter__(self) -> "Sess":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, model: object, key: object) -> None:
            return None

        async def exec(self, query: object) -> "Sess":
            return self

        def all(self) -> list[object]:
            return []

        async def commit(self) -> None:
            return None

    async def stream_for(rcw_id: object) -> object:
        return agg

    monkeypatch.setattr(network.persistent, "asession", Sess)
    monkeypatch.setattr(network, "_upload_stream_for_rcw", stream_for)
    task = asyncio.create_task(in_flight())
    await started.wait()
    network._inflight_writes[agg] = task
    try:
        await network.pause_upload(rcw_id=agg)
        assert task.cancelled() or task.done()
    finally:
        task.cancel()
        network._inflight_writes.pop(agg, None)


def test_the_write_dispatch_registers_the_task_for_cancellation() -> None:
    src = inspect.getsource(network.drain_mixwal2)
    assert "_inflight_writes[" in src, (
        "pause_upload and cancel_upload can only cancel a write that the "
        "drain loop registered in _inflight_writes"
    )


def _menu_node() -> ast.AST:
    """The real method body. The attribute is decorated, so inspect.getsource
    returns the wrapper rather than the code under test."""
    path = inspect.getsourcefile(katzen)
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if (isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
                and node.name == "transfers_context_menu"):
            return node
    raise AssertionError("transfers_context_menu not found")


def test_the_transfers_menu_does_not_await_a_sync_session() -> None:
    for node in ast.walk(_menu_node()):
        if isinstance(node, ast.With):
            for child in ast.walk(node):
                if isinstance(child, ast.Await):
                    fn = getattr(child.value, "func", None)
                    name = getattr(fn, "attr", "")
                    assert name not in ("get", "exec"), (
                        "sync Session.%s is not awaitable" % name
                    )


def test_the_failed_row_branch_is_not_duplicated() -> None:
    removes = [
        n for n in ast.walk(_menu_node())
        if isinstance(n, ast.Constant) and n.value == "Remove"
    ]
    assert len(removes) == 1
