# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit frozen-executable startup check; never contacts the speech service."""

import json
from pathlib import Path


def run(report):
    import app
    from unittest.mock import patch

    with patch.object(app.App, "_start_background_jobs"):
        window = app.App()
        try:
            for mode in ("audio", "images", "export", "audio"):
                window._show_studio(mode)
                window.update()
            assert window._btn_generate.winfo_exists()
            Path(report).write_text(
                json.dumps({"status": "passed", "workspaces": 3}), encoding="utf-8"
            )
        finally:
            window._on_close()
