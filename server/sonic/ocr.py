"""Text from a screenshot of Binance's position history, with Tesseract OCR on this server.

Only PNG, JPEG and WebP images up to 8 MB are read. The image is written to a temporary file,
read by the local `tesseract` program (English + Arabic when installed) and deleted. Nothing
leaves the server. The text is then parsed by leader.parse_screenshot, which keeps any field
it cannot read clearly as missing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

MAX_BYTES = 8 * 1024 * 1024
SIGNATURES = {b"\x89PNG": ".png", b"\xff\xd8\xff": ".jpg", b"RIFF": ".webp"}


class OcrError(Exception):
    pass


def kind(data: bytes) -> str | None:
    for magic, suffix in SIGNATURES.items():
        if data.startswith(magic):
            return suffix
    return None


def languages(exe: str) -> str:
    try:
        listed = subprocess.run([exe, "--list-langs"], capture_output=True, text=True,
                                timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return "eng"
    have = {line.strip() for line in listed.splitlines()}
    return "+".join(lang for lang in ("eng", "ara") if lang in have) or "eng"


def read_image(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise OcrError("image_too_large")
    suffix = kind(data)
    if suffix is None:
        raise OcrError("image_type")
    exe = shutil.which("tesseract")
    if exe is None:
        raise OcrError("ocr_not_installed")
    handle, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        done = subprocess.run([exe, path, "stdout", "-l", languages(exe), "--psm", "6"],
                              capture_output=True, timeout=120)
        if done.returncode != 0:
            raise OcrError("ocr_failed")
        return done.stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as error:
        raise OcrError("ocr_timeout") from error
    finally:
        os.remove(path)
