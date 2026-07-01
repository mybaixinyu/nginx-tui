import os
import io
import unittest
from contextlib import redirect_stdout

from nginx_tui import normalize_url, parse_args


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


if __name__ == "__main__":
    unittest.main()
