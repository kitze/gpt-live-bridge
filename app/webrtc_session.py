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

    def push_mulaw_b64(self, payload_b64: str) -> None:
        try:
            pcm8 = audio.mulaw_b64_to_pcm16(payload_b64)
            pcm48 = audio.resample_pcm16(pcm8, audio.MULAW_RATE, audio.WEBRTC_RATE)
        except Exception:
            log.exception("mulaw decode failed")
            return
        try:
            self._queue.put_nowait(pcm48)
        except asyncio.QueueFull:
            # Drop oldest to keep latency low
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(pcm48)
            except asyncio.QueueFull:
                pass

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError

        # Assemble ~20ms of 48k PCM
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
            # Keep remainder for next frame by shoving back — simple truncate for latency
            buf = buf[:need]

        samples = np.frombuffer(bytes(buf), dtype=np.int16)
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = self._sample_rate
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, self._sample_rate)
        self._timestamp += self._samples_per_frame
        return frame


OutboundMediaCallback = Callable[[str], Awaitable[None]]  # mulaw b64 → Twilio


class LiveBridgeSession:
    """One Twilio stream ↔ one gpt-live WebRTC call."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        stream_sid: str,
        *,
        instructions: str | None = None,
        send_mulaw: OutboundMediaCallback,
    ) -> None:
        self.http = http
        self.stream_sid = stream_sid
        self.instructions = instructions
        self.send_mulaw = send_mulaw
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=_ice_servers()))
        self.inbound = TwilioInboundTrack()
        self.control: ControlChannel | None = None
        self.call_id: str | None = None
        self._reader_task: asyncio.Task | None = None
        self._closed = False
        self.ice_state = "new"
        self.connection_state = "new"

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

        live = await create_live_call(self.http, local.sdp, instructions=self.instructions)
        self.call_id = live.call_id
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=live.sdp_answer, type="answer"))

        async def on_event(ev: dict) -> None:
            typ = ev.get("type")
            if typ and typ not in ("response.audio_transcript.delta",):
                log.info("[%s] control %s", self.stream_sid, typ)

        self.control = ControlChannel(live.call_id, self.http, on_event=on_event)
        try:
            await self.control.connect()
        except Exception:
            log.exception("[%s] control WS failed (media may still work)", self.stream_sid)

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
            log.warning("[%s] ICE gathering timeout (state=%s); continuing with partial candidates",
                        self.stream_sid, self.pc.iceGatheringState)

    async def _pump_remote_audio(self, track: MediaStreamTrack) -> None:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=audio.MULAW_RATE)
        try:
            while not self._closed:
                try:
                    frame = await track.recv()
                except MediaStreamError:
                    break
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

    def on_twilio_media(self, payload_b64: str) -> None:
        self.inbound.push_mulaw_b64(payload_b64)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.control:
            await self.control.close()
        try:
            await self.pc.close()
        except Exception:
            log.exception("pc.close failed")
        log.info("[%s] session closed call_id=%s ice=%s conn=%s",
                 self.stream_sid, self.call_id, self.ice_state, self.connection_state)
