"""Opportunistic acknowledgements over the docker mixnet.

Skipped unless ``KATZENQT_DOCKER_INTEGRATION=1`` (see conftest.py).

Alice founds a group and inducts Bob; Bob inducts Carol. Each then sends a
message, which carries an acknowledgement of whatever its sender has read
since its last one, naming each member by its place in the sender's own
roster. Nothing but the messages themselves crosses the mixnet: every role
is a separate subprocess with its own ``KQT_STATE``, so a member can only
resolve another's acknowledgement if it worked out that member's roster the
same way its owner did.

What is checked, through the offline ``info`` verb:

* a founder's roster is itself, and grows by the member it introduces;
* a joiner's roster is its introducer's with itself after it, which only
  the voucher reply can have told it;
* an acknowledgement is accepted by the member it names, which needs the
  right roster index and a box that member really wrote;
* Alice resolves Carol's acknowledgement although she never spoke to Carol:
  she learns Carol's roster from Bob's ``Introduction``;
* Alice numbers Carol only once she has sent a message acknowledging past
  that ``Introduction``.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from tests.integration._bounce_helpers import budget_s
from tests.integration.test_voucher import (
    _assert_ok,
    _expect_info,
    _expect_token,
    _output,
    _read_deadline_s,
    _read_timeout_s,
    _run_role,
    _send_timeout_s,
)


def _demo(state: Path) -> dict[str, object]:
    info = _run_role(state, "info", timeout=budget_s(30.0))
    _assert_ok(info, "info")
    conversations = cast(
        "list[dict[str, object]]", _expect_info(info)["conversations"]
    )
    return next(c for c in conversations if c["name"] == "demo")


def _join(inductor: Path, joiner: Path, name: str) -> None:
    """``joiner`` mints a voucher; ``inductor`` inducts it; it joins."""
    _assert_ok(
        _run_role(joiner, "create-conv", "demo", name), f"{name} create-conv"
    )
    mint = _run_role(
        joiner, "voucher-mint", "demo", name, timeout=budget_s(630.0)
    )
    _assert_ok(mint, f"{name} voucher-mint")
    induct = _run_role(
        inductor,
        "voucher-induct",
        "demo",
        name,
        _expect_token(mint, "VOUCHER="),
        timeout=budget_s(630.0),
    )
    _assert_ok(induct, f"voucher-induct {name}")
    assert "INDUCTED=" in _output(induct)
    joined = _run_role(
        joiner, "voucher-await", "demo", timeout=budget_s(630.0)
    )
    _assert_ok(joined, f"{name} voucher-await")
    assert "JOINED" in _output(joined)


def _send(state: Path, text: str) -> None:
    _assert_ok(
        _run_role(state, "send", "demo", text, timeout=_send_timeout_s()),
        f"send {text!r}",
    )


def _read(state: Path, text: str) -> None:
    read = _run_role(
        state,
        "read",
        "demo",
        _read_deadline_s(),
        text,
        timeout=_read_timeout_s(),
    )
    _assert_ok(read, f"read {text!r}")
    assert _expect_token(read, "RECV=") == text


@pytest.mark.integration
def test_acknowledgements_are_named_by_roster_index(
    kpclientd_endpoint: "tuple[str, int]",
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    alice = tmp_path_factory.mktemp("alice-ack") / "state"
    bob = tmp_path_factory.mktemp("bob-ack") / "state"
    carol = tmp_path_factory.mktemp("carol-ack") / "state"

    _assert_ok(
        _run_role(alice, "create-conv", "demo", "alice"), "alice create-conv"
    )
    _join(alice, bob, "bob")
    assert _demo(bob)["roster"] == ["alice", "bob"]

    # Bob reads Alice and says so in his next message, naming her by his
    # roster index for her. Alice must find herself in it.
    _send(alice, "from alice")
    _read(bob, "from alice")
    _send(bob, "from bob")
    _read(alice, "from bob")
    assert _demo(alice)["roster"] == ["alice", "bob"]
    assert list(cast("dict[str, int]", _demo(alice)["acked"])) == ["bob"]

    # Carol joins through Bob and reads what is there.
    _join(bob, carol, "carol")
    assert _demo(carol)["roster"] == ["alice", "bob", "carol"]
    _read(carol, "from alice")
    _read(carol, "from bob")

    # One message from Carol acknowledges both. Bob knows her roster because
    # he introduced her; Alice has to work it out from his Introduction.
    _send(carol, "from carol")
    _read(bob, "from carol")
    _read(alice, "from carol")
    assert "carol" in cast("dict[str, int]", _demo(bob)["acked"])
    assert sorted(cast("dict[str, int]", _demo(alice)["acked"])) == [
        "bob",
        "carol",
    ]

    # Alice has read Bob's Introduction of Carol but numbers her only in the
    # message that acknowledges past it.
    assert _demo(alice)["roster"] == ["alice", "bob"]
    _send(alice, "again from alice")
    assert _demo(alice)["roster"] == ["alice", "bob", "carol"]
