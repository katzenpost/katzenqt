from __future__ import annotations

import ast
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import pytest

from katzenqt import headless, persistent
from katzenqt.headless import _actions

if TYPE_CHECKING:
    import argparse

CONFIG = ["--config", "/tmp/kqt-contract.toml"]

ACTIONS = sorted(n for n in vars(_actions) if n.startswith("_action_"))

CASES: list[tuple[list[str], dict[str, object]]] = [
    (
        ["create-conv", "room", "me", *CONFIG],
        {
            "action": "create-conv", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp",
            "conv_name": "room", "own_display": "me",
        },
    ),
    (
        ["voucher-mint", "room", "me", "--address", "127.0.0.1:64331"],
        {
            "action": "voucher-mint", "config": None,
            "address": "127.0.0.1:64331", "network": "tcp",
            "conv_name": "room", "display_name": "me",
        },
    ),
    (
        ["voucher-mint", "room", "me", "--address", "@katzenpost",
         "--network", "unix"],
        {
            "action": "voucher-mint", "config": None,
            "address": "@katzenpost", "network": "unix",
            "conv_name": "room", "display_name": "me",
        },
    ),
    (
        ["voucher-induct", "room", "peer", "AAAA", *CONFIG],
        {
            "action": "voucher-induct", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "peer_name": "peer", "voucher_b64": "AAAA",
        },
    ),
    (
        ["voucher-await", "room", *CONFIG],
        {
            "action": "voucher-await", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
        },
    ),
    (
        ["send", "room", "hi", *CONFIG],
        {
            "action": "send", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "text": "hi", "timeout": None,
        },
    ),
    (
        ["send", "room", "hi", "--timeout", "45.5", *CONFIG],
        {
            "action": "send", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "text": "hi", "timeout": 45.5,
        },
    ),
    (
        ["multi-send", "room", "a|b", *CONFIG],
        {
            "action": "multi-send", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "texts": "a|b",
        },
    ),
    (
        ["read", "room", "12.5", *CONFIG],
        {
            "action": "read", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "timeout_s": 12.5, "expected_text": None,
        },
    ),
    (
        ["read", "room", "12", "wanted text", *CONFIG],
        {
            "action": "read", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "timeout_s": 12.0, "expected_text": "wanted text",
        },
    ),
    (
        ["chat-session", "room", "SEND:a", "READ:b", "SLEEP:1", *CONFIG],
        {
            "action": "chat-session", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "steps": ["SEND:a", "READ:b", "SLEEP:1"],
        },
    ),
    (
        ["send-file", "room", "/tmp/payload.bin", *CONFIG],
        {
            "action": "send-file", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "path": "/tmp/payload.bin", "basename": None, "filetype": None,
            "timeout": None,
        },
    ),
    (
        ["send-file", "room", "/tmp/payload.bin", "--basename", "cat.png",
         "--filetype", "image/png", "--timeout", "90", *CONFIG],
        {
            "action": "send-file", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "path": "/tmp/payload.bin", "basename": "cat.png",
            "filetype": "image/png", "timeout": 90.0,
        },
    ),
    (
        ["read-file", "room", "--to-dir", "/tmp/inbox", *CONFIG],
        {
            "action": "read-file", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "to_dir": "/tmp/inbox", "timeout": 600.0, "basename": None,
        },
    ),
    (
        ["read-file", "room", "--to-dir", "/tmp/inbox", "--timeout", "30",
         "--basename", "cat.png", *CONFIG],
        {
            "action": "read-file", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "to_dir": "/tmp/inbox", "timeout": 30.0, "basename": "cat.png",
        },
    ),
    (["info"], {"action": "info"}),
    (
        ["tally-create", "room", "when", "--slot", "mon", "--slot", "tue",
         *CONFIG],
        {
            "action": "tally-create", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "topic": "when", "mode": "approval", "slot": ["mon", "tue"],
            "timeout": 600.0,
        },
    ),
    (
        ["tally-create", "room", "when", "--mode", "availability",
         "--slot", "mon", *CONFIG],
        {
            "action": "tally-create", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "topic": "when", "mode": "availability", "slot": ["mon"],
            "timeout": 600.0,
        },
    ),
    (
        ["tally-create", "room", "when", "--slot", "mon", "--timeout", "42",
         *CONFIG],
        {
            "action": "tally-create", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "topic": "when", "mode": "approval", "slot": ["mon"],
            "timeout": 42.0,
        },
    ),
    (
        ["tally-vote", "room", "--survey", "ab12", "--slot", "s0=yes",
         *CONFIG],
        {
            "action": "tally-vote", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "survey": "ab12", "slot": ["s0=yes"], "timeout": 600.0,
        },
    ),
    (
        ["tally-result", "room", "--survey", "ab12", *CONFIG],
        {
            "action": "tally-result", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "survey": "ab12", "expect_voters": None, "timeout": 600.0,
        },
    ),
    (
        ["tally-result", "room", "--survey", "ab12", "--expect-voters", "3",
         "--timeout", "42", *CONFIG],
        {
            "action": "tally-result", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "survey": "ab12", "expect_voters": 3, "timeout": 42.0,
        },
    ),
    (
        ["tally-close", "room", "--survey", "ab12", *CONFIG],
        {
            "action": "tally-close", "config": "/tmp/kqt-contract.toml",
            "address": None, "network": "tcp", "conv_name": "room",
            "survey": "ab12", "timeout": 600.0,
        },
    ),
    (["tally-list", "room"], {"action": "tally-list", "conv_name": "room"}),
    (["remove-conv", "room"], {"action": "remove-conv", "conv_name": "room"}),
    (
        ["remove-peer", "room", "bob"],
        {"action": "remove-peer", "conv_name": "room", "peer_name": "bob"},
    ),
]

