import curses
import functools
import http.server
import io
import os
import shutil
import ssl
import stat
import subprocess
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

from nginx_tui import (
    Action,
    BrowserApp,
    Entry,
    NavigationStack,
    _dir_label,
    _display_width,
    _flush_stale_input,
    _format_duration,
    _format_entry_size,
    _MIN_TERMINAL_WIDTH,
    _sanitize_display_text,
    _ssl_context,
    _truncate_middle,
    _url_label,
    download_file,
    fetch_index,
    format_mtime,
    format_row,
    format_size,
    main,
    normalize_url,
    parse_args,
    parse_index,
    resolve_action,
)


class TestNormalizeUrl(unittest.TestCase):
    def test_adds_http_scheme_when_missing(self):
        self.assertEqual(normalize_url("example.com/files/"), "http://example.com/files/")

    def test_keeps_http_scheme(self):
        self.assertEqual(normalize_url("http://example.com/files/"), "http://example.com/files/")

    def test_keeps_https_scheme(self):
        self.assertEqual(normalize_url("https://example.com/files/"), "https://example.com/files/")

    def test_strips_outer_whitespace(self):
        self.assertEqual(normalize_url("  http://example.com/files/  "), "http://example.com/files/")

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

    def test_insecure_defaults_to_false(self):
        args = parse_args(["http://example.com/files/"])
        self.assertFalse(args.insecure)

    def test_insecure_flag_is_honored(self):
        args = parse_args(["https://example.com/files/", "--insecure"])
        self.assertTrue(args.insecure)
        args_short = parse_args(["https://example.com/files/", "-k"])
        self.assertTrue(args_short.insecure)

    def test_output_dir_expands_tilde(self):
        with unittest.mock.patch.dict(os.environ, {"HOME": "/tmp/fake-home"}):
            args = parse_args(["http://example.com/files/", "--output-dir", "~/dl"])
        self.assertEqual(args.output_dir, "/tmp/fake-home/dl")


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

    def test_terabytes(self):
        self.assertEqual(format_size(5 * 1024 ** 4), "5.0T")


class TestFormatEntrySize(unittest.TestCase):
    def test_uses_exact_bytes_when_known(self):
        entry = Entry("f", "f", "http://x/f", False, 2048, None)
        self.assertEqual(_format_entry_size(entry), "2.0K")

    def test_falls_back_to_raw_text_when_only_a_rounded_size_is_known(self):
        entry = Entry("f", "f", "http://x/f", False, None, None, size_raw="24M")
        self.assertEqual(_format_entry_size(entry), "24M")

    def test_dash_when_nothing_is_known(self):
        entry = Entry("f", "f", "http://x/f", False, None, None)
        self.assertEqual(_format_entry_size(entry), "-")


class TestFlushStaleInput(unittest.TestCase):
    def test_does_not_raise_without_a_real_initscr(self):
        # curses.flushinp() requires initscr() to have run first -- every
        # test in this file builds a BrowserApp around a fake stdscr without
        # one, so this must degrade silently rather than raise curses.error.
        _flush_stale_input()  # must not raise


class TestDisplayWidth(unittest.TestCase):
    def test_ascii_counts_one_column_per_character(self):
        self.assertEqual(_display_width("abc"), 3)

    def test_east_asian_wide_characters_count_two_columns(self):
        self.assertEqual(_display_width("中文"), 4)

    def test_combining_mark_adds_no_width(self):
        # NFD-decomposed "e" + COMBINING ACUTE ACCENT renders as one visual
        # column on a real terminal -- counting it as two overcounts and
        # under-pads the column (safe direction), but is still wrong.
        decomposed = "é"
        self.assertEqual(_display_width(decomposed), 1)


class TestFormatMtime(unittest.TestCase):
    def test_none_stays_none(self):
        self.assertIsNone(format_mtime(None))

    def test_parses_nginx_date_format(self):
        self.assertEqual(format_mtime("06-Jul-2023 10:00"), "2023-07-06 10:00")

    def test_parses_nginx_date_format_independent_of_locale(self):
        self.assertEqual(format_mtime("01-Jul-2026 17:50"), "2026-07-01 17:50")

    def test_unparseable_falls_back_to_raw(self):
        self.assertEqual(format_mtime("not-a-date"), "not-a-date")

    def test_calendrically_invalid_date_falls_back_to_raw(self):
        # Matches the day/month/year/hour/minute shape but Feb never has 31 days.
        self.assertEqual(format_mtime("31-Feb-2024 10:00"), "31-Feb-2024 10:00")

    def test_unrecognized_month_fallback_sanitizes_embedded_control_characters(self):
        # _LINE_META_RE's \s+ between date and time matches a literal tab --
        # every fallback branch returns this raw string verbatim, so it must
        # be sanitized before curses ever sees it, not just the happy path.
        self.assertEqual(format_mtime("06-Foo-2023\t10:00"), "06-Foo-2023�10:00")


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

    def test_rounded_unit_size_still_yields_date_and_is_shown_as_is(self):
        # autoindex_exact_size off renders sizes like "1.2K" instead of an
        # exact byte count -- the date alongside it is still real and valid,
        # and the size itself is shown as the server reported it (size_raw),
        # not converted to a fabricated byte count.
        # Each entry is on its own line, matching real nginx output (and
        # required by _LINE_META_RE's per-line \s*$ anchor).
        html_text = (
            "<html><body><pre>\n"
            '<a href="big.iso">big.iso</a>          06-Jul-2023 10:00          1.2K\n'
            "</pre></body></html>"
        )
        entries = parse_index(html_text, "http://example.com/files/")
        entry = entries[0]
        self.assertEqual(entry.name, "big.iso")
        self.assertIsNone(entry.size_bytes)
        self.assertEqual(entry.size_raw, "1.2K")
        self.assertEqual(entry.mtime, "2023-07-06 10:00")

    def test_completely_unparseable_meta_degrades_gracefully(self):
        html_text = (
            "<html><body><pre>"
            '<a href="weird.bin">weird.bin</a>          not-a-date-or-size-at-all'
            "</pre></body></html>"
        )
        entries = parse_index(html_text, "http://example.com/files/")
        weird = entries[0]
        self.assertEqual(weird.name, "weird.bin")
        self.assertIsNone(weird.size_bytes)
        self.assertIsNone(weird.mtime)

    def test_calendrically_invalid_date_does_not_abort_the_whole_listing(self):
        html_text = (
            "<html><body><pre>\n"
            '<a href="f.bin">f.bin</a>          31-Feb-2024 10:00          123\n'
            "</pre></body></html>"
        )
        entries = parse_index(html_text, "http://example.com/files/")
        entry = entries[0]
        self.assertEqual(entry.name, "f.bin")
        self.assertEqual(entry.mtime, "31-Feb-2024 10:00")

    def test_genuinely_empty_directory_returns_no_entries(self):
        # A real empty nginx directory still lists the "../" anchor.
        html_text = '<html><body><pre><a href="../">../</a>\n</pre></body></html>'
        entries = parse_index(html_text, "http://example.com/empty/")
        self.assertEqual(entries, [])

    def test_page_with_no_anchors_at_all_also_returns_no_entries(self):
        # Not every server includes a "../" anchor for an empty directory
        # (e.g. Python's http.server emits zero <a> tags for one) -- an
        # anchor-free page can't be reliably distinguished from a genuinely
        # empty listing, so it degrades to an empty list rather than erroring.
        html_text = '{"error": "not found"}'
        entries = parse_index(html_text, "http://example.com/api/")
        self.assertEqual(entries, [])

    def test_embedded_newline_in_href_is_sanitized(self):
        # A filename with a literal newline byte (real nginx serves this as
        # href="evil%0Aname.txt") would otherwise reach curses addstr raw and
        # split the row across two physical lines.
        html_text = (
            '<a href="../">../</a>\n'
            '<a href="evil%0Aname.txt">evil\nname.txt</a>  06-Jul-2023 10:00  5\n'
        )
        entries = parse_index(html_text, "http://example.com/files/")
        self.assertEqual(len(entries), 1)
        self.assertNotIn("\n", entries[0].name)
        self.assertIn("evil", entries[0].name)
        self.assertIn("name.txt", entries[0].name)

    def test_nested_anchor_flushes_the_outer_one_instead_of_dropping_it(self):
        # Malformed/nested <a> markup used to silently discard whichever
        # anchor opened first.
        html_text = '<a href="outer.txt">out <a href="inner.txt">in</a></a>'
        entries = parse_index(html_text, "http://example.com/files/")
        names = {e.name for e in entries}
        self.assertIn("outer.txt", names)
        self.assertIn("inner.txt", names)

    def test_double_escaped_ampersand_in_href_still_matches_its_meta(self):
        # HTMLParser entity-decodes attribute values once ("&amp;amp;" ->
        # "&amp;"), but the meta regex reads the raw text -- without
        # unescaping the regex's own href capture the same way, the two
        # would use different dict keys and size/mtime would go missing.
        html_text = '<a href="a&amp;amp;b.txt">a&amp;amp;b.txt</a> 06-Jul-2023 10:00 123\n'
        entries = parse_index(html_text, "http://example.com/files/")
        entry = entries[0]
        self.assertEqual(entry.name, "a&amp;b.txt")
        self.assertEqual(entry.size_bytes, 123)
        self.assertEqual(entry.mtime, "2023-07-06 10:00")


