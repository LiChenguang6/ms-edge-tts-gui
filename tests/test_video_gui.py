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
            finally:
                window._on_close()
