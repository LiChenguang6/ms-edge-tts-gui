# SPDX-License-Identifier: GPL-3.0-or-later
from collections import OrderedDict
from pathlib import Path
import re
from PIL import Image, ImageOps
from project_manager import ImageClip

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def natural_key(path):
    return [
        int(s) if s.isdigit() else s.casefold()
        for s in re.split(r"(\d+)", Path(path).name)
    ]


def add_images(project, paths):
    added = []
    order = max((i.import_order for i in project.images), default=-1) + 1
    for path in paths:
        p = Path(path).resolve()
        if p.suffix.lower() not in EXTENSIONS or not p.is_file():
            raise ValueError(f"不支持的图片：{p.name}")
        with Image.open(p) as image:
            image.verify()
        added.append(ImageClip(str(p), import_order=order))
        order += 1
    project.images.extend(added)
    return added


def sort_images(project, mode, explicit=False):
    if project.manual_order and not explicit:
        return
    key = {
        "name": lambda c: Path(c.path).name.casefold(),
        "natural": lambda c: natural_key(c.path),
        "modified": lambda c: Path(c.path).stat().st_mtime,
        "import": lambda c: c.import_order,
    }[mode]
    project.images.sort(key=key)
    project.manual_order = False


def move_image(project, index, destination):
    project.images.insert(
        max(0, min(destination, len(project.images) - 1)), project.images.pop(index)
    )
    project.manual_order = True


class ThumbnailCache:
    """Bounded LRU; decode one source at a time, retain only tiny thumbnails."""

    def __init__(self, limit=64):
        self.limit = limit
        self.cache = OrderedDict()

    def get(self, path, size=(240, 160)):
        p = Path(path)
        key = (str(p.resolve()), p.stat().st_mtime_ns, size)
        if key not in self.cache:
            with Image.open(p) as image:
                image.draft("RGB", size)
                thumb = ImageOps.exif_transpose(image)
                thumb.thumbnail(size, Image.Resampling.LANCZOS)
                self.cache[key] = thumb.convert("RGB").copy()
        self.cache.move_to_end(key)
        while len(self.cache) > self.limit:
            self.cache.popitem(last=False)
        return self.cache[key]