class TestSanitizeDisplayText(unittest.TestCase):
    def test_replaces_control_characters(self):
        self.assertEqual(_sanitize_display_text("evil\nname.txt"), "evil�name.txt")

    def test_replaces_tab_and_carriage_return(self):
        self.assertEqual(_sanitize_display_text("a\tb\rc"), "a�b�c")

    def test_leaves_normal_text_unchanged(self):
        self.assertEqual(_sanitize_display_text("normal 文件.txt"), "normal 文件.txt")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


class _ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.serve_dir = tempfile.mkdtemp()
        handler = functools.partial(_QuietHandler, directory=cls.serve_dir)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.serve_dir, ignore_errors=True)


class TestFetchIndex(_ServerTestCase):
    def test_fetches_and_decodes_text(self):
        with open(os.path.join(self.serve_dir, "page.html"), "w", encoding="utf-8") as f:
            f.write("<html>你好</html>")
        html_text, final_url = fetch_index(self.base_url + "page.html")
        self.assertEqual(html_text, "<html>你好</html>")
        self.assertEqual(final_url, self.base_url + "page.html")

    def test_raises_on_404(self):
        with self.assertRaises(urllib.error.HTTPError):
            fetch_index(self.base_url + "missing.html")

    def test_returns_final_url_after_redirect(self):
        # nginx 301-redirects a directory request missing its trailing slash;
        # relative hrefs in the listing must resolve against the final URL.
        os.mkdir(os.path.join(self.serve_dir, "subdir"))
        with open(os.path.join(self.serve_dir, "subdir", "inner.txt"), "w") as f:
            f.write("x")
        html_text, final_url = fetch_index(self.base_url + "subdir")
        self.assertEqual(final_url, self.base_url + "subdir/")

    def test_oversized_body_raises(self):
        with open(os.path.join(self.serve_dir, "huge.html"), "w") as f:
            f.write("x" * 2000)
        with unittest.mock.patch("nginx_tui._MAX_INDEX_BODY_SIZE", 1000):
            with self.assertRaises(ValueError):
                fetch_index(self.base_url + "huge.html")

    def test_body_under_the_cap_is_returned_normally(self):
        with open(os.path.join(self.serve_dir, "small.html"), "w") as f:
            f.write("<html>ok</html>")
        with unittest.mock.patch("nginx_tui._MAX_INDEX_BODY_SIZE", 1000):
            html_text, _ = fetch_index(self.base_url + "small.html")
        self.assertEqual(html_text, "<html>ok</html>")


class TestDownloadFile(_ServerTestCase):
    def setUp(self):
        self.dest_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dest_dir, ignore_errors=True)

    def test_downloads_file_content_exactly(self):
        content = os.urandom(5000)
        with open(os.path.join(self.serve_dir, "blob.bin"), "wb") as f:
            f.write(content)
        dest_path = os.path.join(self.dest_dir, "blob.bin")
        download_file(self.base_url + "blob.bin", dest_path)
        with open(dest_path, "rb") as f:
            self.assertEqual(f.read(), content)
        self.assertFalse(os.path.exists(dest_path + ".part"))

    def test_reports_progress_in_chunks(self):
        content = os.urandom(5000)
        with open(os.path.join(self.serve_dir, "blob2.bin"), "wb") as f:
            f.write(content)
        dest_path = os.path.join(self.dest_dir, "blob2.bin")
        progress_calls = []
        with unittest.mock.patch("nginx_tui.CHUNK_SIZE", 1024):
            download_file(
                self.base_url + "blob2.bin", dest_path,
                progress_cb=lambda downloaded, total: progress_calls.append((downloaded, total)),
            )
        self.assertGreaterEqual(len(progress_calls), 5)
        self.assertEqual(progress_calls[-1], (5000, 5000))

    def test_cleans_up_partial_file_on_progress_callback_error(self):
        content = os.urandom(5000)
        with open(os.path.join(self.serve_dir, "blob3.bin"), "wb") as f:
            f.write(content)
        dest_path = os.path.join(self.dest_dir, "blob3.bin")

        def failing_progress(downloaded, total):
            raise KeyboardInterrupt()

        with unittest.mock.patch("nginx_tui.CHUNK_SIZE", 1024):
            with self.assertRaises(KeyboardInterrupt):
                download_file(self.base_url + "blob3.bin", dest_path, progress_cb=failing_progress)
        self.assertFalse(os.path.exists(dest_path))
        self.assertFalse(os.path.exists(dest_path + ".part"))

    def test_raises_and_leaves_no_partial_file_on_404(self):
        dest_path = os.path.join(self.dest_dir, "missing.bin")
        with self.assertRaises(urllib.error.HTTPError):
            download_file(self.base_url + "missing.bin", dest_path)
        self.assertFalse(os.path.exists(dest_path + ".part"))

    def test_open_failure_does_not_delete_a_preexisting_part_file(self):
        # Regression test: opened_part must only become True once open()
        # has actually succeeded, or a failed open() on a pre-existing
        # ".part" file (e.g. read-only) wrongly deletes a file this call
        # never touched.
        with open(os.path.join(self.serve_dir, "blocked.bin"), "wb") as f:
            f.write(b"x" * 100)
        dest_path = os.path.join(self.dest_dir, "blocked.bin")
        part_path = dest_path + ".part"
        with open(part_path, "wb") as f:
            f.write(b"PRE_EXISTING_UNRELATED_DATA")
        os.chmod(part_path, stat.S_IRUSR)
        self.addCleanup(os.chmod, part_path, stat.S_IRUSR | stat.S_IWUSR)
        with self.assertRaises(PermissionError):
            download_file(self.base_url + "blocked.bin", dest_path)
        self.assertTrue(os.path.exists(part_path))
        with open(part_path, "rb") as f:
            self.assertEqual(f.read(), b"PRE_EXISTING_UNRELATED_DATA")


