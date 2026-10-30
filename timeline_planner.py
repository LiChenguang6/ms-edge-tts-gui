# SPDX-License-Identifier: GPL-3.0-or-later
"""Dynamic programming over complete sentence ends, including fixed boundaries."""

import math
from srt_parser import sentence_boundaries


class PlanningError(ValueError):
    pass


def plan_timeline(project):
    if project.mode == "scenes":
        if project.timing_conflict:
            raise PlanningError(project.timing_conflict)
        if not project.audio or any(c.end <= c.start for c in project.images):
            raise PlanningError(
                "场景模式需要先生成逐段配音或复用可精确映射的音频与 SRT；不按图片平均分配"
            )
        return project.images
    clips = project.images
    if (
        not clips
        or not math.isfinite(project.audio_duration)
        or project.audio_duration <= 0
    ):
        raise PlanningError("请先导入图片和具有实际时长的配音")
    duration = float(project.planning.get("target") or project.audio_duration)
    if duration < project.audio_duration - 0.001:
        raise PlanningError("目标时长短于配音，不能自动裁剪；请调整目标时长")
    trusted = project.timeline.get("boundary") == "SentenceBoundary"
    ends = sentence_boundaries(project.cues, duration, trusted)
    points = [0.0, *ends, duration]
    count = len(clips)
    if count == 1:
        if clips[0].locked and abs(clips[0].end - duration) > 0.001:
            raise PlanningError("锁定边界与总时长冲突")
        clips[0].start, clips[0].end = 0.0, duration
        clips[0].sentence_start, clips[0].sentence_end = (
            1 if project.cues else 0
        ), len(project.cues)
        return clips
    minimum, maximum = float(project.planning["minimum"]), float(
        project.planning["maximum"]
    )
    desired = float(project.planning["desired"]) or duration / count
    if (
        not all(math.isfinite(v) for v in (duration, minimum, maximum, desired))
        or minimum <= 0
        or maximum < minimum
        or desired <= 0
    ):
        raise PlanningError("图片最短、最长和期望时长必须为有效正数")
    # Manual lock can be exceptional, but only after the user explicitly selected it.
    locks = {n + 1: clip.end for n, clip in enumerate(clips) if clip.locked}
    for rank, end in locks.items():
        if (
            not math.isfinite(end)
            or end <= 0
            or end > duration
            or (rank == count and abs(end - duration) > 0.001)
        ):
            raise PlanningError("锁定的边界与总时长冲突，请先解除锁定")
        points.append(end)
    points = sorted(set(points))
    if len(points) < count + 1:
        raise PlanningError(
            f"旧自动分配模式：{count} 张图片只有 {len(points)-2} 个内部句末切换点。请改用导入视频项目（每场景一段解说），或减少图片；不会自动截断句子"
        )
    states = {0: (0.0, [])}
    for rank in range(1, count + 1):
        next_states = {}
        for j in range(1, len(points)):
            end = points[j]
            if rank == count and abs(end - duration) > 0.001:
                continue
            if rank < count and end >= duration:
                continue
            if rank in locks and abs(end - locks[rank]) > 0.001:
                continue
            # Newly inserted exceptional boundaries are not available to unlocked ranks.
            if rank not in locks and end not in ends and end != duration:
                continue
            best = None
            for i, (score, path) in states.items():
                length = end - points[i]
                if i >= j or length < minimum - 0.001 or length > maximum + 0.001:
                    continue
                candidate = (score + (length - desired) ** 2, path + [end])
                if best is None or candidate[0] < best[0]:
                    best = candidate
            if best:
                next_states[j] = best
        states = next_states
    final = states.get(len(points) - 1)
    if not final:
        raise PlanningError(
            "句末、锁定边界与时长范围无法同时满足，请放宽最短/最长时长或减少图片"
        )
    start = 0.0
    for clip, end in zip(clips, final[1]):
        clip.start, clip.end = start, end
        indices = [
            n + 1
            for n, c in enumerate(project.cues)
            if c["end"] > start and c["start"] < end
        ]
        clip.sentence_start = min(indices, default=0)
        clip.sentence_end = max(indices, default=0)
        start = end
    return clips


def edit_boundary(project, index, end, snap=False):
    if not 0 <= index < len(project.images) - 1:
        raise PlanningError("最后一张图片的结束时间由完整配音时长决定")
    points = sentence_boundaries(
        project.cues,
        project.images[-1].end,
        project.timeline.get("boundary") == "SentenceBoundary",
    )
    if snap:
        if not points:
            raise PlanningError("没有可吸附的完整句末")
        end = min(points, key=lambda point: abs(point - end))
    previous, following = project.images[index], project.images[index + 1]
    if not math.isfinite(end) or not previous.start < end < following.end:
        raise PlanningError("边界必须位于相邻图片片段内部")
    previous.end = end
    following.start = end
    return not any(abs(end - point) < 0.001 for point in points)
