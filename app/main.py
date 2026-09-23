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
from urllib.parse import parse_qs, urlencode, unquote

import aiohttp
from aiohttp import web

from . import config
from .webrtc_session import LiveBridgeSession

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("gpt-live-bridge")

# Optional: import Twilio's official validator if available
try:
    from twilio.request_validator import RequestValidator as TwilioRequestValidator
    _TWILIO_VALIDATOR_AVAILABLE = True
except ImportError:
    _TWILIO_VALIDATOR_AVAILABLE = False
    log.warning("twilio package not available; using fallback HMAC validator")


def _validate_twilio_signature(request: web.Request, body: bytes) -> bool:
    """
    Validate Twilio X-Twilio-Signature header.
    
    When TWILIO_AUTH_TOKEN is set: FAIL CLOSED (require valid signature).
    When unset: permissive for local dev (log warning).
    
    Emergency escape: TWILIO_SKIP_SIGNATURE=1 disables validation (default off).
    """
    # Emergency kill-switch (default off, explicit opt-in only)
    skip_env = os.environ.get("TWILIO_SKIP_SIGNATURE", "").strip().lower()
    if skip_env in ("1", "true", "yes", "on"):
        if not getattr(_validate_twilio_signature, "_skip_logged", False):
            log.warning("TWILIO_SKIP_SIGNATURE=1: signature validation DISABLED (emergency only)")
            _validate_twilio_signature._skip_logged = True  # type: ignore[attr-defined]
        return True
    
    token = config.TWILIO_AUTH_TOKEN
    if not token:
        if not getattr(_validate_twilio_signature, "_notoken_logged", False):
            log.warning("TWILIO_AUTH_TOKEN not set; signature validation disabled for local dev")
            _validate_twilio_signature._notoken_logged = True  # type: ignore[attr-defined]
        return True
    
    sig = request.headers.get("X-Twilio-Signature", "")
    if not sig:
        log.warning(
            "Twilio signature missing: path=%s ctype=%s body_len=%d",
            request.path_qs,
            request.content_type,
            len(body),
        )
        return False
    
    # Parse form params
    params: dict[str, str] = {}
    ctype = request.content_type or ""
    if "application/x-www-form-urlencoded" in ctype:
        raw = body.decode("utf-8", errors="replace")
        if raw:
            form = parse_qs(raw, keep_blank_values=True)
            params = {k: v[0] for k, v in form.items()}
    
    # Build URL candidates: Twilio signs the public URL it called
    url_candidates = []
    
    # 1. PUBLIC_BASE_URL + path_qs (primary for proxied deployments)
    if config.PUBLIC_BASE_URL:
        url_candidates.append(config.PUBLIC_BASE_URL + request.path_qs)
    
    # 2. Request URL as-is (direct / local dev)
    url_candidates.append(str(request.url))
    
    # 3. Reconstruct from X-Forwarded headers (fallback)
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "https")
    forwarded_host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host")
    if forwarded_host:
        url_candidates.append(f"{forwarded_proto}://{forwarded_host}{request.path_qs}")
    
    # 4. Strip :10000 funnel port if present (chicken funnel origin uses :10000)
    for base_url in list(url_candidates):
        if ":10000" in base_url:
            url_candidates.append(base_url.replace(":10000", ""))
    
    # Build param variants: handle CallToken double-encoding issue
    param_variants = [params]
    if "CallToken" in params:
        # Try single unquote
        p2 = dict(params)
        try:
            p2["CallToken"] = unquote(params["CallToken"])
            param_variants.append(p2)
        except Exception:
            pass
        # Try double unquote
        p3 = dict(params)
        try:
            p3["CallToken"] = unquote(unquote(params["CallToken"]))
            param_variants.append(p3)
        except Exception:
            pass
    
    # Try validation with Twilio's official validator if available
    if _TWILIO_VALIDATOR_AVAILABLE:
        validator = TwilioRequestValidator(token)
        for url in url_candidates:
            for pv in param_variants:
                if validator.validate(url, pv, sig):
                    return True
    else:
        # Fallback HMAC implementation
        for url in url_candidates:
            for pv in param_variants:
                pieces = url + "".join(k + pv[k] for k in sorted(pv))
                digest = hmac.new(token.encode(), pieces.encode("utf-8"), hashlib.sha1).digest()
                expected = base64.b64encode(digest).decode()
                if hmac.compare_digest(expected, sig):
                    return True
    
    # All candidates failed
    log.warning(
        "Twilio signature mismatch: tried %d URL candidates × %d param variants. "
        "path=%s public=%s req_url=%s sig_prefix=%s params=%s",
        len(url_candidates),
        len(param_variants),
        request.path_qs,
        config.PUBLIC_BASE_URL,
        str(request.url)[:100],
        sig[:20],
        sorted(params.keys()),
    )
    return False


