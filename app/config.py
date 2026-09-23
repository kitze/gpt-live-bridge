from __future__ import annotations

import os


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v


CODEX_LB_URL = (_env("CODEX_LB_URL", "https://codex-lb.service.beast.kitze.io") or "").rstrip("/")
CODEX_LB_API_KEY = _env("CODEX_LB_API_KEY", "") or ""
LIVE_MODEL = _env("LIVE_MODEL", "gpt-live-1-codex") or "gpt-live-1-codex"
LIVE_VOICE = _env("LIVE_VOICE", "cove") or "cove"
LIVE_INSTRUCTIONS = _env(
    "LIVE_INSTRUCTIONS",
    "You are a helpful voice assistant for Kitze. Keep replies concise and conversational. "
    "You can look up calendar, Dayfold day plan, and Beeper chats via tools when the user asks. "
    "Never send Beeper messages.",
) or ""
PUBLIC_BASE_URL = (
    _env("PUBLIC_BASE_URL", "https://gpt-live-bridge.exposed.kitze.io") or ""
).rstrip("/")
PORT = int(_env("PORT", "8080") or "8080")
HOST = _env("HOST", "0.0.0.0") or "0.0.0.0"
TWILIO_AUTH_TOKEN = _env("TWILIO_AUTH_TOKEN", "") or ""
BRIDGE_SHARED_SECRET = _env("BRIDGE_SHARED_SECRET", "") or _env("BRIDGE_TOKEN", "") or ""
LOG_LEVEL = (_env("LOG_LEVEL", "INFO") or "INFO").upper()

# WebRTC ICE — optional RTCIceServer JSON list (STUN/TURN).
# Default public Google STUN helps gather srflx from Docker NATs.
# For duplex failures in Docker bridge networking, prefer host network
# (Coolify custom_docker_run_options / network_mode=host) or a TURN entry here.
ICE_SERVERS_JSON = _env("ICE_SERVERS_JSON", "") or ""
DEFAULT_STUN = "stun:stun.l.google.com:19302"

# Explicit barge-in: cancel model speech when user energy / Twilio clear arrives.
BARGE_IN_ENABLED = (_env("BARGE_IN_ENABLED", "1") or "1") not in ("0", "false", "False", "no")
# RMS threshold on μ-law-decoded PCM16 (int16) for barge-in while agent is speaking.
BARGE_IN_RMS = float(_env("BARGE_IN_RMS", "500") or "500")

# Tool backends (client delegation)
ALMANAC_URL = (_env("ALMANAC_URL", "https://almanac.service.beast.kitze.io") or "").rstrip("/")
DAYFOLD_URL = (_env("DAYFOLD_URL", "https://dayfold.service.beast.kitze.io") or "").rstrip("/")
DAYFOLD_TOKEN = _env("DAYFOLD_TOKEN", "") or ""
# Optional HTTP bridge that fronts Executor Local / Skillbox MCP on CEO or beast.
MCP_HTTP_BRIDGE_URL = (_env("MCP_HTTP_BRIDGE_URL", "") or "").rstrip("/")
MCP_BRIDGE_TOKEN = _env("MCP_BRIDGE_TOKEN", "") or ""
BEEPER_BRIDGE_URL = (_env("BEEPER_BRIDGE_URL", "") or "").rstrip("/")

# Twilio outbound scaffold
TWILIO_ACCOUNT_SID = _env("TWILIO_ACCOUNT_SID", "") or ""
TWILIO_API_KEY_SID = _env("TWILIO_API_KEY_SID", "") or ""
TWILIO_API_KEY_SECRET = _env("TWILIO_API_KEY_SECRET", "") or ""
TWILIO_FROM_NUMBER = _env("TWILIO_FROM_NUMBER", "+48739670417") or "+48739670417"
