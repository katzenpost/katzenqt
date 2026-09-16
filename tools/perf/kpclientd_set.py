"""Start a set of kpclientd daemons, one per test worker.

Each daemon gets its own Listen port and a config pinning exactly one
gateway, so the set spreads over every gateway the source config knows
rather than sharing one link. Gateway selection in the daemon is a random
pick over PinnedGateways, so a config carrying a single entry is
deterministic.

  python3 tools/perf/kpclientd_set.py start \
      --config config/client.toml --binary ./kpclientd \
      --count 6 --base-port 64331 --out-dir clients

Writes clients/manifest.json describing what was started, and exits non-zero
unless every daemon reached a gateway.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

_GW_BLOCK = re.compile(r"\s*\[\[PinnedGateways\.Gateways\]\]")
_SECTION = re.compile(r"\[[A-Za-z]")
_UNIX_LISTEN = '[Listen]\n  [Listen.Unix]\n    Address = "@katzenpost"'


def _gateway_blocks(lines: list[str]) -> tuple[list[list[str]], int, int]:
    starts = [i for i, l in enumerate(lines) if _GW_BLOCK.match(l)]
    if not starts:
        raise SystemExit("config has no [[PinnedGateways.Gateways]] blocks")
    end = next((i for i in range(starts[-1] + 1, len(lines))
                if _SECTION.match(lines[i])), len(lines))
    bounds = starts + [end]
    blocks = [lines[bounds[i]:bounds[i + 1]] for i in range(len(starts))]
    return blocks, starts[0], end


def write_configs(src: Path, out_dir: Path, count: int, base_port: int) -> list[dict]:
    lines = src.read_text().splitlines(keepends=True)
    blocks, first, end = _gateway_blocks(lines)
    names = [re.search(r'Name = "([^"]+)"', "".join(b)).group(1) for b in blocks]
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for i in range(count):
        port = base_port + i
        gw = i % len(blocks)
        body = "".join(lines[:first] + blocks[gw] + lines[end:])
        if body.count(_UNIX_LISTEN) != 1:
            raise SystemExit("config's [Listen] block is not the expected unix form")
        body = body.replace(
            _UNIX_LISTEN,
            f'[Listen]\n  [Listen.Tcp]\n    Address = "127.0.0.1:{port}"\n'
            f'    Network = "tcp"',
            1,
        )
        path = out_dir / f"client{i}.toml"
        path.write_text(body)
        made.append({"index": i, "port": port, "gateway": names[gw], "config": str(path)})
    return made


def start(args) -> int:
    made = write_configs(Path(args.config), Path(args.out_dir), args.count, args.base_port)
    log_dir = Path(args.out_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    for entry in made:
        log = log_dir / f"d{entry['index']}.log"
        handle = log.open("w")
        proc = subprocess.Popen(
            [args.binary, "--config", entry["config"]], stdout=handle, stderr=handle)
        entry["pid"] = proc.pid
        entry["log"] = str(log)

    deadline = time.time() + args.timeout
    pending = {e["index"]: e for e in made}
    while pending and time.time() < deadline:
        for idx, entry in list(pending.items()):
            text = Path(entry["log"]).read_text(errors="replace")
            found = re.search(r'Connected to gateway "([^"]+)"', text)
            if found:
                entry["connected_to"] = found.group(1)
                del pending[idx]
        if pending:
            time.sleep(1.0)

    manifest = Path(args.out_dir) / "manifest.json"
    manifest.write_text(json.dumps({"daemons": made}, indent=2))
    for entry in made:
        print(f"d{entry['index']} port={entry['port']} gateway={entry['gateway']} "
              f"connected={entry.get('connected_to', 'NO')}")
    if pending:
        print(f"{len(pending)} daemon(s) never reached a gateway", file=sys.stderr)
        return 1
    print(f"{len(made)} client(s) up; manifest at {manifest}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--config", required=True)
    s.add_argument("--binary", required=True)
    s.add_argument("--count", type=int, default=4)
    s.add_argument("--base-port", type=int, default=64331)
    s.add_argument("--out-dir", default="clients")
    s.add_argument("--timeout", type=float, default=180.0)
    s.set_defaults(func=start)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
