# SPDX-License-Identifier: GPL-3.0-or-later
"""Render images sequentially; only one source image is decoded per process."""

from pathlib import Path
import math
import tempfile
from media_tools import MediaError, ExportCancelled, run_ffmpeg, check_disk_space


def image_filter(video, frames):
    w, h, fps = int(video["width"]), int(video["height"]), int(video["fps"])
    mode, motion = video["fit"], video["motion"]
    prefix = ""
    if mode == "blur":
        prefix = (
            f"split[bg][fg];[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},boxblur=20:2[bg2];[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fg2];"
            "[bg2][fg2]overlay=(W-w)/2:(H-h)/2,"
        )
    elif mode == "cover":
        prefix = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
    else:
        prefix = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
    if mode == "custom":
        z, x, y = float(video["zoom"]), float(video["x"]), float(video["y"])
        if not 1 <= z <= 4 or not 0 <= x <= 1 or not 0 <= y <= 1:
            raise MediaError("自定义缩放需为 1–4，位置需为 0–1")
        prefix += f"scale=iw*{z}:ih*{z},crop={w}:{h}:(iw-ow)*{x}:(ih-oh)*{y},"
    if motion != "none":
        steps = max(1, frames - 1)
        zoom = {
            "zoom_in": f"1+0.08*on/{steps}",
            "zoom_out": f"1.08-0.08*on/{steps}",
        }.get(motion, "1.08")
        x, y = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
        if motion == "left_right":
            x = f"(iw-iw/zoom)*on/{steps}"
        if motion == "right_left":
            x = f"(iw-iw/zoom)*(1-on/{steps})"
        if motion == "vertical":
            y = f"(ih-ih/zoom)*on/{steps}"
        prefix += f"zoompan=z='{zoom}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps},"
    return prefix + f"setsar=1,fps={fps},format=yuv420p"


def validate_timeline(project):
    if not project.images:
        raise MediaError("请先导入图片并分配时长")
    fps = int(project.video["fps"])
    if fps not in (24, 25, 30, 60):
        raise MediaError("支持 24、25、30、60 FPS")
    if any(
        int(project.video[k]) <= 0
        or int(project.video[k]) > 4096
        or int(project.video[k]) % 2
        for k in ("width", "height")
    ):
        raise MediaError("分辨率必须为 4096 以内的正偶数")
    start = 0
    for clip in project.images:
        if (
            not all(math.isfinite(v) for v in (clip.start, clip.end))
            or abs(clip.start - start) > 0.001
            or clip.end <= clip.start
        ):
            raise MediaError("图片时间轴必须连续、无重叠且时长为正，请重新分配")
        if round(clip.end * fps) <= round(clip.start * fps):
            raise MediaError("图片片段短于一帧")
        start = clip.end
    if start < project.audio_duration - 0.001:
        raise MediaError("图片视频短于配音，请重新规划；不会自动裁掉配音")
    return start


def render_images(
    project, output, cancel=None, progress=None, preview=False, status=None
):
    duration = validate_timeline(project)
    settings = dict(project.video)
    if preview:
        ratio = min(1, 480 / max(settings["width"], settings["height"]))
        settings.update(
            width=max(2, round(settings["width"] * ratio / 2) * 2),
            height=max(2, round(settings["height"] * ratio / 2) * 2),
        )
    fps = int(settings["fps"])
    with tempfile.TemporaryDirectory(prefix="edge-video-") as temp:
        root = Path(temp)
        check_disk_space(root, max(16 * 1048576, int(duration * 1500000)))
        for n, clip in enumerate(project.images):
            start, end = round(clip.start * fps), round(clip.end * fps)
            scene = getattr(clip, "scene_id", "") or str(n + 1)
            if status:
                status(
                    f"处理场景 {scene} · 图片 {n+1}/{len(project.images)} · 剩余 {len(project.images)-n-1}：{Path(clip.path).name}"
                )
            filters = image_filter(
                {
                    **settings,
                    "motion": getattr(clip, "motion", "") or settings["motion"],
                },
                end - start,
            )
            if settings["transition"] == "fade":
                fade = min(
                    float(settings["transition_duration"]), (end - start) / fps / 2
                )
                if n:
                    filters += f",fade=t=in:st=0:d={fade}"
                if n < len(project.images) - 1:
                    filters += f",fade=t=out:st={(end-start)/fps-fade}:d={fade}"
            try:
                run_ffmpeg(
                    [
                        *(
                            [
                                "-f",
                                "lavfi",
                                "-i",
                                f"color=c=black:s={settings['width']}x{settings['height']}:r={fps}",
                            ]
                            if clip.skipped
                            else ["-loop", "1", "-framerate", fps, "-i", clip.path]
                        ),
                        "-vf",
                        filters,
                        "-frames:v",
                        end - start,
                        "-an",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-threads",
                        "2",
                        "-filter_complex_threads",
                        "1",
                        "-filter_threads",
                        "1",
                        root / f"{n}.mp4",
                    ],
                    cancel,
                    (
                        (
                            lambda value, n=n: progress(
                                (n + value) / len(project.images) * 0.95
                            )
                        )
                        if progress
                        else None
                    ),
                    (end - start) / fps,
                    settings.get("ffmpeg", ""),
                )
            except ExportCancelled:
                raise
            except MediaError as exc:
                raise MediaError(
                    str(exc),
                    stage="图片分段渲染",
                    scene=scene,
                    command=getattr(exc, "command", None),
                    log_path=getattr(exc, "log_path", ""),
                ) from exc
        (root / "list.txt").write_text(
            "".join(f"file '{n}.mp4'\n" for n in range(len(project.images))),
            encoding="ascii",
        )
        run_ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                "list.txt",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(Path(output).resolve()),
            ],
            cancel,
            (lambda p: progress(0.95 + p * 0.05)) if progress else None,
            duration,
            settings.get("ffmpeg", ""),
            cwd=root,
        )
    return str(output)