class TestDownloadFileIncompleteTransfer(unittest.TestCase):
    def test_connection_dropped_before_content_length_raises_and_cleans_up(self):
        # HTTPResponse.read() returns b"" (not an exception) when the
        # connection closes early -- without a completeness check, this used
        # to commit the truncated bytes as if the download had succeeded.
        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "10000")
                self.end_headers()
                self.wfile.write(b"x" * 3000)
                self.close_connection = True

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        dest_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest_dir, ignore_errors=True)
        dest_path = os.path.join(dest_dir, "out.bin")

        with self.assertRaises(OSError):
            download_file(f"http://127.0.0.1:{server.server_port}/x", dest_path)
        self.assertFalse(os.path.exists(dest_path))
        self.assertFalse(os.path.exists(dest_path + ".part"))

    def test_negative_content_length_is_treated_as_unknown_not_incomplete(self):
        # int("-1") succeeds, so a malformed negative Content-Length used to
        # sail past the "is not None" guard and make a fully-received
        # download fail the completeness check (downloaded != -1 is always
        # true), deleting the file it had just finished writing correctly.
        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "-1")
                self.end_headers()
                self.wfile.write(b"complete payload")

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        dest_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest_dir, ignore_errors=True)
        dest_path = os.path.join(dest_dir, "out.bin")

        download_file(f"http://127.0.0.1:{server.server_port}/x", dest_path)
        with open(dest_path, "rb") as f:
            self.assertEqual(f.read(), b"complete payload")


class TestSslContext(unittest.TestCase):
    def test_returns_none_when_not_insecure(self):
        self.assertIsNone(_ssl_context(False))

    def test_returns_permissive_context_when_insecure(self):
        context = _ssl_context(True)
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)


class _HttpsServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("openssl") is None:
            raise unittest.SkipTest("openssl CLI not available to generate a self-signed test certificate")
        cls.serve_dir = tempfile.mkdtemp()
        cls.cert_dir = tempfile.mkdtemp()
        cert_path = os.path.join(cls.cert_dir, "cert.pem")
        key_path = os.path.join(cls.cert_dir, "key.pem")
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", key_path, "-out", cert_path,
                "-days", "1", "-subj", "/CN=127.0.0.1",
            ],
            check=True, capture_output=True,
        )
        handler = functools.partial(_QuietHandler, directory=cls.serve_dir)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        cls.server.socket = server_context.wrap_socket(cls.server.socket, server_side=True)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"https://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.serve_dir, ignore_errors=True)
        shutil.rmtree(cls.cert_dir, ignore_errors=True)


class TestInsecureFlagAgainstSelfSignedServer(_HttpsServerTestCase):
    def test_fetch_index_rejects_self_signed_cert_by_default(self):
        with open(os.path.join(self.serve_dir, "page.html"), "w") as f:
            f.write("<html>ok</html>")
        with self.assertRaises(urllib.error.URLError):
            fetch_index(self.base_url + "page.html")

    def test_fetch_index_succeeds_with_insecure_true(self):
        with open(os.path.join(self.serve_dir, "page2.html"), "w") as f:
            f.write("<html>ok</html>")
        html_text, _final_url = fetch_index(self.base_url + "page2.html", insecure=True)
        self.assertEqual(html_text, "<html>ok</html>")

    def test_download_file_succeeds_with_insecure_true(self):
        content = os.urandom(2000)
        with open(os.path.join(self.serve_dir, "blob.bin"), "wb") as f:
            f.write(content)
        dest_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest_dir, ignore_errors=True)
        dest_path = os.path.join(dest_dir, "blob.bin")
        download_file(self.base_url + "blob.bin", dest_path, insecure=True)
        with open(dest_path, "rb") as f:
            self.assertEqual(f.read(), content)


def _make_entry(name):
    return Entry(name=name, href=name, url="http://x/" + name, is_dir=False, size_bytes=1, mtime=None)


class TestNavigationStack(unittest.TestCase):
    def test_starts_with_initial_frame(self):
        entries = [_make_entry("a.txt")]
        stack = NavigationStack("http://x/", entries)
        self.assertEqual(stack.current.url, "http://x/")
        self.assertEqual(stack.current.entries, entries)
        self.assertTrue(stack.at_root())

    def test_push_moves_current_to_new_frame(self):
        stack = NavigationStack("http://x/", [])
        sub_entries = [_make_entry("b.txt")]
        stack.push("http://x/sub/", sub_entries)
        self.assertEqual(stack.current.url, "http://x/sub/")
        self.assertEqual(stack.current.entries, sub_entries)
        self.assertFalse(stack.at_root())

    def test_pop_restores_previous_frame_with_cached_state(self):
        stack = NavigationStack("http://x/", [])
        stack.current.selected = 3
        stack.current.offset = 1
        stack.push("http://x/sub/", [_make_entry("b.txt")])
        popped = stack.pop()
        self.assertTrue(popped)
        self.assertEqual(stack.current.url, "http://x/")
        self.assertEqual(stack.current.selected, 3)
        self.assertEqual(stack.current.offset, 1)

    def test_pop_at_root_is_noop(self):
        stack = NavigationStack("http://x/", [])
        popped = stack.pop()
        self.assertFalse(popped)
        self.assertTrue(stack.at_root())


class _FakeStdScr:
    def erase(self):
        pass

    def getmaxyx(self):
        return (24, 100)

    def addstr(self, *args, **kwargs):
        pass

    def refresh(self):
        pass

    def keypad(self, *args, **kwargs):
        pass

    def timeout(self, *args, **kwargs):
        pass

    def getch(self):
        return ord("q")


class TestRefreshCurrent(unittest.TestCase):
    def test_refresh_reloads_current_directory(self):
        initial_entries = [Entry("old.txt", "old.txt", "http://x/old.txt", False, 1, None)]
        refreshed_entries = [Entry("new.txt", "new.txt", "http://x/new.txt", False, 2, None)]
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)) as fetch_mock, \
            unittest.mock.patch("nginx_tui.parse_index", return_value=refreshed_entries) as parse_mock:
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stack.current.entries = initial_entries
            app.stack.current.selected = 0
            app._refresh_current()
        self.assertEqual(fetch_mock.call_args.args[0], "http://x/")
        self.assertEqual(parse_mock.call_args.args[1], "http://x/")
        self.assertEqual(app.stack.current.entries, refreshed_entries)
        self.assertEqual(app.stack.current.selected, 0)

    def test_keyboard_interrupt_cancels_without_losing_current_listing(self):
        initial_entries = [Entry("old.txt", "old.txt", "http://x/old.txt", False, 1, None)]
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stack.current.entries = initial_entries
            with unittest.mock.patch("nginx_tui.fetch_index", side_effect=KeyboardInterrupt):
                app._refresh_current()  # must not raise
        self.assertEqual(app.stack.current.entries, initial_entries)
        self.assertIn("已取消刷新", app.status)

    def test_status_shows_directory_name_not_the_full_url(self):
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/a/b/c/very-long-directory-name/", "/tmp")
            with unittest.mock.patch("nginx_tui.fetch_index", side_effect=KeyboardInterrupt):
                app._refresh_current()
        self.assertIn("very-long-directory-name", app.status)
        self.assertNotIn("http://x/a/b/c", app.status)

    def test_directory_label_keeps_trailing_slash_like_entering_it_does(self):
        # Entering a directory takes its label from the Entry's own name
        # (trailing "/" intact); refreshing used to take it from _url_label()
        # instead, which strips the slash -- same directory, two different
        # looking status messages ("正在加载 subdir/" vs "正在刷新 subdir").
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/subdir/", "/tmp")
            with unittest.mock.patch("nginx_tui.fetch_index", side_effect=KeyboardInterrupt):
                app._refresh_current()
        self.assertIn("subdir/", app.status)


