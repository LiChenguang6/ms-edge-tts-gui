# SPDX-License-Identifier: GPL-3.0-or-later
"""Video workspaces share state with the existing audio application."""
import customtkinter as ctk


class VideoWorkspace(ctk.CTkFrame):
    def __init__(self, app, mode):
        super().__init__(app, fg_color=app._c('surface'), corner_radius=18)
        self.app = app
        self.mode = mode
        self.grid(row=2, column=0, sticky='nsew', padx=18, pady=(0, 10))
        ctk.CTkLabel(self, text='图拼视频' if mode == 'images' else '合成导出').pack(pady=20)
