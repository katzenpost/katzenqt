import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from katzenqt import network
from tests.integration._role import run_observed
from tests.test_read_arming_pass_failures import _install_loop


@pytest.mark.parametrize("failure", [ValueError("bad reply"), TimeoutError("bug")])
def test_caught_setup_failure_cannot_become_a_delivery_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: Exception,
) -> None:
    state, connection = _install_loop(
        monkeypatch, failure=RuntimeError(), stage="unused",
        prior_success=True,
    )
    state.failed = True
    monkeypatch.setattr(connection, "encrypt_read", AsyncMock(side_effect=failure))
    monkeypatch.setattr(network, "logger", logging.getLogger("katzen.network"))

    def invoke(args: list[str]) -> int:
        asyncio.run(network.readables_to_mixwal(connection))
        logging.getLogRecordFactory()(
            "katzen.headless", logging.ERROR, "test", 1,
            "TIMEOUT", (), None, "_action_read",
        )
        return 1

    report = tmp_path / "role.json"
    assert run_observed(invoke, [], report) == 1
    assert json.loads(report.read_text())["status"] == "failed"
