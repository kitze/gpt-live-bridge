"""aiortc peer that bridges Twilio μ-law ↔ gpt-live WebRTC audio."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import aiohttp
import av
import fractions
import numpy as np
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
    MediaStreamTrack,
)
from aiortc.mediastreams import MediaStreamError

from . import audio, config
from .codex_lb import ControlChannel, create_live_call
from .tools import DelegationHandler, tool_schemas_for_prompt

log = logging.getLogger("gpt-live-bridge.webrtc")


def _ice_servers() -> list[RTCIceServer]:
    if config.ICE_SERVERS_JSON:
        try:
            raw = json.loads(config.ICE_SERVERS_JSON)
            return [RTCIceServer(**item) if isinstance(item, dict) else RTCIceServer(urls=item) for item in raw]
        except Exception:
            log.exception("bad ICE_SERVERS_JSON; falling back to default STUN")
    return [RTCIceServer(urls=config.DEFAULT_STUN)]


class TwilioInboundTrack(MediaStreamTrack):
    """Outbound WebRTC track fed by Twilio μ-law packets."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._timestamp = 0
        self._sample_rate = audio.WEBRTC_RATE
        self._samples_per_frame = self._sample_rate // 50  # 20ms
        self._started = time.time()
        self.frames_pushed = 0
        self.bytes_pushed = 0
        self.last_rms = 0.0

    def push_mulaw_b64(self, payload_b64: str) -> float:
        """Decode Twilio μ-law → 48k PCM and enqueue. Returns RMS of 8k PCM."""
        try:
            pcm8 = audio.mulaw_b64_to_pcm16(payload_b64)
            samples = np.frombuffer(pcm8, dtype=np.int16)
            rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if samples.size else 0.0
            self.last_rms = rms
            pcm48 = audio.resample_pcm16(pcm8, audio.MULAW_RATE, audio.WEBRTC_RATE)
        except Exception:
            log.exception("mulaw decode failed")
            return 0.0
        try:
            self._queue.put_nowait(pcm48)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(pcm48)
            except asyncio.QueueFull:
                pass
        self.frames_pushed += 1
        self.bytes_pushed += len(pcm48)
        return self.last_rms

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError

        need = self._samples_per_frame * 2  # s16 mono bytes
        buf = bytearray()
        deadline = time.monotonic() + 0.02
        while len(buf) < need:
            timeout = max(0.001, deadline - time.monotonic())
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                buf.extend(chunk)
            except asyncio.TimeoutError:
                break

        if len(buf) < need:
            buf.extend(b"\x00" * (need - len(buf)))
        else:
            buf = buf[:need]

        samples = np.frombuffer(bytes(buf), dtype=np.int16)
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = self._sample_rate
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, self._sample_rate)
        self._timestamp += self._samples_per_frame
        return frame


OutboundMediaCallback = Callable[[str], Awaitable[None]]  # mulaw b64 → Twilio
ClearCallback = Callable[[], Awaitable[None]]  # Twilio clear


