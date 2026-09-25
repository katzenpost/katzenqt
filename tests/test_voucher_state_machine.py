from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import TYPE_CHECKING, cast

import pytest
from katzenpost_thinclient.pigeonhole import (
    VoucherInductResult,
    VoucherMintResult,
    VoucherOpenResult,
    VoucherStreamResult,
)
from sqlmodel import col, select

from katzenqt import persistent, voucher
from tests.fakes.thinclient import FakeThinClient

if TYPE_CHECKING:
    from katzenpost_thinclient import ThinClient

_INDEX = voucher._INDEX_LEN
_READ_CAP_LEN = 32 + _INDEX
_WRITE_CAP_LEN = 32 + _READ_CAP_LEN

JOINER_STEPS: dict[str | None, dict[str, str | None]] = {
    None: {"mint_and_publish": voucher.STEP_AWAITING},
    voucher.STEP_AWAITING: {"await_and_open": None},
}
INDUCTOR_STEPS: dict[str | None, dict[str, str | None]] = {
    None: {"derive_read_and_induct": None},
}


def _pubkey(secret: bytes) -> bytes:
    return hashlib.blake2b(secret, digest_size=32).digest()


def _mutate_read_cap(read_cap: bytes, salt: bytes) -> bytes:
    prefix = hashlib.blake2b(read_cap[:32], key=salt, digest_size=32).digest()
    return prefix + read_cap[32:]


def _fresh_write_cap() -> bytes:
    return secrets.token_bytes(64) + secrets.token_bytes(_INDEX)


class VoucherDaemon(FakeThinClient):

    def __init__(self) -> None:
        super().__init__()
        self.streams: dict[bytes, tuple[bytes, bytes, bytes]] = {}
        self.mints: list[VoucherMintResult] = []
        self.tombstoned: set[tuple[bytes, bytes]] = set()

    async def voucher_mint(
        self, message_write_cap: bytes, display_name: str,
    ) -> VoucherMintResult:
        voucher_write_cap = _fresh_write_cap()
        voucher_read_cap = voucher_write_cap[32:]
        secret = secrets.token_bytes(32)
        payload = voucher_read_cap + _pubkey(secret) + display_name.encode()
        token = hashlib.blake2b(payload, digest_size=32).digest()
        self.streams[token] = (voucher_write_cap, voucher_read_cap, message_write_cap)
        result = VoucherMintResult(
            voucher=token,
            voucher_payload=payload,
            voucher_write_cap=voucher_write_cap,
            voucher_read_cap=voucher_read_cap,
            voucher_secret_key=secret,
            voucher_public_key=_pubkey(secret),
        )
        self.mints.append(result)
        return result

    async def voucher_derive_stream(self, voucher: bytes) -> VoucherStreamResult:
        write_cap, read_cap, _ = self.streams[voucher]
        return VoucherStreamResult(
            voucher_write_cap=write_cap, voucher_read_cap=read_cap,
        )

    async def voucher_induct(
        self, voucher: bytes, voucher_payload: bytes, who_reply: bytes,
    ) -> VoucherInductResult:
        if hashlib.blake2b(voucher_payload, digest_size=32).digest() != voucher:
            raise ValueError("voucher payload does not hash to the voucher")
        write_cap, read_cap, joiner_write_cap = self.streams[voucher]
        salt = secrets.token_bytes(32)
        display_name = voucher_payload[_READ_CAP_LEN + 32:].decode()
        pubkey = voucher_payload[_READ_CAP_LEN:_READ_CAP_LEN + 32]
        return VoucherInductResult(
            display_name=display_name,
            mutated_message_read_cap=_mutate_read_cap(joiner_write_cap[32:], salt),
            sealed_reply=pubkey + salt + who_reply,
            voucher_write_cap=write_cap,
            voucher_read_cap=read_cap,
            salt=salt,
        )

    async def voucher_open(
        self, voucher_secret_key: bytes, sealed_reply: bytes,
        message_write_cap: bytes,
    ) -> VoucherOpenResult:
        if sealed_reply[:32] != _pubkey(voucher_secret_key):
            raise ValueError("sealed reply is not for this voucher key")
        salt = sealed_reply[32:64]
        mutated_read = _mutate_read_cap(message_write_cap[32:], salt)
        return VoucherOpenResult(
            who_reply=sealed_reply[64:],
            salt=salt,
            mutated_message_write_cap=message_write_cap[:32] + mutated_read,
        )