VERBS = [
    "create-conv", "voucher-mint", "voucher-induct", "voucher-await",
    "send", "multi-send", "read", "chat-session", "send-file", "read-file",
    "info", "tally-create", "tally-vote", "tally-result", "tally-close",
    "tally-list", "remove-conv", "remove-peer",
]

OFFLINE_VERBS = [
    "info", "tally-list", "remove-conv", "remove-peer",
]

DISPATCH = {
    "create-conv": "_action_create_conv",
    "voucher-mint": "_action_voucher_mint",
    "voucher-induct": "_action_voucher_induct",
    "voucher-await": "_action_voucher_await",
    "send": "_action_send",
    "multi-send": "_action_multi_send",
    "read": "_action_read",
    "chat-session": "_action_chat_session",
    "send-file": "_action_send_file",
    "read-file": "_action_read_file",
    "info": "_action_info",
    "tally-create": "_action_tally_create",
    "tally-vote": "_action_tally_vote",
    "tally-result": "_action_tally_result",
    "tally-close": "_action_tally_close",
    "tally-list": "_action_tally_list",
    "remove-conv": "_action_remove_conv",
    "remove-peer": "_action_remove_peer",
}

TOKENS_AND_CODES: dict[
    str, tuple[tuple[tuple[str, str], ...], tuple[int, ...]]
] = {
    "_action_create_conv": (
        (("error", "no read_cap provisioned after timeout"),
         ("info", "CREATED")),
        (0, 2),
    ),
    "_action_voucher_mint": (
        (("error", "conversation %r not found"),
         ("error", "write cap not provisioned after timeout"),
         ("info", "VOUCHER=%s")),
        (0, 2),
    ),
    "_action_voucher_induct": (
        (("error", "conversation %r not found"),
         ("error", "invalid voucher encoding"),
         ("info", "INDUCTED=%s")),
        (0, 2),
    ),
    "_action_voucher_await": (
        (("error", "conversation %r not found"), ("info", "JOINED")),
        (0, 2),
    ),
    "_send_one_gcm": (
        (("error", "conversation %r not found"),
         ("error", "send timed out waiting for SentLog"),
         ("info", "SENT")),
        (0, 2, 3),
    ),
    "_action_send": ((), ()),
    "_action_send_file": (
        (("error", "file exceeds %d-byte cap: %d bytes"),
         ("error", "file not found: %s")),
        (2,),
    ),
    "_action_read_file": (
        (("error", "conversation %r not found"),
         ("info", "RECV_FILE=%s"),
         ("info", "TIMEOUT")),
        (0, 1, 2),
    ),
    "_action_multi_send": (
        (("error", "conversation %r not found"),
         ("error", "multi-send timed out"),
         ("info", "SENT")),
        (0, 2, 3),
    ),
    "_action_chat_session": (
        (("error", "STEP_FAIL:"),
         ("error", "conversation %r not found"),
         ("info", "SESSION_DONE"),
         ("info", "STEP_OK:"),
         ("info", "STEP_POLL:"),
         ("info", "STEP_WAITING_ACK:")),
        (0, 2, 3, 4, 5),
    ),
    "_action_read": (
        (("error", "conversation %r not found"),
         ("info", "RECV=%s"),
         ("info", "RECV_ADD=%s added %s"),
         ("info", "TIMEOUT")),
        (0, 1, 2),
    ),
    "_action_info": ((), (0,)),
    "_action_tally_create": (
        (("error", "conversation %r not found"),
         ("error", "unknown mode %r"),
         ("info", "TALLY_CREATED=%s")),
        (2,),
    ),
    "_action_tally_vote": (
        (("error", "%s"),
         ("error", "conversation %r not found"),
         ("error", "could not apply vote to survey %s"),
         ("error", "invalid vote: %s"),
         ("error", "survey %s not received within %.0fs"),
         ("error", "vote send timed out for survey %s"),
         ("info", "VOTED")),
        (0, 1, 2, 3),
    ),
    "_action_tally_result": (
        (("error", "tally result timed out for survey %s"),
         ("info", "%s"),
         ("info", "TALLY=%s")),
        (0, 1),
    ),
    "_action_tally_close": (
        (("error", "close send timed out for survey %s"),
         ("error", "conversation %r not found"),
         ("error", "could not close survey %s (only the creator may)"),
         ("error", "survey %s not received within %.0fs"),
         ("info", "CLOSED")),
        (0, 1, 2, 3),
    ),
    "_action_tally_list": (
        (("error", "conversation %r not found"),
         ("info", "(no surveys)"),
         ("info", "SURVEY=%s status=%s voters=%d mode=%s topic=%s")),
        (0, 2),
    ),
    "_action_remove_conv": (
        (("error", "conversation %r not found"),
         ("info", "REMOVED_CONV")),
        (0, 2),
    ),
    "_action_remove_peer": (
        (("error", "%s"),
         ("error", "conversation %r not found"),
         ("error", "peer %r not found in conversation %r"),
         ("info", "REMOVED_PEER=%s")),
        (0, 2),
    ),
}