class TestLoadCancellation(unittest.TestCase):
    def test_keyboard_interrupt_while_entering_a_subdirectory_stays_at_parent(self):
        parent_entries = [Entry("subdir/", "subdir/", "http://x/subdir/", True, None, None)]
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=parent_entries):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            with unittest.mock.patch("nginx_tui.fetch_index", side_effect=KeyboardInterrupt):
                result = app._load("http://x/subdir/", push=True)  # must not raise
        self.assertFalse(result)
        self.assertIn("已取消加载", app.status)
        # No new frame was pushed -- still showing the parent listing.
        self.assertEqual(app.stack.current.url, "http://x/")
        self.assertEqual(app.stack.current.entries, parent_entries)
        self.assertTrue(app.stack.at_root())
        # Cancelling a subdirectory load is not the startup load -- must not
        # be mistaken for it when main() decides the process exit code.
        self.assertFalse(app.startup_cancelled)

    def test_keyboard_interrupt_during_startup_load_leaves_stack_none(self):
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=KeyboardInterrupt):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")  # must not raise
        self.assertIsNone(app.stack)
        self.assertIn("已取消加载", app.status)
        self.assertTrue(app.startup_cancelled)

    def test_startup_load_label_keeps_trailing_slash(self):
        # The startup load has no Entry to take a label from, so it falls
        # back to _dir_label(url) -- must match how entering a subdirectory
        # from within the app labels it (trailing "/" intact).
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=KeyboardInterrupt):
            app = BrowserApp(_FakeStdScr(), "http://x/a/subdir/", "/tmp")  # must not raise
        self.assertIn("subdir/", app.status)

    def test_entering_a_directory_shows_its_name_not_the_full_url(self):
        entries = [Entry(
            "subdir/", "subdir/", "http://x/a/b/c/d/e/f/g/h/subdir/", True, None, None,
        )]
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=entries):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stack.current.entries = entries
            app.stack.current.selected = 0
            with unittest.mock.patch("nginx_tui.fetch_index", side_effect=KeyboardInterrupt):
                app._enter_dir_selected()
        self.assertIn("subdir/", app.status)
        self.assertNotIn("http://x/a/b/c/d/e/f/g/h", app.status)

    def test_exception_text_landing_in_status_is_sanitized(self):
        # HTTP reason phrases legally allow a literal tab (RFC 9112's
        # reason-phrase grammar includes HTAB) -- str(exc) can carry it
        # straight into the status message unless _set_status sanitizes.
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=OSError("boom\tsplat")):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
        self.assertNotIn("\t", app.status)
        self.assertIn("boom�splat", app.status)


class TestFormatDuration(unittest.TestCase):
    def test_under_a_minute(self):
        self.assertEqual(_format_duration(12), "0:12")

    def test_pads_seconds_below_ten(self):
        self.assertEqual(_format_duration(65), "1:05")

    def test_multiple_minutes(self):
        self.assertEqual(_format_duration(724), "12:04")

    def test_negative_clamps_to_zero(self):
        self.assertEqual(_format_duration(-3), "0:00")


class TestFormatProgress(unittest.TestCase):
    def test_includes_cancel_hint_with_known_total(self):
        text = BrowserApp._format_progress("f.zip", 50, 100, 12)
        self.assertIn("Ctrl-C", text)

    def test_includes_cancel_hint_without_known_total(self):
        text = BrowserApp._format_progress("f.zip", 50, None, 12)
        self.assertIn("Ctrl-C", text)

    def test_includes_elapsed_time_with_known_total(self):
        text = BrowserApp._format_progress("f.zip", 50, 100, 65)
        self.assertIn("1:05", text)

    def test_includes_elapsed_time_without_known_total(self):
        text = BrowserApp._format_progress("f.zip", 50, None, 65)
        self.assertIn("1:05", text)


class TestEnterDirSelected(unittest.TestCase):
    def test_enters_directory(self):
        dir_entries = [Entry("subdir/", "subdir/", "http://x/subdir/", True, None, None)]
        child_entries = [Entry("inner.txt", "inner.txt", "http://x/subdir/inner.txt", False, 1, None)]
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=dir_entries):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            with unittest.mock.patch("nginx_tui.parse_index", return_value=child_entries):
                app._enter_dir_selected()
        self.assertEqual(app.stack.current.entries, child_entries)
        self.assertEqual(app.stack.current.url, "http://x/subdir/")

    def test_does_not_download_a_selected_file(self):
        file_entries = [Entry("notes.txt", "notes.txt", "http://x/notes.txt", False, 12, None)]
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=file_entries), \
            unittest.mock.patch("nginx_tui.download_file") as download_mock:
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app._enter_dir_selected()
        download_mock.assert_not_called()
        self.assertEqual(app.stack.current.entries, file_entries)


class TestConfirmOverwrite(unittest.TestCase):
    def test_keyboard_interrupt_cancels_instead_of_propagating(self):
        class _InterruptingStdScr(_FakeStdScr):
            def getch(self):
                raise KeyboardInterrupt

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stdscr = _InterruptingStdScr()
            result = app._confirm_overwrite("/tmp/dl/existing.txt")
        self.assertFalse(result)

    def test_escape_key_cancels_like_n(self):
        class _EscStdScr(_FakeStdScr):
            def getch(self):
                return 27

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stdscr = _EscStdScr()
            result = app._confirm_overwrite("/tmp/dl/existing.txt")
        self.assertFalse(result)

    def test_mouse_event_is_drained_not_left_queued(self):
        class _MouseThenAnswerStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = 0

            def getch(self):
                self.calls += 1
                return curses.KEY_MOUSE if self.calls == 1 else ord("n")

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stdscr = _MouseThenAnswerStdScr()
            with unittest.mock.patch("nginx_tui.curses.getmouse") as getmouse_mock:
                result = app._confirm_overwrite("/tmp/dl/existing.txt")
        getmouse_mock.assert_called_once()
        self.assertFalse(result)

    def test_prompt_wording_when_the_destination_file_itself_exists(self):
        dest_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest_dir, ignore_errors=True)
        dest_path = os.path.join(dest_dir, "movie.mkv")
        with open(dest_path, "wb"):
            pass

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app._confirm_overwrite(dest_path)  # default fake getch() answers "q" -> cancel
        self.assertIn("movie.mkv 已存在", app.status)
        self.assertNotIn(".part", app.status)

    def test_prompt_wording_when_only_the_part_file_exists(self):
        # The caller only triggers this when dest_path OR dest_path+".part"
        # exists -- if it's only the ".part" staging file, "movie.mkv 已存在"
        # is misleading since movie.mkv itself isn't actually there yet.
        dest_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest_dir, ignore_errors=True)
        dest_path = os.path.join(dest_dir, "movie.mkv")
        with open(dest_path + ".part", "wb"):
            pass

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app._confirm_overwrite(dest_path)  # default fake getch() answers "q" -> cancel
        self.assertNotIn("movie.mkv 已存在", app.status)
        self.assertIn(".part", app.status)

    def test_resize_during_prompt_clamps_viewport(self):
        entries = [_make_entry(f"f{i}.txt") for i in range(100)]

        class _ResizeThenAnswerStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = 0
                self.size = (10, 100)  # shrunk while the prompt is up -> visible=7

            def getmaxyx(self):
                return self.size

            def getch(self):
                self.calls += 1
                return curses.KEY_RESIZE if self.calls == 1 else ord("y")

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=entries):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stack.current.selected = 99
            app.stack.current.offset = 79
            app.stdscr = _ResizeThenAnswerStdScr()
            result = app._confirm_overwrite("/tmp/dl/existing.txt")
        frame = app.stack.current
        visible = app._page_size()
        self.assertTrue(frame.offset <= frame.selected < frame.offset + visible)
        self.assertTrue(result)

    def test_keyboard_interrupt_during_resize_redraw_cancels(self):
        class _ResizeThenAnswerStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = 0

            def getch(self):
                self.calls += 1
                return curses.KEY_RESIZE if self.calls == 1 else ord("y")

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stdscr = _ResizeThenAnswerStdScr()
            real_draw = app._draw
            draw_calls = []

            def flaky_draw():
                draw_calls.append(1)
                if len(draw_calls) == 2:
                    raise KeyboardInterrupt
                real_draw()

            with unittest.mock.patch.object(app, "_draw", side_effect=flaky_draw):
                result = app._confirm_overwrite("/tmp/dl/existing.txt")
        self.assertFalse(result)


