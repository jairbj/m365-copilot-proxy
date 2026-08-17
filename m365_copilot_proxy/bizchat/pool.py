"""Mapping stateless OpenAI requests onto stateful BizChat conversations.

An OpenAI client resends the whole history on every request; BizChat instead keeps
the conversation server-side and charges one of its 600 message slots per turn.
Replaying the full history each time would burn that budget and throw away
Copilot's own context, so we fingerprint the conversation and send only what is
new.

The pool holds the mapping. It deliberately knows nothing about OpenAI message
shapes — the caller supplies a stable key and a message count.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field

from m365_copilot_proxy.bizchat.protocol import MAX_MESSAGES_PER_CONVERSATION
from m365_copilot_proxy.bizchat.session import CopilotSession
from m365_copilot_proxy.config import get_settings

log = logging.getLogger(__name__)


def conversation_key(model: str, first_user_message: str) -> str:
    """A stable id for a conversation: same opening message, same conversation.

    The model is part of the key because switching models mid-thread means a
    different `tone`, and mixing tones inside one server-side conversation is not
    something the web client ever does.
    """
    digest = hashlib.sha256(f"{model}\x00{first_user_message}".encode()).hexdigest()
    return digest[:32]


@dataclass
class _State:
    session: CopilotSession
    #: How many of the client's messages have already been conveyed to Copilot.
    sent_messages: int = 0
    last_used: float = field(default_factory=time.monotonic)


@dataclass
class PooledTurn:
    """The pool's answer for one incoming request."""

    session: CopilotSession
    #: Index into the client's message list from which content is still unsent.
    start_index: int
    #: True when Copilot has no context for this conversation and the whole
    #: history (system prompt included) has to be replayed.
    is_new: bool
    _pool: SessionPool
    _key: str

    def commit(self, message_count: int) -> None:
        """Record that everything up to `message_count` reached Copilot.

        Call this only after the turn succeeded: a failed turn must be resendable.
        """
        self._pool._commit(self._key, message_count)


class SessionPool:
    """Conversation fingerprint -> live BizChat session."""

    def __init__(self) -> None:
        self._states: dict[str, _State] = {}
        #: Kept apart from the states so a lock can be taken BEFORE the state
        #: exists, and survives the state being evicted underneath it.
        self._locks: dict[str, asyncio.Lock] = {}

    def acquire(self, key: str, message_count: int) -> PooledTurn:
        """Resolve the request to a session and the slice of history to send."""
        self._evict_stale()
        state = self._states.get(key)

        if state is None:
            state = _State(session=CopilotSession())
            self._states[key] = state
            log.info(
                "New conversation %s: sid=%s cid=%s",
                key,
                state.session.session_id,
                state.session.conversation_id,
            )
            return PooledTurn(state.session, 0, True, self, key)

        state.last_used = time.monotonic()

        if message_count < state.sent_messages:
            # The client rewound the thread (edited a message, restarted the chat).
            # Copilot cannot un-say anything, so the honest move is a fresh
            # conversation replaying the history the client now believes in.
            log.info(
                "Conversation %s rewound (%d < %d messages) — starting over",
                key,
                message_count,
                state.sent_messages,
            )
            state.session.reset_conversation()
            state.sent_messages = 0
            return PooledTurn(state.session, 0, True, self, key)

        headroom = get_settings().conversation_turn_headroom
        if state.session.messages_used >= MAX_MESSAGES_PER_CONVERSATION - headroom:
            # The 600-message cap is per ConversationId and is hard. Rotate before
            # hitting it and replay the history so context survives the move.
            log.info(
                "Conversation %s near the %d-message cap (%d used) — rotating",
                key,
                MAX_MESSAGES_PER_CONVERSATION,
                state.session.messages_used,
            )
            state.session.reset_conversation()
            state.sent_messages = 0
            return PooledTurn(state.session, 0, True, self, key)

        return PooledTurn(state.session, state.sent_messages, False, self, key)

    def lock_for(self, key: str) -> asyncio.Lock:
        """Serialize turns on one conversation — a session cannot run two at once.

        Take this BEFORE `acquire`: two concurrent requests on the same thread must
        not both be told they are new.
        """
        return self._locks.setdefault(key, asyncio.Lock())

    def _commit(self, key: str, message_count: int) -> None:
        state = self._states.get(key)
        if state is None:
            return
        state.sent_messages = message_count
        state.last_used = time.monotonic()

    def _evict_stale(self) -> None:
        timeout = get_settings().session_idle_timeout
        now = time.monotonic()
        stale = [
            key
            for key, state in self._states.items()
            if now - state.last_used > timeout and not self._is_busy(key)
        ]
        for key in stale:
            log.info("Evicting idle conversation %s", key)
            del self._states[key]
            self._locks.pop(key, None)

    def _is_busy(self, key: str) -> bool:
        lock = self._locks.get(key)
        return lock is not None and lock.locked()

    def clear(self) -> None:
        self._states.clear()
        self._locks.clear()

    def __len__(self) -> int:
        return len(self._states)
