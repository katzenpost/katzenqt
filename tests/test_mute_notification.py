from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from katzenqt import katzen, persistent  # noqa: E402

from tests.test_katzen_gui_common import (  # noqa: E402,F401
    add_seeded_conversation,
    boxes,
    cap_of,
    fresh_queues,
    instant_timer,
    loaded_window,
    qt_app,
    seed_conversation,
    systray_of,
    window,
)


async def _background_state(
    loaded_window: katzen.MainWindow,
) -> katzen.ConversationUIState:
    other = await seed_conversation(name="background room", own_name="me2")
    await add_seeded_conversation(loaded_window, other.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    return next(
        state
        for cid, state in loaded_window.conversation_state_by_id.items()
        if cid != other.conversation_id
    )


@pytest.mark.asyncio
async def test_a_muted_background_conversation_raises_no_notification(
    loaded_window: katzen.MainWindow,
) -> None:
    state = await _background_state(loaded_window)
    await persistent.set_muted(state.conversation_id, muted=True)
    await loaded_window._process_conversation_update(
        state.conversation_id, False,
    )
    assert systray_of(loaded_window).new_messages == 0


@pytest.mark.asyncio
async def test_unmuting_restores_the_notification(
    loaded_window: katzen.MainWindow,
) -> None:
    state = await _background_state(loaded_window)
    await persistent.set_muted(state.conversation_id, muted=True)
    await persistent.set_muted(state.conversation_id, muted=False)
    await loaded_window._process_conversation_update(
        state.conversation_id, False,
    )
    assert systray_of(loaded_window).new_messages == 1