class TestDownloadOverwriteCheck(unittest.TestCase):
    def test_preexisting_part_file_triggers_overwrite_confirmation(self):
        # dest_path itself doesn't exist -- only its ".part" staging file
        # does -- and that must still prompt, or download_file would
        # silently truncate it (download_file always opens part_path "wb").
        output_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, output_dir, ignore_errors=True)
        with open(os.path.join(output_dir, "movie.mkv.part"), "wb") as f:
            f.write(b"PRE_EXISTING_UNRELATED_DATA")
        entries = [Entry("movie.mkv", "movie.mkv", "http://x/movie.mkv", False, 100, None)]
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=entries), \
            unittest.mock.patch("nginx_tui.download_file") as download_mock:
            app = BrowserApp(_FakeStdScr(), "http://x/", output_dir)
            with unittest.mock.patch.object(app, "_confirm_overwrite", return_value=False) as confirm_mock:
                app._activate_selected()
        confirm_mock.assert_called_once()
        download_mock.assert_not_called()

    def test_existing_directory_at_dest_path_is_rejected_up_front(self):
        # Answering "y" to an overwrite prompt here would still fail at the
        # final os.replace (IsADirectoryError) only after downloading the
        # whole file -- reject before spending any bandwidth on it.
        output_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, output_dir, ignore_errors=True)
        os.mkdir(os.path.join(output_dir, "movie.mkv"))
        entries = [Entry("movie.mkv", "movie.mkv", "http://x/movie.mkv", False, 100, None)]
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=entries), \
            unittest.mock.patch("nginx_tui.download_file") as download_mock:
            app = BrowserApp(_FakeStdScr(), "http://x/", output_dir)
            with unittest.mock.patch.object(app, "_confirm_overwrite") as confirm_mock:
                app._activate_selected()
        confirm_mock.assert_not_called()
        download_mock.assert_not_called()
        self.assertIn("是一个目录", app.status)

    def test_dangling_symlink_at_dest_path_still_triggers_confirmation(self):
        # os.path.exists follows symlinks and returns False for a broken
        # one, which used to skip the prompt and let os.replace silently
        # clobber it.
        output_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, output_dir, ignore_errors=True)
        dest_path = os.path.join(output_dir, "movie.mkv")
        os.symlink(os.path.join(output_dir, "does_not_exist"), dest_path)
        entries = [Entry("movie.mkv", "movie.mkv", "http://x/movie.mkv", False, 100, None)]
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=entries), \
            unittest.mock.patch("nginx_tui.download_file") as download_mock:
            app = BrowserApp(_FakeStdScr(), "http://x/", output_dir)
            with unittest.mock.patch.object(app, "_confirm_overwrite", return_value=False) as confirm_mock:
                app._activate_selected()
        confirm_mock.assert_called_once()
        download_mock.assert_not_called()


class TestDownloadStatusFeedback(unittest.TestCase):
    def test_shows_a_status_message_before_the_network_call_starts(self):
        # download_file() doesn't report anything until the first byte
        # arrives via on_progress -- without an upfront status, a slow
        # connect/TLS/request-headers phase looks identical to the keypress
        # not having registered at all.
        entries = [Entry("movie.mkv", "movie.mkv", "http://x/movie.mkv", False, 100, None)]
        output_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, output_dir, ignore_errors=True)
        status_when_called = []

        def fake_download_file(url, dest_path, progress_cb=None, **kwargs):
            status_when_called.append(app.status)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=entries), \
            unittest.mock.patch("nginx_tui.download_file", side_effect=fake_download_file):
            app = BrowserApp(_FakeStdScr(), "http://x/", output_dir)
            app._activate_selected()

        self.assertEqual(len(status_when_called), 1)
        self.assertIn("正在下载 movie.mkv", status_when_called[0])

    def test_keyboard_interrupt_during_download_uses_the_same_wording_as_other_cancellations(self):
        # Declining the overwrite prompt and cancelling load/refresh with
        # Ctrl-C all say "已取消 X" -- Ctrl-C during an in-progress download
        # used to say "下载已中断" instead, a different word for the same
        # underlying event (user-initiated cancellation).
        entries = [Entry("movie.mkv", "movie.mkv", "http://x/movie.mkv", False, 100, None)]
        output_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, output_dir, ignore_errors=True)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=entries), \
            unittest.mock.patch("nginx_tui.download_file", side_effect=KeyboardInterrupt):
            app = BrowserApp(_FakeStdScr(), "http://x/", output_dir)
            app._activate_selected()  # must not raise

        self.assertEqual(app.status, "已取消下载")


class TestBreadcrumbDisplay(unittest.TestCase):
    def test_breadcrumb_shows_percent_decoded_url(self):
        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_RecordingStdScr(), "http://x/%E4%B8%AD%E6%96%87/", "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        breadcrumb_call = app.stdscr.calls[0]
        self.assertEqual(breadcrumb_call[2], "http://x/中文/")

    def test_breadcrumb_sanitizes_control_characters(self):
        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_RecordingStdScr(), "http://x/evil%0Adir/", "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        breadcrumb_call = app.stdscr.calls[0]
        self.assertNotIn("\n", breadcrumb_call[2])
        self.assertIn("evil", breadcrumb_call[2])

    def test_header_attr_falls_back_to_reverse_without_color_support(self):
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
        self.assertEqual(app.header_attr, curses.A_REVERSE | curses.A_BOLD)

    def test_header_attr_uses_fixed_white_on_blue_pair_with_color_support(self):
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=True), \
            unittest.mock.patch("nginx_tui.curses.start_color"), \
            unittest.mock.patch("nginx_tui.curses.use_default_colors"), \
            unittest.mock.patch("nginx_tui.curses.init_pair") as init_pair_mock, \
            unittest.mock.patch("nginx_tui.curses.color_pair", side_effect=lambda n: n * 100), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
        init_pair_mock.assert_any_call(2, curses.COLOR_WHITE, curses.COLOR_BLUE)
        self.assertEqual(app.header_attr, 200 | curses.A_BOLD)

    def test_footer_attr_falls_back_to_bold_without_color_support(self):
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
        self.assertEqual(app.footer_attr, curses.A_BOLD)
        self.assertNotEqual(app.footer_attr & curses.A_DIM, curses.A_DIM)

    def test_footer_attr_uses_fixed_cyan_pair_with_color_support(self):
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=True), \
            unittest.mock.patch("nginx_tui.curses.start_color"), \
            unittest.mock.patch("nginx_tui.curses.use_default_colors"), \
            unittest.mock.patch("nginx_tui.curses.init_pair") as init_pair_mock, \
            unittest.mock.patch("nginx_tui.curses.color_pair", side_effect=lambda n: n * 100), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
        init_pair_mock.assert_any_call(3, curses.COLOR_CYAN, -1)
        self.assertEqual(app.footer_attr, 300 | curses.A_BOLD)

    def test_breadcrumb_truncates_in_the_middle_when_too_narrow(self):
        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def getmaxyx(self):
                return (24, 40)

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        long_url = "http://x/" + "dir/" * 10 + "leaf-directory/"
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_RecordingStdScr(), long_url, "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        shown = app.stdscr.calls[0][2]
        self.assertLessEqual(_display_width(shown), 39)
        self.assertIn("…", shown)
        self.assertTrue(shown.startswith("http://x/"))
        self.assertTrue(shown.endswith("directory/"))

    def test_footer_line_no_longer_uses_dim_attribute(self):
        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_RecordingStdScr(), "http://x/", "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        footer_call = next(c for c in app.stdscr.calls if c[0] == 23)  # height-1 for the 24x100 fake screen
        self.assertNotEqual(footer_call[3] & curses.A_DIM, curses.A_DIM)

    def test_footer_hint_keeps_the_quit_key_visible(self):
        # The full footer hint is 114 columns wide -- front-truncating it (as
        # opposed to middle-truncating) used to cut off "q/Q 退出" at every
        # width below 115, i.e. effectively always.
        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_RecordingStdScr(), "http://x/", "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        footer_call = next(c for c in app.stdscr.calls if c[0] == 23)  # height-1 for the 24x100 fake screen
        self.assertIn("q/Q 退出", footer_call[2])


