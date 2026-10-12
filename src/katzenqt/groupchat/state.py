"""A :class:`~.group.Group` as bytes, for a client to keep between runs.
Every dataclass field is kept except the codec, which is not state. Dicts
are written as lists of pairs, since their keys are bytes and tuples.
"""

from __future__ import annotations

import dataclasses
import enum
import types
import typing
from typing import Any

import cbor2

from .group import Group
from .wire import CborCodec, Codec, counter

VERSION = 2
"""Version 2 keeps acknowledgements heard, and boxes a scan looked for, by
``Idx64``; version 1 kept their whole ``MessageBoxIndex``."""


class StateError(ValueError):
    """The bytes are not a group state this version can read."""


def dump(group: Group) -> bytes:
    """
    >>> group = Group.found(bytes(136), [bytes(136)])
    >>> load(dump(group)) == group
    True
    """
    return cbor2.dumps([VERSION, _encode(group)])


def load(data: bytes, codec: Codec | None = None) -> Group:
    try:
        version, body = cbor2.loads(data)
    except (cbor2.CBORDecodeError, TypeError, ValueError) as error:
        raise StateError(str(error)) from error
    if version == 1:
        body = _from_v1(body)
    elif version != VERSION:
        raise StateError(f"group state version {version}")
    group: Group = _decode(Group, body)
    group.codec = codec or CborCodec()
    return group


def _from_v1(body: Any) -> Any:
    """A version 1 body as version 2 writes it.

    >>> group = Group.found(bytes(136), [bytes(136), bytes([1]) * 136])
    >>> group.ledger.heard[b"s"] = {b"m": 7}
    >>> (reader,) = group.readers.values()
    >>> reader.tried = [9]
    >>> v1 = lambda idx64: idx64.to_bytes(8, "little") + bytes(96)
    >>> body = _encode(group)
    >>> body["ledger"]["heard"] = [[b"s", [[b"m", v1(7)]]]]
    >>> body["readers"][0][1]["tried"] = [v1(9)]
    >>> load(cbor2.dumps([1, body])) == group
    True
    """
    for _, by in body["ledger"]["heard"]:
        for pair in by:
            pair[1] = counter(pair[1])
    for _, reader in body["readers"]:
        reader["tried"] = [counter(index) for index in reader["tried"]]
    return body


def _encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _encode(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if f.compare
        }
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return [[_encode(k), _encode(v)] for k, v in value.items()]
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _decode(kind: Any, value: Any) -> Any:
    origin = typing.get_origin(kind)
    args = typing.get_args(kind)
    if origin in (typing.Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        (only,) = [a for a in args if a is not type(None)]
        return _decode(only, value)
    if dataclasses.is_dataclass(kind):
        hints = typing.get_type_hints(kind)
        return typing.cast(Any, kind)(
            **{
                f.name: _decode(hints[f.name], value[f.name])
                for f in dataclasses.fields(kind)
                if f.compare and f.name in value
            }
        )
    if isinstance(kind, type) and issubclass(kind, enum.Enum):
        return kind(value)
    if origin is dict:
        key, item = args
        return {_decode(key, k): _decode(item, v) for k, v in value}
    if origin is list:
        return [_decode(args[0], item) for item in value]
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], item) for item in value)
        return tuple(_decode(a, item) for a, item in zip(args, value))
    return value
