"""codex-lb gpt-live HTTP + control WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Awaitable
from urllib.parse import urlparse

import aiohttp

from . import config

log = logging.getLogger("gpt-live-bridge.codex")


@dataclass
class LiveCall:
    call_id: str
    sdp_answer: str
    location: str


def _ws_base(http_base: str) -> str:
    u = urlparse(http_base)
    scheme = "wss" if u.scheme == "https" else "ws"
    return f"{scheme}://{u.netloc}"


async def create_live_call(
    session: aiohttp.ClientSession,
    sdp_offer: str,
    *,
    instructions: str | None = None,
    model: str | None = None,
    voice: str | None = None,
) -> LiveCall:
    url = (
        f"{config.CODEX_LB_URL}/backend-api/codex/realtime/calls"
        "?intent=quicksilver&architecture=avas"
    )
    body = {
        "sdp": sdp_offer,
        "session": {
            "model": model or config.LIVE_MODEL,
            "instructions": instructions if instructions is not None else config.LIVE_INSTRUCTIONS,
            "audio": {"output": {"voice": voice or config.LIVE_VOICE}},
            "delegation": {"type": "client"},
        },
    }
    headers = {
        "Authorization": f"Bearer {config.CODEX_LB_API_KEY}",
        "Content-Type": "application/json",
        "openai-alpha": "quicksilver=v2",
    }
    async with session.post(url, json=body, headers=headers) as resp:
        text = await resp.text()
        if resp.status != 201:
            raise RuntimeError(f"create_live_call failed HTTP {resp.status}: {text[:500]}")
        location = resp.headers.get("Location") or resp.headers.get("location") or ""
        call_id = location.rstrip("/").split("/")[-1] if location else ""
        if not call_id.startswith("rtc_"):
            # Fallback: try parse from body error paths
            raise RuntimeError(f"missing call id in Location={location!r}")
        log.info("created live call %s", call_id)
        return LiveCall(call_id=call_id, sdp_answer=text, location=location)


class ControlChannel:
    """WebSocket control channel at /v1/live/{call_id}."""

    def __init__(
        self,
        call_id: str,
        session: aiohttp.ClientSession,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.call_id = call_id
        self.session = session
        self.on_event = on_event
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None
        self.closed = asyncio.Event()

    @property
    def url(self) -> str:
        return f"{_ws_base(config.CODEX_LB_URL)}/v1/live/{self.call_id}"

    async def connect(self) -> None:
        headers = {
            "Authorization": f"Bearer {config.CODEX_LB_API_KEY}",
            "openai-alpha": "quicksilver=v2",
        }
        self._ws = await self.session.ws_connect(self.url, headers=headers, heartbeat=20)
        self._task = asyncio.create_task(self._reader(), name=f"live-ctrl-{self.call_id}")
        log.info("control WS connected %s", self.call_id)

    async def _reader(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        log.warning("non-json control msg: %s", msg.data[:200])
                        continue
                    typ = data.get("type", "?")
                    log.debug("control event %s", typ)
                    if self.on_event:
                        try:
                            await self.on_event(data)
                        except Exception:
                            log.exception("on_event handler failed")
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except Exception:
            log.exception("control WS reader error")
        finally:
            self.closed.set()

    async def send(self, payload: dict[str, Any]) -> None:
        if not self._ws or self._ws.closed:
            raise RuntimeError("control WS not connected")
        await self._ws.send_json(payload)

    async def append_speakable(self, text: str) -> None:
        await self.send(
            {
                "type": "session.context.append",
                "channel": "speakable",
                "content": [{"type": "input_text", "text": text}],
            }
        )

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self.closed.set()