class TestDrawResilience(unittest.TestCase):
    def test_curses_error_during_draw_does_not_propagate(self):
        class _FailingStdScr(_FakeStdScr):
            def addstr(self, *args, **kwargs):
                raise curses.error("boundary write failed")

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FailingStdScr(), "http://x/", "/tmp")
            app._draw()  # must not raise curses.error

    def test_oversized_size_and_mtime_are_clipped_to_their_columns(self):
        # _rjust only pads, never clips -- an overlong value (e.g. a raw
        # unparseable-month date, or an unbounded unit-suffixed size) used to
        # overflow its column and wrap the row onto the next line.
        entry = Entry(
            "f.txt", "f.txt", "http://x/f.txt", False,
            None, "06-Foo-2024 10:00",
            size_raw="123456789012345678901234567890.5M",
        )

        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[entry]):
            app = BrowserApp(_RecordingStdScr(), "http://x/", "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        row_call = next(c for c in app.stdscr.calls if "f.txt" in c[2])
        # _FakeStdScr is 100 columns wide; the row must stay within it (with
        # the usual 1-column margin) instead of overflowing onto the next line.
        self.assertLessEqual(_display_width(row_call[2]), 99)


class TestSmallTerminalMessage(unittest.TestCase):
    def test_message_tells_the_user_how_to_recover(self):
        # No footer key-hint line is drawn in this branch -- if the message
        # doesn't say what to do, the user has no way to know q still quits.
        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def getmaxyx(self):
                return (24, 25)

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_RecordingStdScr(), "http://x/", "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        text = app.stdscr.calls[0][2]
        self.assertIn("窗口太小，按 q 退出", text)

    def test_message_survives_untruncated_across_the_entire_too_small_width_range(self):
        # The message itself is drawn in the same too-narrow space it's
        # warning about -- unlike ordinary status text, it must stay
        # readable (not get truncated into mush) across the whole range
        # where this branch can fire, not just at the widest end of it.
        for width in range(23, _MIN_TERMINAL_WIDTH):
            class _RecordingStdScr(_FakeStdScr):
                def __init__(self, w=width):
                    self.calls = []
                    self._w = w

                def getmaxyx(self):
                    return (24, self._w)

                def addstr(self, *args, **kwargs):
                    self.calls.append(args)

            with unittest.mock.patch("nginx_tui.curses.curs_set"), \
                unittest.mock.patch("nginx_tui.curses.mousemask"), \
                unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
                unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
                unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
                app = BrowserApp(_RecordingStdScr(), "http://x/", "/tmp")
                app.stdscr.calls.clear()
                app._draw()
            self.assertIn(
                "窗口太小，按 q 退出", app.stdscr.calls[0][2],
                f"message got truncated at width={width}",
            )

    def test_q_still_quits_while_the_terminal_is_too_small(self):
        class _TooSmallThenQuitStdScr(_FakeStdScr):
            def getmaxyx(self):
                return (24, 25)  # too small to render the listing

            def getch(self):
                return ord("q")

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stdscr = _TooSmallThenQuitStdScr()
            app.run()  # must return -- q must still quit even though nothing is visibly clickable

    def test_width_in_the_previously_broken_20_to_34_range_shows_centered_message(self):
        entries = [_make_entry("f.txt")]

        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def getmaxyx(self):
                return (24, 25)  # inside the old broken 20-34 range

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=entries):
            app = BrowserApp(_RecordingStdScr(), "http://x/", "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        # Only the centered "too small" message is drawn -- no attempt at a
        # squeezed name/size/mtime row that would wrap or get cut off.
        self.assertEqual(len(app.stdscr.calls), 1)
        y, x, text = app.stdscr.calls[0][0], app.stdscr.calls[0][1], app.stdscr.calls[0][2]
        self.assertIn("窗口太小，按 q 退出", text)
        self.assertGreater(y, 0)  # vertically centered, not pinned to row 0

    def test_width_exactly_at_the_minimum_still_renders_the_listing(self):
        entries = [_make_entry("f.txt")]

        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def getmaxyx(self):
                return (24, _MIN_TERMINAL_WIDTH)

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=entries):
            app = BrowserApp(_RecordingStdScr(), "http://x/", "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        self.assertFalse(any("太小" in c[2] for c in app.stdscr.calls if len(c) > 2))
        self.assertTrue(any("f.txt" in c[2] for c in app.stdscr.calls if len(c) > 2))

    def test_minimum_width_still_leaves_the_same_right_margin_as_breadcrumb_and_footer(self):
        # At the floor, the name column is clamped rather than computed --
        # confirm that clamp doesn't silently drop the 1-column margin the
        # rest of the layout (breadcrumb, footer) always reserves.
        entries = [_make_entry("f.txt")]

        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def getmaxyx(self):
                return (24, _MIN_TERMINAL_WIDTH)

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=entries):
            app = BrowserApp(_RecordingStdScr(), "http://x/", "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        row_call = next(c for c in app.stdscr.calls if "f.txt" in c[2])
        self.assertEqual(_display_width(row_call[2]), _MIN_TERMINAL_WIDTH - 1)

    def test_one_column_narrower_than_the_minimum_shows_the_message(self):
        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def getmaxyx(self):
                return (24, _MIN_TERMINAL_WIDTH - 1)

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[_make_entry("f.txt")]):
            app = BrowserApp(_RecordingStdScr(), "http://x/", "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        self.assertEqual(len(app.stdscr.calls), 1)
        self.assertIn("窗口太小，按 q 退出", app.stdscr.calls[0][2])

    def test_short_height_also_shows_centered_message(self):
        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def getmaxyx(self):
                return (3, 100)

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_RecordingStdScr(), "http://x/", "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        self.assertEqual(len(app.stdscr.calls), 1)
        self.assertIn("窗口太小，按 q 退出", app.stdscr.calls[0][2])


class TestMouseInterval(unittest.TestCase):
    def test_mouseinterval_is_disabled_at_init(self):
        # ncurses' default mouseinterval (200ms) makes it hold a button-press
        # event that long waiting for a possible release to pair it with
        # into a BUTTON1_CLICKED -- since _handle_mouse already reacts to a
        # bare BUTTON1_PRESSED, that wait is a pure, perceptible click-to-
        # download lag with nothing gained from it.
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.mouseinterval") as mouseinterval_mock, \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
        mouseinterval_mock.assert_called_once_with(0)


class TestHandleMouseTooSmall(unittest.TestCase):
    def _make_app(self, stdscr, entries):
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=entries):
            return BrowserApp(stdscr, "http://x/", "/tmp")

    def test_click_is_ignored_when_terminal_too_narrow(self):
        entries = [_make_entry("secret.txt")]

        class _NarrowStdScr(_FakeStdScr):
            def getmaxyx(self):
                return (24, 25)  # below _MIN_TERMINAL_WIDTH -- listing not drawn

        app = self._make_app(_NarrowStdScr(), entries)
        with unittest.mock.patch(
            "nginx_tui.curses.getmouse", return_value=(0, 0, 2, 0, curses.BUTTON1_CLICKED)
        ), unittest.mock.patch.object(app, "_download") as download_mock:
            app._handle_mouse()
        download_mock.assert_not_called()

    def test_click_is_ignored_when_terminal_too_short(self):
        entries = [_make_entry("secret.txt")]

        class _ShortStdScr(_FakeStdScr):
            def getmaxyx(self):
                return (3, 100)  # below the height floor -- listing not drawn

        app = self._make_app(_ShortStdScr(), entries)
        with unittest.mock.patch(
            "nginx_tui.curses.getmouse", return_value=(0, 0, 2, 0, curses.BUTTON1_CLICKED)
        ), unittest.mock.patch.object(app, "_download") as download_mock:
            app._handle_mouse()
        download_mock.assert_not_called()

    def test_click_still_works_at_a_normal_size(self):
        entries = [_make_entry("secret.txt")]
        app = self._make_app(_FakeStdScr(), entries)
        with unittest.mock.patch(
            "nginx_tui.curses.getmouse", return_value=(0, 0, 2, 0, curses.BUTTON1_CLICKED)
        ), unittest.mock.patch.object(app, "_download") as download_mock:
            app._handle_mouse()
        download_mock.assert_called_once()


