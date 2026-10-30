# -*- coding: utf-8 -*-
"""Offline tests for SRT timestamp-aligned dubbing (no network required)."""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tts_engine import TTSConfig, UserCanceled
from srt_dubbing import (
    SRTCue, SRTDurationError, dub_srt, parse_srt, read_srt, trim_edge_silence,
)


class ParseSRTTests(unittest.TestCase):
    def test_multiline_bom_crlf_html_and_comma_ms(self):
        cues = parse_srt(
            "\ufeff1\r\n00:00:01,250 --> 00:00:03,000\r\n"
            "<i>你好</i>\r\n世界 &amp; 玩家\r\n\r\n"
            "2\r\n00:00:05.000 --> 00:00:06.250\r\n第二句\r\n"
        )
        self.assertEqual(
            [(x.start_ms, x.end_ms, x.text) for x in cues],
            [(1250, 3000, "你好 世界 & 玩家"), (5000, 6250, "第二句")],
        )

    def test_srt_without_numeric_indices(self):
        cues = parse_srt("00:00:01,000 --> 00:00:02,000\nHello")
        self.assertEqual(cues[0].text, "Hello")

    def test_overlapping_and_invalid_times_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "重叠"):
            parse_srt(
                "1\n00:00:00,000 --> 00:00:03,000\nA\n\n"
                "2\n00:00:02,000 --> 00:00:04,000\nB"
            )
        with self.assertRaises(ValueError):
            parse_srt("1\n00:01:60,000 --> 00:02:00,000\nA")
        with self.assertRaises(ValueError):
            parse_srt("1\n00:00:02,000 --> 00:00:01,000\nA")
        with self.assertRaises(ValueError):
            parse_srt("not a subtitle")

    def test_utf16_and_gb18030_files(self):
        srt = "1\n00:00:01,000 --> 00:00:02,000\n中文配音"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "captions.srt"
            for codec in ("utf-16", "gb18030"):
                path.write_bytes(srt.encode(codec))
                self.assertEqual(read_srt(path)[0].text, "中文配音")

    def test_trim_small_silence_keeps_speech(self):
        audio = b"\0\0" * 5000 + b"\xff\x1f" * 24000 + b"\0\0" * 6000
        result = trim_edge_silence(audio)
        self.assertLess(len(result), len(audio))
        self.assertGreater(len(result), 48000)


class DubSRTTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "中文配音.mp3"
        self.rates = []

    def _fake_engine(self):
        parent = self

        class FakeTTSEngine:
            def __init__(self, *, cancel_event):
                self.cancel_event = cancel_event

            def generate(self, text, path, cfg):
                parent.rates.append(cfg.rate)
                Path(path).write_bytes(b"fake")
                return {"status": "done"}

        return FakeTTSEngine

    def test_silent_gaps_and_output_file(self):
        cues = [
            SRTCue(1, 1000, 2000, "Hello"),
            SRTCue(2, 4000, 5000, "World"),
        ]
        progress = []
        fake_pcm = b"\x10\x00" * 12000  # 0.5 seconds at 24kHz
        with patch("srt_dubbing.TTSEngine", self._fake_engine()), patch(
            "srt_dubbing.decode_mp3", return_value=fake_pcm
        ):
            result = dub_srt(
                cues, self.output, TTSConfig(voice="zh-CN-YunxiNeural"),
                on_progress=lambda i, total, rate: progress.append((i, total)),
            )
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["duration_seconds"], 5.0)
        self.assertEqual(progress, [(1, 2), (2, 2)])
        self.assertTrue(self.output.stat().st_size > 100)
        self.assertEqual(self.rates, ["+0%", "+0%"])

    def test_automatic_speed_increase(self):
        cue = SRTCue(1, 0, 1000, "Very long speech")
        def fake_decode(_path):
            # Original 1.6s, accelerated 0.8s.
            return b"\x10\x00" * (38400 if self.rates[-1] == "+0%" else 19200)
        with patch("srt_dubbing.TTSEngine", self._fake_engine()), patch(
            "srt_dubbing.decode_mp3", side_effect=fake_decode
        ):
            result = dub_srt([cue], self.output, TTSConfig())
        self.assertEqual(len(self.rates), 2)
        self.assertGreater(int(self.rates[-1].rstrip("%")), 0)
        self.assertEqual(result["speed_changes"][0][0], 1)

    def test_overrun_protects_existing_output(self):
        self.output.write_bytes(b"original mp3")
        with patch("srt_dubbing.TTSEngine", self._fake_engine()), patch(
            "srt_dubbing.decode_mp3", return_value=b"\x10\x00" * 48000
        ):
            with self.assertRaises(SRTDurationError):
                dub_srt(
                    [SRTCue(1, 0, 500, "Far too long")],
                    self.output, TTSConfig(), auto_speed=False,
                )
        self.assertEqual(self.output.read_bytes(), b"original mp3")

    def test_cancellation_preserves_existing_file(self):
        self.output.write_bytes(b"original")
        stopped = threading.Event()
        stopped.set()
        with patch("srt_dubbing.TTSEngine", self._fake_engine()):
            with self.assertRaises(UserCanceled):
                dub_srt(
                    [SRTCue(1, 0, 1000, "Stop")], self.output, TTSConfig(),
                    cancel_event=stopped,
                )
        self.assertEqual(self.output.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
