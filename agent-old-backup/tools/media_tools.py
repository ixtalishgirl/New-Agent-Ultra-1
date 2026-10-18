"""Media tools — image, audio, video processing.

Tools:
- image_info: Get image metadata
- image_resize: Resize images
- image_convert: Convert image formats
- audio_info: Get audio metadata
- video_info: Get video metadata

Uses PIL (Pillow) when available, falls back to file inspection.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .registry import ToolDef


def _get_pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def _png_dimensions(path: str) -> Optional[Tuple[int, int]]:
    """Read PNG dimensions without PIL."""
    try:
        with open(path, "rb") as f:
            data = f.read(33)
            if data[:8] != b"\x89PNG\r\n\x1a\n":
                return None
            w, h = struct.unpack(">II", data[16:24])
            return w, h
    except Exception:
        return None


def _jpeg_dimensions(path: str) -> Optional[Tuple[int, int]]:
    """Read JPEG dimensions without PIL."""
    try:
        with open(path, "rb") as f:
            data = f.read(65536)
            if data[:2] != b"\xff\xd8":
                return None
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                if marker == 0xD9:
                    break
                size = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + size
    except Exception:
        pass
    return None


def _gif_dimensions(path: str) -> Optional[Tuple[int, int]]:
    try:
        with open(path, "rb") as f:
            data = f.read(10)
            if data[:6] not in (b"GIF87a", b"GIF89a"):
                return None
            w, h = struct.unpack("<HH", data[6:10])
            return w, h
    except Exception:
        return None


def image_info(path: str) -> Dict[str, Any]:
    """Get image file metadata."""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"Not found: {path}"}
    if not p.is_file():
        return {"ok": False, "error": f"Not a file: {path}"}

    result: Dict[str, Any] = {
        "ok": True,
        "path": str(p),
        "size_bytes": p.stat().st_size,
        "format": None,
        "width": None,
        "height": None,
    }

    Image = _get_pil()
    if Image:
        try:
            with Image.open(path) as img:
                result["format"] = img.format
                result["width"], result["height"] = img.size
                result["mode"] = img.mode
                return result
        except Exception:
            pass

    # Fallback: manual header parsing
    suffix = p.suffix.lower()
    dims = None
    if suffix == ".png":
        dims = _png_dimensions(path)
        result["format"] = "PNG"
    elif suffix in (".jpg", ".jpeg"):
        dims = _jpeg_dimensions(path)
        result["format"] = "JPEG"
    elif suffix == ".gif":
        dims = _gif_dimensions(path)
        result["format"] = "GIF"
    elif suffix == ".webp":
        result["format"] = "WEBP"

    if dims:
        result["width"], result["height"] = dims

    return result


def image_resize(path: str, width: int, height: int,
                 output: Optional[str] = None) -> Dict[str, Any]:
    """Resize an image. Requires Pillow."""
    Image = _get_pil()
    if not Image:
        return {"ok": False,
                "error": "Pillow not installed (pip install Pillow)"}
    try:
        with Image.open(path) as img:
            resized = img.resize((width, height), Image.LANCZOS)
            out = output or str(
                Path(path).parent /
                f"{Path(path).stem}_{width}x{height}{Path(path).suffix}")
            resized.save(out)
            return {"ok": True, "output": out,
                    "width": width, "height": height}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def image_convert(path: str, format: str,
                  output: Optional[str] = None) -> Dict[str, Any]:
    """Convert image format. Requires Pillow."""
    Image = _get_pil()
    if not Image:
        return {"ok": False,
                "error": "Pillow not installed (pip install Pillow)"}
    try:
        fmt = format.upper()
        if fmt == "JPG":
            fmt = "JPEG"
        with Image.open(path) as img:
            out = output or str(
                Path(path).parent /
                f"{Path(path).stem}.{format.lower()}")
            # Handle transparency for JPEG
            if fmt == "JPEG" and img.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            img.save(out, fmt)
            return {"ok": True, "output": out, "format": fmt}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def image_thumbnail(path: str, size: int = 128,
                    output: Optional[str] = None) -> Dict[str, Any]:
    """Create a square thumbnail. Requires Pillow."""
    Image = _get_pil()
    if not Image:
        return {"ok": False,
                "error": "Pillow not installed (pip install Pillow)"}
    try:
        with Image.open(path) as img:
            img.thumbnail((size, size), Image.LANCZOS)
            out = output or str(
                Path(path).parent /
                f"{Path(path).stem}_thumb{Path(path).suffix}")
            img.save(out)
            return {"ok": True, "output": out,
                    "width": img.width, "height": img.height}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def audio_info(path: str) -> Dict[str, Any]:
    """Get audio file metadata (basic header parsing)."""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"Not found: {path}"}

    result: Dict[str, Any] = {
        "ok": True, "path": str(p),
        "size_bytes": p.stat().st_size,
        "format": p.suffix.lower().lstrip(".") or "unknown",
    }

    # Try WAV header
    if p.suffix.lower() == ".wav":
        try:
            with open(path, "rb") as f:
                riff = f.read(12)
                if riff[:4] == b"RIFF" and riff[8:12] == b"WAVE":
                    f.seek(16)
                    fmt_size = struct.unpack("<I", f.read(4))[0]
                    fmt = f.read(fmt_size)
                    channels, rate = struct.unpack("<HHI", fmt[:8])[:2] + \
                        struct.unpack("<I", fmt[4:8])
                    # Simpler: parse properly
                    f.seek(20)
                    audio_fmt, channels = struct.unpack(
                        "<HH", f.read(4))
                    sample_rate = struct.unpack("<I", f.read(4))[0]
                    result.update({
                        "channels": channels,
                        "sample_rate": sample_rate,
                        "audio_format": audio_fmt,
                    })
        except Exception:
            pass

    # Try mutagen if available
    try:
        import mutagen
        audio = mutagen.File(path)
        if audio:
            result["duration_seconds"] = getattr(
                audio.info, "length", None)
            result["bitrate"] = getattr(
                audio.info, "bitrate", None)
    except ImportError:
        pass
    except Exception:
        pass

    return result


def video_info(path: str) -> Dict[str, Any]:
    """Get video file metadata."""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"Not found: {path}"}

    result: Dict[str, Any] = {
        "ok": True, "path": str(p),
        "size_bytes": p.stat().st_size,
        "format": p.suffix.lower().lstrip(".") or "unknown",
    }

    # Try ffprobe if available
    import subprocess
    import json as _json
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(p)],
            capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            data = _json.loads(proc.stdout)
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    result["width"] = stream.get("width")
                    result["height"] = stream.get("height")
                    result["codec"] = stream.get("codec_name")
                    result["fps"] = stream.get("avg_frame_rate")
                elif stream.get("codec_type") == "audio":
                    result["audio_codec"] = stream.get("codec_name")
            fmt = data.get("format", {})
            if fmt.get("duration"):
                result["duration_seconds"] = float(fmt["duration"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def get_tools() -> list:
    """Return ToolDef list for registry (dispatcher routes by name)."""
    return [
        ToolDef(
            name="image_info",
            version="1.0.0",
            description="Get image metadata (dimensions, format, size)",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        ToolDef(
            name="image_resize",
            version="1.0.0",
            description="Resize an image to specified dimensions",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                    "output": {"type": "string"},
                },
                "required": ["path", "width", "height"],
            },
        ),
        ToolDef(
            name="image_convert",
            version="1.0.0",
            description="Convert image to different format",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "format": {"type": "string"},
                    "output": {"type": "string"},
                },
                "required": ["path", "format"],
            },
        ),
        ToolDef(
            name="image_thumbnail",
            version="1.0.0",
            description="Create a thumbnail of an image",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "size": {"type": "integer"},
                    "output": {"type": "string"},
                },
                "required": ["path"],
            },
        ),
        ToolDef(
            name="audio_info",
            version="1.0.0",
            description="Get audio file metadata",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        ToolDef(
            name="video_info",
            version="1.0.0",
            description="Get video file metadata",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
    ]
