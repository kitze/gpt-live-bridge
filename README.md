# gpt-live-bridge

Bridges **Twilio Media Streams** (μ-law 8 kHz over WSS) ↔ **gpt-live** on Kitze `codex-lb` (ChatGPT subscription via `sk-clb-…`, **not** OpenAI API credits). No Vapi.

Public URL: `https://gpt-live-bridge.service.beast.kitze.io`

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness JSON |
| POST/GET | `/twilio/voice` | TwiML `<Connect><Stream>` |
| WSS | `/twilio/media` | Twilio Media Streams |
| GET | `/smoke/create-call` | Create a real aiortc offer → codex-lb (no Twilio) |

## Env

| Var | Default | Notes |
|-----|---------|-------|
| `CODEX_LB_URL` | `https://codex-lb.service.beast.kitze.io` | |
| `CODEX_LB_API_KEY` | — | `sk-clb-…` (HomeBrain key allows `gpt-live-1-codex`) |
| `LIVE_MODEL` | `gpt-live-1-codex` | |
| `LIVE_VOICE` | `cove` | |
| `LIVE_INSTRUCTIONS` | short helpful assistant | |
| `PUBLIC_BASE_URL` | `https://gpt-live-bridge.service.beast.kitze.io` | Used for TwiML WSS URL + optional Twilio sig |
| `PORT` | `8080` | |
| `TWILIO_AUTH_TOKEN` | — | Optional request signature validation |
| `BRIDGE_SHARED_SECRET` | — | Optional `?secret=` / `X-Bridge-Secret` |
| `ICE_SERVERS_JSON` | Google STUN | Optional RTCIceServer JSON list |

## Twilio wiring

**Test numbers (Kitze):** from `+48 732125178` → Kitze `+48 739670417`.

### Option A — Phone number webhook

In Twilio Console → Phone Numbers → `+48 739670417` (or your inbound number):

- **A call comes in** → Webhook `POST`  
  `https://gpt-live-bridge.service.beast.kitze.io/twilio/voice`

### Option B — TwiML Bin

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://gpt-live-bridge.service.beast.kitze.io/twilio/media" />
  </Connect>
</Response>
```

Point the number’s voice URL at that TwiML Bin.

### Option C — Custom instructions

`https://gpt-live-bridge.service.beast.kitze.io/twilio/voice?instructions=You%20are%20Kitze%27s%20concise%20assistant.`

## Local run

```bash
export CODEX_LB_API_KEY=sk-clb-…
pip install -r requirements.txt
python -m app.main
```

## Audio / ICE notes

Upstream gpt-live answers with **ICE-lite** WebRTC (Opus). This service runs `aiortc` inside Docker on the Coolify `coolify` network.

- HTTP/WSS for Twilio is fine behind Traefik/Caddy.
- WebRTC media is **UDP**. If duplex audio fails after a healthy `/health` and successful `create-call`, the usual fix is host networking or a TURN server (`ICE_SERVERS_JSON`) / `custom_docker_run_options` for UDP.

Smoke without Twilio:

```bash
curl -sS 'https://gpt-live-bridge.service.beast.kitze.io/smoke/create-call'
```
