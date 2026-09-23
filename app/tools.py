"""Client-delegation tool allowlist + HTTP runners (Almanac / Dayfold / Beeper).

GPT-Live client delegation emits session.delegation.created; we then run
allowlisted tools and return results via session.commentary.append.

Expanding the allowlist:
  1. Add a ToolSpec to ALLOWLIST with name, description, parameters schema.
  2. Implement an async runner in RUNNERS (or point MCP_HTTP_BRIDGE_URL).
  3. Keep Beeper write / send tools OUT until Kitze explicitly approves.

Safe default: calendar read, dayfold read, beeper read-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin

import aiohttp

from . import config

log = logging.getLogger("gpt-live-bridge.tools")

Runner = Callable[[aiohttp.ClientSession, dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: str = "read"  # read | write


ALLOWLIST: list[ToolSpec] = [
    ToolSpec(
        name="calendar_agenda",
        description="Read Kitze's Google Calendar agenda for a day (Almanac).",
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "ISO date YYYY-MM-DD; defaults to today (Europe/Warsaw).",
                },
                "days": {
                    "type": "integer",
                    "description": "Number of days to include (1-7). Default 1.",
                },
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="dayfold_read_day",
        description="Read Kitze's Dayfold day canvas/calendar bands for a date.",
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "ISO date YYYY-MM-DD; defaults to today.",
                }
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="beeper_list_chats",
        description="List recent Beeper chats (read-only). Never sends messages.",
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max chats (default 10)."},
                "query": {"type": "string", "description": "Optional title/search filter."},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="beeper_list_messages",
        description="Read recent messages from a Beeper chat (read-only). Never sends.",
        parameters={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Beeper chat id."},
                "limit": {"type": "integer", "description": "Max messages (default 20)."},
            },
            "required": ["chat_id"],
            "additionalProperties": False,
        },
    ),
]


def allowlist_names() -> list[str]:
    return [t.name for t in ALLOWLIST]


def tool_schemas_for_prompt() -> str:
    return "\n".join(f"- {t.name}: {t.description}" for t in ALLOWLIST)


async def _post_json(
    http: aiohttp.ClientSession,
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> Any:
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    async with http.post(url, json=body, headers=hdrs) as resp:
        text = await resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} from {url}: {text[:400]}")
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {"raw": text[:2000]}


def _summarize(data: Any, limit: int = 1200) -> str:
    if isinstance(data, str):
        return data[:limit]
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        text = str(data)
    return text[:limit]


async def run_calendar_agenda(http: aiohttp.ClientSession, args: dict[str, Any]) -> str:
    base = config.ALMANAC_URL
    if not base:
        return "Almanac URL not configured (ALMANAC_URL)."
    days = max(1, min(int(args.get("days") or 1), 7))
    body: dict[str, Any] = {"days": days}
    if args.get("date"):
        body["date"] = args["date"]
    headers = {}
    if config.ALMANAC_TOKEN:
        headers["Authorization"] = f"Bearer {config.ALMANAC_TOKEN}"
    data = await _post_json(
        http, urljoin(base + "/", "api/google-calendar/agenda"), body, headers=headers
    )
    return _summarize(data, limit=1200)


async def run_dayfold_read_day(http: aiohttp.ClientSession, args: dict[str, Any]) -> str:
    if config.MCP_HTTP_BRIDGE_URL:
        return await _mcp_bridge(http, "dayfold_read_day", args)
    base = config.DAYFOLD_URL
    if not base:
        return "Dayfold not configured (DAYFOLD_URL or MCP_HTTP_BRIDGE_URL)."
    date = args.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = urljoin(base.rstrip("/") + "/", f"api/agent/day?date={date}")
    hdrs = {}
    if config.DAYFOLD_TOKEN:
        hdrs["Authorization"] = f"Bearer {config.DAYFOLD_TOKEN}"
    async with http.get(url, headers=hdrs) as resp:
        text = await resp.text()
        if resp.status >= 400:
            return f"Dayfold read failed HTTP {resp.status}: {text[:300]}"
        return text[:1500]


async def run_beeper_list_chats(http: aiohttp.ClientSession, args: dict[str, Any]) -> str:
    if not config.MCP_HTTP_BRIDGE_URL and not config.BEEPER_BRIDGE_URL:
        return "Beeper bridge not configured (MCP_HTTP_BRIDGE_URL / BEEPER_BRIDGE_URL)."
    return await _mcp_bridge(http, "beeper_list_chats", args)


async def run_beeper_list_messages(http: aiohttp.ClientSession, args: dict[str, Any]) -> str:
    if not config.MCP_HTTP_BRIDGE_URL and not config.BEEPER_BRIDGE_URL:
        return "Beeper bridge not configured (MCP_HTTP_BRIDGE_URL / BEEPER_BRIDGE_URL)."
    return await _mcp_bridge(http, "beeper_list_messages", args)


async def _mcp_bridge(http: aiohttp.ClientSession, name: str, args: dict[str, Any]) -> str:
    """POST {tool, arguments} to a CEO/beast MCP HTTP bridge (Executor Local proxy)."""
    base = config.MCP_HTTP_BRIDGE_URL or config.BEEPER_BRIDGE_URL
    assert base
    if re.search(r"(send|message_send|create_message|post)", name, re.I):
        return "Blocked: Beeper send requires Kitze approval."
    data = await _post_json(
        http,
        urljoin(base.rstrip("/") + "/", "tools/invoke"),
        {"tool": name, "arguments": args, "source": "gpt-live-bridge"},
        headers={"Authorization": f"Bearer {config.MCP_BRIDGE_TOKEN}"} if config.MCP_BRIDGE_TOKEN else None,
    )
    return _summarize(data, limit=1500)


RUNNERS: dict[str, Runner] = {
    "calendar_agenda": run_calendar_agenda,
    "dayfold_read_day": run_dayfold_read_day,
    "beeper_list_chats": run_beeper_list_chats,
    "beeper_list_messages": run_beeper_list_messages,
}


def pick_tools_from_transcript(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Heuristic router for client-delegation (safe reads only)."""
    t = (text or "").lower()
    picks: list[tuple[str, dict[str, Any]]] = []
    if any(w in t for w in ("calendar", "agenda", "what's on", "schedule", "meeting", "events")):
        picks.append(("calendar_agenda", {"days": 1}))
    if any(w in t for w in ("dayfold", "my day", "today's plan", "canvas")):
        picks.append(("dayfold_read_day", {}))
    if any(w in t for w in ("beeper", "messages", "imessage", "whatsapp", "text from", "sms")):
        picks.append(("beeper_list_chats", {"limit": 8}))
    return picks


