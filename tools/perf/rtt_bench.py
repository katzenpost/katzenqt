"""Interleaved A/B of start_resending_encrypted_message latency.

Arms alternate in small blocks so mixnet drift hits both equally; the
comparison is the paired per-block difference, not two absolute numbers
gathered at different times.

  python3 tools/perf/rtt_bench.py --alice-state /tmp/a --bob-state /tmp/b \
      --a 127.0.0.1:64331 --b 127.0.0.1:64331,127.0.0.1:64332 --pairs 24 --json out.json
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

_SENT = re.compile(r"INFO thinclient: (\w+) request sent\.")
_RECV = re.compile(r"INFO thinclient: (\w+) response received\.")
_RETRANSMIT = re.compile(r"ARQ resend|retransmit", re.I)
_NOTREADY = re.compile(r"box does not exist yet|data not ready", re.I)


def _run(state: Path, address: str, args: "list[str]", repo: Path, python: str) -> dict:
    env = dict(os.environ, KQT_STATE=str(state), KQT_LOG_LEVEL="DEBUG",
               PYTHONPATH=str(repo / "src"))
    argv = [python, "-u", "-m", "katzenqt.integration_runner", *args,
            "--address", address, "--network", "tcp"]
    started = time.monotonic()
    proc = subprocess.Popen(argv, env=env, cwd=str(repo), stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    pending: "collections.deque" = collections.deque()
    durations: "list[float]" = []
    overlapped = retransmits = notready = 0
    for line in proc.stderr:
        now = time.monotonic()
        if (m := _SENT.search(line)) and m.group(1) == "start_resending_encrypted_message":
            if pending:
                overlapped += 1
            pending.append(now)
        elif (m := _RECV.search(line)) and m.group(1) == "start_resending_encrypted_message":
            if pending:
                durations.append(now - pending.popleft())
        if _RETRANSMIT.search(line):
            retransmits += 1
        if _NOTREADY.search(line):
            notready += 1
    rc = proc.wait()
    return {"rc": rc, "wall_s": round(time.monotonic() - started, 2),
            "durations": [round(d, 2) for d in durations],
            "overlapped": overlapped, "retransmits": retransmits,
            "not_ready": notready}


def _bootstrap_ci(diffs: "list[float]", rounds: int, rng: random.Random) -> "list[float]":
    if len(diffs) < 2:
        return []
    means = []
    for _ in range(rounds):
        sample = [rng.choice(diffs) for _ in diffs]
        means.append(statistics.mean(sample))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means)) - 1]
    return [round(lo, 2), round(hi, 2)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--alice-state", required=True)
    ap.add_argument("--bob-state", required=True)
    ap.add_argument("--a", required=True, help="comma-separated addresses for arm A")
    ap.add_argument("--b", required=True, help="comma-separated addresses for arm B")
    ap.add_argument("--pairs", type=int, default=8,
                    help="send+read pairs PER ARM; total work is twice this")
    ap.add_argument("--block", type=int, default=4)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    alice = Path(args.alice_state)
    bob = Path(args.bob_state)
    arms = {"A": args.a.split(","), "B": args.b.split(",")}
    rng = random.Random(args.seed)

    results = {"A": [], "B": []}
    block_stats = []
    done = 0
    while done < args.pairs:
        for arm in ("A", "B"):
            addrs = arms[arm]
            per_block = []
            for i in range(args.block):
                idx = done + i
                a_addr = addrs[idx % len(addrs)]
                b_addr = addrs[(idx + 1) % len(addrs)]
                send = _run(alice, a_addr, ["send", "demo", f"m{idx}", "--timeout", "450"], repo, args.python)
                read = _run(bob, b_addr, ["read", "demo", "450", f"m{idx}"], repo, args.python)
                per_block.append({"send": send, "read": read})
                results[arm].extend(send["durations"] + read["durations"])
            block_stats.append({"arm": arm, "pairs": per_block})
        done += args.block

    summary = {}
    for arm, values in results.items():
        summary[arm] = {
            "n": len(values),
            "median_s": round(statistics.median(values), 2) if values else None,
            "mean_s": round(statistics.mean(values), 2) if values else None,
            "p90_s": round(sorted(values)[int(0.9 * len(values))], 2) if values else None,
        }
    paired = []
    a_blocks = [b for b in block_stats if b["arm"] == "A"]
    b_blocks = [b for b in block_stats if b["arm"] == "B"]
    for ab, bb in zip(a_blocks, b_blocks):
        av = [d for p in ab["pairs"] for k in ("send", "read") for d in p[k]["durations"]]
        bv = [d for p in bb["pairs"] for k in ("send", "read") for d in p[k]["durations"]]
        if av and bv:
            paired.append(statistics.median(av) - statistics.median(bv))
    result = {
        "arms": {"A": arms["A"], "B": arms["B"]},
        "pairs_per_arm": args.pairs, "block": args.block,
        "summary": summary,
        "paired_block_median_diffs": [round(d, 2) for d in paired],
        "paired_mean_diff_s": round(statistics.mean(paired), 2) if paired else None,
        "paired_ci95": _bootstrap_ci(paired, 2000, rng),
        "failures": sum(1 for b in block_stats for p in b["pairs"]
                        for k in ("send", "read") if p[k]["rc"] != 0),
        "covariates": {
            "retransmits": sum(p[k]["retransmits"] for b in block_stats for p in b["pairs"] for k in ("send", "read")),
            "not_ready_polls": sum(p[k]["not_ready"] for b in block_stats for p in b["pairs"] for k in ("send", "read")),
            "overlapped_pairings": sum(p[k]["overlapped"] for b in block_stats for p in b["pairs"] for k in ("send", "read")),
        },
    }
    print(json.dumps(result, indent=2))
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(result, indent=2))
    if result["failures"] or not result["paired_block_median_diffs"]:
        print("no usable samples", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
