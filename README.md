# m365-copilot-proxy

An OpenAI-compatible HTTP proxy in front of **Microsoft 365 Copilot (BizChat)**.

Point any OpenAI client — the `openai` SDK, Cursor, Cline, Open WebUI, `curl` — at
`http://127.0.0.1:8765/v1` and it talks to the Copilot your organisation already
pays for.

It is built for one person on one machine: you sign in once with your own account,
and the proxy runs locally under your identity. There is no multi-user mode and no
shared deployment story, by design.

> **Unofficial.** This speaks Copilot's internal web protocol, which Microsoft does
> not document and can change without notice. Check your organisation's acceptable
> use policy before running it — you are using your own account, and everything you
> send is subject to the same tenant policies, auditing and data handling as the
> Copilot web app. No warranty; see [LICENSE](LICENSE).

## How it works

```
OpenAI client ──HTTP──▶ this proxy ──WebSocket (SignalR)──▶ substrate.office.com
                            │
                            └── MSAL: silent token refresh, browser only at first login
```

* **Auth** — a one-time interactive sign-in in a real browser window (Playwright),
  using Microsoft's own first-party Office Copilot client id with PKCE. The
  refresh token is then cached locally, so every later start acquires tokens
  silently over HTTP with no browser running.
* **Chat** — each turn opens a WebSocket to the BizChat Chathub and speaks its
  SignalR JSON framing, streaming the answer back as OpenAI SSE chunks.
* **Conversations** — Copilot keeps conversation state server-side, so the proxy
  fingerprints each thread and sends only the messages the client added since the
  last turn. That preserves Copilot's own context and conserves the 600-message
  budget it allows per conversation.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/jairbj/m365-copilot-proxy
cd m365-copilot-proxy
uv sync
uv run playwright install chromium   # only used for the login window
```

## Use

```bash
uv run m365-copilot-proxy login    # opens a browser; complete SSO/MFA yourself
uv run m365-copilot-proxy status   # confirms silent refresh works
uv run m365-copilot-proxy capture  # learns your tenant's models — see below
uv run m365-copilot-proxy serve    # http://127.0.0.1:8765/v1
```

Then:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="unused")
response = client.chat.completions.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "Explain SignalR in two sentences."}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

Or with curl:

```bash
curl -N http://127.0.0.1:8765/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"m365-copilot","stream":true,
       "messages":[{"role":"user","content":"hello"}]}'
```

The proxy ignores the API key — it binds to loopback only and authenticates to
Microsoft with your cached token, not with anything the client sends.

## Models

Copilot picks its backing model from a `tone` field, so each model id here maps to
one tone. `GET /v1/models` lists them all.

| Model id | Backing model |
| --- | --- |
| `m365-copilot`, `auto` | Copilot's own routing |
| `quick`, `think-deeper` | GPT fast / reasoning |
| `claude-sonnet`, `claude-opus` | Anthropic models on the Copilot subscription |
| `claude-sonnet-think-deeper` | Claude reasoning |
| `gpt-5.5`, `gpt-5.5-think-deeper` | GPT-5.5 |
| `gpt-5.6-think-deeper`, `gpt-5.6-quick` | GPT-5.6 |
| `gpt-5.4`, `gpt-5.3`, `gpt-5.2` (+ `-quick` / `-think-deeper`) | older generations |
| `m365-copilot-image` | image generation |

The server validates tones: an id that maps to an unknown tone fails the turn. An
unmapped `claude-*` id routes to the Claude tone rather than silently serving GPT
under a Claude name.

### Learning your tenant's models: `capture`

That table is a **default, not a fact**. It describes one tenant at one moment, and
Microsoft changes both the model line-up and the surface parameters. Your tenant
almost certainly offers a different set — and a `tone` the server does not recognise
fails the turn outright, so guessing is expensive.

`capture` stops the guessing by watching the real client:

```bash
uv run m365-copilot-proxy capture
```

It opens Microsoft 365 Copilot in the browser profile you already signed in with,
then reads two things off the wire: the Chathub URL's query fields (which encode
your licence surface — a *work* tenant sends `agent=work`, `scenario=officeweb`,
`licenseType=Premium`, an individual one does not) and each `type:4` chat
invocation, which carries the `tone` behind the entry you picked in the model
selector.

So: **pick a model, send any short message, wait for the reply to start, repeat.**
Each new tone is printed as it is seen. Close the window when you are done and the
result lands in `~/.config/m365-copilot-proxy/profile.json`:

```json
{
  "query": { "agent": "work", "scenario": "officeweb", "licenseType": "Premium" },
  "tones": {
    "gpt-5.6-quick": "Gpt_5_6_Quick",
    "claude-sonnet": "Claude_Sonnet"
  }
}
```

The profile wins over the built-in defaults, and the server re-reads it when it
changes — no restart. Model ids are derived from the tone (`Gpt_5_6_Quick` →
`gpt-5.6-quick`); rename the keys in that file if you prefer something else. Re-run
`capture` whenever a new model shows up in the picker: it merges by default, so
previously captured models survive.

The access token is never recorded — it lives in the WebSocket URL, and `capture`
reads every field except that one.

### Tool calling

BizChat has no function-calling API, so tools are **emulated**: the definitions are
described in the prompt and the reply is parsed back into `tool_calls`. It works
well enough for agentic clients but it is heuristic — a model can ignore the
contract. Requests carrying `tools` are answered without incremental streaming,
because half a tool call must never reach the client.

### Image generation

Use the `m365-copilot-image` model (or set `M365_IMAGES_ALWAYS=1`). Generated
images come back as markdown with an inlined data URI, so any client can render
them without a second authenticated fetch.

## Using it with opencode

Copy [`examples/opencode.json`](examples/opencode.json) to
`~/.config/opencode/opencode.json` (applies everywhere) or to `opencode.json` in a
project (applies there and wins over the global one):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "m365": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Microsoft 365 Copilot",
      "options": {
        "baseURL": "http://127.0.0.1:8765/v1",
        "apiKey": "unused",
        "timeout": 600000,
        "chunkTimeout": 300000
      },
      "models": {
        "claude-sonnet": {
          "name": "M365 · Claude Sonnet",
          "limit": { "context": 128000, "output": 16384 }
        },
        "gpt-5.6-think-deeper": {
          "name": "M365 · GPT 5.6 Reasoning",
          "limit": { "context": 128000, "output": 16384 }
        }
      }
    }
  },
  "model": "m365/claude-sonnet",
  "small_model": "m365/quick"
}
```