class LiveBridgeSession:
    """One Twilio stream ↔ one gpt-live WebRTC call."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        stream_sid: str,
        *,
        instructions: str | None = None,
        send_mulaw: OutboundMediaCallback,
        clear_twilio: ClearCallback | None = None,
    ) -> None:
        self.http = http
        self.stream_sid = stream_sid
        self.instructions = instructions
        self.send_mulaw = send_mulaw
        self.clear_twilio = clear_twilio
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=_ice_servers()))
        self.inbound = TwilioInboundTrack()
        self.control: ControlChannel | None = None
        self.call_id: str | None = None
        self._reader_task: asyncio.Task | None = None
        self._stats_task: asyncio.Task | None = None
        self._closed = False
        self.ice_state = "new"
        self.connection_state = "new"
        self.agent_speaking = False
        self.out_frames = 0
        self.out_bytes = 0
        self.barge_ins = 0
        self._last_barge_at = 0.0
        self.delegation: DelegationHandler | None = None

        self.pc.addTrack(self.inbound)

        @self.pc.on("connectionstatechange")
        async def on_conn() -> None:
            self.connection_state = self.pc.connectionState
            log.info("[%s] pc connectionState=%s", stream_sid, self.pc.connectionState)

        @self.pc.on("iceconnectionstatechange")
        async def on_ice() -> None:
            self.ice_state = self.pc.iceConnectionState
            log.info("[%s] iceConnectionState=%s", stream_sid, self.pc.iceConnectionState)

        @self.pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            log.info("[%s] remote track kind=%s", stream_sid, track.kind)
            if track.kind == "audio":
                self._reader_task = asyncio.create_task(
                    self._pump_remote_audio(track), name=f"remote-audio-{stream_sid}"
                )

    async def start(self) -> None:
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        await self._wait_ice_gathering()

        local = self.pc.localDescription
        assert local is not None
        log.info("[%s] local SDP ready (%d bytes), creating live call", self.stream_sid, len(local.sdp))
        # Log whether host candidates exist (helps diagnose Docker UDP issues)
        host_cands = [ln for ln in local.sdp.splitlines() if ln.startswith("a=candidate:") and " typ host " in ln]
        srflx_cands = [ln for ln in local.sdp.splitlines() if ln.startswith("a=candidate:") and " typ srflx " in ln]
        log.info("[%s] ice candidates host=%d srflx=%d", self.stream_sid, len(host_cands), len(srflx_cands))

        tool_hint = (
            "\n\nAvailable read-only tools (client delegation): "
            + tool_schemas_for_prompt()
            + "\nNever send Beeper messages."
        )
        instructions = (self.instructions or config.LIVE_INSTRUCTIONS) + tool_hint

        live = await create_live_call(self.http, local.sdp, instructions=instructions)
        self.call_id = live.call_id
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=live.sdp_answer, type="answer"))

        async def on_event(ev: dict) -> None:
            await self._on_control_event(ev)

        self.control = ControlChannel(live.call_id, self.http, on_event=on_event)
        self.delegation = DelegationHandler(self.http, self.control)
        try:
            await self.control.connect()
        except Exception:
            log.exception("[%s] control WS failed (media may still work)", self.stream_sid)

        self._stats_task = asyncio.create_task(self._stats_loop(), name=f"stats-{stream_sid}")

    async def _on_control_event(self, ev: dict) -> None:
        typ = ev.get("type") or ""
        if typ in ("session.input_transcript.delta", "session.output_transcript.delta"):
            delta = ev.get("delta") or ""
            if self.delegation:
                self.delegation.on_transcript(typ, delta)
            if typ.startswith("session.output") and delta:
                self.agent_speaking = True
            return
        if typ == "session.delegation.created":
            deleg = ev.get("delegation") or {}
            did = deleg.get("id")
            target = deleg.get("target")
            log.info("[%s] delegation created id=%s target=%s", self.stream_sid, did, target)
            if did and target == "client" and self.delegation:
                self.delegation.handle_delegation(did)
            return
        if typ in (
            "response.done",
            "response.cancelled",
            "session.output_audio.done",
            "output_audio_buffer.stopped",
        ):
            self.agent_speaking = False
        if typ and typ not in ("response.audio_transcript.delta",):
            log.info("[%s] control %s", self.stream_sid, typ)

    async def _wait_ice_gathering(self, timeout: float = 5.0) -> None:
        if self.pc.iceGatheringState == "complete":
            return
        done = asyncio.Event()

        @self.pc.on("icegatheringstatechange")
        async def _() -> None:
            if self.pc.iceGatheringState == "complete":
                done.set()

        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning(
                "[%s] ICE gathering timeout (state=%s); continuing with partial candidates",
                self.stream_sid,
                self.pc.iceGatheringState,
            )

    async def _pump_remote_audio(self, track: MediaStreamTrack) -> None:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=audio.MULAW_RATE)
        try:
            while not self._closed:
                try:
                    frame = await track.recv()
                except MediaStreamError:
                    break
                self.agent_speaking = True
                try:
                    out_frames = resampler.resample(frame)
                except Exception:
                    log.exception("resample failed")
                    continue
                for of in out_frames:
                    arr = of.to_ndarray()
                    if arr.ndim == 2:
                        samples = arr[0] if arr.shape[0] <= arr.shape[1] else arr[:, 0]
                    else:
                        samples = arr
                    if samples.dtype != np.int16:
                        samples = (np.clip(samples.astype(np.float32), -1.0, 1.0) * 32767.0).astype(np.int16)
                    pcm = samples.astype(np.int16).tobytes()
                    if not pcm:
                        continue
                    self.out_frames += 1
                    self.out_bytes += len(pcm)
                    b64 = audio.pcm16_to_mulaw_b64(pcm)
                    try:
                        await self.send_mulaw(b64)
                    except Exception:
                        log.exception("send_mulaw failed")
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] remote audio pump error", self.stream_sid)

    async def _stats_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(5.0)
                log.info(
                    "[%s] audio duplex in_frames=%d in_bytes=%d out_frames=%d out_bytes=%d "
                    "ice=%s conn=%s agent_speaking=%s barge_ins=%d rms=%.1f",
                    self.stream_sid,
                    self.inbound.frames_pushed,
                    self.inbound.bytes_pushed,
                    self.out_frames,
                    self.out_bytes,
                    self.ice_state,
                    self.connection_state,
                    self.agent_speaking,
                    self.barge_ins,
                    self.inbound.last_rms,
                )
        except asyncio.CancelledError:
            raise

    async def barge_in(self, reason: str = "user") -> None:
        """Stop agent speech: response.cancel + Twilio clear."""
        now = time.monotonic()
        if now - self._last_barge_at < 0.4:
            return
        self._last_barge_at = now
        self.barge_ins += 1
        self.agent_speaking = False
        log.info("[%s] barge-in reason=%s call_id=%s", self.stream_sid, reason, self.call_id)
        if self.control:
            await self.control.cancel_response(event_id=f"barge_{self.barge_ins}")
        if self.clear_twilio:
            try:
                await self.clear_twilio()
            except Exception:
                log.exception("twilio clear failed")

    def on_twilio_media(self, payload_b64: str) -> None:
        rms = self.inbound.push_mulaw_b64(payload_b64)
        if (
            config.BARGE_IN_ENABLED
            and self.agent_speaking
            and rms >= config.BARGE_IN_RMS
            and self.out_frames > 0
        ):
            asyncio.create_task(self.barge_in("energy"), name=f"barge-{self.stream_sid}")

    async def on_twilio_clear(self) -> None:
        await self.barge_in("twilio_clear")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in (self._reader_task, self._stats_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self.control:
            await self.control.close()
        try:
            await self.pc.close()
        except Exception:
            log.exception("pc.close failed")
        log.info(
            "[%s] session closed call_id=%s ice=%s conn=%s in=%d out=%d barge=%d",
            self.stream_sid,
            self.call_id,
            self.ice_state,
            self.connection_state,
            self.inbound.frames_pushed,
            self.out_frames,
            self.barge_ins,
        )
