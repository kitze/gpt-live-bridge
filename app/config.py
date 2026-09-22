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
    "You are a helpful voice assistant for Kitze. Keep replies concise and conversational.",
) or ""
PUBLIC_BASE_URL = (
    _env("PUBLIC_BASE_URL", "https://gpt-live-bridge.service.beast.kitze.io") or ""
).rstrip("/")
PORT = int(_env("PORT", "8080") or "8080")
HOST = _env("HOST", "0.0.0.0") or "0.0.0.0"
TWILIO_AUTH_TOKEN = _env("TWILIO_AUTH_TOKEN", "") or ""
BRIDGE_SHARED_SECRET = _env("BRIDGE_SHARED_SECRET", "") or _env("BRIDGE_TOKEN", "") or ""
LOG_LEVEL = (_env("LOG_LEVEL", "INFO") or "INFO").upper()

# WebRTC ICE — optional comma-separated STUN/TURN urls
# Default public Google STUN helps gather srflx from Docker NATs.
ICE_SERVERS_JSON = _env("ICE_SERVERS_JSON", "") or ""
DEFAULT_STUN = "stun:stun.l.google.com:19302"