Start the proxy (`uv run m365-copilot-proxy serve`), run `opencode`, and `/models`
will list the `m365/*` entries. You do **not** need `/connect`: the key is inline,
and the proxy ignores it anyway — it authenticates to Microsoft with your cached
token.

What the fields mean:

* **`npm`** — `@ai-sdk/openai-compatible`, because this proxy speaks
  `/v1/chat/completions`. (`@ai-sdk/openai` is for providers using `/v1/responses`.)
* **`models`** — the ids must match what `GET /v1/models` returns. Use the ones your
  `capture` run produced; the example lists the ones a work tenant typically offers.
* **`limit`** — opencode pulls context sizes from models.dev for known providers,
  but a custom one has to declare them or it cannot tell how much context is left.
  The values here are estimates: Microsoft publishes no context window.
* **`timeout` / `chunkTimeout`** — deliberately generous, see the streaming note
  below.

**Why `claude-sonnet` as the default.** In the reference implementation, the
default GPT tone did not reliably call tools on real agentic tasks — it would
confabulate that it had no shell instead of running one — while the Claude tone
called them consistently. If your tenant behaves differently, any id from
`/v1/models` works.

### Caveats for agentic use

* **Tool calls are emulated**, not native. The model is taught a fenced-block
  contract in the prompt; it usually follows it, sometimes it doesn't. Expect the
  occasional turn where it answers in prose instead of calling the tool.
* **No incremental streaming when tools are declared** — which, in opencode, is
  always. The proxy must see the whole reply before it can tell a tool call from
  prose, so the answer arrives in one piece. It does open the stream immediately so
  the connection is not mistaken for a stall, but a reasoning model can still take a
  minute to reply; hence the generous `timeout`/`chunkTimeout`.
* **The 600-message conversation budget goes fast.** Every opencode step — each tool
  result, each follow-up — is one message. Long sessions will hit the rotation.
* **No file or image input.** Non-text content parts are dropped, so dragging an
  image into the chat sends only the text around it.

## Configuration

Everything is settable through `M365_*` environment variables or a `.env` file:

| Variable | Default | Purpose |
| --- | --- | --- |
| `M365_HOST` / `M365_PORT` | `127.0.0.1` / `8765` | Bind address |
| `M365_CONFIG_DIR` | `~/.config/m365-copilot-proxy` | Token cache, browser profile, tenant profile |
| `M365_LOGIN_TIMEOUT` | `600` | Seconds to wait for the sign-in |
| `M365_CHROMIUM_PATH` | *(bundled)* | Use a system Chromium instead |
| `M365_TURN_TIMEOUT` | `300` | Seconds of silence before a turn fails |
| `M365_SESSION_IDLE_TIMEOUT` | `1800` | Idle seconds before a conversation is evicted |
| `M365_OUTPUT_CHAR_CEILING` | `12000` | Report `finish_reason: "length"` above this; `0` disables |
| `M365_IMAGES_ALWAYS` | `false` | Enable image generation on every turn |
| `M365_DUMP_FRAMES` | `false` | Write every SignalR frame to `<config_dir>/frames/` |
| `M365_LOG_LEVEL` | `INFO` | Logging level |

## Known limits

* **600 messages per conversation.** A hard server-side cap; the proxy rotates to a
  fresh conversation shortly before hitting it and replays the history.
* **Throttling follows your identity**, not the token — signing in again does not
  reset it.
* **Output is soft-capped** around 12k characters, and Copilot *concludes early*
  rather than truncating, so a long answer comes back looking complete but isn't.
  The proxy reports `finish_reason: "length"` past that ceiling so harnesses can
  ask for a continuation.
* **No sampling controls.** `temperature`, `top_p`, `seed` and friends are accepted
  and ignored, because BizChat exposes nothing to map them onto.
* **Safety refusals** surface as a `Disengaged` turn; the proxy says so explicitly
  instead of returning a blank answer.
* **The wire format is undocumented and moves.** Model tones, feature variants and
  the licence surface all change without notice — `capture` exists precisely because
  the built-in defaults will drift out of date.

## Security

`~/.config/m365-copilot-proxy` holds a refresh token (`msal-cache.json`, mode 0600)
and the browser profile with your Entra session cookies. Treat that directory like
a password store; `m365-copilot-proxy logout` clears both. The access token travels
in the WebSocket query string as the protocol requires, and every log line that
touches that URL is redacted.

## Development

```bash
uv run pytest
uv run ruff check .
uv run python scripts/live_smoke.py "say hi in three words"   # one real turn
```

## Prior art

The BizChat protocol here was pieced together from two open-source
implementations, both worth reading:
[`cramt/m365-copilot-proxy`](https://github.com/cramt/m365-copilot-proxy) (the auth
flow and the mandatory `Metrics` frame) and
[`diegosouzapw/OmniRoute`](https://github.com/diegosouzapw/OmniRoute) (the option
sets, variants and tone mapping).
