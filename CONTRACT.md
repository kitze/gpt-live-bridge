# gpt-live-bridge contract (proven 2026-09-23)

## Goal
Public service `https://gpt-live-bridge.service.beast.kitze.io` on Coolify (beast VM 192.168.1.180 / Coolify uuid destination for beast apps).

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
    <Stream url="wss://gpt-live-bridge.service.beast.kitze.io/twilio/media" />
  </Connect>
</Response>
```

Also expose:
- `GET /health` → 200 JSON
- `POST /twilio/voice` → TwiML pointing at the WSS (optional query params for instructions)
- Validate Twilio signatures when `TWILIO_AUTH_TOKEN` set

## Bridge job
For each Twilio Media Stream session:
1. Stand up a local WebRTC peer (e.g. `aiortc` in Python, or `wrtc` in Node) that can produce a real SDP offer.
2. POST that offer to codex-lb as above; apply SDP answer.
3. Attach control WS `/v1/live/{id}`.
4. Bidirectional audio: Twilio mulaw@8kHz ↔ WebRTC Opus/PCM (resample).
5. On stream stop, close peer + WS cleanly.

Env vars (Coolify):
- `CODEX_LB_URL` default `https://codex-lb.service.beast.kitze.io`
- `CODEX_LB_API_KEY` (sk-clb-…)
- `LIVE_MODEL` default `gpt-live-1-codex`
- `BRIDGE_TOKEN` or `VOICE_GATEWAY_TOKEN`-style shared secret for non-Twilio probes
- `TWILIO_AUTH_TOKEN` optional signature check
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
