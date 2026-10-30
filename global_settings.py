# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-user atomic settings; independent of the executable's install directory."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading

_LOCK = threading.RLock()


class SettingsError(ValueError):
    pass


def config_dir():
    configured = os.environ.get("APPDATA")
    if configured:
        return Path(configured) / "EdgeTTSGui"
    if os.name == "nt":
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(32768)
            if (
                ctypes.windll.shell32.SHGetFolderPathW(None, 26, None, 0, buffer) == 0
                and buffer.value
            ):
                return Path(buffer.value) / "EdgeTTSGui"
        except (OSError, AttributeError):
            pass
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "EdgeTTSGui"
    try:
        base = Path.home()
    except RuntimeError:
        base = Path(tempfile.gettempdir())
    return base / "EdgeTTSGui"


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class GlobalSettings:
    def __init__(self, path=None):
        self.path = Path(path) if path else config_dir() / "video-defaults.json"

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                return {}
            for key in ("video", "style", "sound", "planning", "narration"):
                if key in data and not isinstance(data[key], dict):
                    data.pop(key)
            return data
        except (OSError, ValueError):
            return {}

    def update(self, **updates):
        with _LOCK:
            data = self.read()
            for key, value in updates.items():
                if isinstance(value, dict):
                    data[key] = {**data.get(key, {}), **deepcopy(value)}
                else:
                    data[key] = value
            try:
                atomic_json(self.path, data)
            except OSError as exc:
                raise SettingsError(
                    f"无法保存用户配置 {self.path}：{exc}；请检查 AppData 目录写权限。"
                ) from exc
        return data

    def remember_project(self, project):
        # Never save project media paths or imported script/prompt metadata as defaults.
        return self.update(
            video=project.video,
            style=project.style,
            sound=project.sound,
            planning=project.planning,
            narration=project.narration,
            output_dir=project.output_dir,
        )
