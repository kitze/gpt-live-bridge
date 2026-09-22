"""aiohttp entrypoint: /health, /twilio/voice, /twilio/media."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import base64
import json
import logging
import os
from typing import Any
from urllib.parse import parse_qs, urlencode

import aiohttp
from aiohttp import web

from . import config
from .webrtc_session import LiveBridgeSession

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("gpt-live-bridge")


def _validate_twilio_signature(request: web.Request, body: bytes) -> bool:
    token = config.TWILIO_AUTH_TOKEN
    if not token:
        return True
    sig = request.headers.get("X-Twilio-Signature", "")
    if not sig:
        return False
    # Twilio signs the full URL + sorted POST params
    url = str(request.url)
    # Prefer PUBLIC_BASE_URL path for signature if proxy rewrote host
    if config.PUBLIC_BASE_URL:
        url = config.PUBLIC_BASE_URL + request.path_qs
    params: dict[str, str] = {}
    ctype = request.content_type or ""
    if "application/x-www-form-urlencoded" in ctype:
        form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        params = {k: v[0] for k, v in form.items()}
    pieces = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(token.encode(), pieces.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, sig)


def _check_shared_secret(request: web.Request) -> bool:
    secret = config.BRIDGE_SHARED_SECRET
    if not secret:
        return True
    got = request.headers.get("X-Bridge-Secret") or request.query.get("secret")
    return hmac.compare_digest(str(got or ""), secret)


async def health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "service": "gpt-live-bridge",
            "codex_lb": config.CODEX_LB_URL,
            "model": config.LIVE_MODEL,
            "has_api_key": bool(config.CODEX_LB_API_KEY),
            "public_base_url": config.PUBLIC_BASE_URL,
        }
    )


async def twilio_voice(request: web.Request) -> web.Response:
    """Return TwiML Connect/Stream pointing at our media WSS."""
    body = await request.read()
    if not _validate_twilio_signature(request, body):
        log.warning("invalid Twilio signature on /twilio/voice")
        raise web.HTTPForbidden(text="invalid signature")

    instructions = request.query.get("instructions") or config.LIVE_INSTRUCTIONS
    # Pass instructions via customParameters / query on the stream URL
    q = {}
    if instructions:
        q["instructions"] = instructions
    if config.BRIDGE_SHARED_SECRET:
        q["secret"] = config.BRIDGE_SHARED_SECRET
    qs = ("?" + urlencode(q)) if q else ""

    # Prefer wss derived from PUBLIC_BASE_URL
    base = config.PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    stream_url = f"{base}/twilio/media{qs}"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{stream_url}">
      <Parameter name="instructions" value="{_xml_escape(instructions[:500])}" />
    </Stream>
  </Connect>
</Response>
"""
    return web.Response(text=twiml, content_type="text/xml")


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def twilio_media(request: web.Request) -> web.WebSocketResponse:
    if not _check_shared_secret(request):
        raise web.HTTPForbidden(text="bad secret")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    log.info("Twilio media WS connected from %s", request.remote)

    http: aiohttp.ClientSession = request.app["http"]
    bridge: LiveBridgeSession | None = None
    stream_sid: str | None = None
    instructions = request.query.get("instructions") or config.LIVE_INSTRUCTIONS

    async def send_mulaw(payload_b64: str) -> None:
        nonlocal stream_sid
        if not stream_sid or ws.closed:
            return
        await ws.send_json(
            {
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": payload_b64},
            }
        )

    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    break
                continue
            try:
                data: dict[str, Any] = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            event = data.get("event")
            if event == "connected":
                log.info("twilio connected protocol=%s", data.get("protocol"))
            elif event == "start":
                start = data.get("start") or {}
                stream_sid = data.get("streamSid") or start.get("streamSid")
                custom = start.get("customParameters") or {}
                if custom.get("instructions"):
                    instructions = custom["instructions"]
                log.info(
                    "twilio start streamSid=%s callSid=%s",
                    stream_sid,
                    start.get("callSid"),
                )
                if not config.CODEX_LB_API_KEY:
                    log.error("CODEX_LB_API_KEY missing; cannot bridge")
                    await ws.close(code=1011, message=b"missing api key")
                    break
                try:
                    bridge = LiveBridgeSession(
                        http,
                        stream_sid or "unknown",
                        instructions=instructions,
                        send_mulaw=send_mulaw,
                    )
                    await bridge.start()
                    log.info(
                        "bridge started call_id=%s ice=%s",
                        bridge.call_id,
                        bridge.ice_state,
                    )
                except Exception as e:
                    log.exception("failed to start WebRTC bridge: %s", e)
                    await ws.send_json(
                        {
                            "event": "mark",
                            "streamSid": stream_sid,
                            "mark": {"name": "bridge_error"},
                        }
                    )
                    await ws.close(code=1011, message=b"bridge start failed")
                    break
            elif event == "media":
                media = data.get("media") or {}
                payload = media.get("payload")
                if payload and bridge:
                    bridge.on_twilio_media(payload)
            elif event == "stop":
                log.info("twilio stop streamSid=%s", data.get("streamSid"))
                break
            elif event == "mark":
                log.debug("twilio mark %s", data.get("mark"))
    finally:
        if bridge:
            await bridge.close()
        if not ws.closed:
            await ws.close()
        log.info("Twilio media WS closed streamSid=%s", stream_sid)
    return ws


async def smoke_create_call(request: web.Request) -> web.Response:
    """Dev/smoke: create a live call with a real aiortc offer (no Twilio)."""
    if not _check_shared_secret(request):
        raise web.HTTPForbidden(text="bad secret")
    if not config.CODEX_LB_API_KEY:
        raise web.HTTPServiceUnavailable(text="CODEX_LB_API_KEY not set")

    from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer
    from .webrtc_session import _ice_servers, TwilioInboundTrack
    from .codex_lb import create_live_call

    pc = RTCPeerConnection(RTCConfiguration(iceServers=_ice_servers()))
    pc.addTrack(TwilioInboundTrack())
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    # brief ICE gather
    await asyncio.sleep(1.0)
    try:
        live = await create_live_call(
            request.app["http"],
            pc.localDescription.sdp,
            instructions=request.query.get("instructions") or "Reply with a short hello.",
        )
        body = {
            "ok": True,
            "call_id": live.call_id,
            "location": live.location,
            "answer_sdp_bytes": len(live.sdp_answer),
            "ice_gathering": pc.iceGatheringState,
        }
        return web.json_response(body)
    finally:
        await pc.close()


async def on_startup(app: web.Application) -> None:
    timeout = aiohttp.ClientTimeout(total=60)
    app["http"] = aiohttp.ClientSession(timeout=timeout)
    log.info(
        "startup public=%s codex=%s model=%s key=%s",
        config.PUBLIC_BASE_URL,
        config.CODEX_LB_URL,
        config.LIVE_MODEL,
        ("set:" + config.CODEX_LB_API_KEY[:12] + "…") if config.CODEX_LB_API_KEY else "MISSING",
    )


async def on_cleanup(app: web.Application) -> None:
    await app["http"].close()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", health)
    app.router.add_post("/twilio/voice", twilio_voice)
    app.router.add_get("/twilio/voice", twilio_voice)  # Twilio console sometimes GETs
    app.router.add_get("/twilio/media", twilio_media)
    app.router.add_get("/smoke/create-call", smoke_create_call)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    app = create_app()
    web.run_app(app, host=config.HOST, port=config.PORT, print=lambda *a: log.info("%s", " ".join(map(str, a))))


if __name__ == "__main__":
    main()
