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

Playwright does not yet ship wheels for every dependency on Python 3.14. If `uv`
picks it and the browser misbehaves, pin an older interpreter for the run:
`uv run --python 3.12 m365-copilot-proxy login`.

## Use

```bash
uv run m365-copilot-proxy doctor   # checks TLS reachability (see below if it fails)
uv run m365-copilot-proxy login    # opens a browser; complete SSO/MFA yourself
uv run m365-copilot-proxy status   # confirms silent refresh works
uv run m365-copilot-proxy capture  # learns your tenant's models — see below
uv run m365-copilot-proxy prompt   # the instructions to paste into an agent
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
| `magic` | Copilot's own routing (the picker's "Automatic") |
| `chat`, `reasoning` | quick answer / think deeper |
| `claude-sonnet`, `claude-opus` | Anthropic models on the Copilot subscription |
| `gpt-5.6-reasoning`, `gpt-5.6-chat` | GPT-5.6 |
| `gpt-5.5-chat` | GPT-5.5 |
| `m365-copilot-image` | image generation |

Those eight are what a real work tenant offered in August 2026, and they are what
the example configs use. Friendlier aliases (`m365-copilot`, `auto`, `quick`,
`think-deeper`, `gpt-5.5`, …) and older generations are also mapped, as the
pre-capture fallback.

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
Do the same inside one of your declarative agents to record it — see below.
Each new tone is printed as it is seen. Run it once with Work IQ on and once with it
off to record both surfaces (see below) — each run keeps the other. Close the window
when you are done and the result lands in
`~/.config/m365-copilot-proxy/profile.json`:

```json
{
  "tones": {
    "claude-sonnet": "Claude_Sonnet",
    "gpt-5.6-reasoning": "Gpt_5_6_Reasoning",
    "gpt-5.6-chat": "Gpt_5_6_Chat",
    "magic": "Magic"
  },
  "surfaces": {
    "work": { "query": { "agent": "work", "scenario": "officeweb" }, "option_sets": ["..."] },
    "web":  { "query": { "agent": "web", "scenario": "OfficeWebPaidCopilot" }, "option_sets": ["..."] }
  }
}
```

Tones sit at the top level because they are shared: the toggle changes the surface
around the models, not which models exist.

The profile wins over the built-in defaults, and the server re-reads it when it
changes — no restart. Model ids are derived from the tone (`Gpt_5_6_Chat` →
`gpt-5.6-chat`); rename the keys in that file if you prefer something else. Re-run
`capture` whenever a new model shows up in the picker: it merges by default, so
previously captured models survive.

The access token is never recorded — it lives in the WebSocket URL, and `capture`
reads every field except that one.

### Work IQ: grounding in your work content

The web client has a "Work IQ" toggle that decides whether Copilot searches your
tenant — mail, files, Teams. Here it is a **model suffix**:

```bash
claude-sonnet        # default: no work grounding
claude-sonnet-work   # searches your work content
claude-sonnet-web    # explicitly off (same as no suffix, by default)
```

Any model id takes the suffix, and `GET /v1/models` lists both variants so it shows
up in a client's model picker. `M365_WORK_IQ=1` flips the default for ids without a
suffix.

**Off by default**, because it costs latency, it can derail a coding task with a
search nobody asked for, and company content should not enter a conversation
unasked.

Under the hood the toggle is not one field — it is a whole surface. Captured from a
real tenant, with everything below changing together:

| | Work IQ on | Work IQ off |
| --- | --- | --- |
| `agent` | `work` | `web` |
| `scenario` | `officeweb` | `OfficeWebPaidCopilot` |
| `optionsSets` | `enterprise_*` family | `cwc_*` family |
| `variants` | one list | a different one |

So `capture` stores one **surface** per toggle state, and a turn serves one of them
whole. Run it twice — once with Work IQ on, once with it off — and each run files
itself under the `agent` it observes, keeping the other. With only one surface
captured the proxy uses it for both and says so in the log, rather than mixing
fields from the two, which would produce a combination no real client sends and
that the server accepts in silence.

A thread cannot change surface midway, so `claude-sonnet` and `claude-sonnet-work`
become separate Copilot conversations even with identical history.

> If you captured a profile before this existed, it migrates into whichever slot
> its `agent` names. A profile captured with Work IQ on was pinning every request
> to work grounding; after upgrading, the default becomes off.

### Declarative agents: instructions it actually follows

Copilot regularly ignores a system prompt. There is no field for one on the wire —
the proxy glues it into the first turn as a `[System instructions]` block, and the
model treats it as something a user said, which it is free to talk past. Put the same
text in the instructions of a **declarative agent** (the "Create agent" flow in the
Copilot UI) and it is honoured instead.

So: build the agent once, point the proxy at it, and every turn starts inside it.

**1. Get the text.** The proxy keeps a copy of the system prompt of each conversation
it starts, and prints it composed with the tool-calling contract:

```bash
uv run m365-copilot-proxy prompt              # latest, contract + system prompt
uv run m365-copilot-proxy prompt --list       # what has been recorded
uv run m365-copilot-proxy prompt --contract   # the tool contract alone
uv run m365-copilot-proxy prompt --raw        # the client's system prompt alone
uv run m365-copilot-proxy prompt --out agent.md
```

The document goes to stdout and its size to stderr, so it pipes cleanly:

```
  Tool calling           980 chars
  System prompt        9,210 chars
  Total               10,190 chars — 2,190 over the agent's 8,000-character field.
