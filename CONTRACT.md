# gpt-live-bridge contract (proven 2026-09-23)

## Goal
Public service `https://gpt-live-bridge.exposed.kitze.io` (Worker → chicken funnel origin); internal `https://gpt-live-bridge.service.beast.kitze.io` on Coolify (beast VM 192.168.1.180 / Coolify uuid destination for beast apps).

Twilio phone Media Streams (mulaw 8k PCM over WSS) ↔ OpenAI GPT-Live via Kitze `codex-lb` (ChatGPT **subscription**, not API credits). **No Vapi.**

## Upstream (codex-lb) — working smoke

Base: `https://codex-lb.service.beast.kitze.io`

Auth: Bearer `sk-clb-…` (HomeBrain PWA key allows `gpt-live-1-codex`)

### 1) Create live call (HTTP)
`POST /backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas`

Headers:
- `Authorization: Bearer <CODEX_LB_API_KEY>`
- `Content-Type: application/json`
- `openai-alpha: quicksilver=v2`

Body (JSON):
```json
{
  "sdp": "<browser/webrtc SDP offer string>",
  "session": {
    "model": "gpt-live-1-codex",
    "instructions": "…",
    "audio": { "output": { "voice": "cove" } },
    "delegation": { "type": "client" }
  }
}
```

Success: **HTTP 201**
- `Location: /v1/realtime/calls/rtc_…`
- Body: SDP **answer** (text/plain)

Wrong content-type / bare `{"model":…}` → 400 `realtime_call_unavailable` (`unsupported_content_type` / `upstream_error`).

### 2) Control channel (WebSocket)
`wss://codex-lb.service.beast.kitze.io/v1/live/{call_id}`

Headers: `Authorization: Bearer …`, `openai-alpha: quicksilver=v2`

HomeBrain sends events like:
`{ "type": "session.context.append", "channel": "speakable", "content": [{ "type": "input_text", "text": "…" }] }`

Audio media is WebRTC (SDP offer/answer), not Twilio mulaw.

## Twilio side

Outbound/inbound call TwiML should roughly:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://gpt-live-bridge.exposed.kitze.io/twilio/media" />
  </Connect>
</Response>
```

Also expose:
- `GET /health` → 200 JSON (always public; shows `auth_required` and `twilio_signature_validation` status)
- `POST /twilio/voice` → TwiML pointing at the WSS (optional query params for instructions)
- **Validate Twilio signatures when `TWILIO_AUTH_TOKEN` set** (uses official `RequestValidator` from `twilio` package; tries multiple URL candidates to handle Cloudflare Worker → funnel origin rewrites)

## Bridge job
For each Twilio Media Stream session:
1. Stand up a local WebRTC peer (e.g. `aiortc` in Python, or `wrtc` in Node) that can produce a real SDP offer.
2. POST that offer to codex-lb as above; apply SDP answer.
3. Attach control WS `/v1/live/{id}`.
4. Bidirectional audio: Twilio mulaw@8kHz ↔ WebRTC Opus/PCM (resample).
5. On stream stop, close peer + WS cleanly.

Env vars (Coolify):
- `CODEX_LB_URL` default `https://codex-lb.service.beast.kitze.io`
- `CODEX_LB_API_KEY` (sk-clb-…) — **required**
- `LIVE_MODEL` default `gpt-live-1-codex`
- **`BRIDGE_SHARED_SECRET`** (alias `BRIDGE_TOKEN`) — **required for production** to protect `/smoke/create-call` and `/outbound` from unauthorized gpt-live usage
- **`TWILIO_AUTH_TOKEN`** — **required for production** to validate `X-Twilio-Signature` on `/twilio/voice` (fails closed)
- `TWILIO_ACCOUNT_SID` — required for `/outbound` calls
- `PORT` (Coolify)

## Deploy notes
- Dockerfile, healthcheck `/health`
- Domain: `gpt-live-bridge.service.beast.kitze.io` (+ maybe bare fqdn)
- Same Coolify server as voice-gateway / codex-lb (beast coolify VM)
- README: how to point a Twilio phone number / TwiML Bin at `/twilio/voice` and place a test call to +48 739670417 from +48 732125178

## Non-goals
- Vapi
- OpenAI API credit keys
- Home Assistant routing (voice-gateway already does that)


## Client delegation tools
Control WS handles `session.delegation.created` (target=client). Bridge runs allowlisted tools
(calendar / dayfold / beeper read) and returns `session.commentary.append`.
Beeper **send** is never auto-run.

## Barge-in
On user speech energy or Twilio clear → `response.cancel` + Twilio `clear`.

## Homebrain PWA proxy (added)

Homebrain can point **live only** at this bridge while chat stays on codex-lb:

| Method | Path | Auth |
|--------|------|------|
| POST | `/backend-api/codex/realtime/calls?...` | `Authorization: Bearer <BRIDGE_SHARED_SECRET\|CODEX_LB_API_KEY>` or `X-Bridge-Secret` |
| WS | `/v1/live/{call_id}` | same |

Bridge forwards to `CODEX_LB_URL` with its own `CODEX_LB_API_KEY`. Set Homebrain:

- `CODEX_LIVE_URL=https://gpt-live-bridge.service.beast.kitze.io`
- `CODEX_LIVE_API_KEY=<BRIDGE_SHARED_SECRET>`
- keep `CODEX_LB_URL` / `CODEX_LB_API_KEY` for chat (`gpt-6-sol`)
