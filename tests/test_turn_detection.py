"""The local audio detector distinguishes an incomplete pause from a finished answer."""

import asyncio
import base64
import io
import json
from pathlib import Path

import av
from livekit import rtc
from livekit.agents import inference

_FIXTURES = Path(__file__).with_name("fixtures") / "turn_audio.json"
_SAMPLE_RATE = 16000
_SAMPLES_PER_FRAME = 320


def decode_clip(encoded: str) -> bytes:
    decoder = av.open(io.BytesIO(base64.b64decode(encoded)), mode="r")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=_SAMPLE_RATE)
    pcm: list[bytes] = []
    for packet in decoder.demux(audio=0):
        for frame in packet.decode():
            pcm.extend(
                output.to_ndarray().tobytes() for output in resampler.resample(frame)
            )
    pcm.extend(output.to_ndarray().tobytes() for output in resampler.resample(None))
    decoder.close()
    return b"".join(pcm)


async def end_probability(encoded: str) -> tuple[float, float]:
    detector = inference.TurnDetector(version="v1-mini")
    stream = detector.stream()
    try:
        pcm = decode_clip(encoded)
        width = _SAMPLES_PER_FRAME * 2
        for offset in range(0, len(pcm), width):
            frame = rtc.AudioFrame(
                pcm[offset : offset + width].ljust(width, b"\x00"),
                sample_rate=_SAMPLE_RATE,
                num_channels=1,
                samples_per_channel=_SAMPLES_PER_FRAME,
            )
            stream.push_audio(frame)
        # The detector consumes pushed frames asynchronously before prediction.
        await asyncio.sleep(0.2)
        result = await asyncio.wait_for(stream.predict(), timeout=5)
        threshold = await stream.unlikely_threshold(None)
        assert threshold is not None
        return result.end_of_turn_probability, threshold
    finally:
        await stream.aclose()


async def test_local_audio_detector_waits_for_unfinished_phrase_then_allows_completion() -> (
    None
):
    fixtures = json.loads(_FIXTURES.read_text(encoding="utf-8"))["clips"]
    unfinished, threshold = await end_probability(fixtures["unfinished"])
    finished, finished_threshold = await end_probability(fixtures["finished"])
    assert threshold == finished_threshold
    assert unfinished < threshold < finished
