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
    pass


class ExportCancelled(MediaError):
    pass


def executable(name, configured=''):
    path = configured or os.environ.get(name.upper() + '_PATH', '')
    if path:
        if Path(path).is_file():
            return str(Path(path).resolve())
    else:
        # Optional user-supplied binaries next to the Windows executable.
        import sys
        adjacent = Path(sys.executable).parent / (name + ('.exe' if os.name == 'nt' else ''))
        if getattr(sys, 'frozen', False) and adjacent.is_file():
            return str(adjacent)
        found = shutil.which(name)
        if found:
            return found
    raise MediaError(f'未找到 {name}。请在工具设置选择 {name}.exe，或将其所在目录加入 PATH。')


def probe(path, ffprobe=''):
    result = subprocess.run([executable('ffprobe', ffprobe), '-v','error', '-show_format',
                             '-show_streams', '-of','json', str(path)],
                            capture_output=True, timeout=30, creationflags=_flags())
    if result.returncode:
        raise MediaError(result.stderr.decode('utf-8', 'replace')[-4000:])
    data = json.loads(result.stdout)
    data['duration'] = float(data.get('format', {}).get('duration', 0))
    if data['duration'] <= 0:
        raise MediaError('无法取得媒体的实际时长')
    return data


def _flags():
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def run_ffmpeg(arguments, cancel=None, progress=None, duration=1, ffmpeg='', cwd=None):
    cancel = cancel or threading.Event()
    if cancel.is_set():
        raise ExportCancelled('导出已取消')
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen([executable('ffmpeg', ffmpeg), '-hide_banner', '-y',
            '-nostdin', '-progress', 'pipe:1', '-nostats', *map(str, arguments)], cwd=cwd,
            stdout=subprocess.PIPE, stderr=log, creationflags=_flags())
        def read_progress():
            for line in process.stdout:
                if line.startswith(b'out_time_us=') and progress:
                    try:
                        progress(min(.999, max(0, int(line.split(b'=')[1]) / 1e6 / duration)))
                    except (ValueError, ZeroDivisionError):
                        pass
        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        try:
            while process.poll() is None:
                if cancel.wait(.05):
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    process.wait()
                    raise ExportCancelled('导出已取消；原始素材保留')
            reader.join(timeout=2)
            if cancel.is_set():
                raise ExportCancelled('导出已取消')
            if process.returncode:
                log.seek(0, 2)
                length = log.tell()
                log.seek(max(0, length-12000))
                raise MediaError(log.read().decode('utf-8', 'replace'))
            if progress:
                progress(1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            reader.join(timeout=2)
            process.stdout.close()
