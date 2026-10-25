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
_ocr = None


def recognize_captcha(image: bytes, *, classify=None) -> str | None:
    """Return one alphanumeric guess, or None when no usable engine result exists."""
    if not isinstance(image, bytes) or not image:
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
