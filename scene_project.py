# SPDX-License-Identifier: GPL-3.0-or-later
"""Import opaque image plans and map stable IDs to actual narration intervals."""

from copy import deepcopy
import json
import math
from pathlib import Path
import re
import unicodedata
from global_settings import atomic_json
from project_manager import ImageClip, new_project
from timeline_planner import PlanningError

MOTIONS = {"none", "zoom_in", "zoom_out", "left_right", "right_left", "vertical"}


def parse_script(path):
    result = {}
    for number, line in enumerate(
        Path(path).read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        if not line.strip():
            continue
        if "|" not in line:
            raise ValueError(f"script.txt 第 {number} 行缺少 ID|解说文本")
        scene_id, text = (part.strip() for part in line.split("|", 1))
        if not scene_id or not text or scene_id in result:
            raise ValueError(f"script.txt 第 {number} 行编号重复或文本为空：{scene_id}")
        result[scene_id] = {"line": number, "text": text}
    if not result:
        raise ValueError("script.txt 没有场景解说")
    return result


def _time(value, label):
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{label} 需要非负秒数或 null")
    return float(value)


def import_scene_project(directory, store=None):
    directory = Path(directory).resolve()
    path = directory / "image_plan.json"
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("image_plan.json 顶层必须为对象")
    if payload.get("schema_version") not in (1, "1", "1.0"):
        raise ValueError("image_plan.json 需要 schema_version: 1")
    script_path = (path.parent / payload.get("script_file", "script.txt")).resolve()
    script = parse_script(script_path)
    scenes = payload.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("image_plan.json 的 scenes 不能为空")
    project = new_project(store)
    project.mode = "scenes"
    project.project_dir, project.plan_file, project.script_file = (
        str(directory),
        str(path),
        str(script_path),
    )
    project.output_dir = str(directory / "output")
    project.plan_metadata = deepcopy(payload)
    video = payload.get("video", {})
    if not isinstance(video, dict):
        raise ValueError("video 应为 JSON 对象")
    project.video.update(
        {k: v for k, v in video.items() if k not in ("ffmpeg", "ffprobe")}
    )
    resolution = video.get("resolution")
    if resolution is not None:
        try:
            parts = (
                re.split("[x×]", resolution)
                if isinstance(resolution, str)
                else resolution
            )
            project.video["width"], project.video["height"] = map(int, parts)
        except (ValueError, TypeError) as exc:
            raise ValueError("分辨率应为 1080x1920 或 [1080,1920]") from exc
    elif (
        video.get("aspect_ratio") in ("9:16", "16:9", "1:1")
        and "width" not in video
        and "height" not in video
    ):
        project.video["width"], project.video["height"] = {
            "9:16": (1080, 1920),
            "16:9": (1920, 1080),
            "1:1": (1080, 1080),
        }[video["aspect_ratio"]]
    try:
        for key in ("width", "height", "fps"):
            project.video[key] = int(project.video[key])
    except (ValueError, TypeError) as exc:
        raise ValueError("视频分辨率和 FPS 必须为整数") from exc
    if any(
        project.video[k] <= 0 or project.video[k] % 2 or project.video[k] > 4096
        for k in ("width", "height")
    ) or project.video["fps"] not in (24, 25, 30, 60):
        raise ValueError("分辨率需为 4096 以内正偶数，FPS 支持 24/25/30/60")
    divisor = math.gcd(project.video["width"], project.video["height"])
    project.video["aspect_ratio"] = (
        f"{project.video['width']//divisor}:{project.video['height']//divisor}"
    )
    seen = set()
    for order, item in enumerate(scenes):
        if not isinstance(item, dict):
            raise ValueError(f"场景 {order+1} 不是对象")
        scene_id = item.get("id")
        if not isinstance(scene_id, str) or scene_id not in script or scene_id in seen:
            raise ValueError(
                f"场景编号重复、非字符串或与 script.txt 不对应：{scene_id}"
            )
        seen.add(scene_id)
        entry = script[scene_id]
        if item.get("script_line") not in (
            None,
            entry["line"],
            str(entry["line"]),
            scene_id,
        ):
            raise ValueError(f"场景 {scene_id} 的 script_line 与脚本实际行不一致")
        if (
            item.get("narration") is not None
            and item["narration"].strip() != entry["text"]
        ):
            raise ValueError(
                f"场景 {scene_id} 的 narration 与 script.txt 不一致，请先校对"
            )
        image_file = item.get("image_file")
        if not isinstance(image_file, str) or not image_file:
            raise ValueError(f"场景 {scene_id} 缺少 image_file")
        image = (path.parent / image_file).resolve()
        start, end = _time(item.get("start"), scene_id + ".start"), _time(
            item.get("end"), scene_id + ".end"
        )
        if start is not None and end is not None and end <= start:
            raise ValueError(f"场景 {scene_id} 的 end 必须大于 start")
        motion = item.get("motion") or project.video["motion"]
        if isinstance(motion, dict):
            motion = motion.get("type", "none")
        if motion not in MOTIONS:
            raise ValueError(f"场景 {scene_id} 的 motion 不支持：{motion}")
        project.images.append(
            ImageClip(
                str(image),
                start or 0,
                end or 0,
                import_order=order,
                scene_id=scene_id,
                narration=entry["text"],
                script_line=entry["line"],
                motion=motion,
                requested_start=start,
                requested_end=end,
                timing_source=(
                    "json" if start is not None or end is not None else "auto"
                ),
                timing_origins={
                    "start": "json" if start is not None else "auto",
                    "end": "json" if end is not None else "auto",
                },
                status="pending" if image.is_file() else "missing",
                metadata=deepcopy(item),
            )
        )
    missing = set(script) - seen
    if missing:
        raise ValueError(
            "脚本场景未出现在 JSON，不能静默丢弃解说：" + ", ".join(sorted(missing))
        )
    return project


def _normal(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text)).casefold()


