# SPDX-License-Identifier: GPL-3.0-or-later
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave
from PIL import Image
from global_settings import GlobalSettings, atomic_json
from image_manager import ThumbnailCache
from project_manager import save_project, load_project
from scene_project import (
    import_scene_project,
    map_existing_cues,
    apply_scene_spans,
    write_resolved_plan,
)
from scene_audio import prepare_scene_audio, reuse_existing_audio, RATE
from media_tools import ExportCancelled, MediaError, probe
from media_merger import export_video
from tts_engine import TTSConfig


def fixture(root, count, missing=None):
    root = Path(root)
    (root / "images").mkdir(parents=True, exist_ok=True)
    script = []
    scenes = []
    for n in range(1, count + 1):
        ident = f"{n:03}"
        text = f"这是第{n}段完整解说。"
        script.append(f"{ident}|{text}")
        if n != missing:
            Image.new("RGB", (48, 32), "red" if n % 2 else "blue").save(
                root / "images" / f"{ident}.png"
            )
        scenes.append(
            dict(
                id=ident,
                image_file=f"images/{ident}.png",
                script_line=n,
                narration=text,
                start=None,
                end=None,
                motion="none",
                prompt={"opaque": "不得执行的元数据"},
                extra=["保留", n],
            )
        )
    (root / "script.txt").write_text("\n".join(script), encoding="utf-8-sig")
    payload = dict(
        schema_version=1,
        topic="本地测试",
        script_file="script.txt",
        timing_mode="tts",
        video=dict(width=64, height=96, fps=30),
        scenes=scenes,
        unknown={"keep": True},
    )
    atomic_json(root / "image_plan.json", payload)
    return root


