"""Record traces of the group chat simulator for CryptWalker's replay
(``lake exe CryptWalker.GroupChat.replay``): every scenario under
``tests/data/groupchat``, a fixed set of the stateful test's random runs, and
a group chat driven through the app's own adapter (``acks``), as
``adapter-scenario.jsonl``.

    python tools/groupchat_traces.py OUT_DIR [EXAMPLES]

EXAMPLES is the number of random runs of each stateful machine, 50 by
default; 0 records the scenarios alone, as CryptWalker keeps them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    out = Path(sys.argv[1])
    examples = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.jsonl"):
        old.unlink()
    os.environ["GROUPCHAT_TRACE_DIR"] = str(out)
    sys.path.insert(0, str(ROOT / "tests"))

    from hypothesis import settings
    from hypothesis.stateful import run_state_machine_as_test

    from katzenqt.groupchat import sim
    from test_groupchat_stateful import GroupChat, PassiveGroupChat

    for path in sorted((ROOT / "tests/data/groupchat").glob("*.txt")):
        with (out / f"scenario-{path.stem}.jsonl").open("w") as trace:
            sim.run(path.read_text(), trace)
    for machine in (GroupChat, PassiveGroupChat) if examples else ():
        run_state_machine_as_test(
            machine,
            settings=settings(
                max_examples=examples, stateful_step_count=40, deadline=None,
                database=None, derandomize=True,
            ),
        )
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-n0", "--no-cov", "-p", "no:cacheprovider",
         "tests/test_groupchat_adapter_trace.py::test_every_call_the_app_makes_is_recorded"],
        cwd=ROOT, env={**os.environ, "GROUPCHAT_ADAPTER_TRACE_OUT": str(out)}, check=True,
    )
    print(f"{len(list(out.glob('*.jsonl')))} traces in {out}")


if __name__ == "__main__":
    main()
