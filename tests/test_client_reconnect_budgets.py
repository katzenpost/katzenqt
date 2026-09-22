from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from tests.integration import test_client_reconnect as scenario
from tests.integration._outcomes import result_path


@dataclass
class _Observed:
    reader_s: float = 0.0
    reader_wait_s: float = 0.0
    writer_sleep_s: float = 0.0
    writer_process_s: float = 0.0
    commit_s: float = 0.0


@pytest.mark.parametrize("epoch", [30, 120, 1200])
def test_reader_outlives_commit_kill_and_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory, epoch: float,
) -> None:
    observed = _Observed()
    monkeypatch.setattr(scenario, "epoch_duration_s", lambda: epoch)
    monkeypatch.setattr(scenario, "_bootstrap_voucher", Mock())

    class Process:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, *, timeout: float) -> int:
            observed.reader_wait_s = timeout
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    def spawn(
        state: Path, *args: str, stdout_path: Path, stderr_path: Path,
    ) -> Process:
        stdout_path.write_text("", encoding="ascii")
        if args[-1].startswith("READ:"):
            observed.reader_s = float(args[-1].rsplit(":", 1)[1])
            stderr_path.write_text(
                "STEP_OK:0:READ:m1\nSESSION_DONE\n", encoding="ascii",
            )
        else:
            stderr_path.write_text(
                "STEP_WAITING_ACK:0:SEND:m1\n", encoding="ascii",
            )
        result_path(stderr_path).write_text(
            json.dumps({"version": 1, "status": "passed", "returncode": 0}),
            encoding="ascii",
        )
        return Process()

    def run(
        state: Path, *args: str, timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        if args[-1].startswith("SLEEP:"):
            observed.writer_sleep_s = float(args[-1].split(":")[1])
            observed.writer_process_s = timeout
            return subprocess.CompletedProcess(
                list(args), 0, stdout="SESSION_DONE", stderr="",
            )
        return subprocess.CompletedProcess(
            list(args), 0, stdout="STEP_OK:0:SEND:m0", stderr="",
        )

    def token(
        path: Path, value: str, deadline_s: float, what: str,
    ) -> None:
        observed.commit_s = deadline_s

    def terminate(
        proc: Process, what: str, *, expect_signal: bool = True,
    ) -> None:
        proc.returncode = -15

    monkeypatch.setattr(scenario, "_spawn_role", spawn)
    monkeypatch.setattr(scenario, "_run_role", run)
    monkeypatch.setattr(scenario, "_wait_for_token", token)
    monkeypatch.setattr(scenario, "_terminate", terminate)
    scenario.test_write_survives_client_reconnect(
        ("127.0.0.1", 64331), tmp_path_factory,
    )
    assert observed.writer_sleep_s == epoch + 100
    assert observed.writer_process_s >= observed.writer_sleep_s + 60
    assert observed.reader_s >= (
        observed.commit_s + 20 + observed.writer_process_s
    )
    assert observed.reader_s <= 7200
    assert 0 <= observed.reader_wait_s <= observed.reader_s + 120


def test_scenario_validates_epoch_before_creating_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    def invalid_epoch() -> float:
        raise RuntimeError("missing target epoch")

    bootstrap = Mock()
    run = Mock(side_effect=AssertionError("network ran before validation"))
    monkeypatch.setattr(scenario, "epoch_duration_s", invalid_epoch)
    monkeypatch.setattr(scenario, "_bootstrap_voucher", bootstrap)
    monkeypatch.setattr(scenario, "_run_role", run)
    with pytest.raises(RuntimeError, match="missing target epoch"):
        scenario.test_write_survives_client_reconnect(
            ("127.0.0.1", 64331), tmp_path_factory,
        )
    bootstrap.assert_not_called()
    run.assert_not_called()