class Peer:

    def __init__(self, name: str, daemon: VoucherDaemon) -> None:
        self.name = name
        self.connection = cast("ThinClient", daemon)
        self.conversation_id = 0
        self.write_cap = _fresh_write_cap()

    async def create_view(self) -> None:
        wcw = persistent.WriteCapWAL(
            id=uuid.uuid4(), write_cap=self.write_cap,
            next_index=self.write_cap[-_INDEX:],
        )
        rcw = persistent.ReadCapWAL(
            id=uuid.uuid4(), write_cap_id=wcw.id,
            read_cap=self.write_cap[32:], next_index=self.write_cap[-_INDEX:],
        )
        convo = persistent.Conversation(name="group", write_cap=wcw.id, first_unread=0)
        own = persistent.ConversationPeer(
            name=self.name, read_cap_id=rcw.id, active=False, conversation=convo,
        )
        convo.own_peer = own
        async with persistent.asession() as sess:
            sess.add(wcw)
            sess.add(rcw)
            sess.add(convo)
            sess.add(own)
            await sess.commit()
            await sess.refresh(convo)
            self.conversation_id = convo.id

    async def pending_step(self) -> str | None:
        async with persistent.asession() as sess:
            row = (await sess.exec(
                select(persistent.PendingVoucher).where(
                    persistent.PendingVoucher.conversation_id == self.conversation_id,
                )
            )).first()
            return None if row is None else row.step

    async def members(self) -> list[tuple[str, bytes]]:
        async with persistent.asession() as sess:
            rows = (await sess.exec(
                select(persistent.ConversationPeer, persistent.ReadCapWAL)
                .join(persistent.ConversationPeerLink)
                .join(
                    persistent.ReadCapWAL,
                    col(persistent.ReadCapWAL.id) == col(persistent.ConversationPeer.read_cap_id),
                )
                .where(
                    persistent.ConversationPeerLink.conversation_id == self.conversation_id,
                    col(persistent.ConversationPeer.active).is_(True),
                )
            )).all()
            return sorted((peer.name, rcw.read_cap or b"") for peer, rcw in rows)

    async def current_write_cap(self) -> bytes:
        async with persistent.asession() as sess:
            convo = await sess.get(persistent.Conversation, self.conversation_id)
            assert convo is not None
            wcw = await sess.get(persistent.WriteCapWAL, convo.write_cap)
            assert wcw is not None and wcw.write_cap is not None
            return wcw.write_cap


async def _check_transition(
    peer: Peer, table: dict[str | None, dict[str, str | None]],
    before: str | None, action: str,
) -> None:
    assert action in table[before], (
        f"{peer.name}: {action!r} is not a legal transition from {before!r}"
    )
    expected = table[before][action]
    after = await peer.pending_step()
    assert after == expected, (
        f"{peer.name}: {action} left the row at {after!r}, expected {expected!r}"
    )