def _check_shared_secret(request: web.Request, *, required: bool = False) -> bool:
    """
    Check bridge shared secret via X-Bridge-Secret header or ?secret= query param.
    
    When required=True: FAIL CLOSED (return False if secret not configured OR wrong).
    When required=False: permissive if not configured (for /twilio/media WSS).
    """
    secret = config.BRIDGE_SHARED_SECRET
    if not secret:
        if required:
            log.warning("BRIDGE_SHARED_SECRET not set; rejecting request to %s", request.path)
            return False
        return True
    got = request.headers.get("X-Bridge-Secret") or request.query.get("secret")
    return hmac.compare_digest(str(got or ""), secret)


async def health(request: web.Request) -> web.Response:
    from .tools import allowlist_names

    return web.json_response(
        {
            "ok": True,
            "service": "gpt-live-bridge",
            "codex_lb": config.CODEX_LB_URL,
            "model": config.LIVE_MODEL,
            "has_api_key": bool(config.CODEX_LB_API_KEY),
            "public_base_url": config.PUBLIC_BASE_URL,
            "barge_in": config.BARGE_IN_ENABLED,
            "tools": allowlist_names(),
            "almanac": bool(config.ALMANAC_URL),
            "mcp_bridge": bool(config.MCP_HTTP_BRIDGE_URL or config.BEEPER_BRIDGE_URL),
            "auth_required": bool(config.BRIDGE_SHARED_SECRET),
            "twilio_signature_validation": bool(config.TWILIO_AUTH_TOKEN),
            "homebrain_proxy": True,
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
    # Check secret if configured; permissive if not set (backward compat)
    if not _check_shared_secret(request, required=False):
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

    async def clear_twilio() -> None:
        nonlocal stream_sid
        if not stream_sid or ws.closed:
            return
        await ws.send_json({"event": "clear", "streamSid": stream_sid})
        log.info("sent Twilio clear streamSid=%s", stream_sid)

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
                        clear_twilio=clear_twilio,
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
            elif event == "clear":
                log.info("twilio clear streamSid=%s", data.get("streamSid"))
                if bridge:
                    await bridge.on_twilio_clear()
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



async def outbound(request: web.Request) -> web.Response:
    """Scaffold: place an outbound Twilio call that hits /twilio/voice.

    Body JSON: {to, agent?, mission?, opening?}
    Requires BRIDGE_SHARED_SECRET + TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN.
    """
    if not _check_shared_secret(request, required=True):
        raise web.HTTPForbidden(text="bad secret")
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="json body required")
    to = (body.get("to") or "").strip()
    if not to:
        raise web.HTTPBadRequest(text="to required")
    opening = body.get("opening") or body.get("mission") or ""
    agent = body.get("agent") or "kitze-assistant"
    if not config.TWILIO_ACCOUNT_SID or not config.TWILIO_AUTH_TOKEN:
        return web.json_response(
            {
                "ok": False,
                "error": "outbound scaffold: set TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN",
                "would_call": {"to": to, "from": config.TWILIO_FROM_NUMBER, "agent": agent},
                "voice_url": f"{config.PUBLIC_BASE_URL}/twilio/voice",
            },
            status=501,
        )
    instructions = config.LIVE_INSTRUCTIONS
    if opening:
        instructions = f"{instructions}\nOpening line / mission: {opening}"
    from urllib.parse import quote

    voice_url = f"{config.PUBLIC_BASE_URL}/twilio/voice?instructions={quote(instructions[:800])}"
    auth = aiohttp.BasicAuth(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    form = {
        "To": to,
        "From": config.TWILIO_FROM_NUMBER,
        "Url": voice_url,
        "Method": "POST",
    }
    url = f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Calls.json"
    async with request.app["http"].post(url, data=form, auth=auth) as resp:
        text = await resp.text()
        if resp.status >= 300:
            return web.json_response({"ok": False, "status": resp.status, "body": text[:500]}, status=502)
        return web.json_response({"ok": True, "agent": agent, "twilio": text[:1000], "voice_url": voice_url})


async def smoke_create_call(request: web.Request) -> web.Response:
    """Dev/smoke: create a live call with a real aiortc offer (no Twilio).
    
    Requires BRIDGE_SHARED_SECRET to prevent unauthorized gpt-live usage.
    """
    if not _check_shared_secret(request, required=True):
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
    app.router.add_post("/outbound", outbound)
    # Homebrain PWA live/voice proxy (HTTP create-call + control WS)
    from .homebrain_proxy import register as register_homebrain_proxy
    register_homebrain_proxy(app)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    app = create_app()
    web.run_app(app, host=config.HOST, port=config.PORT, print=lambda *a: log.info("%s", " ".join(map(str, a))))


if __name__ == "__main__":
    main()
