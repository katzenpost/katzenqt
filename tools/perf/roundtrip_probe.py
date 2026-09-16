"""Time each daemon RPC a headless command makes, and say where the wall went.

Runs a katzenqt headless subcommand with debug logging, stamps every line as
it arrives, and pairs each "<rpc> request sent." with the "<rpc> response
received." that follows it. That separates the two costs the wall clock
hides: work the daemon does locally (encrypt_read, encrypt_write) from the
mixnet round trip (start_resending_encrypted_message), which is where the
time actually goes.

  python3 tools/perf/roundtrip_probe.py --state /tmp/bob --address 127.0.0.1:64331 \
      --json out.json -- read demo 450 "hello"
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import statistics
import subprocess
import sys
import time

_SENT = re.compile(r"INFO thinclient: (\w+) request sent\.")
_RECV = re.compile(r"INFO thinclient: (\w+) response received\.")


def _busy_seconds(intervals: "list[tuple[float, float]]") -> float:
    """Union, not sum: RPCs overlap."""
    busy = 0.0
    end_so_far = None
    for start, end in sorted(intervals):
        if end_so_far is None or start > end_so_far:
            busy += end - start
            end_so_far = end
        elif end > end_so_far:
            busy += end - end_so_far
            end_so_far = end
    return busy


def summarise(intervals: "dict[str, list[tuple[float, float]]]", wall: float) -> dict:
    per_rpc = {}
    everything = []
    for name, spans in sorted(intervals.items()):
        values = [end - start for start, end in spans]
        everything.extend(spans)
        per_rpc[name] = {
            "count": len(values),
            "busy_s": round(_busy_seconds(spans), 2),
            "max_s": round(max(values), 2),
            "median_s": round(statistics.median(values), 2),
        }
    busy = _busy_seconds(everything)
    return {
        "wall_s": round(wall, 2),
        "in_rpc_s": round(busy, 2),
        "idle_s": round(wall - busy, 2),
        "rpcs": per_rpc,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", required=True)
    ap.add_argument("--address", required=True)
    ap.add_argument("--network", default="tcp")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--label", default="")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="headless subcommand after --")
    args = ap.parse_args(argv)
    command = [c for c in args.command if c != "--"]
    if not command:
        ap.error("no headless subcommand given after --")

    env = dict(os.environ, KQT_STATE=args.state, KQT_LOG_LEVEL="DEBUG")
    argv_full = [sys.executable, "-u", "-m", "katzenqt.integration_runner", *command,
                 "--address", args.address, "--network", args.network]

    started = time.monotonic()
    proc = subprocess.Popen(argv_full, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    pending: "dict[str, collections.deque]" = collections.defaultdict(collections.deque)
    intervals: "dict[str, list[tuple[float, float]]]" = {}
    for line in proc.stderr:
        now = time.monotonic()
        if m := _SENT.search(line):
            pending[m.group(1)].append(now)
        elif m := _RECV.search(line):
            queue = pending.get(m.group(1))
            if queue:
                intervals.setdefault(m.group(1), []).append((queue.popleft(), now))
    rc = proc.wait()
    result = summarise(intervals, time.monotonic() - started)
    result["returncode"] = rc
    result["label"] = args.label or " ".join(command[:2])
    if unanswered := sorted(n for n, q in pending.items() if q):
        result["unanswered"] = unanswered

    print(json.dumps(result, indent=2))
    if args.json_path:
        with open(args.json_path, "w") as fh:
            json.dump(result, fh, indent=2)
    return rc


if __name__ == "__main__":
    sys.exit(main())
