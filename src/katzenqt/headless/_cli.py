from __future__ import annotations

import math
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

import click

from . import _actions, _args

A = TypeVar("A", bound=_args.Offline)
Action = Callable[[A], Coroutine[Any, Any, int]]

PROG = "katzenqt-headless"

PATH = click.Path(path_type=str, readable=False)


@dataclass(frozen=True)
class Plan(Generic[A]):
    func: Action[A]
    args: A

    def run(self) -> "Coroutine[Any, Any, int]":
        return self.func(self.args)


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


def require_one(config: "str | None", address: "str | None") -> None:
    if (config is None) == (address is None):
        raise click.UsageError(
            "exactly one of --config and --address is required",
            ctx=click.get_current_context(),
        )


def verb() -> str:
    return str(click.get_current_context().info_name)


def plan(action: str, args: A) -> "Plan[A]":
    return Plan(cast("Action[A]", getattr(_actions, action)), args)


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
def _create_conv(
    config: "str | None", address: "str | None", network: str,
    conv_name: str, own_display: str,
) -> "Plan[_args.CreateConv]":
    require_one(config, address)
    return plan("_action_create_conv", _args.CreateConv(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name, own_display=own_display,
    ))


@verbs.command("voucher-mint")
@connection
@click.argument("conv_name")
@click.argument("display_name")
def _voucher_mint(
    config: "str | None", address: "str | None", network: str,
    conv_name: str, display_name: str,
) -> "Plan[_args.VoucherMint]":
    require_one(config, address)
    return plan("_action_voucher_mint", _args.VoucherMint(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name, display_name=display_name,
    ))


@verbs.command("voucher-induct")
@connection
@click.argument("conv_name")
@click.argument("peer_name")
@click.argument("voucher_b64")
def _voucher_induct(
    config: "str | None", address: "str | None", network: str,
    conv_name: str, peer_name: str, voucher_b64: str,
) -> "Plan[_args.VoucherInduct]":
    require_one(config, address)
    return plan("_action_voucher_induct", _args.VoucherInduct(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name, peer_name=peer_name,
        voucher_b64=voucher_b64,
    ))


@verbs.command("voucher-await")
@connection
@click.argument("conv_name")
def _voucher_await(
    config: "str | None", address: "str | None", network: str,
    conv_name: str,
) -> "Plan[_args.VoucherAwait]":
    require_one(config, address)
    return plan("_action_voucher_await", _args.VoucherAwait(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name,
    ))


@verbs.command("send")
@connection
@click.argument("conv_name")
@click.argument("text")
@click.option(
    "--timeout", type=seconds, default=None,
    help="seconds to wait for delivery; default scales with message size",
)
def _send(
    config: "str | None", address: "str | None", network: str,
    conv_name: str, text: str, timeout: "float | None",
) -> "Plan[_args.Send]":
    require_one(config, address)
    return plan("_action_send", _args.Send(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name, text=text, timeout=timeout,
    ))


@verbs.command(
    "multi-send",
    help="queue a pipe-separated list of texts and wait for the last ACK",
)
@connection
@click.argument("conv_name")
@click.argument("texts")
def _multi_send(
    config: "str | None", address: "str | None", network: str,
    conv_name: str, texts: str,
) -> "Plan[_args.MultiSend]":
    require_one(config, address)
    return plan("_action_multi_send", _args.MultiSend(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name, texts=texts,
    ))


@verbs.command("read")
@connection
@click.argument("conv_name")
@click.argument("timeout_s", type=float)
@click.argument("expected_text", required=False, default=None)
def _read(
    config: "str | None", address: "str | None", network: str,
    conv_name: str, timeout_s: float, expected_text: "str | None",
) -> "Plan[_args.Read]":
    require_one(config, address)
    return plan("_action_read", _args.Read(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name, timeout_s=timeout_s,
        expected_text=expected_text,
    ))


