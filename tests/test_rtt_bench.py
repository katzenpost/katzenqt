import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "tools" / "perf" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load("rtt_bench")
probe = _load("roundtrip_probe")


class TestBusySeconds:
    def test_disjoint_intervals_add(self):
        assert probe._busy_seconds([(0, 1), (2, 3)]) == 2.0

    def test_overlapping_intervals_do_not_double_count(self):
        assert probe._busy_seconds([(0, 5), (1, 3)]) == 5.0

    def test_nested_and_trailing(self):
        assert probe._busy_seconds([(0, 10), (2, 4), (3, 12)]) == 12.0

    def test_empty(self):
        assert probe._busy_seconds([]) == 0.0


class TestBootstrapCI:
    def test_too_few_samples(self):
        assert bench._bootstrap_ci([1.0], 100, __import__("random").Random(0)) == []

    def test_brackets_the_mean(self):
        rng = __import__("random").Random(1)
        lo, hi = bench._bootstrap_ci([1.0, 2.0, 3.0, 4.0], 500, rng)
        assert lo <= 2.5 <= hi


class TestPairsAreUnique:
    def test_each_pair_gets_its_own_message(self, tmp_path):
        seen = tmp_path / "seen.txt"
        stub = tmp_path / "stub.sh"
        stub.write_text(
            "#!/bin/bash\n"
            f'for a in "$@"; do case "$a" in m[0-9]*) echo "$a" >> {seen};; esac; done\n'
            'echo "INFO thinclient: start_resending_encrypted_message request sent." >&2\n'
            'echo "INFO thinclient: start_resending_encrypted_message response received." >&2\n'
        )
        stub.chmod(0o755)
        out = tmp_path / "out.json"
        subprocess.run(
            [sys.executable, str(_ROOT / "tools" / "perf" / "rtt_bench.py"),
             "--alice-state", str(tmp_path / "a"), "--bob-state", str(tmp_path / "b"),
             "--a", "1.1.1.1:1", "--b", "1.1.1.1:1,2.2.2.2:2",
             "--pairs", "2", "--block", "1", "--repo", str(_ROOT),
             "--python", str(stub), "--json", str(out)],
            capture_output=True, check=False)
        messages = seen.read_text().split()
        assert len(messages) == len(set(messages)) * 2, messages
        assert len(set(messages)) == 4, set(messages)

    def test_reports_partial_flag(self, tmp_path):
        result = bench._summarise(
            {"A": ["x"], "B": ["y"]},
            type("A", (), {"pairs": 1, "block": 1})(),
            {"A": [1.0], "B": [2.0]}, [], __import__("random").Random(0),
            partial=True)
        assert result["partial"] is True
