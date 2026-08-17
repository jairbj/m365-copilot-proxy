"""Conversation pooling: fingerprints, rewinds, rotation, eviction."""

from __future__ import annotations

import time

import pytest

from m365_copilot_proxy.bizchat.pool import SessionPool, conversation_key
from m365_copilot_proxy.bizchat.protocol import MAX_MESSAGES_PER_CONVERSATION
from m365_copilot_proxy.config import get_settings


@pytest.fixture
def pool() -> SessionPool:
    return SessionPool()


def _skip_ahead(seconds: float):
    """A `time.monotonic` replacement that jumps forward by `seconds`.

    The real function is captured now — reading it through the module after
    patching would recurse into the replacement.
    """
    real = time.monotonic
    return lambda: real() + seconds


def test_same_opening_message_is_the_same_conversation():
    assert conversation_key("m365-copilot", "hi") == conversation_key("m365-copilot", "hi")


def test_different_model_is_a_different_conversation():
    # A different model means a different tone, which the web client never mixes
    # inside one server-side conversation.
    assert conversation_key("m365-copilot", "hi") != conversation_key("claude-sonnet", "hi")


def test_first_request_is_new_and_replays_from_the_start(pool: SessionPool):
    turn = pool.acquire("k", 1)
    assert turn.is_new is True
    assert turn.start_index == 0


def test_second_request_sends_only_the_tail(pool: SessionPool):
    first = pool.acquire("k", 1)
    first.commit(2)  # user message + the assistant answer Copilot produced

    second = pool.acquire("k", 3)
    assert second.is_new is False
    assert second.start_index == 2
    assert second.session is first.session


def test_uncommitted_turn_is_resent(pool: SessionPool):
    pool.acquire("k", 1)  # failed mid-turn: never committed
    again = pool.acquire("k", 1)
    assert again.start_index == 0


def test_rewound_history_starts_a_new_conversation(pool: SessionPool):
    first = pool.acquire("k", 1)
    first.commit(6)
    conversation_before = first.session.conversation_id

    # The client dropped back to a shorter history (edited or restarted the thread).
    rewound = pool.acquire("k", 2)
    assert rewound.is_new is True
    assert rewound.start_index == 0
    assert rewound.session.conversation_id != conversation_before


def test_conversation_rotates_before_the_server_side_cap(pool: SessionPool):
    turn = pool.acquire("k", 1)
    turn.commit(2)
    before = turn.session.conversation_id
    headroom = get_settings().conversation_turn_headroom
    turn.session.messages_used = MAX_MESSAGES_PER_CONVERSATION - headroom

    rotated = pool.acquire("k", 3)
    assert rotated.session.conversation_id != before
    assert rotated.is_new is True  # history is replayed into the fresh conversation
    assert rotated.session.turn_count == 0


def test_idle_conversations_are_evicted(pool: SessionPool, monkeypatch):
    turn = pool.acquire("k", 1)
    turn.commit(2)
    assert len(pool) == 1

    monkeypatch.setattr(time, "monotonic", _skip_ahead(10_000))
    pool.acquire("other", 1)
    assert "k" not in pool._states


async def test_a_busy_conversation_is_not_evicted(pool: SessionPool, monkeypatch):
    turn = pool.acquire("k", 1)
    turn.commit(2)

    async with pool.lock_for("k"):  # a turn is in flight on this conversation
        monkeypatch.setattr(time, "monotonic", _skip_ahead(10_000))
        pool.acquire("other", 1)
        assert "k" in pool._states


def test_lock_is_shared_per_conversation(pool: SessionPool):
    assert pool.lock_for("k") is pool.lock_for("k")
    assert pool.lock_for("k") is not pool.lock_for("other")
