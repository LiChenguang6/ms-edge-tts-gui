# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
from global_settings import GlobalSettings, atomic_json
from project_manager import new_project, load_project
from media_tools import (
    validate_tool,
    executable,
    ensure_writable,
    run_ffmpeg,
    MediaError,
)


class DefaultsTests(unittest.TestCase):
    def test_restart_and_project_priority(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "AppData" / "video.json"
            store = GlobalSettings(path)
            store.update(
                video={
                    "ffmpeg": "C:/tools/ffmpeg.exe",
                    "ffprobe": "C:/tools/ffprobe.exe",
                    "fps": 25,
                },
                output_dir="C:/videos",
                planning={"paragraph_pause": 0.15},
            )
            project = new_project(GlobalSettings(path))
            self.assertEqual(project.video["fps"], 25)
            self.assertEqual(project.output_dir, "C:/videos")
            self.assertEqual(project.video["ffprobe"], "C:/tools/ffprobe.exe")
            payload = {"version": 1, "video": {"fps": 60}, "images": []}
            filename = Path(root) / "project.json"
            atomic_json(filename, payload)
            with patch("project_manager.new_project", return_value=project):
                restored = load_project(filename)
            self.assertEqual(restored.video["fps"], 60)
            self.assertEqual(restored.output_dir, "C:/videos")
            self.assertEqual(restored.video["ffmpeg"], "C:/tools/ffmpeg.exe")
            self.assertFalse(list(path.parent.glob("*.tmp")))
            from global_settings import config_dir

            with patch.dict(
                "os.environ", {"APPDATA": str(Path(root) / "AppData")}
            ), patch(
                "pathlib.Path.home", side_effect=RuntimeError("profile unavailable")
            ):
                self.assertEqual(config_dir(), Path(root) / "AppData" / "EdgeTTSGui")

    def test_directory_is_not_an_executable(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(MediaError, "文件夹"):
                executable("ffmpeg", root)

    def test_winerror_five_has_stage_and_command(self):
        with patch("media_tools.executable", return_value="C:/tools/ffmpeg.exe"), patch(
            "subprocess.Popen", side_effect=PermissionError("[WinError 5] 拒绝访问")
        ):
            with self.assertRaises(MediaError) as found:
                run_ffmpeg(["-i", "中文 图片.png", "out.mp4"])
            self.assertIn("WinError 5", str(found.exception))
            self.assertIn("中文 图片.png", str(found.exception))
            self.assertIn("步骤", str(found.exception))

    def test_validate_and_save_tool(self):
        with patch("media_tools.executable", return_value="ffprobe.exe"), patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, b"ffprobe version 8", b""),
        ):
            self.assertEqual(validate_tool("ffprobe", "ffprobe.exe"), "ffprobe.exe")

    def test_atomic_failure_preserves_old_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "settings.json"
            atomic_json(path, {"fps": 30})
            with patch(
                "global_settings.os.replace",
                side_effect=PermissionError("[WinError 5] 拒绝访问"),
            ):
                with self.assertRaises(PermissionError):
                    atomic_json(path, {"fps": 60})
            import json

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"fps": 30})
            self.assertFalse(list(Path(root).glob("*.tmp")))

    def test_output_permission_error_is_actionable(self):
        with tempfile.TemporaryDirectory() as root, patch(
            "media_tools.tempfile.mkstemp",
            side_effect=PermissionError("[WinError 5] 拒绝访问"),
        ):
            with self.assertRaises(MediaError) as error:
                ensure_writable(root)
            self.assertIn("WinError 5", str(error.exception))
            self.assertIn(root, str(error.exception))
            self.assertIn("用户可写", str(error.exception))