EXIT_CODES = {
    0: "success",
    1: "receive deadline expired",
    2: "usage or precondition error",
    3: "send deadline expired",
    4: "chat-session read step deadline expired",
    5: "chat-session step unusable",
}


class _Outcome(TypedDict):
    code: int
    raises: BaseException | None


@pytest.fixture
def run(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., tuple[int, dict[str, object]]]:
    seen: dict[str, object] = {}
    outcome: _Outcome = {"code": 0, "raises": None}

    def recorder(
        name: str,
    ) -> Callable[[argparse.Namespace], Coroutine[object, object, int]]:
        async def record(args: argparse.Namespace) -> int:
            seen.clear()
            seen.update(vars(args))
            seen["dispatched"] = name
            if outcome["raises"] is not None:
                raise outcome["raises"]
            return int(outcome["code"])
        return record

    for name in ACTIONS:
        monkeypatch.setattr(_actions, name, recorder(name))
    monkeypatch.setattr(persistent, "init_and_migrate", lambda: None)
    monkeypatch.setattr(headless, "_configure_logging", lambda: None)

    def invoke(
        argv: list[str], code: int = 0, raises: BaseException | None = None,
    ) -> tuple[int, dict[str, object]]:
        outcome["code"] = code
        outcome["raises"] = raises
        seen.clear()
        returncode = headless.cli(argv)
        namespace = dict(seen)
        namespace.pop("func", None)
        return returncode, namespace

    return invoke


def test_verb_set_is_exactly_these() -> None:
    assert sorted(DISPATCH) == sorted(VERBS)
    assert sorted(DISPATCH.values()) == ACTIONS


@pytest.mark.parametrize(
    ("argv", "expected"), CASES, ids=[" ".join(a) for a, _ in CASES],
)
def test_arguments_reach_the_action_unchanged(
    run: Callable[..., tuple[int, dict[str, object]]],
    argv: list[str],
    expected: dict[str, object],
) -> None:
    returncode, namespace = run(argv)
    assert returncode == 0
    assert namespace.pop("dispatched") == DISPATCH[argv[0]]
    assert namespace == expected
    for key, value in expected.items():
        assert type(namespace[key]) is type(value), key


def test_every_verb_is_covered() -> None:
    assert {argv[0] for argv, _ in CASES} == set(VERBS)


@pytest.mark.parametrize("verb", OFFLINE_VERBS)
def test_offline_verbs_take_no_connection(
    run: Callable[..., tuple[int, dict[str, object]]], verb: str,
) -> None:
    argv = {"info": [verb], "remove-peer": [verb, "room", "bob"]}.get(verb, [verb, "room"])
    _, namespace = run(argv)
    assert "config" not in namespace
    assert "address" not in namespace
    assert "network" not in namespace
    with pytest.raises(SystemExit) as exc:
        run([*argv, *CONFIG])
    assert exc.value.code == 2


@pytest.mark.parametrize("code", sorted(EXIT_CODES))
def test_action_exit_code_is_returned_unchanged(
    run: Callable[..., tuple[int, dict[str, object]]], code: int,
) -> None:
    returncode, _ = run(["info"], code=code)
    assert returncode == code