async def execute_tool(http: aiohttp.ClientSession, name: str, args: dict[str, Any] | None = None) -> str:
    if name not in RUNNERS:
        return f"Tool {name!r} is not in the allowlist ({', '.join(allowlist_names())})."
    try:
        return await RUNNERS[name](http, args or {})
    except Exception as e:
        log.exception("tool %s failed", name)
        return f"Tool {name} error: {e}"


class DelegationHandler:
    """Handles session.delegation.created for client delegation."""

    def __init__(self, http: aiohttp.ClientSession, control: Any) -> None:
        self.http = http
        self.control = control
        self.input_transcript = ""
        self.output_transcript = ""
        self._tasks: set[asyncio.Task] = set()

    def on_transcript(self, typ: str, delta: str) -> None:
        if not delta:
            return
        if "input" in typ:
            self.input_transcript = (self.input_transcript + delta)[-4000:]
        elif "output" in typ:
            self.output_transcript = (self.output_transcript + delta)[-4000:]

    def handle_delegation(self, delegation_id: str) -> None:
        task = asyncio.create_task(self._run(delegation_id), name=f"deleg-{delegation_id[:12]}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, delegation_id: str) -> None:
        await asyncio.sleep(0.6)
        text = self.input_transcript.strip() or self.output_transcript.strip()
        picks = pick_tools_from_transcript(text)
        if not picks:
            await self.control.send(
                {
                    "type": "session.thinking.append",
                    "event_id": f"think_{delegation_id[:8]}",
                    "delegation_id": delegation_id,
                    "content": (
                        "No allowlisted tool matched. Available read tools: "
                        + ", ".join(allowlist_names())
                        + ". Ask the user to clarify, or answer from conversation."
                    ),
                }
            )
            return

        await self.control.send(
            {
                "type": "session.thinking.append",
                "event_id": f"prog_{delegation_id[:8]}",
                "delegation_id": delegation_id,
                "content": f"Running: {', '.join(n for n, _ in picks)}.",
            }
        )
        chunks: list[str] = []
        for name, args in picks:
            result = await execute_tool(self.http, name, args)
            chunks.append(f"{name}: {result}")
        content = " | ".join(chunks)
        if len(content) > 1500:
            content = content[:1500] + "…"
        await self.control.send(
            {
                "type": "session.commentary.append",
                "event_id": f"res_{delegation_id[:8]}",
                "delegation_id": delegation_id,
                "content": content,
            }
        )
        log.info("delegation %s completed tools=%s", delegation_id, [n for n, _ in picks])
