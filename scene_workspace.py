# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene workflow actions, kept separate from the legacy video workspace."""

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import threading
from tkinter import filedialog, messagebox, simpledialog
from global_settings import GlobalSettings
from scene_project import import_scene_project, map_existing_cues, write_resolved_plan
from scene_audio import prepare_scene_audio, reuse_existing_audio, scene_signature
from media_merger import export_video
from media_tools import MediaError


def invalidate_scene_audio(project):
    if project.mode != "scenes":
        return
    project.audio = ""
    project.audio_duration = 0
    project.cues = []
    project.timeline = {}
    project.timing_conflict = (
        "场景顺序或解说已修改，请重新生成/复用逐段配音；已完成缓存会自动复用"
    )
    for clip in project.images:
        clip.start = clip.end = 0
        clip.status = "pending" if Path(clip.path).is_file() else "missing"


class SceneWorkspaceMixin:
    def replace_project(self, project):
        self.vars.clear()
        if hasattr(self, "subtitle_enabled"):
            self.subtitle_enabled.set(project.style["burn"])
            self.subtitle_source.set(
                "选择 SRT 文件"
                if project.style.get("subtitle_source") == "srt"
                else "当前工程字幕 / TTS 时间轴"
            )
        self.app.project = project
        self.app._apply_project_narration(project)
        self.app._textbox.delete("1.0", "end")
        self.app._textbox.insert(
            "1.0",
            (
                "\n".join(c.narration for c in project.images)
                if project.mode == "scenes"
                else ""
            ),
        )
        self.app._on_text_changed()
        self.app._rebuild_ui()

    def import_video_project(self):
        if self.app._busy:
            raise MediaError("请先停止当前配音任务")
        if self.app._file_batch_active or self.app._pages:
            raise MediaError(
                "请先回到普通音频模式，再切换视频工程；当前批量配音不会被静默丢弃"
            )
        settings = GlobalSettings()
        directory = filedialog.askdirectory(
            title="选择包含 script.txt 和 image_plan.json 的视频项目",
            initialdir=settings.read().get("recent_project_dir") or None,
        )
        if not directory:
            return
        project = import_scene_project(directory)
        note = ""
        try:
            if reuse_existing_audio(project):
                note = "已有音频与 SRT 已精确映射，无须重复 TTS。"
        except (ValueError, MediaError) as exc:
            note = "工程已导入；现有音频无法直接映射：" + str(exc)
        settings.update(recent_project_dir=directory)
        missing = [c.scene_id for c in project.images if c.status == "missing"]
        self.replace_project(project)
        if missing:
            messagebox.showwarning(
                "缺少场景图片",
                "缺图场景："
                + ", ".join(missing)
                + "\n请在镜头表选择替换，或暂时跳过图片（保留解说，以黑画面显示）。",
                parent=self.app,
            )
        elif note:
            messagebox.showinfo("视频项目", note, parent=self.app)

    def reuse_scene_audio(self):
        if self.project.mode != "scenes":
            raise MediaError("请先导入视频项目")
        draft = deepcopy(self.project)
        if draft.audio and draft.cues:
            from media_tools import probe

            duration = probe(draft.audio, draft.video.get("ffprobe", ""))["duration"]
            map_existing_cues(draft, draft.cues, duration)
            draft.timing_conflict = ""
        elif not reuse_existing_audio(draft):
            raise MediaError(
                "未找到有效音频与 SRT。请导入配音/SRT 或点击生成场景配音。"
            )
        self.app.project = draft
        self.refresh()
        write_resolved_plan(draft)
        self.status.configure(text="已用实际音频/SRT 精确映射场景，没有重新请求 TTS。")

    def generate_scene_voice(self, compose=False):
        if self.project.mode != "scenes":
            raise MediaError("请先点击“导入视频项目”")
        if self.app._busy:
            raise MediaError("请先停止音频页任务")
        self.collect()
        output = ""
        if compose:
            output = filedialog.asksaveasfilename(
                initialdir=self.project.output_dir,
                defaultextension=".mp4",
                filetypes=[("MP4", "*.mp4")],
            )
            if not output:
                return
        snapshot = deepcopy(self.project)
        if output:
            snapshot.output_dir = str(Path(output).parent)
        cfg = self.app._build_cfg()
        snapshot.narration = asdict(cfg)
        GlobalSettings().remember_project(snapshot)
        # The Cancel button uses a bound Event.set; keep its original event object.
        self.cancel = self._cancel_button_event
        self.cancel.clear()
        cancel = self.cancel
        self.running = True
        self.progress.set(0)
        for button in self.buttons:
            button.configure(state="disabled")

        def worker():
            try:
                ready = bool(
                    snapshot.audio
                    and snapshot.audio_duration
                    and not snapshot.timing_conflict
                    and all(c.end > c.start for c in snapshot.images)
                    and (
                        not snapshot.scene_audio
                        or (
                            all(
                                snapshot.scene_audio.get(c.scene_id, {}).get(
                                    "fingerprint"
                                )
                                == scene_signature(c, cfg)
                                for c in snapshot.images
                            )
                            and snapshot.rendered_pause
                            == float(snapshot.planning.get("paragraph_pause", 0.15))
                        )
                    )
                )
                if not (compose and ready):
                    prepare_scene_audio(
                        snapshot,
                        cfg,
                        cancel,
                        lambda p: self.events.put(
                            ("progress", p * (0.4 if compose else 1))
                        ),
                        lambda text: self.events.put(("scene_status", text)),
                    )
                if output and not snapshot.timing_conflict:
                    if snapshot.timing_warnings:
                        self.events.put(("scene_ready_confirm", snapshot, output))
                        return
                    write_resolved_plan(snapshot)
                    export_video(
                        snapshot,
                        output,
                        cancel,
                        lambda p: self.events.put(("progress", 0.4 + 0.6 * p)),
                        status=lambda text: self.events.put(("scene_status", text)),
                    )
                self.events.put(
                    (
                        "scene_ready",
                        snapshot,
                        output if not snapshot.timing_conflict else "",
                    )
                )
            except Exception as exc:
                self.events.put(("failed", exc))

        threading.Thread(target=worker, daemon=True).start()

    def receive_scene_result(self, event):
        self.running = False
        for button in self.buttons:
            button.configure(state="normal")
        project = event[1]
        output = event[2]
        parent = self.app
        self.replace_project(project)
        workspace = parent._video_workspace
        if project.timing_conflict:
            workspace.error(
                MediaError(project.timing_conflict, stage="保留配音，等待解决时间冲突")
            )
        elif event[0] == "scene_ready_confirm":
            workspace.export(output_override=output)
        elif output:
            workspace.last_output = output
            workspace.progress.set(1)
            workspace.status.configure(text="视频导出完成：" + output)
        else:
            workspace.progress.set(1)
            workspace.status.configure(
                text=f"逐场景配音已完成，真实总时长 {project.audio_duration:.6f}s；resolved_image_plan.json 已保存。"
            )

    def edit_scene(self):
        clip = self.project.images[self.selected()]
        if not clip.scene_id:
            raise MediaError("旧模式请在原音频页编辑文稿")
        text = simpledialog.askstring(
            "编辑场景 " + clip.scene_id,
            "修改该场景朗读文字（不会写回原 script.txt）：",
            initialvalue=clip.narration,
            parent=self,
        )
        if text is None:
            return
        if not text.strip():
            raise MediaError("场景解说不能为空；不能静默删除脚本")
        if text.strip() != clip.narration:
            clip.narration = text.strip()
            invalidate_scene_audio(self.project)
        self.refresh()

    def skip_scene_image(self):
        clip = self.project.images[self.selected()]
        if self.project.mode != "scenes":
            raise MediaError("暂时跳过图片仅用于场景工程")
        if messagebox.askyesno(
            "保留解说，暂时跳过图片",
            f"场景 {clip.scene_id} 保留全部配音与字幕，以黑画面代替图片。继续？",
            parent=self,
        ):
            clip.skipped = True
            clip.status = "skipped"
            self.refresh()

    def reset_scene_timing(self):
        clip = self.project.images[self.selected()]
        if self.project.mode != "scenes":
            raise MediaError("请选择视频项目场景")
        if not messagebox.askyesno(
            "恢复自动时间",
            "清除此场景和相邻衔接边界的手动/JSON 时间，恢复真实配音映射？",
            parent=self,
        ):
            return
        n = self.selected()
        clip.requested_start = clip.requested_end = None
        clip.locked = False
        clip.timing_source = "auto"
        clip.timing_origins.update(start="auto", end="auto")
        if n:
            self.project.images[n - 1].requested_end = None
        if n + 1 < len(self.project.images):
            self.project.images[n + 1].requested_start = None
        if self.project.audio and self.project.cues:
            map_existing_cues(
                self.project, self.project.cues, self.project.audio_duration
            )
            self.project.timing_conflict = ""
        self.refresh()
