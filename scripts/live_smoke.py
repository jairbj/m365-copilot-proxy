#!/usr/bin/env python
"""One real turn against Microsoft 365 Copilot, printed as it streams.

The unit tests run against a fake server, so this is the check that the wire
format still matches production. Run it after any protocol change:

    uv run python scripts/live_smoke.py "say hi in three words"
    uv run python scripts/live_smoke.py --model claude-sonnet "which model are you?"
    uv run python scripts/live_smoke.py --model agent:agent-1 "who are you?"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from m365_copilot_proxy.auth.tokens import NeedsLoginError, decode_jwt, get_chat_token
from m365_copilot_proxy.bizchat.protocol import (
    DEFAULT_MODEL,
    agent_for_model,
    is_agent_id,
    parse_model,
)
from m365_copilot_proxy.bizchat.session import BizChatError, CopilotSession, TurnResult


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="+", help="What to ask Copilot")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--images", action="store_true", help="Request image generation")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    try:
        token = await get_chat_token()
    except NeedsLoginError as exc:
        print(exc, file=sys.stderr)
        return 1

    base_model, work_iq = parse_model(args.model)
    # An `agent:` id has to be resolved here too, or the turn goes to plain Copilot
    # under the agent's name — which looks like a pass and proves nothing.
    agent = agent_for_model(base_model)
    if agent is None and is_agent_id(base_model):
        print(
            f"No captured agent named '{base_model}'. Run `m365-copilot-proxy capture`, "
            "open it in the chat window and send it a message.",
            file=sys.stderr,
        )
        return 1

    claims = decode_jwt(token)
    print(f"Account: {claims.upn or claims.oid}  (token valid {int(claims.seconds_remaining)}s)")
    print(f"Model:   {args.model}\n")

    session = CopilotSession()
    result = TurnResult()
    try:
        async for chunk in session.chat(
            token=token,
            text=" ".join(args.prompt),
            model=base_model,
            generate_images=args.images,
            work_iq=work_iq,
            agent=agent,
            result=result,
        ):
            print(chunk, end="", flush=True)
    except BizChatError as exc:
        print(f"\n\nTurn failed: {exc}", file=sys.stderr)
        return 1

    print("\n\n---")
    print(f"chars={len(result.text)} throttle={result.throttle} "
          f"messageType={result.message_type} turnState={result.turn_state}")
    if result.images:
        print(f"images={[i.url for i in result.images]}")
    if result.scores:
        print(f"scores={result.scores}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