def test_uncaught_action_error_is_not_swallowed(
    run: Callable[..., tuple[int, dict[str, object]]],
) -> None:
    with pytest.raises(RuntimeError):
        run(["info"], raises=RuntimeError("boom"))


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["no-such-verb"],
        ["info", "--no-such-flag"],
        ["create-conv", "room", "me"],
        ["create-conv", "room", "me", "--address", "a", *CONFIG],
        ["create-conv", "room", *CONFIG],
        ["create-conv", "room", "me", "extra", *CONFIG],
        ["create-conv", "room", "me", "--address", "a", "--network", "sctp"],
        ["tally-create", "room", "when", "--mode", "plurality",
         "--slot", "mon", *CONFIG],
        ["tally-create", "room", "when", *CONFIG],
        ["tally-vote", "room", "--slot", "s0=yes", *CONFIG],
        ["tally-vote", "room", "--survey", "ab", *CONFIG],
        ["tally-result", "room", *CONFIG],
        ["tally-result", "room", "--survey", "ab", "--expect-voters", "many",
         *CONFIG],
        ["tally-close", "room", *CONFIG],
        ["read-file", "room", *CONFIG],
        ["read", "room", *CONFIG],
        ["read", "room", "soon", *CONFIG],
        ["chat-session", "room", *CONFIG],
    ],
    ids=lambda argv: " ".join(argv) or "<no verb>",
)
def test_usage_errors_exit_2(
    run: Callable[..., tuple[int, dict[str, object]]], argv: list[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        run(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("verb", ["send", "send-file"])
@pytest.mark.parametrize(
    "value", ["0", "-1", "nan", "inf", "-inf", "1e999", "bad"],
)
def test_send_timeout_rejects_nonpositive_and_infinite(
    run: Callable[..., tuple[int, dict[str, object]]], verb: str, value: str,
) -> None:
    with pytest.raises(SystemExit) as exc:
        run([verb, "room", "payload", "--timeout", value, *CONFIG])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["read-file", "room", "--to-dir", "/tmp/inbox"], 0.0),
        (["tally-create", "room", "when", "--slot", "mon"], 0.0),
        (["tally-vote", "room", "--survey", "ab", "--slot", "s0=yes"], 0.0),
        (["tally-result", "room", "--survey", "ab"], 0.0),
        (["tally-close", "room", "--survey", "ab"], 0.0),
    ],
    ids=[
        "read-file", "tally-create", "tally-vote", "tally-result",
        "tally-close",
    ],
)
def test_other_timeouts_are_a_plain_float(
    run: Callable[..., tuple[int, dict[str, object]]],
    argv: list[str],
    expected: float,
) -> None:
    _, namespace = run([*argv, "--timeout", "0", *CONFIG])
    assert namespace["timeout"] == expected
    _, namespace = run([*argv, "--timeout", "inf", *CONFIG])
    assert namespace["timeout"] == float("inf")


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("argv", [[], *[[v] for v in VERBS]])
def test_help_exits_0(
    run: Callable[..., tuple[int, dict[str, object]]],
    argv: list[str],
    flag: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        run([*argv, flag])
    assert exc.value.code == 0
    assert "katzenqt-headless" in capsys.readouterr().out


def _action_source() -> ast.Module:
    path = Path(_actions.__file__)
    return ast.parse(path.read_text(encoding="ascii"))


def _leading_literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    parts = []
    for value in node.values:
        if not (isinstance(value, ast.Constant)
                and isinstance(value.value, str)):
            break
        parts.append(value.value)
    return "".join(parts) or None


def _tokens_and_codes(
    node: ast.AST,
) -> tuple[tuple[tuple[str, str], ...], tuple[int, ...]]:
    tokens, codes = set(), set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "logger"
                and func.attr in ("info", "error")
                and sub.args
            ):
                text = _leading_literal(sub.args[0])
                if text is not None:
                    tokens.add((func.attr, text))
        if (
            isinstance(sub, ast.Return)
            and isinstance(sub.value, ast.Constant)
            and isinstance(sub.value.value, int)
            and not isinstance(sub.value.value, bool)
        ):
            codes.add(sub.value.value)
    return tuple(sorted(tokens)), tuple(sorted(codes))


def test_result_tokens_and_exit_codes_are_frozen() -> None:
    found: dict[str, tuple[tuple[tuple[str, str], ...], tuple[int, ...]]] = {}
    for node in _action_source().body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in TOKENS_AND_CODES:
            found[node.name] = _tokens_and_codes(node)
    assert found == TOKENS_AND_CODES


def test_exit_codes_used_are_documented() -> None:
    used = {
        code
        for _, codes in TOKENS_AND_CODES.values()
        for code in codes
    }
    assert used <= set(EXIT_CODES)
