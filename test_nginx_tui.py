import os
import io
import unittest
from contextlib import redirect_stdout

from nginx_tui import normalize_url, parse_args, Entry, format_mtime, format_size, parse_index


class TestNormalizeUrl(unittest.TestCase):
    def test_adds_http_scheme_when_missing(self):
        self.assertEqual(normalize_url("example.com/files/"), "http://example.com/files/")

    def test_keeps_http_scheme(self):
        self.assertEqual(normalize_url("http://example.com/files/"), "http://example.com/files/")

    def test_keeps_https_scheme(self):
        self.assertEqual(normalize_url("https://example.com/files/"), "https://example.com/files/")

    def test_adds_http_scheme_to_hostname_with_port(self):
        self.assertEqual(normalize_url("localhost:8000/files/"), "http://localhost:8000/files/")

    def test_adds_http_scheme_to_ip_with_port(self):
        self.assertEqual(normalize_url("127.0.0.1:8080"), "http://127.0.0.1:8080")


class TestParseArgs(unittest.TestCase):
    def test_no_args_prints_help_and_exits_zero(self):
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                parse_args([])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("usage", out.getvalue().lower())

    def test_output_dir_defaults_to_cwd(self):
        args = parse_args(["http://example.com/files/"])
        self.assertEqual(args.output_dir, os.getcwd())

    def test_url_scheme_gets_normalized(self):
        args = parse_args(["example.com/files/"])
        self.assertEqual(args.url, "http://example.com/files/")

    def test_output_dir_flag_is_honored(self):
        args = parse_args(["http://example.com/files/", "--output-dir", "/tmp/dl"])
        self.assertEqual(args.output_dir, "/tmp/dl")


SAMPLE_INDEX_HTML = (
    '<html>\n<head><title>Index of /files/</title></head>\n<body>\n'
    '<h1>Index of /files/</h1><hr><pre><a href="../">../</a>\n'
    '<a href="report%202024.pdf">report 2024.pdf</a>              06-Jul-2023 10:00              123456\n'
    '<a href="notes.txt">notes.txt</a>                            06-Jul-2023 10:05                  42\n'
    '<a href="subdir/">subdir/</a>                               06-Jul-2023 10:10                   -\n'
    '<a href="%E4%B8%AD%E6%96%87/">中文/</a>                          06-Jul-2023 10:15                   -\n'
    '</pre><hr></body>\n</html>\n'
)


class TestFormatSize(unittest.TestCase):
    def test_none_is_dash(self):
        self.assertEqual(format_size(None), "-")

    def test_bytes(self):
        self.assertEqual(format_size(512), "512B")

    def test_kilobytes(self):
        self.assertEqual(format_size(2048), "2.0K")

    def test_megabytes(self):
        self.assertEqual(format_size(5 * 1024 * 1024), "5.0M")

    def test_gigabytes(self):
        self.assertEqual(format_size(3 * 1024 ** 3), "3.0G")


class TestFormatMtime(unittest.TestCase):
    def test_none_stays_none(self):
        self.assertIsNone(format_mtime(None))

    def test_parses_nginx_date_format(self):
        self.assertEqual(format_mtime("06-Jul-2023 10:00"), "2023-07-06 10:00")

    def test_unparseable_falls_back_to_raw(self):
        self.assertEqual(format_mtime("not-a-date"), "not-a-date")


class TestParseIndex(unittest.TestCase):
    def test_skips_parent_dir_entry(self):
        entries = parse_index(SAMPLE_INDEX_HTML, "http://example.com/files/")
        names = [e.name for e in entries]
        self.assertNotIn("..", names)
        self.assertNotIn("../", names)

    def test_extracts_file_entry_with_size_and_mtime(self):
        entries = parse_index(SAMPLE_INDEX_HTML, "http://example.com/files/")
        notes = next(e for e in entries if e.name == "notes.txt")
        self.assertFalse(notes.is_dir)
        self.assertEqual(notes.size_bytes, 42)
        self.assertEqual(notes.mtime, "2023-07-06 10:05")
        self.assertEqual(notes.url, "http://example.com/files/notes.txt")

    def test_decodes_percent_encoded_unicode_name(self):
        entries = parse_index(SAMPLE_INDEX_HTML, "http://example.com/files/")
        chinese_dir = next(e for e in entries if e.href == "%E4%B8%AD%E6%96%87/")
        self.assertEqual(chinese_dir.name, "中文/")
        self.assertTrue(chinese_dir.is_dir)

    def test_directory_size_is_none_even_if_meta_has_dash(self):
        entries = parse_index(SAMPLE_INDEX_HTML, "http://example.com/files/")
        subdir = next(e for e in entries if e.name == "subdir/")
        self.assertTrue(subdir.is_dir)
        self.assertIsNone(subdir.size_bytes)

    def test_unparseable_meta_degrades_gracefully(self):
        html_text = (
            "<html><body><pre>"
            '<a href="weird.bin">weird.bin</a>          06-Jul-2023 10:00          1.2K'
            "</pre></body></html>"
        )
        entries = parse_index(html_text, "http://example.com/files/")
        weird = entries[0]
        self.assertEqual(weird.name, "weird.bin")
        self.assertIsNone(weird.size_bytes)
        self.assertIsNone(weird.mtime)


if __name__ == "__main__":
    unittest.main()
