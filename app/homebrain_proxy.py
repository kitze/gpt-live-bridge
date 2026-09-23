"""Proxy Homebrain-compatible gpt-live HTTP + control WS through this bridge to codex-lb.

Homebrain PWA speaks:
  POST /backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas
  WS   /v1/live/{call_id}

This module forwards those to CODEX_LB_URL using the bridge's CODEX_LB_API_KEY so
voice/live can target gpt-live-bridge while chat stays on codex-lb directly.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from urllib.parse import urlparse

import aiohttp
from aiohttp import WSMsgType, web

from . import config

log = logging.getLogger("gpt-live-bridge.hb-proxy")

CREATE_PATH = "/backend-api/codex/realtime/calls"


def _ws_base(http_base: str) -> str:
    u = urlparse(http_base)
    scheme = "wss" if u.scheme == "https" else "ws"
    return f"{scheme}://{u.netloc}"


def check_proxy_auth(request: web.Request) -> bool:
    """Accept X-Bridge-Secret, Bearer bridge secret, or Bearer upstream API key."""
    secret = config.BRIDGE_SHARED_SECRET
    api_key = config.CODEX_LB_API_KEY
    header_secret = request.headers.get("X-Bridge-Secret") or request.query.get("secret")
    auth = request.headers.get("Authorization") or ""
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if secret and header_secret and hmac.compare_digest(str(header_secret), secret):
        return True
    if secret and bearer and hmac.compare_digest(bearer, secret):
        return True
    if api_key and bearer and hmac.compare_digest(bearer, api_key):
        return True
    return False


async def proxy_create_call(request: web.Request) -> web.Response:
    if not check_proxy_auth(request):
        raise web.HTTPUnauthorized(text="bridge auth required")
    if not config.CODEX_LB_API_KEY:
        raise web.HTTPServiceUnavailable(text="CODEX_LB_API_KEY not configured")

    body = await request.read()
    qs = ("?" + request.query_string) if request.query_string else ""
    url = f"{config.CODEX_LB_URL}{CREATE_PATH}{qs}"
    headers = {
        "Authorization": f"Bearer {config.CODEX_LB_API_KEY}",
        "Content-Type": request.headers.get("Content-Type", "application/json"),
    }
    # Forward alpha / realtime negotiation headers if present
    for name in ("openai-alpha", "OpenAI-Beta", "openai-beta"):
        if name in request.headers:
            headers[name] = request.headers[name]
    if "openai-alpha" not in headers and "OpenAI-Beta" not in headers:
        headers["openai-alpha"] = "quicksilver=v2"

    http: aiohttp.ClientSession = request.app["http"]
    try:
        async with http.post(url, data=body, headers=headers) as resp:
            text = await resp.text()
            out = web.Response(text=text, status=resp.status)
            # Homebrain reads Location for call id
            loc = resp.headers.get("Location") or resp.headers.get("location")
            if loc:
                out.headers["Location"] = loc
            ctype = resp.headers.get("Content-Type")
            if ctype:
                out.headers["Content-Type"] = ctype
            log.info(
                "proxy create-call upstream=%s status=%s location=%s body_len=%s",
                resp.status,
                resp.status,
                (loc or "")[:80],
                len(text),
            )
            return out
    except aiohttp.ClientError as e:
        log.exception("proxy create-call failed")
        raise web.HTTPBadGateway(text=f"upstream error: {e}") from e


async def proxy_live_ws(request: web.Request) -> web.WebSocketResponse:
    if not check_proxy_auth(request):
        raise web.HTTPUnauthorized(text="bridge auth required")
    if not config.CODEX_LB_API_KEY:
        raise web.HTTPServiceUnavailable(text="CODEX_LB_API_KEY not configured")

    call_id = request.match_info["call_id"]
    if not call_id or len(call_id) > 200:
        raise web.HTTPBadRequest(text="bad call id")

    peer = web.WebSocketResponse(heartbeat=20)
    await peer.prepare(request)

    upstream_url = f"{_ws_base(config.CODEX_LB_URL)}/v1/live/{call_id}"
    headers = {
        "Authorization": f"Bearer {config.CODEX_LB_API_KEY}",
        "openai-alpha": request.headers.get("openai-alpha", "quicksilver=v2"),
    }
    http: aiohttp.ClientSession = request.app["http"]
    log.info("proxy live ws call_id=%s", call_id)

    try:
        async with http.ws_connect(upstream_url, headers=headers, heartbeat=20) as upstream:

            async def client_to_upstream() -> None:
                async for msg in peer:
                    if msg.type == WSMsgType.TEXT:
                        await upstream.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await upstream.send_bytes(msg.data)
                    elif msg.type == WSMsgType.PING:
                        await upstream.ping(msg.data)
                    elif msg.type == WSMsgType.PONG:
                        await upstream.pong(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break

            async def upstream_to_client() -> None:
                async for msg in upstream:
                    if msg.type == WSMsgType.TEXT:
                        await peer.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await peer.send_bytes(msg.data)
                    elif msg.type == WSMsgType.PING:
                        await peer.ping(msg.data)
                    elif msg.type == WSMsgType.PONG:
                        await peer.pong(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_upstream(), name=f"c2u-{call_id}"),
                    asyncio.create_task(upstream_to_client(), name=f"u2c-{call_id}"),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc:
                    log.warning("proxy live ws task error call_id=%s: %s", call_id, exc)
    except Exception:
        log.exception("proxy live ws failed call_id=%s", call_id)
        if not peer.closed:
            await peer.close(code=1011, message=b"upstream ws failed")
    finally:
        if not peer.closed:
            await peer.close()
        log.info("proxy live ws closed call_id=%s", call_id)
    return peer


def register(app: web.Application) -> None:
    app.router.add_post(CREATE_PATH, proxy_create_call)
    # Also accept without query (proxy forwards whatever query client sent via path match)
    app.router.add_get("/v1/live/{call_id}", proxy_live_ws)
