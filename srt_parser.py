# SPDX-License-Identifier: GPL-3.0-or-later
"""SRT decoding, validation and conservative complete-sentence boundaries."""

from pathlib import Path
import math
import re

STAMP = r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})"
TIMES = re.compile(r"^" + STAMP + r"\s*-->\s*" + STAMP + r"\s*$")
END = re.compile(r'[。！？.!?][”’"\'）)\]】]*\s*$')


def _seconds(parts):
    h, m, s, ms = map(int, parts)
    if m > 59 or s > 59:
        raise ValueError("字幕时间的分或秒必须小于 60")
    return h * 3600 + m * 60 + s + ms / 1000


def parse_srt(text):
    cues, warnings = [], []
    for block in re.split(
        r"\n\s*\n", text.lstrip("\ufeff").replace("\r\n", "\n").strip()
    ):
        lines = block.splitlines()
        if not lines:
            continue
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise ValueError("SRT 必须包含字幕编号、时间戳和内容")
        match = TIMES.fullmatch(lines[1].strip())
        if not match:
            raise ValueError(f"字幕 {lines[0]} 的时间戳格式错误")
        start, end = _seconds(match.groups()[:4]), _seconds(match.groups()[4:])
        if end <= start:
            raise ValueError(f"字幕 {lines[0]} 的结束时间必须晚于开始时间")
        content = "\n".join(lines[2:]).strip()
        if not content:
            raise ValueError("字幕内容不能为空")
        if cues and start < cues[-1]["end"]:
            warnings.append(f"字幕 {lines[0]} 与前条重叠或未按时间排序")
        cues.append(dict(index=int(lines[0]), start=start, end=end, text=content))
    if not cues:
        raise ValueError("SRT 中没有字幕")
    return cues, warnings


def read_srt(path):
    raw = Path(path).read_bytes()
    encodings = (
        ("utf-16",)
        if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
        else ("utf-8-sig", "gb18030")
    )
    for encoding in encodings:
        try:
            return parse_srt(raw.decode(encoding))
        except UnicodeError:
            continue
    raise ValueError("无法识别字幕编码，请转换为 UTF-8")


def spoken_text(cues):
    return "\n".join(re.sub(r"<[^>]+>", "", c["text"]) for c in cues)


def sentence_boundaries(cues, duration, trusted=False):
    """Never invent a mid-cue boundary. TTS SentenceBoundary is authoritative."""
    previous = 0
    result = []
    for cue in cues:
        start, end = float(cue["start"]), float(cue["end"])
        if (
            not all(math.isfinite(v) for v in (start, end))
            or start < previous - 0.001
            or end <= start
        ):
            raise ValueError("时间轴存在重叠、倒序或无效时间，请先修正字幕")
        previous = end
        if 0 < end < duration - 0.001 and (trusted or END.search(cue["text"])):
            result.append(end)
    return sorted(set(result))


def duration_warning(cues, duration):
    if (
        duration > 0
        and cues
        and abs(max(c["end"] for c in cues) - duration) > max(0.5, duration * 0.03)
    ):
        return f'字幕结束 {max(c["end"] for c in cues):.2f}s，配音 {duration:.2f}s；请检查或手动同步字幕。'
    return ""


def scale_cues(cues, duration):
    if not cues or duration <= 0:
        raise ValueError("需要有效音频和字幕")
    factor = duration / max(c["end"] for c in cues)
    return [{**c, "start": c["start"] * factor, "end": c["end"] * factor} for c in cues]
