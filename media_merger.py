# SPDX-License-Identifier: GPL-3.0-or-later
"""Local FFmpeg muxing, ASS subtitles and narration-first audio mixing."""

from pathlib import Path
import re
import shutil
import tempfile
import os
from media_tools import MediaError, ExportCancelled, probe, run_ffmpeg
from video_renderer import render_images


def _ass_time(seconds):
    ticks = max(0, round(seconds * 100))
    return f"{ticks//360000}:{ticks//6000%60:02}:{ticks//100%60:02}.{ticks%100:02}"


def _color(value):
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise MediaError("字幕颜色需要 #RRGGBB 格式")
    return "&H00" + value[5:7] + value[3:5] + value[1:3]


def write_ass(project, path, width=None, height=None):
    style = project.style
    w, h = width or project.video["width"], height or project.video["height"]
    font = str(style["font"]).replace(",", " ").replace("\n", " ").replace("\r", " ")
    size, outline, shadow, margin, alignment = (
        float(style[k]) for k in ("size", "outline", "shadow", "margin", "alignment")
    )
    if (
        not 8 <= size <= 300
        or not 0 <= outline <= 20
        or not 0 <= shadow <= 20
        or not 0 <= margin <= h / 2
        or int(alignment) not in (2, 5, 8)
    ):
        raise MediaError("字幕字号、描边、阴影、边距或位置无效")
    # Styles remain in the full output coordinate system even for low-res previews.
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {project.video['width']}
PlayResY: {project.video['height']}
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{_color(style['color'])},&H000000FF,{_color(style['outline_color'])},&H80000000,0,0,0,0,100,100,0,0,1,{outline},{shadow},{int(alignment)},40,40,{int(margin)},1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    wrap = int(style["wrap"])
    if not 1 <= wrap <= 200:
        raise MediaError("自动换行字数需为 1–200")
    for cue in project.cues:
        text = re.sub(r"<[^>]*>", "", str(cue["text"]))
        # Treat imported text literally, never as ASS override tags or escapes.
        text = (
            text.replace("\\", "＼")
            .replace("{", "｛")
            .replace("}", "｝")
            .replace("\r", "")
        )
        wrapped = []
        for line in text.split("\n"):
            wrapped.extend(line[i : i + wrap] for i in range(0, len(line), wrap))
        text = r"\N".join(wrapped)
        lines.append(
            f"Dialogue: 0,{_ass_time(cue['start'])},{_ass_time(cue['end'])},Default,,0,0,0,,{text}\n"
        )
    Path(path).write_text("".join(lines), encoding="utf-8-sig")


def validate_output(project, output):
    output = Path(output).resolve()
    inputs = [
        project.audio,
        project.source_srt,
        project.external_video,
        project.bgm,
        project.style.get("font_file", ""),
        *(clip.path for clip in project.images),
    ]
    if output in [Path(p).resolve() for p in inputs if p]:
        raise MediaError("输出路径不能覆盖原始素材")
    if output.suffix.lower() != ".mp4":
        raise MediaError("请选择 .mp4 输出文件")
    return output


def export_video(
    project, output, cancel=None, progress=None, preview=False, silent=False
):
    output = validate_output(project, output)
    if project.missing_media():
        raise MediaError(
            "缺失素材，请重新定位：\n" + "\n".join(project.missing_media())
        )
    if not silent and not project.audio:
        raise MediaError("请先生成或导入配音")
    settings = project.video
    if project.audio:
        info = probe(project.audio, settings.get("ffprobe", ""))
        if not any(s["codec_type"] == "audio" for s in info["streams"]):
            raise MediaError("配音文件没有音频流")
        if abs(info["duration"] - project.audio_duration) > 0.1:
            raise MediaError("配音时长已变化，请重新导入音频并分配图片时间")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="edge-compose-") as temp:
        root = Path(temp)
        if project.external_video:
            video = project.external_video
            info = probe(video, settings.get("ffprobe", ""))
            if not any(s["codec_type"] == "video" for s in info["streams"]):
                raise MediaError("导入的视频没有画面流")
            duration = max(info["duration"], project.audio_duration)
        else:
            video = str(root / "pictures.mp4")
            render_images(
                project,
                video,
                cancel,
                (lambda p: progress(p * 0.65)) if progress else None,
                preview,
            )
            duration = project.images[-1].end
        if silent:
            if cancel and cancel.is_set():
                raise ExportCancelled("导出已取消")
            fd, staging = tempfile.mkstemp(dir=output.parent, suffix=".mp4")
            os.close(fd)
            try:
                shutil.copyfile(video, staging)
                os.replace(staging, output)
            finally:
                Path(staging).unlink(missing_ok=True)
            return str(output)
        audio = ["-i", project.audio]
        filters = []
        vf = "tpad=stop_mode=clone:stop_duration=" + str(duration)
        w, h = int(settings["width"]), int(settings["height"])
        if preview:
            ratio = min(1, 480 / max(w, h))
            w, h = max(2, round(w * ratio / 2) * 2), max(2, round(h * ratio / 2) * 2)
        vf += f",scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        if project.style["burn"] and project.cues:
            write_ass(project, root / "captions.ass")
            font_file = project.style.get("font_file", "")
            if font_file:
                (root / "fonts").mkdir()
                shutil.copyfile(
                    font_file, root / "fonts" / ("font" + Path(font_file).suffix)
                )
            vf += ",subtitles=captions.ass:fontsdir=fonts"
        voice_volume, bgm_volume = float(project.sound["voice_volume"]), float(
            project.sound["bgm_volume"]
        )
        if not 0 <= voice_volume <= 4 or not 0 <= bgm_volume <= 1:
            raise MediaError("配音音量需为 0–4，BGM 音量需为 0–1")
        filters.append(f"[0:v]{vf},format=yuv420p[v]")
        filters.append(f"[1:a]volume={voice_volume},apad[voice]")
        if project.bgm:
            if project.sound["loop"]:
                audio += ["-stream_loop", "-1"]
            audio += ["-i", project.bgm]
            fade = max(0, min(float(project.sound["fade"]), duration / 2))
            filters += [
                f"[2:a]volume={bgm_volume},afade=t=in:d={fade},afade=t=out:st={duration-fade}:d={fade},apad[music]",
                "[voice][music]amix=inputs=2:duration=first:normalize=0[a]",
            ]
        else:
            filters.append("[voice]anull[a]")
        fd, staging = tempfile.mkstemp(dir=output.parent, suffix=".mp4")
        os.close(fd)
        try:
            base = [
                "-i",
                video,
                *audio,
                "-filter_complex_threads",
                "1",
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-t",
                duration,
                "-r",
                settings["fps"],
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-threads",
                "2",
            ]
            encoder = "libx264" if preview else settings.get("encoder", "libx264")
            if encoder not in ("libx264", "h264_nvenc"):
                raise MediaError("不支持的编码器")
            callback = (lambda p: progress(0.65 + 0.35 * p)) if progress else None
            try:
                run_ffmpeg(
                    [*base, "-c:v", encoder, staging],
                    cancel,
                    callback,
                    duration,
                    settings.get("ffmpeg", ""),
                    root,
                )
            except ExportCancelled:
                raise
            except MediaError:
                if encoder != "h264_nvenc":
                    raise
                run_ffmpeg(
                    [*base, "-c:v", "libx264", staging],
                    cancel,
                    callback,
                    duration,
                    settings.get("ffmpeg", ""),
                    root,
                )
            if cancel and cancel.is_set():
                raise ExportCancelled("导出已取消")
            os.replace(staging, output)
        finally:
            Path(staging).unlink(missing_ok=True)
    return str(output)
