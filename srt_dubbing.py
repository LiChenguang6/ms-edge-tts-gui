# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""SRT -> timestamp-aligned MP3 dubbing for Edge TTS Voice Studio.

The source SRT is authoritative: narration starts at each cue's timestamp,
never overlaps the next cue, and missing intervals contain encoded silence.
Audio that cannot fit is retried at a faster speech rate or rejected; speech
is never truncated to make a subtitle fit.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, replace
from html import unescape
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Callable, Sequence

from tts_engine import TTSConfig, TTSEngine, UserCanceled


SAMPLE_RATE = 24000
MAX_RATE = 100
_TIME = r"\d{2}:\d{2}:\d{2}[,.]\d{1,3}"
_TIMING = re.compile(rf"^({_TIME})\s*-->\s*({_TIME})(?:\s+.*)?$")
_TAG = re.compile(r"<[^>]*>|\{\\an\d+\}")


@dataclass(frozen=True)
class SRTCue:
    index: int
    start_ms: int
    end_ms: int
    text: str


class SRTDurationError(ValueError):
    """A spoken cue cannot fit its SRT interval without losing speech."""


def _timestamp_ms(raw: str) -> int:
    hours, minutes, seconds = raw.replace(",", ".").split(":")
    seconds, fraction = seconds.split(".")
    if int(minutes) >= 60 or int(seconds) >= 60:
        raise ValueError(f"无效的 SRT 时间戳：{raw}")
    return (
        (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000
        + int(fraction.ljust(3, "0"))
    )


def parse_srt(content: str) -> list[SRTCue]:
    """Parse standard UTF text SRT; reject overlap and malformed intervals."""
    normalized = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block_number, block in enumerate(re.split(r"\n\s*\n", normalized.strip()), start=1):
        if not block.strip():
            continue
        lines = block.strip().split("\n")
        timing_pos = 1 if len(lines) > 1 and lines[0].strip().isdigit() else 0
        if timing_pos >= len(lines):
            raise ValueError(f"第 {block_number} 条字幕缺少时间轴。")
        match = _TIMING.fullmatch(lines[timing_pos].strip())
        if not match:
            raise ValueError(f"第 {block_number} 条字幕时间轴格式错误：{lines[timing_pos]}")
        start_ms, end_ms = (_timestamp_ms(v) for v in match.groups())
        if end_ms <= start_ms:
            raise ValueError(f"第 {block_number} 条字幕结束时间必须晚于开始时间。")
        if cues and start_ms < cues[-1].end_ms:
            raise ValueError(f"第 {block_number} 条字幕与前一条重叠，无法制作单声道顺序配音。")
        spoken = unescape(_TAG.sub("", " ".join(lines[timing_pos + 1:]))).strip()
        # Empty subtitle blocks (e.g. formatting-only cues) need no voice.
        if spoken:
            cues.append(SRTCue(block_number, start_ms, end_ms, spoken))
    if not cues:
        raise ValueError("没有找到可朗读的 SRT 字幕。")
    return cues


def read_srt(path: str | Path) -> list[SRTCue]:
    data = Path(path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return parse_srt(data.decode("utf-16"))
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return parse_srt(data.decode(encoding))
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别 SRT 文件编码，请保存为 UTF-8。")


def trim_edge_silence(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Remove only short near-silent MP3 edge padding, retaining a 25ms guard."""
    if not pcm or len(pcm) % 2:
        return pcm
    samples = array("h")
    samples.frombytes(pcm)
    if not samples:
        return pcm
    threshold = 80
    guard = int(sample_rate * 0.025)
    max_trim = int(sample_rate * 0.35)
    first = 0
    while first < min(max_trim, len(samples)) and abs(samples[first]) <= threshold:
        first += 1
    last = len(samples)
    while last > max(first, len(samples) - max_trim) and abs(samples[last - 1]) <= threshold:
        last -= 1
    # Do not remove audio when a clip is essentially silent.
    if first >= last:
        return pcm
    left = max(0, first - guard)
    right = min(len(samples), last + guard)
    return pcm[left * 2:right * 2]


def decode_mp3(path: str) -> bytes:
    try:
        import miniaudio
    except ImportError as exc:
        raise RuntimeError("缺少 miniaudio 音频处理组件，请安装 requirements.txt。") from exc
    # bytes-based decode handles Windows paths containing Chinese characters.
    decoded = miniaudio.decode(
        Path(path).read_bytes(),
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=SAMPLE_RATE,
    )
    return trim_edge_silence(bytes(decoded.samples))


def _percent(rate: str) -> int:
    matched = re.fullmatch(r"([+-]?\d+)%", rate.strip())
    if not matched:
        raise ValueError(f"无效语速：{rate}")
    return int(matched.group(1))


def _as_rate(value: int) -> str:
    return f"{value:+d}%"


def _add_silence(encoder, output, frames: int) -> None:
    # Stream silence in small blocks to keep memory usage bounded.
    while frames > 0:
        chunk = min(frames, SAMPLE_RATE)
        output.write(encoder.encode(b"\x00\x00" * chunk))
        frames -= chunk


def dub_srt(
    cues: Sequence[SRTCue],
    output_path: str | Path,
    cfg: TTSConfig,
    *,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    auto_speed: bool = True,
) -> dict:
    """Produce one aligned MP3, atomically replacing output only on success.

    A cue that runs past its end is regenerated at a faster Edge TTS rate.
    We never clip or overlap speech. The original SRT remains untouched.
    """
    if not cues:
        raise ValueError("没有可合成的字幕。")
    # Also validate callers that construct cue objects without parse_srt().
    previous_end = 0
    for cue in cues:
        if cue.start_ms < previous_end or cue.end_ms <= cue.start_ms:
            raise ValueError(f"第 {cue.index} 条字幕时间轴错误或重叠。")
        previous_end = cue.end_ms

    try:
        import lameenc
    except ImportError as exc:
        raise RuntimeError("缺少 lameenc MP3 编码组件，请安装 requirements.txt。") from exc

    stop = cancel_event or threading.Event()
    progress = on_progress or (lambda done, total, rate: None)
    log = on_log or (lambda msg: None)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_output = tempfile.mkstemp(
        prefix=".srt-dubbing-", suffix=".mp3", dir=str(destination.parent)
    )
    os.close(fd)
    speed_changes = []
    try:
        with tempfile.TemporaryDirectory(prefix="edge-srt-") as segment_dir:
            encoder = lameenc.Encoder()
            encoder.set_bit_rate(64)
            encoder.set_in_sample_rate(SAMPLE_RATE)
            encoder.set_channels(1)
            encoder.set_quality(2)
            current_frame = 0
            with open(temp_output, "wb") as output:
                for position, cue in enumerate(cues, start=1):
                    if stop.is_set():
                        raise UserCanceled()
                    start_frame = round(cue.start_ms * SAMPLE_RATE / 1000)
                    end_frame = round(cue.end_ms * SAMPLE_RATE / 1000)
                    allowed_frames = end_frame - start_frame
                    if start_frame < current_frame:
                        raise ValueError(f"第 {cue.index} 条字幕与前一条发生时间重叠。")
                    _add_silence(encoder, output, start_frame - current_frame)
                    current_frame = start_frame
                    requested_rate = _percent(cfg.rate)
                    rate = requested_rate
                    pcm = b""
                    # Retry with a calculated faster rate, at most 3 syntheses.
                    for attempt in range(3):
                        if stop.is_set():
                            raise UserCanceled()
                        part_path = str(Path(segment_dir) / f"cue_{position:05d}.mp3")
                        # SRT controls gaps; do not apply extra native pause settings.
                        part_cfg = replace(
                            cfg, rate=_as_rate(rate), sentence_pause_ms=0,
                            sentence_pause_marks=(), line_pause_enabled=False,
                            line_pause_ms=0,
                        )
                        engine = TTSEngine(cancel_event=stop)
                        result = engine.generate(cue.text, part_path, part_cfg)
                        if result.get("status") == "canceled" or stop.is_set():
                            raise UserCanceled()
                        if result.get("status") != "done":
                            raise RuntimeError(
                                f"第 {cue.index} 条字幕语音生成失败："
                                f"{result.get('error', '未知错误')}"
                            )
                        pcm = decode_mp3(part_path)
                        if not pcm:
                            raise RuntimeError(f"第 {cue.index} 条字幕生成了空音频。")
                        sample_frames = len(pcm) // 2
                        if sample_frames <= allowed_frames:
                            break
                        if not auto_speed or attempt == 2 or rate >= MAX_RATE:
                            raise SRTDurationError(
                                f"第 {cue.index} 条字幕（{cue.start_ms / 1000:.3f}–"
                                f"{cue.end_ms / 1000:.3f} 秒）只有 "
                                f"{allowed_frames / SAMPLE_RATE:.2f} 秒，"
                                f"但朗读需要 {sample_frames / SAMPLE_RATE:.2f} 秒。"
                                "请缩短文案或延长字幕时间。"
                            )
                        # Slight safety margin accounts for nonlinear rate response.
                        suggested = int((100 + rate) * sample_frames / allowed_frames * 1.08 - 100)
                        rate = min(MAX_RATE, max(rate + 10, suggested))
                        log(f"第 {cue.index} 条超过字幕时长，尝试语速 {_as_rate(rate)}")
                    if rate != requested_rate:
                        speed_changes.append((cue.index, rate))
                    output.write(encoder.encode(pcm))
                    current_frame += len(pcm) // 2
                    progress(position, len(cues), rate)
                    log(f"已完成字幕 {position}/{len(cues)}")
                final_end = round(cues[-1].end_ms * SAMPLE_RATE / 1000)
                _add_silence(encoder, output, final_end - current_frame)
                output.write(encoder.flush())
        if stop.is_set():
            raise UserCanceled()
        os.replace(temp_output, destination)
        return {
            "status": "done",
            "path": str(destination),
            "cues": len(cues),
            "duration_seconds": cues[-1].end_ms / 1000,
            "speed_changes": speed_changes,
        }
    finally:
        if os.path.exists(temp_output):
            os.unlink(temp_output)
