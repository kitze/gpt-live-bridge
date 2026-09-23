# gpt-live-bridge

Bridges **Twilio Media Streams** (μ-law 8 kHz over WSS) ↔ **gpt-live** on Kitze `codex-lb` (ChatGPT subscription via `sk-clb-…`, **not** OpenAI API credits). No Vapi.

## Public URLs (final)

| Role | URL |
|------|-----|
| **Public (Twilio) — preferred** | `https://gpt-live-bridge.exposed.kitze.io` |
| Public alias (Universal SSL) | `https://gpt-live-bridge.kitze.io` (same Worker) |
| Voice webhook | `https://gpt-live-bridge.exposed.kitze.io/twilio/voice` |
| Media WSS | `wss://gpt-live-bridge.exposed.kitze.io/twilio/media` |
| Health | `https://gpt-live-bridge.exposed.kitze.io/health` |
| Internal (Coolify / Tailscale) | `https://gpt-live-bridge.service.beast.kitze.io` |
| Origin (chicken funnel) | `https://traefik.chicken-galaxy.ts.net:10000` (Worker ORIGIN) |

`PUBLIC_BASE_URL` must be the **exposed** hostname so TwiML `<Stream>` points at a Twilio-reachable WSS.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness JSON (+ tool allowlist) |
| POST/GET | `/twilio/voice` | TwiML `<Connect><Stream>` |
| WSS | `/twilio/media` | Twilio Media Streams |
| GET | `/smoke/create-call` | Create a real aiortc offer → codex-lb (no Twilio) |
| POST | `/outbound` | Scaffold outbound call `{to, agent, mission, opening}` |

## Env

| Var | Default | Notes |
|-----|---------|-------|
| `CODEX_LB_URL` | `https://codex-lb.service.beast.kitze.io` | |
| `CODEX_LB_API_KEY` | — | **Required.** `sk-clb-…` (HomeBrain key allows `gpt-live-1-codex`) |
| `LIVE_MODEL` | `gpt-live-1-codex` | |
| `LIVE_VOICE` | `cove` | |
| `LIVE_INSTRUCTIONS` | short helpful assistant | |
| `PUBLIC_BASE_URL` | `https://gpt-live-bridge.exposed.kitze.io` | Used for TwiML WSS URL and Twilio signature validation |
| `PORT` | `8080` | |
| **`TWILIO_AUTH_TOKEN`** | — | **Required for production.** Validates `X-Twilio-Signature` on `/twilio/voice`. **Fails closed** when set. Also used for `/outbound` calls. |
| `TWILIO_ACCOUNT_SID` | — | Required for `/outbound` scaffold |
| **`BRIDGE_SHARED_SECRET`** | — | **Required for production.** Protects `/smoke/create-call` and `/outbound` from unauthorized gpt-live usage. Accepts `X-Bridge-Secret` header or `?secret=` query param. Alias: `BRIDGE_TOKEN`. **Fails closed** on sensitive endpoints. |
| `TWILIO_SKIP_SIGNATURE` | `0` | **Emergency only.** Set `1` to disable Twilio signature validation (not recommended). |
| `ICE_SERVERS_JSON` | Google STUN | Optional RTCIceServer JSON list (add TURN if needed) |
| `BARGE_IN_ENABLED` | `1` | On user energy / Twilio clear → `response.cancel` + Twilio `clear` |
| `BARGE_IN_RMS` | `500` | PCM16 RMS threshold while agent speaking |
| `ALMANAC_URL` | Almanac on beast | Calendar agenda HTTP |
| `DAYFOLD_URL` | dayfold.service.beast | Optional day read |
| `MCP_HTTP_BRIDGE_URL` | — | POST `/tools/invoke` for Executor/Skillbox/Beeper |
| `BEEPER_BRIDGE_URL` | — | Alias for Beeper read tools |

## Tools (client delegation)

Session uses `delegation: { type: "client" }`. On `session.delegation.created` the bridge:

1. Reads recent input transcript
2. Runs allowlisted tools
3. Returns `session.commentary.append` / `session.thinking.append`

**Allowlist (safe reads):**

- `calendar_agenda` — Almanac `POST /api/google-calendar/agenda`
- `dayfold_read_day` — Dayfold / MCP bridge
- `beeper_list_chats` — MCP bridge (read-only)
- `beeper_list_messages` — MCP bridge (read-only)

**Beeper send is blocked** until Kitze explicitly approves.

### Expand the allowlist

1. Add a `ToolSpec` in `app/tools.py` `ALLOWLIST`
2. Implement a runner in `RUNNERS` **or** expose it on `MCP_HTTP_BRIDGE_URL` as `POST /tools/invoke` `{tool, arguments}`
3. Optionally teach `pick_tools_from_transcript` new keywords
4. Redeploy

## Security & Authentication

### Twilio signature validation

When `TWILIO_AUTH_TOKEN` is set, `/twilio/voice` **validates** the `X-Twilio-Signature` header using Twilio's official `RequestValidator`. 

- **Fails closed**: invalid/missing signature → HTTP 403
- Tries multiple URL candidates to handle proxy/Worker rewrites (Cloudflare Worker → chicken funnel origin)
- Handles CallToken double-encoding edge cases
- Emergency escape: `TWILIO_SKIP_SIGNATURE=1` (not recommended for production)

### Bridge shared secret

`BRIDGE_SHARED_SECRET` (alias `BRIDGE_TOKEN`) protects non-Twilio endpoints from unauthorized gpt-live usage:

- **Required endpoints** (fail closed if not set OR wrong):
  - `GET /smoke/create-call` — dev/test endpoint
  - `POST /outbound` — outbound call scaffold
- **Optional for** `GET /twilio/media` — backward compat (secret passed in stream URL by `/twilio/voice`)
- **Auth methods:** `X-Bridge-Secret` header OR `?secret=` query param
- `/health` always public (shows `auth_required: true` when secret is configured)

**For production on Coolify:** set both `TWILIO_AUTH_TOKEN` and `BRIDGE_SHARED_SECRET`.

## Twilio wiring

**Test numbers (Kitze):** from `+48 732125178` → Kitze `+48 739670417`.

### Phone number webhook

Twilio Console → Phone Numbers → `+48 739670417`:

- **A call comes in** → Webhook `POST`  
  `https://gpt-live-bridge.exposed.kitze.io/twilio/voice`

### How to test a call

1. `curl -sS https://gpt-live-bridge.exposed.kitze.io/health` → `ok`, `public_base_url` exposed, `tools` listed
2. Call `+48 739670417` from `+48 732125178`
3. Coolify logs should show: `twilio start`, `bridge started`, `iceConnectionState=…`, periodic `audio duplex in_frames=… out_frames=…`, and on interrupt `barge-in reason=energy|twilio_clear`

## Audio / ICE / barge-in

Upstream gpt-live answers with **ICE-lite** WebRTC (Opus). Full duplex depends on UDP from the Coolify container to OpenAI.

- HTTP/WSS for Twilio is fine behind Traefik + Cloudflare Worker (`*.exposed.kitze.io`).
- WebRTC media is **UDP**. If `in_frames>0` but `out_frames=0` (or vice versa) after a healthy create-call, set Coolify **host network** (`custom_docker_run_options` / network mode host) or add a TURN server in `ICE_SERVERS_JSON`.
- Barge-in: while agent audio is flowing, inbound RMS ≥ `BARGE_IN_RMS` (or Twilio `clear`) triggers `response.cancel` and Twilio `clear` so playback stops immediately.

Smoke without Twilio:

```bash
curl -sS 'https://gpt-live-bridge.service.beast.kitze.io/smoke/create-call'
```
