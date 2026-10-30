# SPDX-License-Identifier: GPL-3.0-or-later
"""Portable, versioned video project state. Source media is never deleted."""

from dataclasses import asdict, dataclass, field
from pathlib import Path
import json
import math
import os
import tempfile


@dataclass
class ImageClip:
    path: str
    start: float = 0
    end: float = 0
    sentence_start: int = 0
    sentence_end: int = 0
    locked: bool = False  # Locks this clip's end boundary.
    import_order: int = 0
    scene_id: str = ""
    narration: str = ""
    script_line: int = 0
    motion: str = ""
    requested_start: float | None = None
    requested_end: float | None = None
    timing_source: str = "auto"
    timing_origins: dict = field(default_factory=dict)
    skipped: bool = False
    status: str = "pending"
    metadata: dict = field(default_factory=dict)


@dataclass
class Project:
    audio: str = ""
    audio_duration: float = 0
    source_srt: str = ""
    subtitle_srt: str = ""
    subtitle_cues: list = field(default_factory=list)
    source_cues: list = field(default_factory=list)
    cues: list = field(default_factory=list)
    timeline: dict = field(default_factory=dict)
    images: list[ImageClip] = field(default_factory=list)
    external_video: str = ""
    bgm: str = ""
    output_dir: str = ""
    manual_order: bool = False
    narration: dict = field(default_factory=dict)
    mode: str = "legacy"
    project_dir: str = ""
    script_file: str = ""
    plan_file: str = ""
    plan_metadata: dict = field(default_factory=dict)
    scene_audio: dict = field(default_factory=dict)
    timing_warnings: list = field(default_factory=list)
    timing_conflict: str = ""
    rendered_pause: float | None = None
    video: dict = field(
        default_factory=lambda: dict(
            width=1080,
            height=1920,
            fps=30,
            fit="contain",
            motion="none",
            transition="cut",
            transition_duration=0.3,
            zoom=1.0,
            x=0.5,
            y=0.5,
            encoder="libx264",
        )
    )
    style: dict = field(
        default_factory=lambda: dict(
            font="Microsoft YaHei",
            font_file="",
            size=54,
            color="#FFFFFF",
            outline_color="#000000",
            outline=3,
            shadow=1,
            margin=100,
            alignment=2,
            burn=True,
            subtitle_source="current",
            wrap=22,
        )
    )
    sound: dict = field(
        default_factory=lambda: dict(
            voice_volume=1.0, bgm_volume=0.15, loop=True, fade=1.0
        )
    )
    planning: dict = field(
        default_factory=lambda: dict(
            desired=8.0, minimum=1.0, maximum=30.0, target=0.0, paragraph_pause=0.15
        )
    )

    def missing_media(self):
        paths = [
            self.audio,
            self.source_srt,
            self.external_video,
            self.bgm,
            self.style.get("font_file", ""),
            *(i.path for i in self.images if not i.skipped),
        ]
        return [p for p in dict.fromkeys(paths) if p and not Path(p).is_file()]


def save_project(project, filename):
    dest = Path(filename).resolve()
    data = asdict(project)

    def relative(path):
        if not path:
            return ""
        try:
            return os.path.relpath(Path(path).resolve(), dest.parent)
        except ValueError:  # Different Windows drives.
            return str(Path(path).resolve())

    for key in (
        "audio",
        "source_srt",
        "subtitle_srt",
        "external_video",
        "bgm",
        "output_dir",
        "project_dir",
        "script_file",
        "plan_file",
    ):
        data[key] = relative(data[key])
    data["style"]["font_file"] = relative(data["style"].get("font_file", ""))
    for image in data["images"]:
        image["path"] = relative(image["path"])
    for record in data["scene_audio"].values():
        if record.get("wav"):
            record["wav"] = relative(record["wav"])
    data["version"] = 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(name, dest)
    finally:
        Path(name).unlink(missing_ok=True)


def load_project(filename):
    source = Path(filename).resolve()
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    if data.pop("version", None) != 1:
        raise ValueError("不支持的工程版本")
    base = new_project()
    for key in ("video", "style", "sound", "planning", "narration"):
        data[key] = {**getattr(base, key), **data.get(key, {})}

    for tool in ("ffmpeg", "ffprobe"):
        requested = data["video"].get(tool, "")
        fallback = base.video.get(tool, "")
        if (
            requested
            and not Path(requested).is_file()
            and fallback
            and Path(fallback).is_file()
        ):
            data["video"][tool] = fallback

    def absolute(path):
        return str((source.parent / path).resolve()) if path else ""

    for key in (
        "audio",
        "source_srt",
        "subtitle_srt",
        "external_video",
        "bgm",
        "output_dir",
        "project_dir",
        "script_file",
        "plan_file",
    ):
        data[key] = absolute(data[key]) if key in data else getattr(base, key)
    data["style"]["font_file"] = absolute(data["style"].get("font_file", ""))
    for record in data.get("scene_audio", {}).values():
        if record.get("wav"):
            record["wav"] = absolute(record["wav"])
    data["images"] = [
        ImageClip(**{**item, "path": absolute(item["path"])})
        for item in data.get("images", [])
    ]
    return Project(**data)


def capture_tts_audio(project, path, timeline, directory):
    """Persistent copies, independent from the legacy transient audio preview."""
    import shutil
    import uuid
    from tts_engine import timeline_to_srt
    from media_tools import probe

    root = Path(directory) / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    audio = root / "voice.mp3"
    shutil.copy2(path, audio)
    duration = probe(audio, project.video.get("ffprobe", ""))["duration"]
    project.audio = str(audio.resolve())
    project.timeline = timeline or {}
    if timeline:
        project.cues = [dict(c) for c in timeline.get("sentences", [])]
        (root / "voice.timeline.json").write_text(
            json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (root / "voice.srt").write_text(timeline_to_srt(timeline), encoding="utf-8-sig")
    else:
        project.cues = []  # Never attach stale imported timing to fresh narration.
    project.audio_duration = duration


def new_project(store=None):
    from global_settings import GlobalSettings

    project = Project()
    defaults = (store or GlobalSettings()).read()
    for group in ("video", "style", "sound", "planning", "narration"):
        values = dict(defaults.get(group, {}))
        if group == "style":
            values.pop("subtitle_source", None)
        getattr(project, group).update(values)
    project.output_dir = defaults.get("output_dir", "")
    return project
