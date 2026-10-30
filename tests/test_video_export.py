# SPDX-License-Identifier: GPL-3.0-or-later
"""Small generated fixtures exercise the real FFmpeg pipeline on Windows and Linux."""

import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from project_manager import Project, ImageClip
from media_tools import executable, probe, run_ffmpeg, MediaError, ExportCancelled
from media_merger import export_video, write_ass
from video_renderer import render_images


class ToolTests(unittest.TestCase):
    def test_missing_tool_message(self):
        with patch("shutil.which", return_value=None), patch.dict(
            "os.environ", {}, clear=True
        ):
            with self.assertRaisesRegex(MediaError, "未找到 ffmpeg"):
                executable("ffmpeg")

    def test_subtitles_are_literal(self):
        p = Project(cues=[dict(start=0, end=1, text=r"{\pos(0,0)}测试\N")])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "字幕.ass"
            write_ass(p, path)
            content = path.read_text(encoding="utf-8-sig")
            self.assertNotIn(r"{\pos", content)
            self.assertIn("测试", content)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/FFprobe not installed"
)
class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="中文 media ")
        self.root = Path(self.temp.name)
        for n, color in enumerate(["red", "blue"]):
            Image.new("RGB", (120, 80), color).save(self.root / f"图片 {n}.png")
        run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-c:a",
                "pcm_s16le",
                self.root / "配音.wav",
            ]
        )
        self.p = Project(
            audio=str(self.root / "配音.wav"),
            audio_duration=2,
            images=[
                ImageClip(str(self.root / "图片 0.png"), 0, 1),
                ImageClip(str(self.root / "图片 1.png"), 1, 2),
            ],
            cues=[
                dict(start=0, end=1, text="第一句。"),
                dict(start=1, end=2, text="Second sentence!"),
            ],
        )
        self.p.video.update(width=180, height=320, fps=30)
        self.p.style.update(size=24, margin=20)

    def tearDown(self):
        self.temp.cleanup()

    def test_real_mp4_has_full_audio_and_video(self):
        self.p.bgm = self.p.audio
        self.p.video.update(motion="zoom_in", fit="blur", transition="fade")
        progress = []
        output = self.root / "中文 输出.mp4"
        export_video(self.p, output, progress=progress.append)
        info = probe(output)
        self.assertAlmostEqual(info["duration"], 2, delta=0.08)
        self.assertEqual({s["codec_type"] for s in info["streams"]}, {"audio", "video"})
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual(
            (video["codec_name"], video["pix_fmt"], video["width"], video["height"]),
            ("h264", "yuv420p", 180, 320),
        )
        # Real red/blue pictures survive rendering; verifies the image inputs were used.
        for time, channel in [(0.5, 0), (1.5, 2)]:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    str(time),
                    "-i",
                    str(output),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=1:1",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "-",
                ],
                capture_output=True,
                check=True,
            )
            self.assertEqual(len(result.stdout), 3)
            self.assertGreater(result.stdout[channel], result.stdout[2 - channel])
        self.assertGreater(max(progress), 0.99)

    def test_silent_and_external_video(self):
        silent = self.root / "silent.mp4"
        export_video(self.p, silent, silent=True)
        self.assertEqual([s["codec_type"] for s in probe(silent)["streams"]], ["video"])
        self.p.external_video = str(silent)
        self.p.video["encoder"] = (
            "h264_nvenc"  # CPU fallback on machines without NVENC.
        )
        export_video(self.p, self.root / "composed.mp4")
        self.assertAlmostEqual(
            probe(self.root / "composed.mp4")["duration"], 2, delta=0.08
        )

        self.p.external_video = str(self.root / "composed.mp4")
        muted = self.root / "muted.mp4"
        export_video(self.p, muted, silent=True)
        self.assertEqual([s["codec_type"] for s in probe(muted)["streams"]], ["video"])

    def test_cancellation_preserves_output(self):
        output = self.root / "existing.mp4"
        output.write_bytes(b"keep")
        cancel = threading.Event()

        def progress(value):
            cancel.set()

        with self.assertRaises(ExportCancelled):
            export_video(self.p, output, cancel, progress)
        self.assertEqual(output.read_bytes(), b"keep")
        self.assertTrue(Path(self.p.audio).is_file())

    def test_input_overwrite_is_rejected(self):
        with self.assertRaisesRegex(MediaError, "覆盖原始素材"):
            export_video(self.p, self.p.audio)

    def test_all_fit_and_motion_filters(self):
        from video_renderer import image_filter

        for fit in ["contain", "cover", "blur", "custom"]:
            for motion in [
                "none",
                "zoom_in",
                "zoom_out",
                "left_right",
                "right_left",
                "vertical",
            ]:
                video = {**self.p.video, "fit": fit, "motion": motion}
                with self.subTest(fit=fit, motion=motion):
                    run_ffmpeg(
                        [
                            "-loop",
                            "1",
                            "-i",
                            self.p.images[0].path,
                            "-vf",
                            image_filter(video, 2),
                            "-frames:v",
                            "2",
                            "-filter_complex_threads",
                            "1",
                            "-filter_threads",
                            "1",
                            "-f",
                            "null",
                            "-",
                        ]
                    )
