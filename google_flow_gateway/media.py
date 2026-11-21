"""Validate bytes before publication; callers never select filesystem paths."""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
from tempfile import NamedTemporaryFile

from PIL import Image
from .policy import Error

MAX_SIZE = 128 << 20


class Media:
    def __init__(self, root, probe="ffprobe"):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.probe = probe

    def validate(self, data, mime, digest=None):
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_SIZE:
            raise Error("invalid_media")
        actual = hashlib.sha256(data).hexdigest()
        if digest is not None and digest != actual:
            raise Error("digest_mismatch")
        duration = None
        try:
            if mime in ("image/png", "image/jpeg", "image/webp"):
                with Image.open(io.BytesIO(data)) as img:
                    width, height = img.size
                    if Image.MIME[img.format] != mime or width * height > 40_000_000:
                        raise ValueError()
                    img.verify()
                with Image.open(io.BytesIO(data)) as img:
                    img.load()
            elif mime == "video/mp4" and data[4:8] == b"ftyp":
                cmd = [self.probe, "-v", "error", "-protocol_whitelist", "pipe", "-f", "mov",
                       "-show_streams", "-show_format", "-of", "json", "pipe:0"]
                proc = subprocess.run(cmd, input=data, capture_output=True, timeout=20, check=True)
                info = json.loads(proc.stdout)
                video = next(row for row in info["streams"] if row["codec_type"] == "video")
                width, height = video["width"], video["height"]
                duration = float(info["format"]["duration"])
                if not math.isfinite(duration) or min(width, height, duration) <= 0:
                    raise ValueError()
            else:
                raise ValueError()
        except (ValueError, OSError, SyntaxError, KeyError, StopIteration, subprocess.SubprocessError,
                Image.DecompressionBombError):
            raise Error("invalid_media") from None
        return dict(sha256=actual, size=len(data), mime_type=mime, width=width, height=height, duration=duration)

    def put(self, data, mime, digest=None):
        meta = self.validate(data, mime, digest)
        target = self.root / meta["sha256"]
        with NamedTemporaryFile(dir=self.root, delete=False) as tmp:
            tmp.write(data)
        try:
            os.replace(tmp.name, target)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        return meta

    def read(self, digest):
        if not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest):
            raise Error("invalid_media")
        path = self.root / digest
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SIZE:
            raise Error("media_unavailable", 404)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise Error("digest_mismatch")
        return data

    def path_with_ext(self, digest):
        data = self.read(digest)
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = ".png"
        elif data.startswith(b"\xff\xd8\xff"):
            ext = ".jpg"
        elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = ".webp"
        elif len(data) >= 12 and data[4:8] == b"ftyp":
            ext = ".mp4"
        else:
            raise Error("invalid_media")
        target = self.root / f"{digest}{ext}"
        if not target.is_file() or target.stat().st_size != len(data):
            with NamedTemporaryFile(dir=self.root, delete=False) as tmp:
                tmp.write(data)
            try:
                os.replace(tmp.name, target)
            finally:
                Path(tmp.name).unlink(missing_ok=True)
        return target

    def collect(self, path, output, mime):
        base = Path(output).resolve()
        source = Path(path).resolve()
        if not source.is_relative_to(base) or not source.is_file() or source.stat().st_size > MAX_SIZE:
            raise Error("invalid_media")
        return self.put(source.read_bytes(), mime)

    def cleanup(self, before, protected):
        for path in self.root.iterdir():
            match = re.fullmatch(r"([a-f0-9]{64})(?:\.(?:png|jpg|webp|mp4))?", path.name)
            if match and match.group(1) not in protected and path.stat().st_mtime < before:
                path.unlink(missing_ok=True)
