# SPDX-License-Identifier: GPL-3.0-or-later
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from image_manager import add_images, sort_images, move_image
from project_manager import Project, save_project, load_project
from srt_parser import (
    parse_srt,
    spoken_text,
    read_srt,
    sentence_boundaries,
    duration_warning,
)

SRT = "1\n00:00:00,000 --> 00:00:02,000\n你好，\nworld！\n\n2\n00:00:03,000 --> 00:00:04,000\n下一句。\n"


class InputTests(unittest.TestCase):
    def test_bom_multiline_text_and_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "中文字幕.srt"
            p.write_text(SRT, encoding="utf-8-sig")
            cues, warnings = read_srt(p)
            self.assertFalse(warnings)
            self.assertEqual(spoken_text(cues), "你好，\nworld！\n下一句。")
            self.assertEqual(sentence_boundaries(cues, 4), [2])
            self.assertNotIn("-->", spoken_text(cues))
            self.assertTrue(duration_warning(cues, 8))
            self.assertEqual(duration_warning(cues, 0), "")

    def test_invalid_and_overlap(self):
        with self.assertRaises(ValueError):
            parse_srt(SRT.replace("00:00:02,000", "00:00:00,000"))
        cues, warnings = parse_srt(SRT.replace("00:00:03,000", "00:00:01,000"))
        self.assertTrue(warnings)
        with self.assertRaises(ValueError):
            sentence_boundaries(cues, 4)

    def test_sort_and_portable_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for n in range(1, 11):
                Image.new("RGB", (16, 16)).save(root / f"图{n}.png")
            project = Project()
            add_images(project, root.glob("*.png"))
            sort_images(project, "natural", explicit=True)
            self.assertEqual(
                [Path(i.path).stem for i in project.images],
                [f"图{n}" for n in range(1, 11)],
            )
            move_image(project, 9, 0)
            sort_images(project, "natural")
            project.images[0].end = 7
            save_project(project, root / "工程.json")
            restored = load_project(root / "工程.json")
            self.assertEqual(restored, project)
            self.assertEqual(Path(restored.images[0].path).stem, "图10")
            self.assertNotIn(folder, (root / "工程.json").read_text())
