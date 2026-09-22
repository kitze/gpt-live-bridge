"""μ-law 8 kHz (Twilio) ↔ PCM for WebRTC / aiortc."""

from __future__ import annotations

import audioop
from array import array

import av
import numpy as np


MULAW_RATE = 8000
WEBRTC_RATE = 48000
WEBRTC_LAYOUT = "mono"
WEBRTC_FORMAT = "s16"


def mulaw_b64_to_pcm16(payload_b64: str) -> bytes:
    import base64

    mulaw = base64.b64decode(payload_b64)
    return audioop.ulaw2lin(mulaw, 2)


def pcm16_to_mulaw_b64(pcm16: bytes) -> str:
    import base64

    mulaw = audioop.lin2ulaw(pcm16, 2)
    return base64.b64encode(mulaw).decode("ascii")


def resample_pcm16(pcm16: bytes, from_rate: int, to_rate: int) -> bytes:
    if from_rate == to_rate or not pcm16:
        return pcm16
    converted, _ = audioop.ratecv(pcm16, 2, 1, from_rate, to_rate, None)
    return converted


def pcm16_to_audio_frame(pcm16: bytes, sample_rate: int = WEBRTC_RATE) -> av.AudioFrame:
    """Build a mono s16 AudioFrame for aiortc outbound track."""
    if not pcm16:
        # 20ms silence
        n = sample_rate // 50
        samples = np.zeros(n, dtype=np.int16)
    else:
        samples = np.frombuffer(pcm16, dtype=np.int16)
    frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
    frame.sample_rate = sample_rate
    frame.pts = None
    return frame


def audio_frame_to_pcm16(frame: av.AudioFrame, target_rate: int = MULAW_RATE) -> bytes:
    """Convert an aiortc/av AudioFrame to mono s16 PCM at target_rate."""
    # Resample/reformat via av
    resampler = av.AudioResampler(format="s16", layout="mono", rate=target_rate)
    # Ensure planar/packed ndarray path
    frame = frame.to_ndarray() if False else frame  # keep frame object
    out_frames = resampler.resample(frame)
    chunks: list[bytes] = []
    for of in out_frames:
        arr = of.to_ndarray()
        if arr.ndim == 2:
            # layout mono → shape (1, n) or (n, 1)
            if arr.shape[0] == 1:
                samples = arr[0]
            elif arr.shape[1] == 1:
                samples = arr[:, 0]
            else:
                samples = arr[0]
        else:
            samples = arr
        if samples.dtype != np.int16:
            # float → int16
            samples = np.clip(samples, -1.0, 1.0)
            samples = (samples * 32767.0).astype(np.int16)
        chunks.append(samples.astype(np.int16).tobytes())
    return b"".join(chunks)


def silence_mulaw_b64(duration_ms: int = 20) -> str:
    n = int(MULAW_RATE * duration_ms / 1000)
    pcm = b"\x00\x00" * n
    return pcm16_to_mulaw_b64(pcm)
