# -*- coding: utf-8 -*-
"""Verify SRT batch ZIP export uses the original subtitle timestamps."""

import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from app import App, FileBatchItem
from tts_engine import TTSConfig


class SRTBatchExportTests(unittest.TestCase):
    def test_srt_zip_keeps_source_cues_and_audio(self):
        source = "1\n00:00:05,000 --> 00:00:08,000\n机器人开始放电！\n"
        timeline = {
            "boundary": "SRT",
            "sentences": [{
                "index": 0, "text": "机器人开始放电！",
                "start": 5.0, "end": 7.25, "srt_end": 8.0,
            }],
            "duration": 8.0,
            "audio_duration": 8.04,
        }
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "temp-cache.mp3"
            audio.write_bytes(b"fake mp3")
            destination = Path(tmp) / "saved.zip"
            item = FileBatchItem(
                index=0,
                source_path=str(Path(tmp) / "解说.srt"),
                display_name="解说.srt",
                raw_text=source,
                text="机器人开始放电！",
                cfg=TTSConfig(voice="zh-CN-YunxiNeural"),
                is_srt=True,
                status="done",
                output_path=str(audio),
                result_name="解说",
                srt_enabled=True,
                timeline_enabled=True,
                timeline=timeline,
            )
            # No export is performed until the original save action is called.
            self.assertFalse(destination.exists())
            App._write_file_batch_zip(None, str(destination), [item], True)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(archive.read("解说.mp3"), b"fake mp3")
                self.assertEqual(archive.read("解说.srt").decode("utf-8"), source)
                meta = json.loads(archive.read("batch.json").decode("utf-8"))
                self.assertEqual(meta["items"][0]["source_type"], "srt")
                self.assertEqual(meta["items"][0]["subtitle"], "解说.srt")
                produced = json.loads(archive.read("解说.timeline.json"))
                self.assertEqual(produced["sentences"][0]["start"], 5.0)


if __name__ == "__main__":
    unittest.main()