class TestDrawCenterMessage(unittest.TestCase):
    def test_long_name_status_keeps_trailing_prompt_visible(self):
        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def getmaxyx(self):
                return (24, 40)

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_RecordingStdScr(), "http://x/", "/tmp")
            long_name = "a-very-long-file-name-that-does-not-fit-on-one-line.tar.gz"
            app._set_status(f"{long_name} 已存在，是否覆盖？(y/n)")
            app.stdscr.calls.clear()
            app._draw()
        center_call = next(c for c in app.stdscr.calls if "y/n" in c[2])
        self.assertIn("…", center_call[2])
        self.assertTrue(center_call[2].rstrip().endswith("(y/n)"))


def _make_app_with_entries(count):
    entries = [_make_entry(f"f{i}.txt") for i in range(count)]
    with unittest.mock.patch("nginx_tui.curses.curs_set"), \
        unittest.mock.patch("nginx_tui.curses.mousemask"), \
        unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
        unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
        unittest.mock.patch("nginx_tui.parse_index", return_value=entries):
        return BrowserApp(_FakeStdScr(), "http://x/", "/tmp")


class TestPageMove(unittest.TestCase):
    # _FakeStdScr.getmaxyx() is (24, 100) -> _page_size() == 21
    def test_page_down_from_top_scrolls_a_full_page_not_one_line(self):
        app = _make_app_with_entries(100)
        app._page_move(1)
        self.assertEqual(app.stack.current.selected, 21)
        self.assertEqual(app.stack.current.offset, 21)

    def test_page_down_repeated_lands_on_final_page_without_overshoot(self):
        app = _make_app_with_entries(100)
        for _ in range(10):
            app._page_move(1)
        frame = app.stack.current
        self.assertEqual(frame.selected, 99)
        self.assertEqual(frame.offset, 79)  # max_offset = 100 - 21
        # idempotent once fully scrolled
        app._page_move(1)
        self.assertEqual(frame.selected, 99)
        self.assertEqual(frame.offset, 79)

    def test_page_up_after_page_down_preserves_relative_row(self):
        app = _make_app_with_entries(100)
        app._page_move(1)  # selected=21, offset=21 (top of viewport)
        app._page_move(1)  # selected=42, offset=42 (top of viewport)
        app._page_move(-1)
        frame = app.stack.current
        self.assertEqual(frame.selected, 21)
        self.assertEqual(frame.offset, 21)

    def test_page_up_from_top_of_list_clamps_to_start(self):
        app = _make_app_with_entries(100)
        app._page_move(-1)
        frame = app.stack.current
        self.assertEqual(frame.selected, 0)
        self.assertEqual(frame.offset, 0)

    def test_page_move_on_short_list_keeps_offset_at_zero(self):
        app = _make_app_with_entries(5)
        app._page_move(1)
        frame = app.stack.current
        self.assertEqual(frame.selected, 4)
        self.assertEqual(frame.offset, 0)

    def test_page_move_on_empty_list_is_a_noop(self):
        app = _make_app_with_entries(0)
        app._page_move(1)  # must not raise
        frame = app.stack.current
        self.assertEqual(frame.selected, 0)
        self.assertEqual(frame.offset, 0)


class TestResizeAdjustsOffset(unittest.TestCase):
    def test_resize_key_triggers_offset_adjustment(self):
        class _ResizeThenQuitStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = 0

            def getch(self):
                self.calls += 1
                return curses.KEY_RESIZE if self.calls == 1 else ord("q")

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stdscr = _ResizeThenQuitStdScr()
            with unittest.mock.patch.object(app, "_adjust_offset") as adjust_mock:
                app.run()
        adjust_mock.assert_called_once()


class TestRunLoopIgnoresIdleCtrlC(unittest.TestCase):
    def test_keyboard_interrupt_while_idle_browsing_does_not_exit(self):
        class _InterruptThenQuitStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = 0

            def getch(self):
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt
                return ord("q")

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stdscr = _InterruptThenQuitStdScr()
            app.run()  # must not raise / propagate KeyboardInterrupt
        self.assertEqual(app.stdscr.calls, 2)

    def test_keyboard_interrupt_during_action_dispatch_does_not_exit(self):
        # The try used to wrap only _draw()+getch() -- a Ctrl-C landing while
        # a plain navigation action (move/page/back, none of which have their
        # own KeyboardInterrupt handling) was executing would propagate
        # straight out of run() and kill the whole app.
        class _MoveThenQuitStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = 0

            def getch(self):
                self.calls += 1
                return ord("j") if self.calls == 1 else ord("q")

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=lambda url, **k: ("<html></html>", url)), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stdscr = _MoveThenQuitStdScr()
            with unittest.mock.patch.object(app, "_move_selection", side_effect=KeyboardInterrupt):
                app.run()  # must not raise / propagate KeyboardInterrupt
        self.assertEqual(app.stdscr.calls, 2)