def map_existing_cues(project, cues, duration):
    """Exact sequential text matching only; never infer time by character count."""
    if not math.isfinite(duration) or duration <= 0:
        raise PlanningError("已有音频实际时长无效")
    from srt_parser import sentence_boundaries

    sentence_boundaries(cues, duration, True)  # Validate monotonic timestamps.
    if not cues or abs(max(c["end"] for c in cues) - duration) > max(
        0.5, duration * 0.03
    ):
        raise PlanningError(
            "已有字幕末尾与真实音频时长差异过大，不能可靠复用；请校对 SRT 或逐段配音"
        )
    at = 0
    spans = {}
    mapped = []
    for clip in project.images:
        target = _normal(clip.narration)
        text = ""
        group = []
        while at < len(cues) and text != target:
            cue = cues[at]
            text += _normal(cue["text"])
            group.append(cue)
            at += 1
            if not target.startswith(text):
                raise PlanningError(
                    f"已有 SRT 无法可靠对应场景 {clip.scene_id}，请逐场景生成配音或校对 SRT"
                )
        if text != target or not group:
            raise PlanningError(f"已有 SRT 缺少场景 {clip.scene_id} 的完整解说")
        if group[-1]["end"] > duration + 0.05:
            raise PlanningError(f"场景 {clip.scene_id} 字幕超出实际音频")
        spans[clip.scene_id] = (group[0]["start"], group[-1]["end"])
        mapped.extend({**cue, "scene_id": clip.scene_id} for cue in group)
    if at != len(cues):
        raise PlanningError("SRT 含有脚本之外的解说，不能静默丢弃")
    apply_scene_spans(project, spans, duration, mapped)


