# SPDX-License-Identifier: GPL-3.0-or-later
"""Project-oriented CustomTkinter workspaces; all render jobs run off the UI thread."""

from copy import deepcopy
from collections import OrderedDict
from PIL import ImageTk
from scene_workspace import SceneWorkspaceMixin, invalidate_scene_audio
from dataclasses import replace
from pathlib import Path
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import customtkinter as ctk
from image_manager import (
    add_images,
    sort_images,
    move_image,
    ThumbnailCache,
    EXTENSIONS,
)
from project_manager import Project, load_project, save_project
from srt_parser import (
    read_srt,
    spoken_text,
    duration_warning,
    scale_cues,
    sentence_boundaries,
)
from timeline_planner import plan_timeline, edit_boundary
from media_tools import (
    MediaError,
    ExportCancelled,
    executable,
    probe,
    run_ffmpeg,
    validate_tool,
    ensure_writable,
)
from global_settings import GlobalSettings
from media_merger import export_video, subtitle_cues


def open_path(path):
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(
            ["xdg-open", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class VideoWorkspace(SceneWorkspaceMixin, ctk.CTkFrame):
    def __init__(self, app, mode):
        super().__init__(app, fg_color=app._c("surface"), corner_radius=18)
        self.app, self.mode = app, mode
        self.running = False
        self.cancel = threading.Event()
        self._cancel_button_event = self.cancel
        self.row_photos = OrderedDict()
        self.row_loading = False
        self.events = queue.Queue()
        self.cache = ThumbnailCache()
        self.buttons = []
        self.last_output = ""
        self.vars = {}
        self.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 10))
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(1, weight=1)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=6)
        actions = [
            ("导入视频项目", self.import_video_project),
            ("生成场景配音", self.generate_scene_voice),
            ("一键配音并导出", lambda: self.generate_scene_voice(True)),
            ("复用已有配音/SRT", self.reuse_scene_audio),
            ("打开工程", self.open_project),
            ("保存工程", self.save_project),
            ("导入 SRT", self.import_srt),
            ("导入配音", self.import_audio),
            ("添加图片", self.import_images),
            ("图片文件夹", self.import_folder),
            ("定位缺失素材", self.relocate),
            ("同步字幕…", self.sync_captions),
        ]
        for i, (text, command) in enumerate(actions):
            bar.grid_columnconfigure(i % 4, weight=1)
            self.button(bar, text, command).grid(
                row=i // 4, column=i % 4, sticky="ew", padx=3, pady=3
            )
        left = ctk.CTkFrame(self, fg_color=app._c("surface_alt"))
        left.grid(row=1, column=0, sticky="nsew", padx=(10, 5))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)
        tools = ctk.CTkFrame(left, fg_color="transparent")
        tools.grid(row=0, column=0, sticky="ew")
        self.sort_var = tk.StringVar(value="自然数字")
        ctk.CTkOptionMenu(
            tools,
            variable=self.sort_var,
            values=["文件名", "自然数字", "修改时间", "导入顺序"],
            width=110,
        ).pack(side="left", padx=3, pady=4)
        self.button(tools, "排序", self.sort, width=60).pack(side="left", padx=3)
        self.button(tools, "自动分配时长", self.plan, width=110).pack(
            side="left", padx=3
        )
        self.button(tools, "关联字幕", self.assign_sentences, width=80).pack(
            side="left", padx=3
        )
        table = ctk.CTkFrame(left)
        table.grid(row=1, column=0, sticky="nsew")
        table.grid_columnconfigure(0, weight=1)
        table.grid_rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            table,
            columns=(
                "number",
                "name",
                "narration",
                "start",
                "end",
                "duration",
                "sentence",
                "lock",
                "status",
            ),
            show="tree headings",
            selectmode="browse",
        )
        for key, label, width in [
            ("number", "场景 ID", 60),
            ("name", "图片", 100),
            ("narration", "解说文本", 150),
            ("start", "开始", 55),
            ("end", "结束", 55),
            ("duration", "秒", 45),
            ("sentence", "字幕范围", 65),
            ("lock", "锁定", 40),
            ("status", "状态", 60),
        ]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=25, stretch=key == "name")
        self.tree.column("#0", width=66, minwidth=40, stretch=False)
        self.tree.heading("#0", text="缩略图")
        style = ttk.Style(self)
        style.configure("Scene.Treeview", rowheight=42)
        self.tree.configure(style="Scene.Treeview")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.scroll_tree)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(xscrollcommand=horizontal.set)
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.tree.bind(
            "<MouseWheel>",
            lambda e: self.after(80, self.load_visible_thumbnails),
            add="+",
        )
        self.tree.bind(
            "<ButtonPress-1>",
            lambda e: setattr(self, "drag_row", self.tree.identify_row(e.y)),
            add="+",
        )
        self.tree.bind("<ButtonRelease-1>", self.drop_row, add="+")
        app._enable_drop_target(
            self.tree, lambda event: self.add_paths(app.tk.splitlist(event.data))
        )
        edits = ctk.CTkFrame(left, fg_color="transparent")
        edits.grid(row=2, column=0, sticky="ew")
        for i, (text, command) in enumerate(
            [
                ("↑", lambda: self.move(-1)),
                ("↓", lambda: self.move(1)),
                ("首位", lambda: self.move(-99999)),
                ("末位", lambda: self.move(99999)),
                ("复制", self.duplicate),
                ("替换", self.replace_image),
                ("删除", self.delete),
            ]
        ):
            self.button(edits, text, command, width=45).grid(
                row=0, column=i, padx=2, pady=4
            )
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.grid(row=3, column=0, sticky="ew")
        self.start_var = tk.StringVar(value="0")
        self.end_var = tk.StringVar(value="0")
        self.lock_var = tk.BooleanVar()
        for label, var in [("开始", self.start_var), ("结束", self.end_var)]:
            ctk.CTkLabel(row, text=label).pack(side="left")
            ctk.CTkEntry(row, textvariable=var, width=65).pack(side="left", padx=3)
        ctk.CTkCheckBox(row, text="锁定结束", variable=self.lock_var, width=85).pack(
            side="left", padx=4
        )
        self.button(row, "应用", lambda: self.edit(False), width=55).pack(
            side="left", padx=3
        )
        self.button(row, "吸附句末", lambda: self.edit(True), width=75).pack(
            side="left", padx=3
        )
        self.caption = ctk.CTkLabel(left, text="", wraplength=440)
        self.caption.grid(row=4, column=0, sticky="ew")
        self.slider = ctk.CTkSlider(left, from_=0, to=1, command=self.seek)
        self.slider.grid(row=5, column=0, sticky="ew", padx=12, pady=4)
        self.slider.set(0)
        right = ctk.CTkScrollableFrame(self, fg_color=app._c("surface_alt"))
        right.grid(row=1, column=1, sticky="nsew", padx=(5, 10))
        for text, action in [
            ("编辑场景解说", self.edit_scene),
            ("暂时跳过图片", self.skip_scene_image),
            ("恢复场景自动时间", self.reset_scene_timing),
        ]:
            self.button(right, text, action).pack(fill="x", pady=3)
        self.preview_label = ctk.CTkLabel(right, text="选择图片预览", height=120)
        self.preview_label.pack(pady=4)
        self.scene_detail = ctk.CTkLabel(right, text="", wraplength=280, justify="left")
        self.scene_detail.pack(fill="x", pady=4)
        self.settings_form(right)
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=4)
        for text, command in [
            ("输出目录", self.output_folder),
            ("无声 MP4", lambda: self.export(silent=True)),
            ("快速预览", lambda: self.export(preview=True)),
            ("开始导出", self.export),
        ]:
            self.button(bottom, text, command, width=90).pack(side="left", padx=3)
        self.cancel_button = ctk.CTkButton(
            bottom, text="取消", width=60, command=self.cancel.set
        )
        self.cancel_button.pack(side="left", padx=3)
        self.progress = ctk.CTkProgressBar(self)
        self.progress.grid(row=3, column=0, columnspan=2, sticky="ew", padx=15)
        self.progress.set(0)
        self.status = ctk.CTkLabel(
            self, text="", wraplength=900, anchor="w", justify="left"
        )
        self.status.grid(row=4, column=0, columnspan=2, sticky="ew", padx=12, pady=4)
        self.refresh()
        self.after(80, self.poll)

    @property
    def project(self):
        return self.app.project

    def button(self, parent, text, command, **kwargs):
        def guarded():
            try:
                if not self.running:
                    command()
            except Exception as exc:
                self.error(exc)

        button = ctk.CTkButton(parent, text=text, command=guarded, height=28, **kwargs)
        self.buttons.append(button)
        return button

    def error(self, exc):
        self.status.configure(text=str(exc)[-1200:])
        self.app.log("[视频] " + str(exc))
        dialog = ctk.CTkToplevel(self)
        dialog.title("视频处理错误 — 完整诊断")
        dialog.geometry("850x500")
        box = ctk.CTkTextbox(dialog, wrap="word")
        box.pack(fill="both", expand=True, padx=12, pady=12)
        box.insert("1.0", str(exc))
        box.configure(state="disabled")

    def entry(self, parent, label, group, key, options=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text=label, width=110, anchor="w").pack(side="left")
        value = getattr(self.project, group).get(key, "")
        var = tk.StringVar(value=str(value))
        self.vars[(group, key)] = var
        widget = (
            ctk.CTkOptionMenu(row, variable=var, values=options, width=150)
            if options
            else ctk.CTkEntry(row, textvariable=var, width=150)
        )
        widget.pack(side="right", fill="x", expand=True)

    def settings_form(self, parent):
        self.info = ctk.CTkLabel(parent, text="", wraplength=300, justify="left")
        self.info.pack(fill="x", pady=5)
        ctk.CTkLabel(
            parent, text="时间规划（秒）", font=self.app._font(size=15, weight="bold")
        ).pack(anchor="w")
        for key, label in [
            ("desired", "期望单图时长"),
            ("minimum", "最短时长"),
            ("maximum", "最长时长"),
            ("target", "目标时长（0=配音）"),
            ("paragraph_pause", "段落停顿"),
        ]:
            self.entry(parent, label, "planning", key)
        ctk.CTkLabel(
            parent, text="画面与转场", font=self.app._font(size=15, weight="bold")
        ).pack(anchor="w", pady=(12, 0))
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x")
        import math

        w, h = int(self.project.video["width"]), int(self.project.video["height"])
        divisor = math.gcd(w, h)
        self.resolution_var = tk.StringVar(value=f"{w}×{h} · {w//divisor}:{h//divisor}")
        ctk.CTkOptionMenu(
            row,
            variable=self.resolution_var,
            values=[
                "1080×1920 · 9:16",
                "720×1280 · 9:16",
                "1920×1080 · 16:9",
                "1080×1080 · 1:1",
            ],
            command=self.preset,
        ).pack(fill="x")
        for key, label, options in [
            ("width", "宽度", None),
            ("height", "高度", None),
            ("fps", "帧率", ["24", "25", "30", "60"]),
            ("fit", "图片适配", ["contain", "cover", "blur", "custom"]),
            (
                "motion",
                "图片运动",
                ["none", "zoom_in", "zoom_out", "left_right", "right_left", "vertical"],
            ),
            ("transition", "转场", ["cut", "fade"]),
            ("transition_duration", "淡入淡出时长", None),
            ("zoom", "自定义缩放", None),
            ("x", "水平位置 0–1", None),
            ("y", "垂直位置 0–1", None),
            ("encoder", "编码器", ["libx264", "h264_nvenc"]),
        ]:
            self.entry(parent, label, "video", key, options)
        if self.mode == "images":
            self.button(parent, "选择 FFmpeg", lambda: self.choose_tool("ffmpeg")).pack(
                fill="x", pady=4
            )
            self.button(
                parent, "选择 FFprobe", lambda: self.choose_tool("ffprobe")
            ).pack(fill="x", pady=4)
            ctk.CTkLabel(
                parent,
                text="字幕样式、背景音乐及外部视频请到“合成导出”设置。",
                wraplength=280,
            ).pack(pady=8)
            return
        ctk.CTkLabel(
            parent,
            text="字幕（真实配音时间轴优先）",
            font=self.app._font(size=15, weight="bold"),
        ).pack(anchor="w", pady=(12, 0))
        self.subtitle_enabled = tk.BooleanVar(value=self.project.style["burn"])
        ctk.CTkCheckBox(
            parent,
            text="添加字幕到视频（取消勾选则不添加）",
            variable=self.subtitle_enabled,
        ).pack(anchor="w", pady=6)
        self.subtitle_source = tk.StringVar(
            value=(
                "选择 SRT 文件"
                if self.project.style.get("subtitle_source") == "srt"
                else "当前工程字幕 / TTS 时间轴"
            )
        )
        ctk.CTkOptionMenu(
            parent,
            variable=self.subtitle_source,
            values=["当前工程字幕 / TTS 时间轴", "选择 SRT 文件"],
        ).pack(fill="x", pady=4)
        self.button(parent, "选择字幕 SRT…", self.choose_subtitle_srt).pack(
            fill="x", pady=4
        )
        self.subtitle_info = ctk.CTkLabel(
            parent, text="", wraplength=280, justify="left"
        )
        self.subtitle_info.pack(fill="x", pady=4)
        for key, label, options in [
            ("font", "字体名称", None),
            ("size", "字号", None),
            ("color", "文字颜色", None),
            ("outline_color", "描边颜色", None),
            ("outline", "描边宽度", None),
            ("shadow", "阴影", None),
            ("margin", "垂直边距", None),
            ("alignment", "位置 2底/5中/8顶", ["2", "5", "8"]),
            ("wrap", "自动换行字数", None),
        ]:
            self.entry(parent, label, "style", key, options)
        self.button(parent, "选择字体文件（可选）", self.choose_font).pack(
            fill="x", pady=4
        )
        ctk.CTkLabel(
            parent, text="配音与背景音乐", font=self.app._font(size=15, weight="bold")
        ).pack(anchor="w", pady=(12, 0))
        for key, label, options in [
            ("voice_volume", "配音音量 0–4", None),
            ("bgm_volume", "BGM 音量 0–1", None),
            ("loop", "音乐循环", ["True", "False"]),
            ("fade", "音乐淡入淡出秒数", None),
        ]:
            self.entry(parent, label, "sound", key, options)
        for text, command in [
            ("导入 BGM", self.import_bgm),
            ("清除 BGM", lambda: self.set_media("bgm", "")),
            ("导入外部 MP4", self.import_video),
            ("使用当前图片视频", lambda: self.set_media("external_video", "")),
            ("选择 FFmpeg", lambda: self.choose_tool("ffmpeg")),
            ("选择 FFprobe", lambda: self.choose_tool("ffprobe")),
            (
                "打开导出文件",
                lambda: open_path(self.last_output) if self.last_output else None,
            ),
            (
                "打开输出文件夹",
                lambda: (
                    open_path(self.project.output_dir)
                    if self.project.output_dir
                    else None
                ),
            ),
        ]:
            self.button(parent, text, command).pack(fill="x", pady=3)
        ctk.CTkLabel(
            parent,
            text="fade 为句末渐暗/渐亮，不重叠片段；\ncut 保持严格句末切换。预览为低分辨率 MP4。",
            wraplength=300,
            justify="left",
        ).pack(pady=6)

    def collect(self):
        if hasattr(self, "subtitle_enabled"):
            self.project.style["burn"] = self.subtitle_enabled.get()
            self.project.style["subtitle_source"] = (
                "srt" if self.subtitle_source.get() == "选择 SRT 文件" else "current"
            )
        updates = {}
        for (group, key), var in self.vars.items():
            old = getattr(self.project, group)[key]
            text = var.get()
            try:
                value = (text == "True") if isinstance(old, bool) else type(old)(text)
            except ValueError:
                raise ValueError(f"{key} 的值无效：{text}")
            updates[(group, key)] = value
        for (group, key), value in updates.items():
            getattr(self.project, group)[key] = value
        from dataclasses import asdict

        self.project.narration = asdict(self.app._build_cfg())
        GlobalSettings().remember_project(self.project)

    def preset(self, text):
        w, h = text.split(" · ")[0].split("×")
        self.vars[("video", "width")].set(w)
        self.vars[("video", "height")].set(h)

    def set_media(self, key, value):
        setattr(self.project, key, value)
        self.refresh()

    def choose_tool(self, key):
        path = filedialog.askopenfilename(title=f"选择 {key}.exe")
        if path:
            verified = validate_tool(key, path)
            self.project.video[key] = verified
            GlobalSettings().update(video={key: verified})
            self.status.configure(text=f"{key} 已验证并永久保存：{verified}")

    def choose_font(self):
        path = filedialog.askopenfilename(filetypes=[("字体", "*.ttf *.ttc *.otf")])
        if path:
            from PIL import ImageFont

            self.project.style["font_file"] = path
            family = ImageFont.truetype(path, 16).getname()[0]
            self.vars[("style", "font")].set(family)

    def import_srt(self):
        if self.app._on_import_srt():
            self.refresh()

    def choose_subtitle_srt(self):
        path = filedialog.askopenfilename(
            title="选择视频字幕 SRT", filetypes=[("SRT 字幕", "*.srt")]
        )
        if not path:
            return
        cues, warnings = read_srt(path)
        self.project.subtitle_srt = path
        self.project.subtitle_cues = cues
        self.subtitle_enabled.set(True)
        self.subtitle_source.set("选择 SRT 文件")
        self.project.style.update(burn=True, subtitle_source="srt")
        self.refresh()
        warning = duration_warning(cues, self.project.audio_duration)
        note = "\n".join(warnings + ([warning] if warning else []))
        if note:
            messagebox.showwarning("字幕检查", note, parent=self)

    def import_audio(self):
        if self.app._busy:
            raise ValueError("请先停止当前配音生成任务")
        path = filedialog.askopenfilename(filetypes=[("配音", "*.mp3 *.wav *.m4a")])
        if path:
            info = probe(path, self.project.video.get("ffprobe", ""))
            if not any(s["codec_type"] == "audio" for s in info["streams"]):
                raise ValueError("文件没有音频流")
            self.project.audio = path
            self.project.audio_duration = info["duration"]
            self.project.timeline = {}
            self.refresh()
            warning = duration_warning(self.project.cues, info["duration"])
            if warning:
                messagebox.showwarning("字幕与配音时长不同", warning, parent=self)

    def sync_captions(self):
        if not self.project.cues or not self.project.audio_duration:
            raise ValueError("请先导入字幕和配音")
        if messagebox.askyesno(
            "手动同步字幕",
            "按配音总时长等比缩放字幕时间？\n不改变音频，不裁剪配音。此操作不能替代逐句校对。",
            parent=self,
        ):
            self.project.cues = scale_cues(
                self.project.cues, self.project.audio_duration
            )
            self.project.timeline = {}
            self.refresh()

    def import_images(self):
        paths = filedialog.askopenfilenames(
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.bmp")]
        )
        if paths:
            self.add_paths(paths)

    def import_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.add_paths(
                sorted(
                    (
                        str(p)
                        for p in Path(folder).iterdir()
                        if p.suffix.lower() in EXTENSIONS
                    )
                )
            )

    def add_paths(self, paths):
        try:
            if self.running:
                return
            expanded = []
            for path in paths:
                p = Path(path)
                (
                    expanded.extend(
                        str(i) for i in p.iterdir() if i.suffix.lower() in EXTENSIONS
                    )
                    if p.is_dir()
                    else expanded.append(str(p))
                )
            add_images(self.project, expanded)
            self.refresh()
        except Exception as exc:
            self.error(exc)

    def selected(self):
        selection = self.tree.selection()
        if not selection:
            raise ValueError("请先选择图片")
        return int(selection[0])

    def refresh(self):
        self.row_photos.clear()
        if hasattr(self, "subtitle_info"):
            self.subtitle_info.configure(
                text=f'已选 SRT：{Path(self.project.subtitle_srt).name or "未选择"} · {len(self.project.subtitle_cues)} 条字幕\n仅用于视频字幕，不修改原配音文字或图片时间轴。'
            )
        self.tree.delete(*self.tree.get_children())
        for n, clip in enumerate(self.project.images):
            self.tree.insert(
                "",
                "end",
                iid=str(n),
                values=(
                    clip.scene_id or n + 1,
                    Path(clip.path).name,
                    clip.narration,
                    f"{clip.start:.3f}" if clip.end > clip.start else "--",
                    f"{clip.end:.3f}" if clip.end > clip.start else "--",
                    f"{clip.end-clip.start:.3f}" if clip.end > clip.start else "--",
                    f"{clip.sentence_start}–{clip.sentence_end}",
                    "✓" if clip.locked else "",
                    (
                        "跳过图片/保留解说"
                        if clip.skipped
                        else ("缺图" if not Path(clip.path).is_file() else clip.status)
                    ),
                ),
            )
        self.info.configure(
            text=f'配音：{Path(self.project.audio).name or "未导入"}\n实际时长 {self.project.audio_duration:.3f}s · {len(self.project.cues)} 条字幕\n图片 {len(self.project.images)} 张\nBGM：{Path(self.project.bgm).name or "无"}\n视频：{Path(self.project.external_video).name or "当前图片"}\n输出：{self.project.output_dir or "尚未选择"}'
        )
        self.slider.configure(
            to=max(
                0.001,
                (
                    self.project.images[-1].end
                    if self.project.images
                    else self.project.audio_duration
                ),
            )
        )
        self.status.configure(
            text=(
                duration_warning(self.project.cues, self.project.audio_duration)
                if self.project.audio_duration
                else "导入 SRT 后回到“音频生成”配音，或导入外部配音。"
            )
        )

        if self.project.mode == "scenes":
            self.status.configure(
                text=self.project.timing_conflict
                or (
                    f"场景工程：{len(self.project.images)} 段解说，实际配音 {self.project.audio_duration:.6f}s"
                    if self.project.audio
                    else "场景模式：请生成场景配音或复用已有音频与 SRT；不按图片平均分配。"
                )
            )
        self.after(50, self.load_visible_thumbnails)

    def scroll_tree(self, *args):
        self.tree.yview(*args)
        self.after(60, self.load_visible_thumbnails)

    def load_visible_thumbnails(self):
        if self.row_loading or not self.tree.winfo_exists():
            return
        children = self.tree.get_children()
        if not children:
            return
        first = max(0, int(self.tree.yview()[0] * len(children)))
        items = [
            (row, self.project.images[int(row)].path)
            for row in children[first : first + 14]
            if row not in self.row_photos
            and Path(self.project.images[int(row)].path).is_file()
        ]
        if not items:
            return
        self.row_loading = True

        def load():
            for row, path in items:
                try:
                    self.events.put(
                        (
                            "row_thumbnail",
                            row,
                            path,
                            self.cache.get(path, (64, 38)).copy(),
                        )
                    )
                except Exception:
                    pass
            self.events.put(("row_thumbnails_done",))

        threading.Thread(target=load, daemon=True).start()

    def select(self, event=None):
        if not self.tree.selection():
            return
        index = self.selected()
        clip = self.project.images[index]
        self.scene_detail.configure(
            text=f"场景 {clip.scene_id}\n{clip.narration}" if clip.scene_id else ""
        )
        self.start_var.set(f"{clip.start:.3f}")
        self.end_var.set(f"{clip.end:.3f}")
        self.lock_var.set(clip.locked)
        self.show_image(index)
        self.slider.set(clip.start)
        self.show_caption(clip.start)

    def show_image(self, index):
        path = self.project.images[index].path
        self.current_image = path

        def load():
            try:
                self.events.put(("thumbnail", path, self.cache.get(path).copy()))
            except Exception as exc:
                self.events.put(("thumbnail_error", path, str(exc)))

        # One thumbnail at a time; selection changes discard stale results.
        if not getattr(self, "thumb_loading", False):
            self.thumb_loading = True
            threading.Thread(target=load, daemon=True).start()

    def show_caption(self, time):
        cues = [
            f'[{n+1}] {c["text"]}'
            for n, c in enumerate(self.project.cues)
            if c["start"] <= time < c["end"]
        ]
        self.caption.configure(text=f"{time:.2f}s  " + "\n".join(cues))

    def seek(self, value):
        self.show_caption(value)
        for n, clip in enumerate(self.project.images):
            if clip.start <= value < clip.end:
                if getattr(self, "current_image", None) != clip.path:
                    self.show_image(n)
                return

    def assign_sentences(self):
        clip = self.project.images[self.selected()]
        value = simpledialog.askstring(
            "关联字幕",
            "输入字幕编号范围，例如 1-3。此项用于标记；时间仍由时间轴编辑。",
            initialvalue=f"{clip.sentence_start}-{clip.sentence_end}",
            parent=self,
        )
        if value:
            start, end = map(int, value.split("-"))
            if not 1 <= start <= end <= len(self.project.cues):
                raise ValueError("字幕范围无效")
            clip.sentence_start, clip.sentence_end = start, end
            self.refresh()

    def sort(self):
        if self.project.manual_order and not messagebox.askyesno(
            "重新排序", "将替换当前手动顺序。继续？", parent=self
        ):
            return
        sort_images(
            self.project,
            dict(
                zip(
                    ["文件名", "自然数字", "修改时间", "导入顺序"],
                    ["name", "natural", "modified", "import"],
                )
            )[self.sort_var.get()],
            True,
        )
        self.refresh()

    def reorder(self, index, destination):
        if self.running:
            return
        move_image(self.project, index, destination)
        if self.project.mode == "scenes":
            invalidate_scene_audio(self.project)
        else:
            self.reflow()
        self.refresh()
        dest = max(0, min(destination, len(self.project.images) - 1))
        self.tree.selection_set(str(dest))

    def reflow(self):
        # Durations travel with images; automatic planning remains an explicit action.
        start = 0
        for clip in self.project.images:
            duration = max(0, clip.end - clip.start)
            clip.start = start
            clip.end = start + duration
            start = clip.end

    def move(self, amount):
        index = self.selected()
        self.reorder(index, index + amount)

    def drop_row(self, event):
        source = getattr(self, "drag_row", "")
        target = self.tree.identify_row(event.y)
        if source and target and source != target:
            self.reorder(int(source), int(target))

    def duplicate(self):
        if self.project.mode == "scenes":
            raise MediaError(
                "场景 ID 必须唯一；请在脚本和 image_plan.json 中添加新场景后重新导入"
            )
        n = self.selected()
        self.project.images.insert(
            n + 1,
            replace(
                self.project.images[n],
                locked=False,
                import_order=max(c.import_order for c in self.project.images) + 1,
            ),
        )
        self.project.manual_order = True
        self.reflow()
        self.refresh()

    def replace_image(self):
        n = self.selected()
        path = filedialog.askopenfilename(
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.bmp")]
        )
        if path:
            check = Project()
            add_images(check, [path])
            self.project.images[n].path = check.images[0].path
            self.project.images[n].skipped = False
            self.project.images[n].status = "ready"
            self.refresh()

    def delete(self):
        if self.project.mode == "scenes":
            self.skip_scene_image()
            return
        del self.project.images[self.selected()]
        self.project.manual_order = True
        self.reflow()
        self.refresh()

    def plan(self):
        self.collect()
        plan_timeline(self.project)
        self.refresh()

    def edit(self, snap):
        index = self.selected()
        start, end = float(self.start_var.get()), float(self.end_var.get())
        draft = deepcopy(self.project)
        if index == 0 and abs(start) > 0.001:
            raise ValueError("第一张图片必须从 0 开始")
        exceptional = False
        if index:
            exceptional = edit_boundary(draft, index - 1, start, snap)
        if index < len(draft.images) - 1:
            exceptional = edit_boundary(draft, index, end, snap) or exceptional
        elif abs(end - draft.images[-1].end) > 0.001:
            raise ValueError("最后结束时间由完整配音决定，请更改目标时长后重新分配")
        if exceptional and not messagebox.askyesno(
            "非句末切换",
            "此时间位于句子中间。仍保存为手动特殊边界？\n可取消后使用“吸附句末”。",
            parent=self,
        ):
            return
        draft.images[index].locked = self.lock_var.get()
        if draft.mode == "scenes":
            clip = draft.images[index]
            clip.requested_start = clip.start
            clip.requested_end = clip.end
            clip.timing_source = "manual"
            clip.timing_origins.update(start="manual", end="manual")
            if index:
                draft.images[index - 1].requested_end = clip.start
                draft.images[index - 1].timing_origins["end"] = "manual"
            if index + 1 < len(draft.images):
                draft.images[index + 1].requested_start = clip.end
                draft.images[index + 1].timing_origins["start"] = "manual"
            draft.timing_conflict = ""
            draft.timing_warnings = ["手动场景边界位于解说中间"] if exceptional else []
        self.app.project = draft
        self.refresh()
        self.tree.selection_set(str(index))

    def save_project(self):
        self.collect()
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("视频工程", "*.json")]
        )
        if path:
            save_project(self.project, path)
            self.status.configure(text="工程已保存；请保留引用的素材文件。")

    def open_project(self):
        if self.app._busy:
            raise ValueError("请先停止当前配音生成任务")
        path = filedialog.askopenfilename(filetypes=[("视频工程", "*.json")])
        if path:
            project = load_project(path)
            if hasattr(self, "subtitle_enabled"):
                self.subtitle_enabled.set(project.style["burn"])
                self.subtitle_source.set(
                    "选择 SRT 文件"
                    if project.style.get("subtitle_source") == "srt"
                    else "当前工程字幕 / TTS 时间轴"
                )
            self.vars.clear()
            self.app.project = project
            self.app._apply_project_narration(project)
            self.app._textbox.delete("1.0", "end")
            self.app._textbox.insert(
                "1.0",
                (
                    "\n".join(c.narration for c in project.images)
                    if project.mode == "scenes"
                    else spoken_text(project.source_cues or project.cues)
                ),
            )
            self.app._on_text_changed()
            GlobalSettings().update(recent_project_dir=str(Path(path).parent))
            self.app._rebuild_ui()
            if project.missing_media():
                messagebox.showwarning(
                    "缺失素材",
                    "请点击“定位缺失素材”：\n" + "\n".join(project.missing_media()),
                    parent=self.app,
                )

    def relocate(self):
        for missing in self.project.missing_media():
            replacement = filedialog.askopenfilename(
                title=f"重新定位 {Path(missing).name}"
            )
            if not replacement:
                continue
            for key in ("audio", "source_srt", "subtitle_srt", "external_video", "bgm"):
                if getattr(self.project, key) == missing:
                    setattr(self.project, key, replacement)
            for clip in self.project.images:
                if clip.path == missing:
                    clip.path = replacement
            if self.project.style.get("font_file") == missing:
                self.project.style["font_file"] = replacement
        self.refresh()

    def output_folder(self):
        path = filedialog.askdirectory()
        if path:
            ensure_writable(path)
            self.project.output_dir = path
            GlobalSettings().update(output_dir=path)
            self.refresh()

    def import_bgm(self):
        path = filedialog.askopenfilename(filetypes=[("背景音乐", "*.mp3 *.wav *.m4a")])
        if path:
            self.set_media("bgm", path)

    def import_video(self):
        path = filedialog.askopenfilename(filetypes=[("视频", "*.mp4")])
        if path:
            self.set_media("external_video", path)

    def export(self, preview=False, silent=False, output_override=None):
        self.collect()
        if not self.project.external_video and (
            not self.project.images or self.project.images[-1].end <= 0
        ):
            self.plan()
        if preview:
            folder = Path(tempfile.gettempdir()) / "EdgeTTSVideoPreviews"
            folder.mkdir(exist_ok=True)
            import uuid

            output = str(folder / (uuid.uuid4().hex + ".mp4"))
        else:
            output = output_override or filedialog.asksaveasfilename(
                initialdir=self.project.output_dir or None,
                defaultextension=".mp4",
                filetypes=[("MP4 视频", "*.mp4")],
            )
            if not output:
                return
            self.project.output_dir = str(Path(output).parent)
        snapshot = deepcopy(self.project)
        if (
            snapshot.mode == "scenes"
            and snapshot.timing_warnings
            and not messagebox.askyesno(
                "手动边界确认",
                "\n".join(snapshot.timing_warnings) + "\n仍按手动时间导出？",
                parent=self,
            )
        ):
            return
        if snapshot.mode == "scenes":
            from scene_project import write_resolved_plan

            write_resolved_plan(snapshot)
        duration = (
            snapshot.images[-1].end
            if snapshot.images and not snapshot.external_video
            else snapshot.audio_duration
        )
        warning = (
            duration_warning(subtitle_cues(snapshot), snapshot.audio_duration)
            if snapshot.style["burn"] and not silent
            else ""
        )
        if (
            warning
            and not silent
            and not messagebox.askyesno(
                "字幕同步检查", warning + "\n仍继续导出？", parent=self
            )
        ):
            return
        self.cancel = self._cancel_button_event
        self.cancel.clear()
        self.running = True
        self.progress.set(0)
        for button in self.buttons:
            button.configure(state="disabled")
        self.status.configure(
            text=f'预计 {duration:.3f}s · {snapshot.video["width"]}×{snapshot.video["height"]} · {snapshot.video["fps"]} FPS · H.264 + AAC\n{output}'
        )

        def worker():
            try:
                result = export_video(
                    snapshot,
                    output,
                    self.cancel,
                    lambda p: self.events.put(("progress", p)),
                    preview,
                    silent,
                    status=lambda text: self.events.put(("scene_status", text)),
                )
                self.events.put(("done", result, preview))
            except Exception as exc:
                self.events.put(("failed", exc))

        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind in ("scene_ready", "scene_ready_confirm"):
                    self.receive_scene_result(event)
                    return
                elif kind == "row_thumbnail":
                    _, row, path, img = event
                    if (
                        self.tree.exists(row)
                        and int(row) < len(self.project.images)
                        and self.project.images[int(row)].path == path
                    ):
                        photo = ImageTk.PhotoImage(img, master=self)
                        self.row_photos[row] = photo
                        self.tree.item(row, image=photo)
                        while len(self.row_photos) > 24:
                            old, _ = self.row_photos.popitem(last=False)
                            if self.tree.exists(old):
                                self.tree.item(old, image="")
                elif kind == "row_thumbnails_done":
                    self.row_loading = False
                    self.after(20, self.load_visible_thumbnails)
                elif kind == "scene_status":
                    self.status.configure(text=event[1])
                elif kind == "progress":
                    self.progress.set(event[1])
                elif kind in ("thumbnail", "thumbnail_error"):
                    self.thumb_loading = False
                    if kind == "thumbnail" and event[1] == getattr(
                        self, "current_image", None
                    ):
                        img = event[2]
                        self.photo = ctk.CTkImage(img, size=img.size)
                        self.preview_label.configure(image=self.photo, text="")
                    elif kind == "thumbnail_error":
                        self.preview_label.configure(image=None, text=event[2])
                    elif self.tree.selection():
                        self.show_image(self.selected())
                elif kind in ("done", "failed"):
                    self.running = False
                    for button in self.buttons:
                        button.configure(state="normal")
                    if kind == "done":
                        self.last_output = event[1]
                        self.progress.set(1)
                        self.status.configure(text="导出完成：" + event[1])
                        self.app.log("[视频] 导出完成：" + event[1])
                        if event[2]:
                            if messagebox.askyesno(
                                "预览已生成",
                                "打开临时 MP4？\n使用系统播放器播放/暂停、拖动时间与试听配音和 BGM。",
                                parent=self,
                            ):
                                open_path(event[1])
                    elif isinstance(event[1], ExportCancelled):
                        self.status.configure(text=str(event[1]))
                    else:
                        self.error(event[1])
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self.poll)
