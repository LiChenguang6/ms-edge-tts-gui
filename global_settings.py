# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-user atomic settings; independent of the executable's install directory."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading

_LOCK = threading.RLock()


def config_dir():
    return Path(os.environ.get('APPDATA', Path.home())) / 'EdgeTTSGui'


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class GlobalSettings:
    def __init__(self, path=None):
        self.path = Path(path) if path else config_dir()/'video-defaults.json'

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8-sig'))
            return data if isinstance(data, dict) else {}
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
            atomic_json(self.path, data)
        return data

    def remember_project(self, project):
        # Never save project media paths or imported script/prompt metadata as defaults.
        return self.update(video=project.video, style=project.style, sound=project.sound,
                           planning=project.planning, narration=project.narration,
                           output_dir=project.output_dir)