@verbs.command(
    "chat-session",
    help="run steps like SEND:text, READ:text, SLEEP:seconds in one session",
)
@connection
@click.argument("conv_name")
@click.argument("steps", nargs=-1, required=True)
def _chat_session(
    config: "str | None", address: "str | None", network: str,
    conv_name: str, steps: "tuple[str, ...]",
) -> "Plan[_args.ChatSession]":
    require_one(config, address)
    return plan("_action_chat_session", _args.ChatSession(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name, steps=list(steps),
    ))


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
def _send_file(
    config: "str | None", address: "str | None", network: str,
    conv_name: str, path: str, basename: "str | None",
    filetype: "str | None", timeout: "float | None",
) -> "Plan[_args.SendFile]":
    require_one(config, address)
    return plan("_action_send_file", _args.SendFile(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name, path=path,
        basename=basename, filetype=filetype, timeout=timeout,
    ))


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
def _read_file(
    config: "str | None", address: "str | None", network: str,
    conv_name: str, to_dir: str, timeout: float, basename: "str | None",
) -> "Plan[_args.ReadFile]":
    require_one(config, address)
    return plan("_action_read_file", _args.ReadFile(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name, to_dir=to_dir,
        timeout=timeout, basename=basename,
    ))


@verbs.command(
    "info",
    help="emit one line of JSON describing the state file's schema, "
         "conversations, and outstanding WAL counts",
)
def _info() -> "Plan[_args.Info]":
    return plan("_action_info", _args.Info(action=verb()))


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
def _tally_create(
    config: "str | None", address: "str | None", network: str,
    conv_name: str, topic: str, mode: str, slot: "tuple[str, ...]",
    timeout: float,
) -> "Plan[_args.TallyCreate]":
    require_one(config, address)
    return plan("_action_tally_create", _args.TallyCreate(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name, topic=topic, mode=mode,
        slot=list(slot), timeout=timeout,
    ))


@verbs.command("tally-vote")
@connection
@click.argument("conv_name")
@click.option("--survey", required=True, help="survey id in hex")
@click.option(
    "--slot", multiple=True, required=True,
    help="a per-slot vote SLOT_ID=availability; repeat per slot",
)
@click.option("--timeout", type=float, default=600.0)
def _tally_vote(
    config: "str | None", address: "str | None", network: str,
    conv_name: str, survey: str, slot: "tuple[str, ...]", timeout: float,
) -> "Plan[_args.TallyVote]":
    require_one(config, address)
    return plan("_action_tally_vote", _args.TallyVote(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name, survey=survey,
        slot=list(slot), timeout=timeout,
    ))


@verbs.command("tally-result")
@connection
@click.argument("conv_name")
@click.option("--survey", required=True, help="survey id in hex")
@click.option("--expect-voters", type=int, default=None)
@click.option("--timeout", type=float, default=600.0)
def _tally_result(
    config: "str | None", address: "str | None", network: str,
    conv_name: str, survey: str, expect_voters: "int | None", timeout: float,
) -> "Plan[_args.TallyResult]":
    require_one(config, address)
    return plan("_action_tally_result", _args.TallyResult(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name, survey=survey,
        expect_voters=expect_voters, timeout=timeout,
    ))


@verbs.command("tally-close")
@connection
@click.argument("conv_name")
@click.option("--survey", required=True, help="survey id in hex")
@click.option("--timeout", type=float, default=600.0)
def _tally_close(
    config: "str | None", address: "str | None", network: str,
    conv_name: str, survey: str, timeout: float,
) -> "Plan[_args.TallyClose]":
    require_one(config, address)
    return plan("_action_tally_close", _args.TallyClose(
        action=verb(), config=config, address=address, network=network, conv_name=conv_name, survey=survey,
        timeout=timeout,
    ))


@verbs.command("tally-list")
@click.argument("conv_name")
def _tally_list(conv_name: str) -> "Plan[_args.TallyList]":
    return plan("_action_tally_list", _args.TallyList(
        action=verb(), conv_name=conv_name,
    ))


@verbs.command("membership-hash")
@click.argument("conv_name")
def _membership_hash(conv_name: str) -> "Plan[_args.MembershipHash]":
    return plan("_action_membership_hash", _args.MembershipHash(
        action=verb(), conv_name=conv_name,
    ))


def parse(argv: "list[str] | None" = None) -> "Plan[Any]":
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
