"""Local graphic-captcha recognition. The guess never leaves this process."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path


_CAPTCHA = re.compile(r"[A-Za-z0-9]{4,6}")
# Official captchas are a few thousand pixels; preprocessing enlarges each image up to 16 times.
_MAX_PIXELS = 1_000_000
_ocr = None


def recognize_captcha(image: bytes, *, classify=None) -> str | None:
    """Return one alphanumeric guess, or None when no usable engine result exists."""
    if not isinstance(image, bytes) or not image:
        return None
    size = _dimensions(image)
    if size is not None and size[0] * size[1] > _MAX_PIXELS:
        return None
    if classify is not None:
        try:
            return _usable(classify(image))
        except Exception:
            return None
    for blob in _variants(image):
        for reader in (_ddddocr_text, _tesseract_text):
            try:
                guess = _usable(reader(blob))
            except Exception:
                guess = None
            if guess:
                return guess
    return None


def _dimensions(image: bytes) -> tuple[int, int] | None:
    """Width and height from a PNG, GIF or JPEG header, read without decoding any pixels."""
    if image.startswith(b"\x89PNG\r\n\x1a\n") and image[12:16] == b"IHDR" and len(image) >= 24:
        return int.from_bytes(image[16:20], "big"), int.from_bytes(image[20:24], "big")
    if image.startswith((b"GIF87a", b"GIF89a")) and len(image) >= 10:
        return int.from_bytes(image[6:8], "little"), int.from_bytes(image[8:10], "little")
    offset = 2 if image.startswith(b"\xff\xd8") else len(image)
    while offset + 9 <= len(image) and image[offset] == 0xFF:
        marker = image[offset + 1]
        if marker == 0xFF:  # fill byte before a marker
            offset += 1
            continue
        # Start-of-frame segments carry the size; C4, C8 and CC are other segment types.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return (int.from_bytes(image[offset + 7:offset + 9], "big"),
                    int.from_bytes(image[offset + 5:offset + 7], "big"))
        offset += 2 + int.from_bytes(image[offset + 2:offset + 4], "big")
    return None


def _usable(value) -> str | None:
    match = _CAPTCHA.fullmatch(re.sub(r"[^0-9A-Za-z]", "", str(value or "")))
    return match.group(0) if match else None


def _use_project_venv():
    """Make a project-local install visible to the system interpreter."""
    if importlib.util.find_spec("ddddocr") is not None:
        return
    # Compiled dependencies are tied to the interpreter version, so ignore other venvs.
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = Path(__file__).resolve().parent.parent / ".venv" / "lib" / version / "site-packages"
    if site.is_dir() and str(site) not in sys.path:
        sys.path.insert(0, str(site))
        importlib.invalidate_caches()


def _ddddocr_text(image: bytes) -> str:
    global _ocr
    if _ocr is None:
        # Loading the model is slow; remember a failure instead of retrying per variant.
        _ocr = False
        _use_project_venv()
        if importlib.util.find_spec("ddddocr") is None:
            return ""
        import ddddocr
        try:
            engine = ddddocr.DdddOcr(show_ad=False)
        except TypeError:
            engine = ddddocr.DdddOcr()
        if hasattr(engine, "set_ranges"):
            engine.set_ranges("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
        _ocr = engine
    if _ocr is False:
        return ""
    return str(_ocr.classification(image) or "")


def _tesseract_text(image: bytes) -> str:
    executable = shutil.which("tesseract")
    if executable is None:
        return ""
    path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="captcha-", suffix=".png", delete=False) as handle:
            handle.write(image)
            path = Path(handle.name)
        path.chmod(0o600)
        result = subprocess.run(
            [executable, str(path), "stdout", "--psm", "7", "--oem", "3",
             "-c", "tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"],
            capture_output=True, timeout=10, check=False,
        )
        return result.stdout.decode("utf-8", errors="replace")
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def _variants(image: bytes):
    yield image
    try:
        from PIL import Image, ImageFilter, ImageOps
    except Exception:
        return
    try:
        picture = Image.open(BytesIO(image)).convert("RGB")
        width, height = picture.size
        scale = 4 if max(width, height) < 180 else 2
        resample = getattr(Image.Resampling, "LANCZOS", Image.LANCZOS)
        enlarged = picture.resize((width * scale, height * scale), resample)
        gray = ImageOps.grayscale(enlarged)
        contrast = ImageOps.autocontrast(gray)
        denoised = contrast.filter(ImageFilter.MedianFilter(size=3))
        prepared = [gray, contrast, denoised]
        prepared.extend(denoised.point(lambda pixel, limit=limit: 255 if pixel > limit else 0)
                        for limit in (115, 155, 175))
        for variant in prepared:
            buffer = BytesIO()
            variant.save(buffer, format="PNG")
            yield buffer.getvalue()
    except Exception:
        return