async def _induct(
    daemon: VoucherDaemon, joiner: Peer, inductor: Peer,
) -> None:
    before = await joiner.pending_step()
    token = await voucher.mint_and_publish(
        joiner.connection, joiner.conversation_id, joiner.name,
    )
    await _check_transition(joiner, JOINER_STEPS, before, "mint_and_publish")
    mint = daemon.mints[-1]
    index0 = mint.voucher_write_cap[-_INDEX:]
    assert daemon.box_store[(mint.voucher_read_cap, index0)] == mint.voucher_payload
    assert hashlib.blake2b(mint.voucher_payload, digest_size=32).digest() == token

    before = await inductor.pending_step()
    added_name = await voucher.derive_read_and_induct(
        inductor.connection, inductor.conversation_id, joiner.name, token,
    )
    await _check_transition(inductor, INDUCTOR_STEPS, before, "derive_read_and_induct")
    assert added_name == joiner.name

    before = await joiner.pending_step()
    added = await voucher.await_and_open(joiner.connection, joiner.conversation_id)
    await _check_transition(joiner, JOINER_STEPS, before, "await_and_open")
    assert inductor.name in added
    assert await voucher.voucher_used_for(joiner.conversation_id) is True

    joiner_view = dict(await inductor.members())
    assert joiner_view[joiner.name] == (await joiner.current_write_cap())[32:]


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> VoucherDaemon:
    async def _noop(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(voucher, "check_for_new", _noop)
    monkeypatch.setattr(voucher, "_wait_intro_acked", _noop)
    return VoucherDaemon()


async def _grow_star(daemon: VoucherDaemon, n: int) -> list[Peer]:
    peers = [Peer(f"peer{i:03d}", daemon) for i in range(n)]
    for peer in peers:
        await peer.create_view()
    creator = peers[0]
    for k, joiner in enumerate(peers[1:], start=1):
        await _induct(daemon, joiner, creator)
        assert len(await creator.members()) == k
        assert len(await joiner.members()) == k
    return peers


class _StarSweep:
    n: int

    @pytest.mark.asyncio
    async def test_every_peer_holds_every_other_peers_read_cap(
        self, daemon: VoucherDaemon,
    ) -> None:
        peers = await _grow_star(daemon, type(self).n)
        creator = peers[0]
        expected = {p.name: (await p.current_write_cap())[32:] for p in peers[1:]}
        assert dict(await creator.members()) == expected
        last = peers[-1]
        last_view = dict(await last.members())
        assert set(last_view) == {p.name for p in peers[:-1]}
        for other in peers[1:-1]:
            assert last_view[other.name] == (await other.current_write_cap())[32:]


class TestPeers2(_StarSweep):
    n = 2


class TestPeers3(_StarSweep):
    n = 3


class TestPeers4(_StarSweep):
    n = 4


class TestPeers8(_StarSweep):
    n = 8


class TestPeers16(_StarSweep):
    n = 16


class TestPeers32(_StarSweep):
    n = 32


class TestPeers64(_StarSweep):
    n = 64


class TestPeers128(_StarSweep):
    n = 128


@pytest.mark.nightly
class TestPeers256(_StarSweep):
    n = voucher.MAX_GROUP_MEMBERS


@pytest.mark.asyncio
async def test_a_chain_inductor_advertises_its_current_mutated_cap(
    daemon: VoucherDaemon,
) -> None:
    peers = [Peer(f"link{i}", daemon) for i in range(3)]
    for peer in peers:
        await peer.create_view()
    await _induct(daemon, peers[1], peers[0])
    await _induct(daemon, peers[2], peers[1])
    middle_view = dict(await peers[2].members())
    assert middle_view["link1"] == (await peers[1].current_write_cap())[32:]
    assert middle_view["link0"] == (await peers[0].current_write_cap())[32:]


async def _fill_group(
    daemon: VoucherDaemon, monkeypatch: pytest.MonkeyPatch, cap: int,
) -> tuple[Peer, Peer, bytes]:
    monkeypatch.setattr(voucher, "MAX_GROUP_MEMBERS", cap)
    peers = await _grow_star(daemon, cap + 1)
    creator = peers[0]
    assert len(await creator.members()) == cap
    extra = Peer("one_too_many", daemon)
    await extra.create_view()
    token = await voucher.mint_and_publish(
        extra.connection, extra.conversation_id, extra.name,
    )
    return creator, extra, token


@pytest.mark.asyncio
async def test_a_full_group_refuses_the_next_induction(
    daemon: VoucherDaemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator, extra, token = await _fill_group(daemon, monkeypatch, cap=4)
    refused = await voucher.derive_read_and_induct(
        creator.connection, creator.conversation_id, extra.name, token,
    )
    assert refused is None
    members = dict(await creator.members())
    assert len(members) == 4
    assert extra.name not in members


@pytest.mark.xfail(
    strict=True,
    reason="the sealed reply is published before the capacity check, so a "
    "refused joiner still receives every member's read cap "
    "(OPEN_ITEMS 3.26)",
)
@pytest.mark.asyncio
async def test_a_refused_joiner_is_handed_nothing(
    daemon: VoucherDaemon,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator, extra, token = await _fill_group(daemon, monkeypatch, cap=4)
    assert (
        await voucher.derive_read_and_induct(
            creator.connection,
            creator.conversation_id,
            extra.name,
            token,
        )
        is None
    )
    await voucher.await_and_open(extra.connection, extra.conversation_id)
    assert await extra.members() == []
    assert await voucher.voucher_used_for(extra.conversation_id) is False


@pytest.mark.asyncio
async def test_a_second_mint_while_one_is_pending_is_refused(
    daemon: VoucherDaemon,
) -> None:
    joiner = Peer("eager", daemon)
    await joiner.create_view()
    await voucher.mint_and_publish(joiner.connection, joiner.conversation_id, joiner.name)
    with pytest.raises(voucher.PendingVoucherExistsError):
        await voucher.mint_and_publish(
            joiner.connection, joiner.conversation_id, joiner.name,
        )


@pytest.mark.asyncio
async def test_a_joined_peer_cannot_mint_again(daemon: VoucherDaemon) -> None:
    peers = await _grow_star(daemon, 2)
    joiner = peers[1]
    with pytest.raises(voucher.AlreadyJoinedError):
        await voucher.mint_and_publish(
            joiner.connection, joiner.conversation_id, joiner.name,
        )


@pytest.mark.asyncio
async def test_a_tampered_payload_is_refused_by_induction(
    daemon: VoucherDaemon,
) -> None:
    joiner = Peer("victim", daemon)
    inductor = Peer("host", daemon)
    await joiner.create_view()
    await inductor.create_view()
    token = await voucher.mint_and_publish(
        joiner.connection, joiner.conversation_id, joiner.name,
    )
    mint = daemon.mints[-1]
    index0 = mint.voucher_write_cap[-_INDEX:]
    daemon.box_store[(mint.voucher_read_cap, index0)] = b"x" + mint.voucher_payload[1:]
    with pytest.raises(Exception):
        await voucher.derive_read_and_induct(
            inductor.connection, inductor.conversation_id, joiner.name, token,
        )
    assert await inductor.members() == []


@pytest.mark.xfail(
    strict=True,
    reason="spec/contact-vouchers.md tombstones the payload after induction; "
    "neither implementation does yet (OPEN_ITEMS 3.25)",
)
@pytest.mark.asyncio
async def test_the_voucher_payload_is_tombstoned_after_induction(
    daemon: VoucherDaemon,
) -> None:
    await _grow_star(daemon, 2)
    mint = daemon.mints[-1]
    index0 = mint.voucher_write_cap[-_INDEX:]
    assert (mint.voucher_read_cap, index0) in daemon.tombstoned
