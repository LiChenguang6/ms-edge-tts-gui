# SPDX-License-Identifier: GPL-3.0-or-later
"""Safe subprocess execution with cancellation, bounded RAM and useful diagnostics."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time


class MediaError(RuntimeError):
    def __init__(
        self, message, *, stage="媒体处理", command=None, scene="", log_path=""
    ):
        self.stage, self.command, self.scene, self.log_path = (
            stage,
            command,
            scene,
            log_path,
        )
        details = [f"步骤：{stage}"]
        if scene:
            details.append(f"场景/图片：{scene}")
        if command:
            details.append(
                "参数（仅供诊断）：" + subprocess.list2cmdline(list(map(str, command)))
            )
        details.append(str(message))
        if log_path:
            details.append(f"完整日志：{log_path}")
        super().__init__("\n".join(details))


class ExportCancelled(MediaError):
    pass


def executable(name, configured=""):
    from global_settings import GlobalSettings

    path = (
        configured
        or GlobalSettings().read().get("video", {}).get(name, "")
        or os.environ.get(name.upper() + "_PATH", "")
    )
    if path:
        if Path(path).is_dir():
            raise MediaError(
                f"{name} 路径是文件夹，请选择其中的 {name}.exe 文件：{path}",
                stage="验证工具路径",
            )
        if Path(path).is_file():
            return str(Path(path).resolve())
    else:
        # Optional user-supplied binaries next to the Windows executable.
        import sys

        adjacent = Path(sys.executable).parent / (
            name + (".exe" if os.name == "nt" else "")
        )
        if getattr(sys, "frozen", False) and adjacent.is_file():
            return str(adjacent)
        found = shutil.which(name)
        if found:
            return found
    raise MediaError(
        f"未找到 {name}。请在工具设置选择 {name}.exe，或将其所在目录加入 PATH。"
    )


def probe(path, ffprobe=""):
    command = [
        executable("ffprobe", ffprobe),
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=30, creationflags=_flags()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(
            f"FFprobe 无法读取 {path}：{exc}；请重新选择有效工具并检查文件权限。",
            stage="读取实际媒体时长",
            command=command,
        ) from exc
    if result.returncode:
        raise MediaError(
            result.stderr.decode("utf-8", "replace"),
            stage="FFprobe 执行",
            command=command,
        )
    data = json.loads(result.stdout)
    data["duration"] = float(data.get("format", {}).get("duration", 0))
    import math

    if not math.isfinite(data["duration"]) or data["duration"] <= 0:
        raise MediaError(
            "无法取得媒体的实际时长", stage="读取实际媒体时长", command=command
        )
    return data


def _flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_ffmpeg(arguments, cancel=None, progress=None, duration=1, ffmpeg="", cwd=None):
    cancel = cancel or threading.Event()
    if cancel.is_set():
        raise ExportCancelled("导出已取消")
    with tempfile.TemporaryFile() as log:
        command = [
            executable("ffmpeg", ffmpeg),
            "-hide_banner",
            "-y",
            "-nostdin",
            "-xerror",
            "-progress",
            "pipe:1",
            "-nostats",
            *map(str, arguments),
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=log,
                creationflags=_flags(),
            )
        except OSError as exc:
            raise MediaError(
                f"无法启动 FFmpeg：{exc}。请检查 exe 路径、目录写权限和安全软件拦截；不需要始终使用管理员权限。",
                stage="启动 FFmpeg",
                command=command,
            ) from exc

        def read_progress():
            for line in process.stdout:
                if line.startswith(b"out_time_us=") and progress:
                    try:
                        progress(
                            min(
                                0.999, max(0, int(line.split(b"=")[1]) / 1e6 / duration)
                            )
                        )
                    except (ValueError, ZeroDivisionError):
                        pass

        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        try:
            while process.poll() is None:
                if cancel.wait(0.05):
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    process.wait()
                    raise ExportCancelled("导出已取消；原始素材保留")
            reader.join(timeout=2)
            if cancel.is_set():
                raise ExportCancelled("导出已取消")
            if process.returncode:
                log.seek(0, 2)
                length = log.tell()
                log.seek(0)
                full = log.read().decode("utf-8", "replace")
                from global_settings import config_dir
                import uuid

                target = config_dir() / "logs" / (uuid.uuid4().hex + ".log")
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    target = (
                        Path(tempfile.mkdtemp(prefix="edge-media-error-"))
                        / "ffmpeg.log"
                    )
                target.write_text(
                    subprocess.list2cmdline(command) + "\n" + full, encoding="utf-8"
                )
                raise MediaError(
                    full, stage="FFmpeg 执行", command=command, log_path=str(target)
                )
            if progress:
                progress(1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            reader.join(timeout=2)
            process.stdout.close()


def validate_tool(name, path):
    tool = executable(name, path)
    try:
        result = subprocess.run(
            [tool, "-version"], capture_output=True, timeout=15, creationflags=_flags()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(
            f"{name} 无法执行：{exc}", stage="验证工具路径", command=[tool, "-version"]
        ) from exc
    if result.returncode or not result.stdout.lower().startswith(
        (name + " version").encode()
    ):
        raise MediaError(f"请选择真正的 {name} 可执行文件", stage="验证工具版本")
    return tool


def ensure_writable(directory):
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="edge-write-check-", dir=directory)
        os.close(fd)
        Path(path).unlink()
    except OSError as exc:
        raise MediaError(
            f"目录不可写：{directory}\n{exc}\n请改选用户可写的输出目录，不必以管理员身份运行。",
            stage="检查输出/临时目录",
        ) from exc
    return directory


def check_disk_space(directory, required):
    free = shutil.disk_usage(directory).free
    if free < required:
        raise MediaError(
            f"磁盘空间不足：需要约 {required/1048576:.1f} MiB，剩余 {free/1048576:.1f} MiB，目录 {directory}",
            stage="检查磁盘空间",
        )
