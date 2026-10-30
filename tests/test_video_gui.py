# SPDX-License-Identifier: GPL-3.0-or-later
import os
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
        ):
            root = Path(folder)
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
        self.assertEqual(restored.subtitle_srt, str(srt))
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