class TestTruncateMiddle(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(_truncate_middle("short", 20), "short")

    def test_text_exactly_at_width_is_unchanged(self):
        self.assertEqual(_truncate_middle("12345", 5), "12345")

    def test_long_text_keeps_head_and_tail(self):
        text = "A" * 10 + "MIDDLE" + "B" * 10
        result = _truncate_middle(text, 12)
        self.assertEqual(_display_width(result), 12)
        self.assertTrue(result.startswith("AAAAAA"))
        self.assertTrue(result.endswith("BBBBB"))
        self.assertIn("…", result)
        self.assertNotIn("MIDDLE", result)

    def test_extremely_small_width_falls_back_to_head_truncate(self):
        self.assertEqual(_truncate_middle("hello", 1), "h")
        self.assertEqual(_truncate_middle("hello", 0), "")


class TestUrlLabel(unittest.TestCase):
    def test_returns_leaf_directory_name(self):
        self.assertEqual(_url_label("http://x/a/b/subdir/"), "subdir")

    def test_returns_leaf_file_name(self):
        self.assertEqual(_url_label("http://x/a/b/file.txt"), "file.txt")

    def test_percent_decodes_the_leaf(self):
        self.assertEqual(_url_label("http://x/%E4%B8%AD%E6%96%87/"), "中文")

    def test_root_url_falls_back_to_full_url(self):
        self.assertEqual(_url_label("http://x/"), "http://x/")

    def test_sanitizes_control_characters(self):
        self.assertNotIn("\n", _url_label("http://x/evil%0Aname/"))
        self.assertIn("evil", _url_label("http://x/evil%0Aname/"))


class TestDirLabel(unittest.TestCase):
    def test_restores_the_trailing_slash_url_label_strips(self):
        # _load's "正在加载 subdir/ ..." (label taken from a directory
        # Entry's own name, which keeps its trailing "/") and
        # _refresh_current's "正在刷新 subdir ..." (label from _url_label,
        # which strips it to find the leaf segment) used to disagree for the
        # very same directory -- _dir_label makes both show "subdir/".
        self.assertEqual(_dir_label("http://x/a/b/subdir/"), "subdir/")

    def test_root_url_is_not_given_a_second_trailing_slash(self):
        self.assertEqual(_dir_label("http://x/"), "http://x/")

    def test_percent_decodes_the_leaf(self):
        self.assertEqual(_dir_label("http://x/%E4%B8%AD%E6%96%87/"), "中文/")


class TestMain(unittest.TestCase):
    def test_output_dir_creation_failure_exits_cleanly(self):
        with unittest.mock.patch("nginx_tui.locale.setlocale"), \
            unittest.mock.patch("nginx_tui.os.makedirs", side_effect=OSError("Permission denied")), \
            unittest.mock.patch("nginx_tui.curses.wrapper") as wrapper_mock:
            err = io.StringIO()
            with redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    main(["http://example.com/files/", "--output-dir", "/no/permission"])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("无法创建输出目录", err.getvalue())
        wrapper_mock.assert_not_called()

    def test_sets_escdelay_default_for_responsive_esc(self):
        with unittest.mock.patch("nginx_tui.locale.setlocale"), \
            unittest.mock.patch("nginx_tui.os.makedirs"), \
            unittest.mock.patch("nginx_tui.curses.wrapper"), \
            unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ESCDELAY", None)
            main(["http://example.com/files/"])
            self.assertEqual(os.environ.get("ESCDELAY"), "25")

    def test_does_not_override_existing_escdelay(self):
        with unittest.mock.patch("nginx_tui.locale.setlocale"), \
            unittest.mock.patch("nginx_tui.os.makedirs"), \
            unittest.mock.patch("nginx_tui.curses.wrapper"), \
            unittest.mock.patch.dict(os.environ, {"ESCDELAY": "100"}):
            main(["http://example.com/files/"])
            self.assertEqual(os.environ.get("ESCDELAY"), "100")

    def test_startup_cancel_exits_with_130_not_1(self):
        with unittest.mock.patch("nginx_tui.locale.setlocale"), \
            unittest.mock.patch("nginx_tui.os.makedirs"), \
            unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=KeyboardInterrupt), \
            unittest.mock.patch("nginx_tui.curses.wrapper", side_effect=lambda fn: fn(_FakeStdScr())):
            err = io.StringIO()
            with redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    main(["http://example.com/files/"])
        self.assertEqual(cm.exception.code, 130)
        self.assertIn("已取消加载", err.getvalue())

    def test_startup_network_failure_exits_with_1(self):
        with unittest.mock.patch("nginx_tui.locale.setlocale"), \
            unittest.mock.patch("nginx_tui.os.makedirs"), \
            unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=OSError("boom")), \
            unittest.mock.patch("nginx_tui.curses.wrapper", side_effect=lambda fn: fn(_FakeStdScr())):
            err = io.StringIO()
            with redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    main(["http://example.com/files/"])
        self.assertEqual(cm.exception.code, 1)


class TestResolveAction(unittest.TestCase):
    def test_up_arrow_and_k_move_up(self):
        self.assertEqual(resolve_action(curses.KEY_UP), Action.MOVE_UP)
        self.assertEqual(resolve_action(ord("k")), Action.MOVE_UP)

    def test_down_arrow_and_j_move_down(self):
        self.assertEqual(resolve_action(curses.KEY_DOWN), Action.MOVE_DOWN)
        self.assertEqual(resolve_action(ord("j")), Action.MOVE_DOWN)

    def test_page_up_and_down(self):
        self.assertEqual(resolve_action(curses.KEY_PPAGE), Action.PAGE_UP)
        self.assertEqual(resolve_action(curses.KEY_NPAGE), Action.PAGE_DOWN)

    def test_refresh_key_maps_to_refresh(self):
        self.assertEqual(resolve_action(ord("r")), Action.REFRESH)
        self.assertEqual(resolve_action(ord("R")), Action.REFRESH)

    def test_enter_variants_activate(self):
        self.assertEqual(resolve_action(10), Action.ACTIVATE)
        self.assertEqual(resolve_action(13), Action.ACTIVATE)
        self.assertEqual(resolve_action(curses.KEY_ENTER), Action.ACTIVATE)

    def test_right_arrow_maps_to_enter_dir(self):
        self.assertEqual(resolve_action(curses.KEY_RIGHT), Action.ENTER_DIR)

    def test_backspace_variants_go_back(self):
        self.assertEqual(resolve_action(curses.KEY_BACKSPACE), Action.BACK)
        self.assertEqual(resolve_action(127), Action.BACK)
        self.assertEqual(resolve_action(curses.KEY_LEFT), Action.BACK)
        self.assertEqual(resolve_action(ord("u")), Action.BACK)
        self.assertEqual(resolve_action(27), Action.BACK)

    def test_q_quits(self):
        self.assertEqual(resolve_action(ord("q")), Action.QUIT)
        self.assertEqual(resolve_action(ord("Q")), Action.QUIT)

    def test_unmapped_key_returns_none(self):
        self.assertIsNone(resolve_action(ord("z")))


class TestFormatRow(unittest.TestCase):
    def test_short_name_is_padded(self):
        entry = Entry(name="a.txt", href="a.txt", url="http://x/a.txt", is_dir=False, size_bytes=1, mtime=None)
        self.assertEqual(format_row(entry, 10), "a.txt     ")

    def test_long_name_is_truncated_with_ellipsis(self):
        entry = Entry(
            name="a_very_long_filename.txt", href="x", url="http://x/x",
            is_dir=False, size_bytes=1, mtime=None,
        )
        result = format_row(entry, 10)
        self.assertEqual(len(result), 10)
        self.assertTrue(result.endswith("…"))

    def test_wide_characters_are_truncated_by_display_width_not_char_count(self):
        entry = Entry(
            name="文件名超长测试文件名超长测试.txt", href="x", url="http://x/x",
            is_dir=False, size_bytes=1, mtime=None,
        )
        result = format_row(entry, 10)
        self.assertEqual(_display_width(result), 10)
        self.assertTrue(result.rstrip().endswith("…"))


if __name__ == "__main__":
    unittest.main()
