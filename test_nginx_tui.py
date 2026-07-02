import curses
import functools
import http.server
import io
import os
import shutil
import ssl
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
    _display_width,
    _format_duration,
    _ssl_context,
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


class TestFormatMtime(unittest.TestCase):
    def test_none_stays_none(self):
        self.assertIsNone(format_mtime(None))

    def test_parses_nginx_date_format(self):
        self.assertEqual(format_mtime("06-Jul-2023 10:00"), "2023-07-06 10:00")

    def test_parses_nginx_date_format_independent_of_locale(self):
        self.assertEqual(format_mtime("01-Jul-2026 17:50"), "2026-07-01 17:50")

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
        result = fetch_index(self.base_url + "page.html")
        self.assertEqual(result, "<html>你好</html>")

    def test_raises_on_404(self):
        with self.assertRaises(urllib.error.HTTPError):
            fetch_index(self.base_url + "missing.html")


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
        result = fetch_index(self.base_url + "page2.html", insecure=True)
        self.assertEqual(result, "<html>ok</html>")

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
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>") as fetch_mock, \
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
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stack.current.entries = initial_entries
            with unittest.mock.patch("nginx_tui.fetch_index", side_effect=KeyboardInterrupt):
                app._refresh_current()  # must not raise
        self.assertEqual(app.stack.current.entries, initial_entries)
        self.assertIn("已取消刷新", app.status)


class TestLoadCancellation(unittest.TestCase):
    def test_keyboard_interrupt_while_entering_a_subdirectory_stays_at_parent(self):
        parent_entries = [Entry("subdir/", "subdir/", "http://x/subdir/", True, None, None)]
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
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

    def test_keyboard_interrupt_during_startup_load_leaves_stack_none(self):
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", side_effect=KeyboardInterrupt):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")  # must not raise
        self.assertIsNone(app.stack)
        self.assertIn("已取消加载", app.status)


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
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
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
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
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
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
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
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stdscr = _EscStdScr()
            result = app._confirm_overwrite("/tmp/dl/existing.txt")
        self.assertFalse(result)


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
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_RecordingStdScr(), "http://x/%E4%B8%AD%E6%96%87/", "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        breadcrumb_call = app.stdscr.calls[0]
        self.assertEqual(breadcrumb_call[2], "http://x/中文/")

    def test_header_attr_falls_back_to_reverse_without_color_support(self):
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
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
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
        init_pair_mock.assert_any_call(2, curses.COLOR_WHITE, curses.COLOR_BLUE)
        self.assertEqual(app.header_attr, 200 | curses.A_BOLD)

    def test_footer_attr_falls_back_to_bold_without_color_support(self):
        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
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
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
        init_pair_mock.assert_any_call(3, curses.COLOR_CYAN, -1)
        self.assertEqual(app.footer_attr, 300 | curses.A_BOLD)

    def test_footer_line_no_longer_uses_dim_attribute(self):
        class _RecordingStdScr(_FakeStdScr):
            def __init__(self):
                self.calls = []

            def addstr(self, *args, **kwargs):
                self.calls.append(args)

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_RecordingStdScr(), "http://x/", "/tmp")
            app.stdscr.calls.clear()
            app._draw()
        footer_call = next(c for c in app.stdscr.calls if c[0] == 23)  # height-1 for the 24x100 fake screen
        self.assertNotEqual(footer_call[3] & curses.A_DIM, curses.A_DIM)


class TestDrawResilience(unittest.TestCase):
    def test_curses_error_during_draw_does_not_propagate(self):
        class _FailingStdScr(_FakeStdScr):
            def addstr(self, *args, **kwargs):
                raise curses.error("boundary write failed")

        with unittest.mock.patch("nginx_tui.curses.curs_set"), \
            unittest.mock.patch("nginx_tui.curses.mousemask"), \
            unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FailingStdScr(), "http://x/", "/tmp")
            app._draw()  # must not raise curses.error


def _make_app_with_entries(count):
    entries = [_make_entry(f"f{i}.txt") for i in range(count)]
    with unittest.mock.patch("nginx_tui.curses.curs_set"), \
        unittest.mock.patch("nginx_tui.curses.mousemask"), \
        unittest.mock.patch("nginx_tui.curses.has_colors", return_value=False), \
        unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
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
            unittest.mock.patch("nginx_tui.fetch_index", return_value="<html></html>"), \
            unittest.mock.patch("nginx_tui.parse_index", return_value=[]):
            app = BrowserApp(_FakeStdScr(), "http://x/", "/tmp")
            app.stdscr = _ResizeThenQuitStdScr()
            with unittest.mock.patch.object(app, "_adjust_offset") as adjust_mock:
                app.run()
        adjust_mock.assert_called_once()


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
