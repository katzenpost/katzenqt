from __future__ import annotations

import math
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import click

from . import _actions

Action = Callable[[SimpleNamespace], Coroutine[Any, Any, int]]

PROG = "katzenqt-headless"

PATH = click.Path(path_type=str, readable=False)


@dataclass(frozen=True)
class Plan:
    func: Action
    args: SimpleNamespace


def seconds(value: str) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise click.BadParameter(
            "timeout must be a finite positive number",
        ) from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise click.BadParameter("timeout must be a finite positive number")
    return timeout


_Decorator = Callable[..., Any]


def connection(func: Any) -> Any:
    options: list[_Decorator] = [
        click.option(
            "--network", type=click.Choice(["tcp", "unix"]), default="tcp",
            help="dial transport for --address (default: tcp)",
        ),
        click.option(
            "--address", metavar="ADDR", default=None,
            help="kpclientd dial address, e.g. 127.0.0.1:64331 (tcp) "
                 "or @katzenpost (unix)",
        ),
        click.option(
            "--config", metavar="THINCLIENT_TOML", type=PATH, default=None,
            help="path to a thinclient.toml describing the kpclientd "
                 "connection",
        ),
    ]
    for option in options:
        func = option(func)
    return func


def plan(action: str, **params: Any) -> Plan:
    ctx = click.get_current_context()
    if "config" in params and (
        (params["config"] is None) == (params["address"] is None)
    ):
        raise click.UsageError(
            "exactly one of --config and --address is required", ctx=ctx,
        )
    args = SimpleNamespace(action=ctx.info_name, **params)
    return Plan(getattr(_actions, action), args)


@click.group(
    name=PROG,
    no_args_is_help=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Headless driver for katzenqt: send/receive over kpclientd.",
)
def verbs() -> None:
    pass


@verbs.command("create-conv")
@connection
@click.argument("conv_name")
@click.argument("own_display")
def _create_conv(**params: Any) -> Plan:
    return plan("_action_create_conv", **params)


@verbs.command("voucher-mint")
@connection
@click.argument("conv_name")
@click.argument("display_name")
def _voucher_mint(**params: Any) -> Plan:
    return plan("_action_voucher_mint", **params)


@verbs.command("voucher-induct")
@connection
@click.argument("conv_name")
@click.argument("peer_name")
@click.argument("voucher_b64")
def _voucher_induct(**params: Any) -> Plan:
    return plan("_action_voucher_induct", **params)


@verbs.command("voucher-await")
@connection
@click.argument("conv_name")
def _voucher_await(**params: Any) -> Plan:
    return plan("_action_voucher_await", **params)


@verbs.command("send")
@connection
@click.argument("conv_name")
@click.argument("text")
@click.option(
    "--timeout", type=seconds, default=None,
    help="seconds to wait for delivery; default scales with message size",
)
def _send(**params: Any) -> Plan:
    return plan("_action_send", **params)


@verbs.command(
    "multi-send",
    help="queue a pipe-separated list of texts and wait for the last ACK",
)
@connection
@click.argument("conv_name")
@click.argument("texts")
def _multi_send(**params: Any) -> Plan:
    return plan("_action_multi_send", **params)


@verbs.command("read")
@connection
@click.argument("conv_name")
@click.argument("timeout_s", type=float)
@click.argument("expected_text", required=False, default=None)
def _read(**params: Any) -> Plan:
    return plan("_action_read", **params)


@verbs.command(
    "chat-session",
    help="run steps like SEND:text, READ:text, SLEEP:seconds in one session",
)
@connection
@click.argument("conv_name")
@click.argument("steps", nargs=-1, required=True)
def _chat_session(**params: Any) -> Plan:
    params["steps"] = list(params["steps"])
    return plan("_action_chat_session", **params)


@verbs.command("send-file", help="send the file at PATH as an attachment")
@connection
@click.argument("conv_name")
@click.argument("path", type=PATH)
@click.option("--basename", default=None)
@click.option("--filetype", default=None)
@click.option(
    "--timeout", type=seconds, default=None,
    help="seconds to wait for delivery; default scales with message size",
)
def _send_file(**params: Any) -> Plan:
    return plan("_action_send_file", **params)


@verbs.command("read-file")
@connection
@click.argument("conv_name")
@click.option(
    "--to-dir", type=PATH, required=True,
    help="directory where the received attachment is written",
)
@click.option(
    "--timeout", type=float, default=600.0,
    help="seconds to wait for a file_marker to arrive",
)
@click.option(
    "--basename", default=None,
    help="restrict to attachments whose basename matches",
)
def _read_file(**params: Any) -> Plan:
    return plan("_action_read_file", **params)


@verbs.command(
    "info",
    help="emit one line of JSON describing the state file's schema, "
         "conversations, and outstanding WAL counts",
)
def _info(**params: Any) -> Plan:
    return plan("_action_info", **params)


@verbs.command("tally-create")
@connection
@click.argument("conv_name")
@click.argument("topic")
@click.option(
    "--mode", type=click.Choice(["availability", "approval"]),
    default="approval",
)
@click.option(
    "--slot", multiple=True, required=True,
    help="descriptive text for one slot; repeat for each slot",
)
@click.option("--timeout", type=float, default=600.0)
def _tally_create(**params: Any) -> Plan:
    params["slot"] = list(params["slot"])
    return plan("_action_tally_create", **params)


@verbs.command("tally-vote")
@connection
@click.argument("conv_name")
@click.option("--survey", required=True, help="survey id in hex")
@click.option(
    "--slot", multiple=True, required=True,
    help="a per-slot vote SLOT_ID=availability; repeat per slot",
)
@click.option("--timeout", type=float, default=600.0)
def _tally_vote(**params: Any) -> Plan:
    params["slot"] = list(params["slot"])
    return plan("_action_tally_vote", **params)


@verbs.command("tally-result")
@connection
@click.argument("conv_name")
@click.option("--survey", required=True, help="survey id in hex")
@click.option("--expect-voters", type=int, default=None)
@click.option("--timeout", type=float, default=600.0)
def _tally_result(**params: Any) -> Plan:
    return plan("_action_tally_result", **params)


@verbs.command("tally-close")
@connection
@click.argument("conv_name")
@click.option("--survey", required=True, help="survey id in hex")
@click.option("--timeout", type=float, default=600.0)
def _tally_close(**params: Any) -> Plan:
    return plan("_action_tally_close", **params)


@verbs.command("tally-list")
@click.argument("conv_name")
def _tally_list(**params: Any) -> Plan:
    return plan("_action_tally_list", **params)


@verbs.command("membership-hash")
@click.argument("conv_name")
def _membership_hash(**params: Any) -> Plan:
    return plan("_action_membership_hash", **params)


def parse(argv: list[str] | None = None) -> Plan:
    try:
        chosen = verbs.main(
            args=argv, prog_name=PROG, standalone_mode=False,
        )
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from None
    except click.exceptions.Abort:
        raise SystemExit(1) from None
    if not isinstance(chosen, Plan):
        raise SystemExit(chosen if isinstance(chosen, int) else 0)
    return chosen