class FakeEngine:
    calls = []

    def __init__(self, **kwargs):
        self.timeline = {}

    def generate(self, text, path, cfg):
        self.calls.append(text)
        frames = 5001 + len(self.calls)
        with wave.open(path, "wb") as output:
            output.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            output.writeframes(b"\x00\x00" * frames)
        self.timeline = dict(
            boundary="SentenceBoundary",
            sentences=[dict(start=0, end=frames / RATE, text=text)],
        )
        return dict(status="done", path=path)


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="中文 场景 ")
        self.root = Path(self.temp.name)
        self.store = GlobalSettings(self.root / "defaults.json")
        FakeEngine.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def project(self, count=11, missing=None):
        return import_scene_project(
            fixture(self.root / "project", count, missing), self.store
        )

    def test_import_eleven_and_preserve_opaque_metadata(self):
        p = self.project()
        self.assertEqual(len(p.images), 11)
        self.assertEqual(
            [c.scene_id for c in p.images], [f"{n:03}" for n in range(1, 12)]
        )
        self.assertEqual(p.plan_metadata["unknown"], {"keep": True})
        self.assertEqual(p.images[0].metadata["extra"], ["保留", 1])
        self.assertNotIn("|", p.images[0].narration)
        self.assertEqual(p.images[0].script_line, 1)

    def test_missing_image_is_kept_with_exact_id(self):
        p = self.project(missing=7)
        self.assertEqual(len(p.images), 11)
        self.assertEqual(p.images[6].scene_id, "007")
        self.assertEqual(p.images[6].status, "missing")
        self.assertIn("007.png", p.missing_media()[0])

    def test_project_aspect_overrides_defaults_and_metadata_cannot_choose_tools(self):
        self.store.update(
            video={"width": 1920, "height": 1080, "ffmpeg": "saved-tool.exe"}
        )
        root = fixture(self.root / "portrait", 2)
        path = root / "image_plan.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["video"] = {"aspect_ratio": "9:16", "ffmpeg": "do-not-execute.exe"}
        atomic_json(path, data)
        project = import_scene_project(root, self.store)
        self.assertEqual(
            (project.video["width"], project.video["height"]), (1080, 1920)
        )
        self.assertEqual(project.video["ffmpeg"], "saved-tool.exe")
        self.assertEqual(project.plan_metadata["video"]["ffmpeg"], "do-not-execute.exe")

    def test_invalid_ids_and_mismatched_narration(self):
        p = self.project(2)
        path = Path(p.plan_file)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["scenes"][1]["id"] = "001"
        atomic_json(path, data)
        with self.assertRaisesRegex(ValueError, "编号"):
            import_scene_project(self.root / "project", self.store)

    def test_real_sample_positions_twenty_and_fifty_no_sentence_limit(self):
        for count in (10, 20, 50):
            with self.subTest(count=count):
                directory = fixture(self.root / str(count), count)
                p = import_scene_project(directory, self.store)
                before = (directory / "image_plan.json").read_bytes()
                call_start = len(FakeEngine.calls)
                prepare_scene_audio(
                    p, TTSConfig(), engine_factory=FakeEngine, request_interval=0
                )
                self.assertEqual(
                    FakeEngine.calls[call_start:], [c.narration for c in p.images]
                )
                expected = sum(r["frames"] for r in p.scene_audio.values()) + round(
                    0.15 * RATE
                ) * (count - 1)
                with wave.open(p.audio, "rb") as audio:
                    self.assertEqual(audio.getnframes(), expected)
                self.assertEqual(p.images[-1].end, expected / RATE)
                self.assertEqual(
                    sum(round((c.end - c.start) * RATE) for c in p.images), expected
                )
                self.assertTrue(
                    all(c.scene_id == q["scene_id"] for c, q in zip(p.images, p.cues))
                )
                self.assertTrue(all("|" not in text for text in FakeEngine.calls))
                resolved = json.loads(
                    (directory / "output" / "resolved_image_plan.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    resolved["scenes"][0]["prompt"], {"opaque": "不得执行的元数据"}
                )
                self.assertEqual((directory / "image_plan.json").read_bytes(), before)
                old = len(FakeEngine.calls)
                prepare_scene_audio(
                    p, TTSConfig(), engine_factory=FakeEngine, request_interval=0
                )
                self.assertEqual(len(FakeEngine.calls), old)

    def test_json_order_and_explicit_manual_cuts(self):
        p = self.project(3)
        p.images.reverse()
        spans = {c.scene_id: (n, n + 1) for n, c in enumerate(p.images)}
        cues = [
            dict(start=n, end=n + 1, text=c.narration) for n, c in enumerate(p.images)
        ]
        p.images[0].requested_end = 1.25
        p.images[0].timing_source = "json"
        apply_scene_spans(p, spans, 3, cues)
        self.assertEqual([c.scene_id for c in p.images], ["003", "002", "001"])
        self.assertEqual(p.images[1].start, 1.25)
        self.assertTrue(p.timing_warnings)
        p.images[0].locked = True
        apply_scene_spans(p, spans, 3, cues, regenerating=True)
        self.assertEqual(p.images[0].end, 1.25)
        p.images[-1].requested_end = 4
        with self.assertRaisesRegex(ValueError, "实际配音"):
            apply_scene_spans(p, spans, 3, cues)

    def test_existing_audio_srt_does_not_request_tts(self):
        p = self.project(11)
        prepare_scene_audio(
            p, TTSConfig(), engine_factory=FakeEngine, request_interval=0
        )
        source = p.audio
        again = import_scene_project(self.root / "project", self.store)
        with patch(
            "scene_audio.TTSEngine", side_effect=AssertionError("must not request TTS")
        ):
            self.assertTrue(reuse_existing_audio(again))
        self.assertEqual(Path(again.audio), Path(source))
        self.assertEqual(len(again.images), 11)
        self.assertAlmostEqual(
            again.images[-1].end, probe(source)["duration"], places=5
        )
        # One cue for the whole article cannot be arbitrarily split into 11 scene timings.
        with self.assertRaisesRegex(ValueError, "无法可靠对应"):
            map_existing_cues(
                again,
                [
                    dict(
                        start=0,
                        end=again.audio_duration,
                        text="".join(c.narration for c in again.images),
                    )
                ],
                again.audio_duration,
            )

    def test_cancel_and_resume_reuses_completed_scene(self):
        p = self.project(3)
        cancel = threading.Event()

        def progress(_):
            cancel.set()

        with self.assertRaises(ExportCancelled):
            prepare_scene_audio(
                p,
                TTSConfig(),
                cancel,
                progress,
                engine_factory=FakeEngine,
                request_interval=0,
            )
        self.assertEqual(len(FakeEngine.calls), 1)
        cancel.clear()
        prepare_scene_audio(
            p, TTSConfig(), cancel, engine_factory=FakeEngine, request_interval=0
        )
        self.assertEqual(len(FakeEngine.calls), 3)

        previous = len(FakeEngine.calls)
        p.images.reverse()
        p.output_dir = str(self.root / "另一个输出目录")
        prepare_scene_audio(
            p, TTSConfig(), engine_factory=FakeEngine, request_interval=0
        )
        self.assertEqual(len(FakeEngine.calls), previous)
        self.assertEqual(p.cues[0]["scene_id"], "003")
        (Path(p.output_dir) / ".scene-cache" / "manifest.json").write_text(
            "[]", encoding="utf-8"
        )
        prepare_scene_audio(
            p, TTSConfig(), engine_factory=FakeEngine, request_interval=0
        )
        self.assertEqual(len(FakeEngine.calls), previous)

        from types import SimpleNamespace
        from video_workspace import VideoWorkspace

        view = SimpleNamespace(
            project=p,
            sort_var=SimpleNamespace(get=lambda: "自然数字"),
            refresh=lambda: None,
        )
        VideoWorkspace.sort(view)
        self.assertEqual(p.audio, "")
        self.assertEqual([c.scene_id for c in p.images], ["001", "002", "003"])
        prepare_scene_audio(
            p, TTSConfig(), engine_factory=FakeEngine, request_interval=0
        )
        self.assertEqual(len(FakeEngine.calls), previous)
        self.assertEqual(p.cues[0]["scene_id"], "001")

    def test_bounded_thumbnail_cache(self):
        p = self.project(50)
        cache = ThumbnailCache(limit=4)
        for clip in p.images:
            image = cache.get(clip.path, (32, 32))
            self.assertLessEqual(image.width * image.height, 1024)
            self.assertLessEqual(len(cache.cache), 4)

    def test_fifty_real_image_segments_render_and_skip_retains_narration(self):
        p = self.project(50)
        p.style.update(size=14, margin=4, wrap=10)
        # Actual WAV samples and an existing matching SRT avoid 50 network calls.
        output = Path(p.output_dir)
        output.mkdir()
        with wave.open(str(output / "voice.wav"), "wb") as audio:
            audio.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * (RATE * 5))
        from tts_engine import timeline_to_srt

        cues = [
            dict(start=n / 10, end=(n + 1) / 10, text=c.narration)
            for n, c in enumerate(p.images)
        ]
        (output / "voice.srt").write_text(
            timeline_to_srt({"sentences": cues}), encoding="utf-8-sig"
        )
        self.assertTrue(reuse_existing_audio(p))
        p.images[7].skipped = True
        Path(p.images[7].path).unlink()
        events = []
        target = output / "50 scenes.mp4"
        export_video(p, target, status=events.append)
        self.assertEqual(len(events), 50)
        self.assertIn("008", events[7])
        self.assertEqual(len(p.cues), 50)
        self.assertAlmostEqual(probe(target)["duration"], 5, delta=1 / 30)

    def test_locked_edits_and_conflicts_survive_regeneration_and_save(self):
        p = self.project(3)
        prepare_scene_audio(
            p, TTSConfig(), engine_factory=FakeEngine, request_interval=0
        )
        p.images[0].requested_end = 0.3
        p.images[0].locked = True
        p.images[0].timing_source = "manual"
        p.images[0].timing_origins["end"] = "manual"
        prepare_scene_audio(
            p, TTSConfig(rate="+10%"), engine_factory=FakeEngine, request_interval=0
        )
        self.assertEqual(p.images[0].end, 0.3)
        p.images[-1].requested_end = 999
        p.images[-1].timing_source = "json"
        prepare_scene_audio(
            p, TTSConfig(rate="+20%"), engine_factory=FakeEngine, request_interval=0
        )
        self.assertTrue(Path(p.audio).is_file())
        self.assertTrue(p.timing_conflict)
        self.assertEqual(p.images[-1].requested_end, 999)
        path = self.root / "saved.json"
        save_project(p, path)
        restored = load_project(path)
        self.assertEqual(restored.images[0].scene_id, "001")
        self.assertTrue(restored.images[0].locked)
        self.assertEqual(
            restored.scene_audio["001"]["frames"], p.scene_audio["001"]["frames"]
        )
        with self.assertRaisesRegex(MediaError, "冲突"):
            export_video(restored, self.root / "bad.mp4")

    def test_render_error_identifies_scene_and_keeps_full_log(self):
        from video_renderer import render_images

        p = self.project(2)
        p.audio_duration = 1
        for n, c in enumerate(p.images):
            c.start = n / 2
            c.end = (n + 1) / 2
        Path(p.images[1].path).write_bytes(b"not an image")
        with self.assertRaises(MediaError) as error:
            render_images(p, self.root / "broken.mp4")
        self.assertIn("002", str(error.exception))
        self.assertIn("参数", str(error.exception))
        self.assertTrue(Path(error.exception.log_path).is_file())
        self.assertIn(
            "002.png", Path(error.exception.log_path).read_text(encoding="utf-8")
        )
