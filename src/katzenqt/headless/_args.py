from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Offline:
    action: str


@dataclass(frozen=True)
class Connected(Offline):
    config: "str | None"
    address: "str | None"
    network: str


@dataclass(frozen=True)
class CreateConv(Connected):
    conv_name: str
    own_display: str


@dataclass(frozen=True)
class VoucherMint(Connected):
    conv_name: str
    display_name: str


@dataclass(frozen=True)
class VoucherInduct(Connected):
    conv_name: str
    peer_name: str
    voucher_b64: str


@dataclass(frozen=True)
class VoucherAwait(Connected):
    conv_name: str


@dataclass(frozen=True)
class Send(Connected):
    conv_name: str
    text: str
    timeout: "float | None"


@dataclass(frozen=True)
class MultiSend(Connected):
    conv_name: str
    texts: str


@dataclass(frozen=True)
class Read(Connected):
    conv_name: str
    timeout_s: float
    expected_text: "str | None"


@dataclass(frozen=True)
class ChatSession(Connected):
    conv_name: str
    steps: "list[str]"


@dataclass(frozen=True)
class SendFile(Connected):
    conv_name: str
    path: str
    basename: "str | None"
    filetype: "str | None"
    timeout: "float | None"


@dataclass(frozen=True)
class ReadFile(Connected):
    conv_name: str
    to_dir: str
    timeout: float
    basename: "str | None"


@dataclass(frozen=True)
class Info(Offline):
    pass


@dataclass(frozen=True)
class TallyCreate(Connected):
    conv_name: str
    topic: str
    mode: str
    slot: "list[str]"
    timeout: float


@dataclass(frozen=True)
class TallyVote(Connected):
    conv_name: str
    survey: str
    slot: "list[str]"
    timeout: float


@dataclass(frozen=True)
class TallyResult(Connected):
    conv_name: str
    survey: str
    expect_voters: "int | None"
    timeout: float


@dataclass(frozen=True)
class TallyClose(Connected):
    conv_name: str
    survey: str
    timeout: float


@dataclass(frozen=True)
class TallyList(Offline):
    conv_name: str


@dataclass(frozen=True)
class MembershipHash(Offline):
    conv_name: str
