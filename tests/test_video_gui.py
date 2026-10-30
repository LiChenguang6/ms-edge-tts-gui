# SPDX-License-Identifier: GPL-3.0-or-later
import os
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(
    os.environ.get("DISPLAY") or os.name == "nt", "requires a desktop display"
)
class WorkspaceTests(unittest.TestCase):
    def test_navigation_import_edit_save_reopen_and_theme(self):
        from PIL import Image
        import app

        with tempfile.TemporaryDirectory() as folder, patch.object(
            app.App, "_start_background_jobs"
        ), ExitStack() as stack:
            root = Path(folder)
            config = root / "AppData" / "EdgeTTSGui"
            stack.enter_context(
                patch.dict(os.environ, {"APPDATA": str(root / "AppData")})
            )
            stack.enter_context(patch.object(app, "SETTINGS_DIR", config))
            stack.enter_context(
                patch.object(app, "SETTINGS_FILE", config / "settings.json")
            )
            srt = root / "测试.srt"
            srt.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\n你好。\n", encoding="utf-8-sig"
            )
            image = root / "图片.png"
            Image.new("RGB", (50, 40), "red").save(image)
            window = app.App()
            try:
                window.update()
                self.assertEqual(window._studio_mode, "audio")
                window._show_studio("images")
                window.update()
                workspace = window._video_workspace
                workspace.vars[("video", "fps")].set("25")
                with patch(
                    "video_workspace.filedialog.askopenfilename", return_value=str(srt)
                ):
                    workspace.import_srt()
                self.assertEqual(window._textbox.get("1.0", "end").strip(), "你好。")
                workspace.add_paths([str(image)])
                window.project.audio_duration = 2
                workspace.plan()
                workspace.tree.selection_set("0")
                workspace.select()
                window.update()
                self.assertEqual(window.project.images[0].end, 2)
                filename = root / "工程.json"
                with patch(
                    "video_workspace.filedialog.asksaveasfilename",
                    return_value=str(filename),
                ):
                    workspace.save_project()
                window._show_studio("audio")
                window.update()
                self.assertEqual(window.project.video["fps"], 25)
                window._show_studio("export")
                window.update()
                with patch(
                    "video_workspace.filedialog.askopenfilename",
                    return_value=str(filename),
                ):
                    window._video_workspace.open_project()
                window.update()
                self.assertEqual(len(window.project.images), 1)
                window._switch_theme()
                window.update()
                self.assertEqual(window._studio_mode, "export")
                window._switch_language()
                window.update()
                self.assertEqual(window.project.video["fps"], 25)
                window._show_studio("audio")
                window.update()
                self.assertTrue(window._btn_generate.winfo_exists())
                self._assert_scene_project(window, root)
                self._assert_srt_inputs(window, root)
            finally:
                window._on_close()

    def _assert_srt_inputs(self, window, root):
        from media_merger import subtitle_cues
        from project_manager import save_project, load_project

        srt = root / "独立字幕.srt"
        srt.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\n独立字幕。\n", encoding="utf-8-sig"
        )
        with patch("app.filedialog.askopenfilename", return_value=str(srt)):
            self.assertTrue(window._on_import_srt())
        self.assertEqual(window._textbox.get("1.0", "end").strip(), "独立字幕。")
        window._textbox.delete("1.0", "end")
        window._textbox.insert("1.0", "已有配音文稿")
        original_cues = list(window.project.cues)
        window._show_studio("export")
        window.update()
        workspace = window._video_workspace
        with patch("video_workspace.filedialog.askopenfilename", return_value=str(srt)):
            workspace.choose_subtitle_srt()
        self.assertEqual(window._textbox.get("1.0", "end").strip(), "已有配音文稿")
        self.assertEqual(window.project.cues, original_cues)
        self.assertEqual(subtitle_cues(window.project)[0]["text"], "独立字幕。")
        workspace.subtitle_enabled.set(False)
        workspace.collect()
        filename = root / "字幕工程.json"
        save_project(window.project, filename)
        restored = load_project(filename)
        self.assertFalse(restored.style["burn"])
        self.assertTrue(Path(restored.subtitle_srt).samefile(srt))
        # Reopening a project must not inherit the previous workspace's checkbox.
        workspace.subtitle_enabled.set(True)
        with patch(
            "video_workspace.filedialog.askopenfilename", return_value=str(filename)
        ):
            workspace.open_project()
        self.assertFalse(window._video_workspace.subtitle_enabled.get())
        window._show_studio("audio")
        window._add_batch_files([str(srt)])
        self.assertEqual(len(window._file_items), 1)
        self.assertNotIn("-->", window._file_items[0].text)

    def _assert_scene_project(self, window, root):
        import time
        import shutil
        from copy import deepcopy
        from scene_audio import prepare_scene_audio
        from global_settings import GlobalSettings
        from test_scene_projects import fixture, FakeEngine

        original = deepcopy(window.project)
        folder = fixture(root / "一键视频 工程", 11, missing=3)
        window._show_studio("images")
        with patch(
            "scene_workspace.filedialog.askdirectory", return_value=str(folder)
        ), patch("scene_workspace.messagebox.showwarning") as warning:
            window._video_workspace.import_video_project()
            self.assertIn("003", str(warning.call_args))
        window.update()
        workspace = window._video_workspace
        self.assertEqual(window.project.mode, "scenes")
        self.assertEqual(len(workspace.tree.get_children()), 11)
        self.assertEqual(workspace.tree.item("0", "values")[0], "001")
        tool = shutil.which("ffmpeg")
        with patch("video_workspace.filedialog.askopenfilename", return_value=tool):
            workspace.choose_tool("ffmpeg")
        self.assertTrue(Path(GlobalSettings().read()["video"]["ffmpeg"]).samefile(tool))
        FakeEngine.calls = []

        def prepare(project, cfg, cancel, progress, status):
            return prepare_scene_audio(
                project,
                cfg,
                cancel,
                progress,
                status,
                engine_factory=FakeEngine,
                request_interval=0,
            )

        with patch("scene_workspace.prepare_scene_audio", side_effect=prepare):
            workspace.generate_scene_voice()
            deadline = time.monotonic() + 30
            while window._video_workspace.running and time.monotonic() < deadline:
                window.update()
                time.sleep(0.01)
            self.assertFalse(window._video_workspace.running)
        workspace = window._video_workspace
        self.assertEqual(len(FakeEngine.calls), 11)
        self.assertEqual(window.project.images[-1].end, window.project.audio_duration)
        workspace.tree.selection_set("2")
        with patch("scene_workspace.messagebox.askyesno", return_value=True):
            workspace.skip_scene_image()
        self.assertTrue(window.project.images[2].skipped)
        self.assertEqual(len(window.project.cues), 11)
        self.assertLessEqual(len(workspace.cache.cache), 64)
        # A current voice/rate edit must override previously rendered audio.
        window.project.style["burn"] = False
        window._rate_var.set(10)
        workspace = window._video_workspace
        destination = folder / "output" / "重新配音.mp4"
        with patch("scene_workspace.prepare_scene_audio", side_effect=prepare), patch(
            "scene_workspace.filedialog.asksaveasfilename",
            return_value=str(destination),
        ):
            workspace.generate_scene_voice(True)
            deadline = time.monotonic() + 30
            while window._video_workspace.running and time.monotonic() < deadline:
                window.update()
                time.sleep(0.01)
        self.assertFalse(window._video_workspace.running)
        self.assertTrue(destination.is_file())
        self.assertEqual(len(FakeEngine.calls), 22)
        destination2 = folder / "output" / "复用配音.mp4"
        with patch(
            "scene_workspace.prepare_scene_audio",
            side_effect=AssertionError("existing voice must be reused"),
        ), patch(
            "scene_workspace.filedialog.asksaveasfilename",
            return_value=str(destination2),
        ):
            window._video_workspace.generate_scene_voice(True)
            deadline = time.monotonic() + 30
            while window._video_workspace.running and time.monotonic() < deadline:
                window.update()
                time.sleep(0.01)
        self.assertTrue(destination2.is_file())
        self.assertEqual(len(FakeEngine.calls), 22)
        workspace = window._video_workspace
        window._rate_var.set(20)
        before_cancel = len(FakeEngine.calls)

        def slow_prepare(project, cfg, cancel, progress, status):
            return prepare_scene_audio(
                project,
                cfg,
                cancel,
                progress,
                status,
                engine_factory=FakeEngine,
                request_interval=10,
            )

        with patch("scene_workspace.prepare_scene_audio", side_effect=slow_prepare):
            workspace.generate_scene_voice()
            deadline = time.monotonic() + 5
            while (
                len(FakeEngine.calls) == before_cancel and time.monotonic() < deadline
            ):
                window.update()
                time.sleep(0.01)
            workspace.cancel_button.invoke()
            deadline = time.monotonic() + 10
            while window._video_workspace.running and time.monotonic() < deadline:
                window.update()
                time.sleep(0.01)
        self.assertFalse(window._video_workspace.running)
        self.assertLess(len(FakeEngine.calls), before_cancel + 11)
        workspace = window._video_workspace
        workspace.replace_project(original)
        window._show_studio("audio")
