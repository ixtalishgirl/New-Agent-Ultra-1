"""Tests for media tools."""
import unittest
import sys
import os
import tempfile
import struct

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from agent.tools import media_tools


def _make_test_png(path: str, width: int = 10, height: int = 10):
    """Create a minimal valid PNG file."""
    import zlib
    def chunk(ctype, data):
        c = ctype + data
        return (struct.pack(">I", len(data)) + c +
                struct.pack(">I", zlib.crc32(c) & 0xffffffff))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b""
    for _ in range(height):
        raw += b"\x00" + b"\x00\x00\x00" * width
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
           chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


class TestImageInfo(unittest.TestCase):
    def test_nonexistent(self):
        r = media_tools.image_info("/tmp/definitely_not_here_xyz.png")
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    def test_png_dimensions(self):
        with tempfile.NamedTemporaryFile(suffix=".png",
                                         delete=False) as f:
            path = f.name
        try:
            _make_test_png(path, 20, 15)
            r = media_tools.image_info(path)
            self.assertTrue(r["ok"])
            self.assertEqual(r["format"], "PNG")
            self.assertEqual(r["width"], 20)
            self.assertEqual(r["height"], 15)
            self.assertGreater(r["size_bytes"], 0)
        finally:
            os.unlink(path)

    def test_directory(self):
        r = media_tools.image_info("/tmp")
        self.assertFalse(r["ok"])


class TestPngDimensions(unittest.TestCase):
    def test_valid(self):
        with tempfile.NamedTemporaryFile(suffix=".png",
                                         delete=False) as f:
            path = f.name
        try:
            _make_test_png(path, 42, 24)
            dims = media_tools._png_dimensions(path)
            self.assertEqual(dims, (42, 24))
        finally:
            os.unlink(path)

    def test_invalid(self):
        with tempfile.NamedTemporaryFile(suffix=".png",
                                         delete=False) as f:
            f.write(b"not a png")
            path = f.name
        try:
            dims = media_tools._png_dimensions(path)
            self.assertIsNone(dims)
        finally:
            os.unlink(path)


class TestAudioVideoInfo(unittest.TestCase):
    def test_audio_nonexistent(self):
        r = media_tools.audio_info("/tmp/nope_xyz.mp3")
        self.assertFalse(r["ok"])

    def test_video_nonexistent(self):
        r = media_tools.video_info("/tmp/nope_xyz.mp4")
        self.assertFalse(r["ok"])

    def test_audio_existing(self):
        with tempfile.NamedTemporaryFile(suffix=".mp3",
                                         delete=False) as f:
            f.write(b"fake audio data")
            path = f.name
        try:
            r = media_tools.audio_info(path)
            self.assertTrue(r["ok"])
            self.assertEqual(r["format"], "mp3")
        finally:
            os.unlink(path)


class TestImageResizeConvert(unittest.TestCase):
    def test_resize_no_pil(self):
        # Should fail gracefully without Pillow or with fake path
        r = media_tools.image_resize("/tmp/nonexistent.png", 100, 100)
        self.assertFalse(r["ok"])

    def test_convert_no_pil(self):
        r = media_tools.image_convert("/tmp/nonexistent.png", "jpg")
        self.assertFalse(r["ok"])

    def test_thumbnail_no_pil(self):
        r = media_tools.image_thumbnail("/tmp/nonexistent.png")
        self.assertFalse(r["ok"])


class TestGetTools(unittest.TestCase):
    def test_tool_definitions(self):
        tools = media_tools.get_tools()
        self.assertEqual(len(tools), 6)
        names = [t.name for t in tools]
        self.assertIn("image_info", names)
        self.assertIn("image_resize", names)
        self.assertIn("image_convert", names)
        self.assertIn("image_thumbnail", names)
        self.assertIn("audio_info", names)
        self.assertIn("video_info", names)


if __name__ == "__main__":
    unittest.main()
