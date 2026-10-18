"""Tests for archive and text tools."""
import unittest
import sys
import os
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from agent.tools import archive_tools, text_tools


class TestArchiveCreate(unittest.TestCase):
    def test_create_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create test files
            f1 = os.path.join(tmp, "a.txt")
            f2 = os.path.join(tmp, "b.txt")
            with open(f1, "w") as f:
                f.write("hello")
            with open(f2, "w") as f:
                f.write("world")
            out = os.path.join(tmp, "test.zip")
            r = archive_tools.archive_create([f1, f2], out, "zip")
            self.assertTrue(r["ok"])
            self.assertTrue(os.path.exists(r["output"]))

    def test_create_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "test.zip")
            r = archive_tools.archive_create(
                ["/tmp/nonexistent_xyz.txt"], out, "zip")
            self.assertFalse(r["ok"])

    def test_create_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "test.rar")
            r = archive_tools.archive_create([], out, "rar")
            self.assertFalse(r["ok"])


class TestArchiveExtract(unittest.TestCase):
    def test_extract_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create zip
            zpath = os.path.join(tmp, "test.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("hello.txt", "hello world")
            dest = os.path.join(tmp, "out")
            r = archive_tools.archive_extract(zpath, dest)
            self.assertTrue(r["ok"])
            self.assertTrue(os.path.exists(
                os.path.join(dest, "hello.txt")))

    def test_extract_missing(self):
        r = archive_tools.archive_extract("/tmp/nope_xyz.zip")
        self.assertFalse(r["ok"])


class TestArchiveList(unittest.TestCase):
    def test_list_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, "test.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("a.txt", "aaa")
                zf.writestr("b.txt", "bbb")
            r = archive_tools.archive_list(zpath)
            self.assertTrue(r["ok"])
            self.assertEqual(r["file_count"], 2)

    def test_list_missing(self):
        r = archive_tools.archive_list("/tmp/nope_xyz.zip")
        self.assertFalse(r["ok"])


class TestGzip(unittest.TestCase):
    def test_compress_decompress(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "test.txt")
            with open(src, "w") as f:
                f.write("hello world " * 100)
            # Compress
            r = archive_tools.gzip_compress(src)
            self.assertTrue(r["ok"])
            gz_path = r["output"]
            self.assertTrue(os.path.exists(gz_path))
            # Decompress
            out = os.path.join(tmp, "restored.txt")
            r2 = archive_tools.gzip_decompress(gz_path, out)
            self.assertTrue(r2["ok"])
            with open(out) as f:
                self.assertEqual(f.read(), "hello world " * 100)

    def test_compress_missing(self):
        r = archive_tools.gzip_compress("/tmp/nope_xyz.txt")
        self.assertFalse(r["ok"])


class TestTextSearch(unittest.TestCase):
    def test_search_basic(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False) as f:
            f.write("hello world\nfoo bar\nhello again\n")
            path = f.name
        try:
            r = text_tools.text_search("hello", path)
            self.assertTrue(r["ok"])
            self.assertEqual(r["matches"], 2)
        finally:
            os.unlink(path)

    def test_search_regex(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False) as f:
            f.write("abc123\ndef456\n")
            path = f.name
        try:
            r = text_tools.text_search(r"\d+", path, regex=True)
            self.assertTrue(r["ok"])
            self.assertEqual(r["matches"], 2)
        finally:
            os.unlink(path)

    def test_search_case_insensitive(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False) as f:
            f.write("Hello\nHELLO\nhello\n")
            path = f.name
        try:
            r = text_tools.text_search("hello", path,
                                       case_sensitive=False)
            self.assertTrue(r["ok"])
            self.assertEqual(r["matches"], 3)
        finally:
            os.unlink(path)

    def test_search_missing(self):
        r = text_tools.text_search("x", "/tmp/nope_xyz.txt")
        self.assertFalse(r["ok"])


class TestTextReplace(unittest.TestCase):
    def test_replace(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False) as f:
            f.write("hello world hello")
            path = f.name
        try:
            r = text_tools.text_replace(path, "hello", "hi")
            self.assertTrue(r["ok"])
            self.assertEqual(r["replacements"], 2)
            with open(path) as f:
                self.assertEqual(f.read(), "hi world hi")
        finally:
            os.unlink(path)

    def test_replace_no_match(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False) as f:
            f.write("hello")
            path = f.name
        try:
            r = text_tools.text_replace(path, "xyz", "abc")
            self.assertTrue(r["ok"])
            self.assertEqual(r["replacements"], 0)
        finally:
            os.unlink(path)


class TestTextStats(unittest.TestCase):
    def test_stats(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False) as f:
            f.write("hello world\nfoo bar baz\n")
            path = f.name
        try:
            r = text_tools.text_stats(path)
            self.assertTrue(r["ok"])
            self.assertEqual(r["lines"], 2)
            self.assertEqual(r["words"], 5)
            self.assertIn("top_words", r)
        finally:
            os.unlink(path)

    def test_stats_missing(self):
        r = text_tools.text_stats("/tmp/nope_xyz.txt")
        self.assertFalse(r["ok"])


class TestTextDiff(unittest.TestCase):
    def test_diff(self):
        r = text_tools.text_diff("hello\nworld\n",
                                 "hello\nthere\n")
        self.assertTrue(r["ok"])
        self.assertTrue(r["has_changes"])
        self.assertIn("world", r["diff"])

    def test_diff_same(self):
        r = text_tools.text_diff("same", "same")
        self.assertTrue(r["ok"])
        self.assertFalse(r["has_changes"])

    def test_diff_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = os.path.join(tmp, "a.txt")
            p2 = os.path.join(tmp, "b.txt")
            with open(p1, "w") as f:
                f.write("line1\nline2\n")
            with open(p2, "w") as f:
                f.write("line1\nline3\n")
            r = text_tools.text_diff_files(p1, p2)
            self.assertTrue(r["ok"])
            self.assertTrue(r["has_changes"])


class TestTextSort(unittest.TestCase):
    def test_sort(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False) as f:
            f.write("banana\napple\ncherry\n")
            path = f.name
        try:
            r = text_tools.text_sort(path)
            self.assertTrue(r["ok"])
            with open(path) as f:
                lines = f.read().splitlines()
            self.assertEqual(lines, ["apple", "banana", "cherry"])
        finally:
            os.unlink(path)

    def test_sort_unique(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False) as f:
            f.write("b\na\nb\na\n")
            path = f.name
        try:
            r = text_tools.text_sort(path, unique=True)
            self.assertTrue(r["ok"])
            self.assertEqual(r["lines"], 2)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