def apply_scene_spans(project, spans, duration, cues, regenerating=False):
    """Make contiguous display intervals; requested JSON/manual cuts take priority."""
    clips = project.images
    automatic = (
        [0.0] + [float(spans[c.scene_id][0]) for c in clips[1:]] + [float(duration)]
    )
    explicit = {}
    warnings = []
    for i, clip in enumerate(clips):
        if regenerating:
            for side in ("start", "end"):
                origin = clip.timing_origins.get(side, clip.timing_source)
                locked = (
                    clip.locked if side == "end" else bool(i and clips[i - 1].locked)
                )
                if origin == "manual" and not locked:
                    setattr(clip, "requested_" + side, None)
                    clip.timing_origins[side] = "auto"
        for boundary, value in ((i, clip.requested_start), (i + 1, clip.requested_end)):
            if value is None:
                continue
            if boundary in explicit and abs(explicit[boundary] - value) > 0.001:
                raise PlanningError(
                    f"场景 {clip.scene_id} 的手动起止与相邻场景冲突，原编辑记录已保留"
                )
            explicit[boundary] = value
    if (0 in explicit and abs(explicit[0]) > 0.001) or (
        len(clips) in explicit and abs(explicit[len(clips)] - duration) > 0.001
    ):
        raise PlanningError(
            f"JSON/锁定时间与实际配音总长 {duration:.6f}s 冲突；不能裁剪配音。请改用自动时间或编辑锁定边界。"
        )
    cuts = [explicit.get(i, t) for i, t in enumerate(automatic)]
    cuts[0], cuts[-1] = 0.0, float(duration)
    if any(b <= a for a, b in zip(cuts, cuts[1:])):
        raise PlanningError("手动边界造成倒序或零时长，原记录已保留，请解除冲突的锁定")
    for i, clip in enumerate(clips):
        clip.start, clip.end = cuts[i], cuts[i + 1]
        indices = [
            n + 1
            for n, c in enumerate(cues)
            if c["end"] > clip.start and c["start"] < clip.end
        ]
        clip.sentence_start = min(indices, default=0)
        clip.sentence_end = max(indices, default=0)
        clip.status = (
            "missing" if not Path(clip.path).is_file() and not clip.skipped else "ready"
        )
        a, b = spans[clip.scene_id]
        clip.metadata["tts_start"], clip.metadata["tts_end"] = a, b
        if i and any(c["start"] + 0.001 < clip.start < c["end"] - 0.001 for c in cues):
            warnings.append(
                f"场景 {clip.scene_id} 的手动切换 {clip.start:.3f}s 位于解说中间"
            )
    project.audio_duration = float(duration)
    project.cues = cues
    project.timeline = {
        "boundary": "SentenceBoundary",
        "sentences": deepcopy(cues),
        "audio_duration": duration,
    }
    project.timing_warnings = warnings
    return clips


def write_resolved_plan(project, filename=None):
    if not project.audio_duration or any(c.end <= c.start for c in project.images):
        raise PlanningError("请先生成或可靠映射真实配音")
    target = Path(filename or Path(project.output_dir) / "resolved_image_plan.json")
    if project.plan_file and target.resolve() == Path(project.plan_file).resolve():
        raise ValueError("不能覆盖原始 image_plan.json")
    data = deepcopy(project.plan_metadata)
    data["scenes"] = [
        {
            **deepcopy(c.metadata),
            "id": c.scene_id,
            "image_file": str(Path(c.path).resolve()),
            "script_line": c.script_line,
            "narration": c.narration,
            "start": c.start,
            "end": c.end,
            "motion": c.motion,
            "timing_source": c.timing_source,
            "locked": c.locked,
            "skipped_image": c.skipped,
            "requested_start": c.requested_start,
            "requested_end": c.requested_end,
        }
        for c in project.images
    ]
    import os

    for item in data["scenes"]:
        try:
            item["image_file"] = os.path.relpath(item["image_file"], target.parent)
        except ValueError:
            pass
    try:
        data["script_file"] = os.path.relpath(project.script_file, target.parent)
    except ValueError:
        data["script_file"] = project.script_file
    try:
        data["audio_file"] = os.path.relpath(
            Path(project.audio).resolve(), target.parent
        )
    except ValueError:
        data["audio_file"] = str(Path(project.audio).resolve())
    data["audio_duration"] = project.audio_duration
    data["video"] = deepcopy(project.video)
    atomic_json(target, data)
    return str(target)
