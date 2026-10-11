"""The traces CryptWalker's replay checks the group state machine against:
one JSON line per call made on a member, with its whole state after."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from katzenqt.groupchat import sim, state
from katzenqt.groupchat.trace import Recorder, TracedGroup

SCENARIOS = Path(__file__).parent / "data" / "groupchat"


def lines(script: str) -> list[dict]:
    trace = io.StringIO()
    sim.run(script, trace)
    return [json.loads(line) for line in trace.getvalue().splitlines()]


def test_every_call_on_a_member_is_one_line() -> None:
    calls = lines((SCENARIOS / "passive.txt").read_text())
    assert {c["call"] for c in calls} >= {"found", "observe", "compose", "written"}
    first = calls[0]
    assert first["call"] == "found" and first["member"] == "alice"
    assert first["state"]["read_cap"] == first["args"]["read_cap"]


def test_introduced_at_is_written_as_a_pair() -> None:
    calls = lines((SCENARIOS / "double_introduction.txt").read_text())
    written = [e["message"] for c in calls for e in c["result"] or [] if e["type"] == "WriteBox"]
    assert any(m["introduced_at"] for m in written)
    at = next(m["introduced_at"] for m in written if m["introduced_at"])
    assert isinstance(at[0], int) and len(bytes.fromhex(at[1])) == 104


def test_messages_are_written_decoded() -> None:
    calls = lines("new-group alice bob\nsend alice hi\nread bob alice\n")
    writes = [e for c in calls for e in c["result"] or [] if e["type"] == "WriteBox"]
    assert writes[0]["message"]["content"] == b"hi".hex()
    reads = [c["args"]["event"] for c in calls if c["call"] == "handle"]
    assert {"data": writes[0]["message"]} in [e.get("result") for e in reads]


def test_a_member_restored_from_its_bytes_is_still_traced() -> None:
    out = io.StringIO()
    founders = [bytes(136)]
    group = TracedGroup.found(founders[0], founders, tracer=Recorder(out))
    restored = TracedGroup.of(state.load(state.dump(group)), group.tracer)
    assert restored == group
    restored.cancel_induction()
    restored.remove(bytes(32))
    restored.observe(bytes(32), bytes(104), b"\xff", bytes(104))
    calls = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [c["call"] for c in calls[-3:]] == ["cancel_induction", "remove", "observe"]
    assert calls[-1]["args"]["message"] is None, "an undecodable box is written as null"


@pytest.mark.nightly
@pytest.mark.skipif(not os.environ.get("CRYPTWALKER"), reason="needs a CryptWalker checkout")
def test_the_lean_member_agrees(tmp_path: Path) -> None:
    """Fresh random runs, replayed through CryptWalker's Lean model of a
    member, which must give the same results and state at every call."""
    root = Path(os.environ["CRYPTWALKER"])
    tool = Path(__file__).parent.parent / "tools" / "groupchat_traces.py"
    subprocess.run([sys.executable, str(tool), str(tmp_path), "200"], check=True)
    lake = shutil.which("lake") or str(Path.home() / ".elan" / "bin" / "lake")
    replay = subprocess.run(
        [lake, "exe", "CryptWalker.GroupChat.replay", str(tmp_path)],
        cwd=root, capture_output=True, text=True,
    )
    assert replay.returncode == 0, replay.stdout[-4000:]