```

An agent's instructions field holds **8000 characters** and nothing here trims to
fit: which paragraph to drop is your call, not the proxy's. The tool contract is
~1k of that, and it is the half worth installing — it is what teaches the model to
keep calling tools across turns instead of answering after the first result.

`GET /v1/system-prompt` returns the same document as JSON (`?format=text` for the
raw body), and `GET /v1/system-prompts` lists what has been recorded. Recording is
local and on by default; `M365_RECORD_SYSTEM_PROMPTS=0` turns it off.

**2. Paste it** into the agent's instructions in the Copilot UI and save.

**3. Capture the agent**, the same way models are captured — open it in the chat
window and send it any message:

```bash
uv run m365-copilot-proxy capture
```

The agent's turn carries a `threadLevelGptId`, the opaque object that puts a thread
inside it. That object is recorded whole and replayed unread, along with the rest of
that connection's shape, under `agents` in `profile.json`:

```json
{
  "agents": {
    "agent-1": {
      "thread_level_gpt_id": { "...": "opaque" },
      "surface": { "query": { "agent": "work" }, "option_sets": ["..."] }
    }
  }
}
```

Rename `agent-1` to whatever you like — the name is not on the wire, and a later
capture recognises the agent by its id, so the rename survives.

**4. Use it** as a model id:

```bash
curl -N http://127.0.0.1:8765/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"agent:agent-1","messages":[{"role":"user","content":"hello"}]}'
```

`GET /v1/models` lists every captured agent as `agent:<name>`, so it shows up in a
client's model picker next to the ordinary ids.

**What an agent does not have.** The agent UI offers no model selector and no Work
IQ toggle, so neither does the proxy here: an `agent:` id takes no `-work` suffix,
and no `tone` is sent for it — whatever model and grounding the agent was built with
is what you get. An `agent:` id that was never captured fails the turn rather than
quietly serving plain Copilot under the agent's name.

Because the agent carries the instructions, an agent turn does **not** repeat the
`[System instructions]` block or the tool contract; only the per-request tool list
still travels, since the agent cannot know it. If you change the client's prompt and
have not updated the agent yet, `M365_AGENT_SEND_SYSTEM=1` puts both back inline, so
the turn carries what a plain-Copilot one would.

**Creating the agent from here** is not supported: it is a browser flow with no
documented API. `capture --record-api` writes the site's own write calls (method,
URL, body — never headers, which is where the token is) to
`<config_dir>/capture/agent-builder-*.ndjson`, which is the evidence needed to judge
whether that could ever change. Nothing replays them.

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
project (applies there and wins over the global one). It ships every model in both
surfaces — each id and its `-work` twin — so `/models` is ready to use as-is; the
excerpt below is shortened:

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

## Using it with pi

[pi](https://pi.dev) reads custom providers from `~/.pi/agent/models.json`. Copy
[`examples/pi-models.json`](examples/pi-models.json) there. Like the opencode one,
it lists every model in both surfaces, so `/model` is ready to use as-is; the
excerpt below is shortened:

```json
{
  "providers": {
    "m365": {
      "baseUrl": "http://127.0.0.1:8765/v1",
      "api": "openai-completions",
      "apiKey": "unused",
      "compat": {
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": false,
        "supportsUsageInStreaming": false
      },
      "models": [
        {
          "id": "claude-sonnet",
          "name": "M365 Claude Sonnet",
          "contextWindow": 128000,
          "maxTokens": 16384,
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
        },
        {
          "id": "claude-sonnet-work",
          "name": "M365 Claude Sonnet (Work IQ)",
          "contextWindow": 128000,
          "maxTokens": 16384,
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
        }
      ]
    }
  }
}
```

Start the proxy, then `pi --list-models` shows the entries and `/model` picks one.
The file reloads every time you open `/model`, so you can edit it mid-session.

What the fields mean:

* **`api: "openai-completions"`** — the API shape this proxy speaks.
* **`apiKey`** — pi hides models with no auth configured, so a keyless local server
  needs a dummy value. The proxy ignores it and authenticates to Microsoft with
  your cached token.
* **`cost`** — zeroed on purpose: the subscription already paid for these turns.
* **`compat`** — not decoration, see below.

### Why those `compat` flags

* **`supportsDeveloperRole: false`** — for models flagged `reasoning`, pi sends the
  system prompt under the newer `developer` role. The proxy now treats `developer`
  as `system`, so either way works, but this is the switch pi itself documents.
* **`supportsReasoningEffort: false`** — Copilot picks reasoning by *model*
  (`reasoning`, `gpt-5.6-reasoning`), not by a per-request effort parameter. We
  accept and ignore `reasoning_effort`; saying so keeps pi from showing a control
  that does nothing.
* **`supportsUsageInStreaming: false`** — our SSE chunks carry no `usage` payload
  (the non-streaming response does).

The models are deliberately **not** marked `reasoning: true`: nothing thinking-shaped
ever comes back, so pi would show a thinking UI that stays empty.

### Caveats

Same as opencode — emulated tool calls, no incremental streaming when `tools` are
declared, the 600-message conversation budget, and no image input. If a very long
turn ever times out, the knobs are `httpIdleTimeoutMs` (default `300000`) and
`retry.provider.timeoutMs` in `~/.pi/agent/settings.json`.

## Corporate TLS interception

If `login` or `doctor` fails like this:

```
SSLCertVerificationError: certificate verify failed: unable to get local issuer certificate
```

your network is inspecting TLS: a proxy terminates the connection and re-signs it
with your company's root CA. The certificate is valid *for your machine* — but
Python does not read the operating system's certificate store by default, so it
never learns about that root.

Start by finding out where you stand:

```bash
uv run m365-copilot-proxy doctor
```

It prints which bundle is in use and tests both endpoints the proxy needs. They go
through different TLS stacks — MSAL uses `requests`, the chat uses a raw socket —
so one can pass while the other fails.

**If both checks pass**, nothing to do: the company root is already in your system
trust store and the proxy picks it up automatically.

**If they fail**, point the proxy at the root explicitly. On WSL the root usually
lives in Windows, not in the Linux distro, so export it first:

```powershell
certutil -store Root                      # find your company's root by name
certutil -store Root "<name>" corp.cer    # export it
certutil -encode corp.cer corp.pem        # convert to PEM
```

From WSL that file is under `/mnt/c/...` — wherever you ran `certutil`. Then
combine it with the public CAs and point the proxy at the result:

```bash
cat "$(uv run python -c 'import certifi; print(certifi.where())')" corp.pem > ~/corp-bundle.pem
export M365_CA_BUNDLE=~/corp-bundle.pem
uv run m365-copilot-proxy doctor
```

Concatenating matters: a bundle containing *only* the company root breaks every
host that is not being intercepted.

### The browser is separate

Chromium keeps its own certificate store and cannot be pointed at a PEM, so the
login and capture windows need their own fix. Import the root into NSS (needs
`libnss3-tools`):

```bash
certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n corp -i corp.pem
```

Or, as a last resort, `M365_BROWSER_IGNORE_TLS_ERRORS=1`. That turns off
certificate checking for the windows this tool opens — which only ever visit
Microsoft domains, on a network that is already inspecting the traffic — but it is
off by default because it is still a real reduction in verification.

### Explicit proxies

`HTTPS_PROXY` / `NO_PROXY` are honoured throughout: `requests` and `httpx` read
them directly, and `websockets` uses the system proxy configuration by default.

## Configuration

Everything is settable through `M365_*` environment variables or a `.env` file:

| Variable | Default | Purpose |
| --- | --- | --- |
| `M365_HOST` / `M365_PORT` | `127.0.0.1` / `8765` | Bind address |
| `M365_CONFIG_DIR` | `~/.config/m365-copilot-proxy` | Token cache, browser profile, tenant profile |
| `M365_LOGIN_TIMEOUT` | `600` | Seconds to wait for the sign-in |
| `M365_CHROMIUM_PATH` | *(bundled)* | Use a system Chromium instead |
| `M365_CA_BUNDLE` | *(system trust)* | PEM to verify certificates against |
| `M365_BROWSER_IGNORE_TLS_ERRORS` | `false` | Let the browser skip certificate checks |
| `M365_TURN_TIMEOUT` | `300` | Seconds of silence before a turn fails |
| `M365_SESSION_IDLE_TIMEOUT` | `1800` | Idle seconds before a conversation is evicted |
| `M365_OUTPUT_CHAR_CEILING` | `12000` | Report `finish_reason: "length"` above this; `0` disables |
| `M365_IMAGES_ALWAYS` | `false` | Enable image generation on every turn |
| `M365_WORK_IQ` | `false` | Ground answers in work content by default |
| `M365_RECORD_SYSTEM_PROMPTS` | `true` | Keep each conversation's system prompt for `prompt` |
| `M365_AGENT_SEND_SYSTEM` | `false` | Send the system block and tool contract on an agent turn |
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
