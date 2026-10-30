# SPDX-License-Identifier: GPL-3.0-or-later
"""Sequential existing Edge TTS calls; PCM-frame accounting avoids timing drift."""

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import shutil
import threading
import uuid
import wave
from global_settings import atomic_json
from media_tools import (
    MediaError,
    ExportCancelled,
    run_ffmpeg,
    probe,
    ensure_writable,
    check_disk_space,
)
from scene_project import apply_scene_spans, map_existing_cues, write_resolved_plan
from srt_parser import read_srt
from tts_engine import TTSEngine, timeline_to_srt

RATE = 24000


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def wav_info(path):
    with wave.open(str(path), "rb") as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (
            RATE,
            1,
            2,
        ):
            raise ValueError("缓存 WAV 需要 24kHz 单声道 PCM16")
        return source.getnframes()


def scene_signature(clip, cfg):
    cfg = replace(
        cfg,
        sentence_pause_ms=0,
        sentence_pause_marks=(),
        line_pause_enabled=False,
        line_pause_ms=0,
    )
    return hashlib.sha256(
        json.dumps(
            [clip.scene_id, clip.narration, asdict(cfg)],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()


def prepare_scene_audio(
    project,
    cfg,
    cancel=None,
    progress=None,
    status=None,
    engine_factory=TTSEngine,
    request_interval=1.5,
):
    cancel = cancel or threading.Event()
    if project.mode != "scenes" or not project.images:
        raise MediaError("请先导入视频项目")
    pause = float(project.planning.get("paragraph_pause", 0.15))
    if not 0 <= pause <= 30:
        raise MediaError("段落停顿需要 0–30 秒")
    cfg = replace(
        cfg,
        sentence_pause_ms=0,
        sentence_pause_marks=(),
        line_pause_enabled=False,
        line_pause_ms=0,
    )
    output = ensure_writable(project.output_dir)
    cache = ensure_writable(output / ".scene-cache")
    manifest_path = cache / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("records", {}), dict
    ):
        manifest = {}
    manifest.setdefault("records", {})
    records = {}
    requested = False
    for index, clip in enumerate(project.images):
        if cancel.is_set():
            raise ExportCancelled("配音已取消，已完成场景缓存保留，可继续生成")
        signature = scene_signature(clip, cfg)
        wav = cache / (signature + ".wav")
        record = manifest["records"].get(signature)
        if not isinstance(record, dict):
            record = None
        prior = project.scene_audio.get(clip.scene_id, {})
        if (
            record is None
            and prior.get("fingerprint") == signature
            and prior.get("wav")
        ):
            try:
                source = Path(prior["wav"])
                if (
                    source.is_file()
                    and wav_info(source) == prior["frames"]
                    and file_hash(source) == prior["sha256"]
                ):
                    if source.resolve() != wav.resolve():
                        shutil.copyfile(source, wav)
                    record = {
                        key: deepcopy(prior[key])
                        for key in ("frames", "sha256", "timeline")
                    }
                    manifest["records"][signature] = record
                    atomic_json(manifest_path, manifest)
            except (OSError, ValueError, wave.Error, KeyError, TypeError):
                pass
        valid = False
        if record and wav.is_file():
            try:
                valid = (
                    wav_info(wav) == record["frames"]
                    and file_hash(wav) == record["sha256"]
                )
            except (OSError, ValueError, wave.Error, KeyError, TypeError):
                pass
        if not valid:
            if requested and cancel.wait(request_interval):
                raise ExportCancelled("配音已取消")
            requested = True
            if status:
                status(
                    f"生成配音 {index+1}/{len(project.images)} · 场景 {clip.scene_id}；其余场景顺序请求，限流由原引擎重试"
                )
            mp3 = cache / (signature + ".mp3")
            engine = engine_factory(
                cancel_event=cancel,
                on_log=(
                    (lambda message: status(f"场景 {clip.scene_id}：{message}"))
                    if status
                    else None
                ),
            )
            try:
                result = engine.generate(clip.narration, str(mp3), cfg)
            except Exception as exc:
                if cancel.is_set():
                    raise ExportCancelled("配音已取消") from exc
                raise MediaError(
                    str(exc), stage="逐场景配音", scene=clip.scene_id
                ) from exc
            if result.get("status") == "canceled" or cancel.is_set():
                raise ExportCancelled("配音已取消")
            if result.get("status") != "done":
                raise MediaError(
                    result.get("error", "TTS 生成失败"),
                    stage="逐场景配音",
                    scene=clip.scene_id,
                )
            try:
                check_disk_space(cache, mp3.stat().st_size * 9 + 16 * 1048576)
                run_ffmpeg(
                    ["-i", mp3, "-ar", RATE, "-ac", "1", "-c:a", "pcm_s16le", wav],
                    cancel,
                    ffmpeg=project.video.get("ffmpeg", ""),
                )
                frames = wav_info(wav)
                if frames <= 0:
                    raise MediaError("生成音频没有可用样本")
                record = {
                    "frames": frames,
                    "sha256": file_hash(wav),
                    "timeline": deepcopy(getattr(engine, "timeline", None) or {}),
                }
                manifest["records"][signature] = record
                atomic_json(manifest_path, manifest)
            except ExportCancelled:
                raise
            except (MediaError, OSError, ValueError, wave.Error) as exc:
                raise MediaError(
                    str(exc), stage="场景音频解码", scene=clip.scene_id
                ) from exc
            finally:
                mp3.unlink(missing_ok=True)
        elif status:
            status(f"复用配音 {index+1}/{len(project.images)} · 场景 {clip.scene_id}")
        records[clip.scene_id] = {
            **record,
            "wav": str(wav.resolve()),
            "fingerprint": signature,
        }
        if progress:
            progress((index + 1) / len(project.images) * 0.8)
    pause_frames = round(pause * RATE)
    total = sum(r["frames"] for r in records.values()) + pause_frames * (
        len(project.images) - 1
    )
    check_disk_space(output, total * 4 + 16 * 1048576)
    run_dir = output / ("voice-" + uuid.uuid4().hex)
    run_dir.mkdir()
    success = False
    try:
        target = run_dir / "voice.wav"
        cursor = 0
        spans = {}
        cues = []
        with wave.open(str(target), "wb") as joined:
            joined.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            for index, clip in enumerate(project.images):
                if cancel.is_set():
                    raise ExportCancelled("配音拼接已取消")
                record = records[clip.scene_id]
                start = cursor / RATE
                end = (cursor + record["frames"]) / RATE
                spans[clip.scene_id] = (start, end)
                with wave.open(record["wav"], "rb") as source:
                    while True:
                        block = source.readframes(32768)
                        if not block:
                            break
                        joined.writeframesraw(block)
                local = record["timeline"].get("sentences", [])
                if not local:
                    local = [
                        dict(start=0, end=record["frames"] / RATE, text=clip.narration)
                    ]
                for cue in local:
                    a = max(0, float(cue["start"]))
                    b = min(record["frames"] / RATE, float(cue["end"]))
                    if b <= a:
                        raise MediaError(
                            "TTS 返回的字幕超出真实音频",
                            stage="映射字幕",
                            scene=clip.scene_id,
                        )
                    cues.append(
                        {
                            **cue,
                            "index": len(cues),
                            "scene_id": clip.scene_id,
                            "start": start + a,
                            "end": start + b,
                        }
                    )
                cursor += record["frames"]
                if index < len(project.images) - 1:
                    remaining = pause_frames
                    while remaining:
                        count = min(remaining, 32768)
                        joined.writeframesraw(b"\0" * (count * 2))
                        remaining -= count
                    cursor += pause_frames
        assert cursor == wav_info(target) == total
        duration = cursor / RATE
        project.audio = str(target.resolve())
        project.scene_audio = records
        project.rendered_pause = pause
        project.audio_duration = duration
        project.cues = cues
        project.timeline = {
            "boundary": "SentenceBoundary",
            "sentences": deepcopy(cues),
            "audio_duration": duration,
        }
        (run_dir / "voice.srt").write_text(
            timeline_to_srt(project.timeline), encoding="utf-8-sig"
        )
        atomic_json(run_dir / "voice.timeline.json", project.timeline)
        project.timing_conflict = ""
        try:
            apply_scene_spans(project, spans, duration, cues, regenerating=True)
            write_resolved_plan(project)
        except ValueError as exc:
            project.timing_conflict = str(
                exc
            )  # Keep new real audio and all locked edit records.
        success = True
        if progress:
            progress(1)
        return project
    finally:
        if not success:
            shutil.rmtree(run_dir, ignore_errors=True)


def reuse_existing_audio(project, audio=None, srt=None):
    output = Path(project.output_dir)
    resolved = output / "resolved_image_plan.json"
    if audio is None:
        # Our resolved plan can point to its persistent voice-UUID directory.
        if resolved.is_file():
            try:
                data = json.loads(resolved.read_text(encoding="utf-8-sig"))
                previous = data.get("scenes", [])
                if [(c.get("id"), c.get("narration")) for c in previous] == [
                    (c.scene_id, c.narration) for c in project.images
                ]:
                    candidate = Path(data.get("audio_file", ""))
                    if not candidate.is_absolute():
                        candidate = resolved.parent / candidate
                    if candidate.is_file():
                        audio = candidate
            except (OSError, ValueError):
                pass
        if audio is None:
            audio = next(
                (
                    output / name
                    for name in ("voice.wav", "voice.mp3", "audio.mp3", "audio.wav")
                    if (output / name).is_file()
                ),
                None,
            )
    if audio is None:
        return False
    audio = Path(audio)
    if srt is None:
        candidates = [
            audio.with_suffix(".srt"),
            output / "voice.srt",
            output / "audio.srt",
        ]
        srt = next((p for p in candidates if p.is_file()), None)
    if srt is None:
        return False
    cues, _warnings = read_srt(srt)
    info = probe(audio, project.video.get("ffprobe", ""))
    if not any(s.get("codec_type") == "audio" for s in info["streams"]):
        raise MediaError("已有媒体没有音频流")
    map_existing_cues(project, cues, info["duration"])
    project.audio = str(audio.resolve())
    project.timing_conflict = ""
    return True
