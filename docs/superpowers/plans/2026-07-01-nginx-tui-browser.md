# nginx 目录浏览下载 TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single-file, zero-dependency Python TUI (`nginx_tui.py`) that browses an nginx `autoindex`-enabled directory listing and downloads files on Enter/click, per `docs/superpowers/specs/2026-07-01-nginx-tui-browser-design.md`.

**Architecture:** One script split internally into pure, unit-testable logic (URL/arg handling, HTML parsing, size/date formatting, history-stack navigation, keypress-to-action mapping, row formatting) plus a thin `curses`-driven `BrowserApp` class that wires that logic to the terminal and network I/O. Pure logic gets automated tests; the `curses` layer is verified manually against a local nginx instance, per the approved design doc.

**Tech Stack:** Python 3, standard library only (`curses`, `urllib`, `html.parser`, `argparse`, `dataclasses`, `enum`, `re`, `datetime`, `http.server` for tests). No `pip install` required for the script or its tests.

## Global Constraints

- No third-party dependencies — stdlib only, for both the script and its tests.
- Target Python 3.8+.
- Single file `nginx_tui.py` at repo root; all tests in `test_nginx_tui.py` at repo root, run via `python3 -m unittest`.
- All code comments and commit messages in English.
- Out of scope (do not implement): recursive/whole-directory download, authentication, resumable downloads, sorting/filter/search.
- `Entry.size_bytes` / `Entry.mtime` must reflect nginx's real returned data (parsed, not fabricated); column layout, colors, and unit formatting are the script's own design (per design doc's "信息忠实 / 样式自定义" decision).

---

## Task 1: CLI argument parsing and URL normalization

**Files:**
- Create: `nginx_tui.py`
- Create: `test_nginx_tui.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces:
  - `normalize_url(raw_url: str) -> str`
  - `build_arg_parser() -> argparse.ArgumentParser`
  - `parse_args(argv: List[str]) -> argparse.Namespace` with `.url: str`, `.output_dir: str`
  - Full module header (shebang + all imports + constants) that every later task appends below

- [ ] **Step 1: Write the failing tests**

Create `test_nginx_tui.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest test_nginx_tui -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nginx_tui'` (the file doesn't exist yet).

- [ ] **Step 3: Create `nginx_tui.py` with the full module header and the CLI functions**

```python
#!/usr/bin/env python3
import argparse
import curses
import datetime
import enum
import html.parser
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

CHUNK_SIZE = 65536
CONNECT_TIMEOUT = 15.0
PROGRESS_THROTTLE_SECONDS = 0.1
_USER_AGENT = "nginx-tui/1.0"


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def normalize_url(raw_url: str) -> str:
    # urlparse().scheme misparses "localhost:8000/x" as scheme="localhost";
    # requiring "://" right after the scheme name avoids that false positive.
    if _SCHEME_RE.match(raw_url):
        return raw_url
    return "http://" + raw_url


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nginx_tui.py",
        description="浏览并下载开启了 autoindex 的 nginx 静态文件服务器目录中的文件（终端 TUI）。",
        epilog="示例：python3 nginx_tui.py http://example.com/files/ --output-dir ~/Downloads",
    )
    parser.add_argument("url", help="nginx autoindex 目录列表的 URL")
    parser.add_argument(
        "-o", "--output-dir", default=os.getcwd(),
        help="下载文件保存的本地目录（默认：当前工作目录）",
    )
    return parser


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = build_arg_parser()
    if not argv:
        parser.print_help()
        raise SystemExit(0)
    args = parser.parse_args(argv)
    args.url = normalize_url(args.url)
    return args
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest test_nginx_tui -v`
Expected: 9 tests, all PASS.

- [ ] **Step 5: Commit**

```bash
git init
git add nginx_tui.py test_nginx_tui.py docs/superpowers
git commit -m "feat: add CLI argument parsing for nginx TUI browser"
```

(This is the first commit in the repo — `git init` is required here since the directory has no `.git` yet. If a later task's `git commit` runs in the same repo, skip `git init`.)

---

## Task 2: nginx autoindex HTML parsing and display formatting

**Files:**
- Modify: `nginx_tui.py` (append)
- Modify: `test_nginx_tui.py` (append)

**Interfaces:**
- Consumes: `urllib.parse` (Task 1 imports)
- Produces:
  - `Entry` dataclass: `name: str, href: str, url: str, is_dir: bool, size_bytes: Optional[int], mtime: Optional[str]`
  - `parse_index(html_text: str, base_url: str) -> List[Entry]`
  - `format_size(size_bytes: Optional[int]) -> str`
  - `format_mtime(raw: Optional[str]) -> Optional[str]`

- [ ] **Step 1: Write the failing tests**

Append to `test_nginx_tui.py`:

```python
from nginx_tui import Entry, format_mtime, format_size, parse_index

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest test_nginx_tui -v`
Expected: FAIL with `ImportError: cannot import name 'parse_index' from 'nginx_tui'`.

- [ ] **Step 3: Append the implementation to `nginx_tui.py`**

```python
@dataclass
class Entry:
    name: str
    href: str
    url: str
    is_dir: bool
    size_bytes: Optional[int]
    mtime: Optional[str]


class _AnchorExtractor(html.parser.HTMLParser):
    """Collects (href, link text) for every <a> tag, tolerant of malformed HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.anchors: List[Tuple[str, str]] = []
        self._current_href: Optional[str] = None
        self._current_text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href is not None:
            self._current_href = href
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href is not None:
            self.anchors.append((self._current_href, "".join(self._current_text)))
            self._current_href = None
            self._current_text = []


# Matches nginx's default autoindex line: <a href="...">text</a>  DD-Mon-YYYY HH:MM  size
_LINE_META_RE = re.compile(
    r'<a\s+href="(?P<href>[^"]*)"[^>]*>.*?</a>'
    r'\s*(?:(?P<date>\d{2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2}))?'
    r'\s*(?P<size>-|\d+)?\s*$'
)

_SKIP_HREFS = {"../", "..", "/"}


def _parse_meta_by_href(html_text: str) -> Dict[str, Tuple[Optional[str], Optional[int]]]:
    meta: Dict[str, Tuple[Optional[str], Optional[int]]] = {}
    for line in html_text.splitlines():
        match = _LINE_META_RE.search(line)
        if not match or match.group("href") in meta:
            continue
        size_raw = match.group("size")
        size_bytes = int(size_raw) if size_raw and size_raw != "-" else None
        meta[match.group("href")] = (match.group("date"), size_bytes)
    return meta


def format_size(size_bytes: Optional[int]) -> str:
    if size_bytes is None:
        return "-"
    size = float(size_bytes)
    for unit in ("B", "K", "M"):
        if size < 1024:
            return f"{int(size)}B" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}G"


def format_mtime(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    try:
        parsed = datetime.datetime.strptime(raw, "%d-%b-%Y %H:%M")
    except ValueError:
        return raw
    return parsed.strftime("%Y-%m-%d %H:%M")


def parse_index(html_text: str, base_url: str) -> List[Entry]:
    extractor = _AnchorExtractor()
    extractor.feed(html_text)
    meta_by_href = _parse_meta_by_href(html_text)

    entries: List[Entry] = []
    seen: set = set()
    for href, text in extractor.anchors:
        if href in _SKIP_HREFS or text.strip() == "..":
            continue
        if href in seen:
            continue
        seen.add(href)
        is_dir = href.endswith("/")
        date_raw, size_bytes = meta_by_href.get(href, (None, None))
        entries.append(Entry(
            name=urllib.parse.unquote(href),
            href=href,
            url=urllib.parse.urljoin(base_url, href),
            is_dir=is_dir,
            size_bytes=None if is_dir else size_bytes,
            mtime=format_mtime(date_raw),
        ))
    return entries
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest test_nginx_tui -v`
Expected: all tests PASS (22 total so far).

- [ ] **Step 5: Commit**

```bash
git add nginx_tui.py test_nginx_tui.py
git commit -m "feat: parse nginx autoindex HTML into Entry list"
```

---

## Task 3: Network layer — fetch and download

**Files:**
- Modify: `nginx_tui.py` (append)
- Modify: `test_nginx_tui.py` (append)

**Interfaces:**
- Consumes: `CHUNK_SIZE`, `CONNECT_TIMEOUT`, `_USER_AGENT` (Task 1 constants)
- Produces:
  - `fetch_index(url: str, timeout: float = CONNECT_TIMEOUT) -> str`
  - `download_file(url: str, dest_path: str, progress_cb: Optional[Callable[[int, Optional[int]], None]] = None, timeout: float = CONNECT_TIMEOUT) -> None`
  - Writes to `dest_path + ".part"` during transfer, `os.replace`s to `dest_path` on success, removes the `.part` file on any failure (including the progress callback raising).

- [ ] **Step 1: Write the failing tests**

Append to `test_nginx_tui.py`:

```python
import functools
import http.server
import shutil
import tempfile
import threading
import unittest.mock
import urllib.error

from nginx_tui import download_file, fetch_index


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest test_nginx_tui -v`
Expected: FAIL with `ImportError: cannot import name 'download_file' from 'nginx_tui'`.

- [ ] **Step 3: Append the implementation to `nginx_tui.py`**

```python
def fetch_index(url: str, timeout: float = CONNECT_TIMEOUT) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def download_file(
    url: str,
    dest_path: str,
    progress_cb: Optional[Callable[[int, Optional[int]], None]] = None,
    timeout: float = CONNECT_TIMEOUT,
) -> None:
    part_path = dest_path + ".part"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total_header = response.headers.get("Content-Length")
            total_bytes = int(total_header) if total_header is not None else None
            downloaded = 0
            with open(part_path, "wb") as out_file:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    out_file.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb is not None:
                        progress_cb(downloaded, total_bytes)
        os.replace(part_path, dest_path)
    except BaseException:
        if os.path.exists(part_path):
            os.remove(part_path)
        raise
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest test_nginx_tui -v`
Expected: all tests PASS (28 total so far).

- [ ] **Step 5: Commit**

```bash
git add nginx_tui.py test_nginx_tui.py
git commit -m "feat: add nginx_tui network layer for index fetch and file download"
```

---

## Task 4: Navigation history stack

**Files:**
- Modify: `nginx_tui.py` (append)
- Modify: `test_nginx_tui.py` (append)

**Interfaces:**
- Consumes: `Entry` (Task 2)
- Produces:
  - `Frame` dataclass: `url: str, entries: List[Entry], selected: int = 0, offset: int = 0`
  - `NavigationStack(initial_url: str, initial_entries: List[Entry])` with `.current -> Frame` (property), `.push(url: str, entries: List[Entry]) -> None`, `.pop() -> bool`, `.at_root() -> bool`

- [ ] **Step 1: Write the failing tests**

Append to `test_nginx_tui.py`:

```python
from nginx_tui import NavigationStack


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest test_nginx_tui -v`
Expected: FAIL with `ImportError: cannot import name 'NavigationStack' from 'nginx_tui'`.

- [ ] **Step 3: Append the implementation to `nginx_tui.py`**

```python
@dataclass
class Frame:
    url: str
    entries: List[Entry]
    selected: int = 0
    offset: int = 0


class NavigationStack:
    """Back-navigation via a cached frame stack — popping never re-fetches."""

    def __init__(self, initial_url: str, initial_entries: List[Entry]) -> None:
        self._frames: List[Frame] = [Frame(url=initial_url, entries=initial_entries)]

    @property
    def current(self) -> Frame:
        return self._frames[-1]

    def push(self, url: str, entries: List[Entry]) -> None:
        self._frames.append(Frame(url=url, entries=entries))

    def pop(self) -> bool:
        if len(self._frames) <= 1:
            return False
        self._frames.pop()
        return True

    def at_root(self) -> bool:
        return len(self._frames) <= 1
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest test_nginx_tui -v`
Expected: all tests PASS (32 total so far).

- [ ] **Step 5: Commit**

```bash
git add nginx_tui.py test_nginx_tui.py
git commit -m "feat: add cached history stack for back navigation"
```

---

## Task 5: Keypress-to-action mapping and row formatting

**Files:**
- Modify: `nginx_tui.py` (append)
- Modify: `test_nginx_tui.py` (append)

**Interfaces:**
- Consumes: `curses`, `unicodedata` (Task 1 imports), `Entry` (Task 2)
- Produces:
  - `Action` enum: `MOVE_UP, MOVE_DOWN, PAGE_UP, PAGE_DOWN, ACTIVATE, BACK, QUIT`
  - `resolve_action(key: int) -> Optional[Action]`
  - `_display_width(text: str) -> int` — terminal column width, counting East-Asian wide/fullwidth characters (e.g. Chinese) as 2
  - `_truncate(text: str, max_width: int) -> str` — cut `text` to fit within `max_width` display columns
  - `_ljust(text: str, width: int) -> str` / `_rjust(text: str, width: int) -> str` — display-width-aware padding (drop-in replacements for `str.ljust`/`str.rjust`, which count characters, not terminal columns)
  - `format_row(entry: Entry, name_width: int) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `test_nginx_tui.py`:

```python
from nginx_tui import Action, _display_width, format_row, resolve_action


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

    def test_enter_variants_activate(self):
        self.assertEqual(resolve_action(10), Action.ACTIVATE)
        self.assertEqual(resolve_action(13), Action.ACTIVATE)
        self.assertEqual(resolve_action(curses.KEY_ENTER), Action.ACTIVATE)

    def test_backspace_variants_go_back(self):
        self.assertEqual(resolve_action(curses.KEY_BACKSPACE), Action.BACK)
        self.assertEqual(resolve_action(127), Action.BACK)
        self.assertEqual(resolve_action(curses.KEY_LEFT), Action.BACK)
        self.assertEqual(resolve_action(ord("u")), Action.BACK)

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest test_nginx_tui -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_action' from 'nginx_tui'`.

- [ ] **Step 3: Append the implementation to `nginx_tui.py`**

```python
class Action(enum.Enum):
    MOVE_UP = "move_up"
    MOVE_DOWN = "move_down"
    PAGE_UP = "page_up"
    PAGE_DOWN = "page_down"
    ACTIVATE = "activate"
    BACK = "back"
    QUIT = "quit"


_KEY_ACTIONS = {
    curses.KEY_UP: Action.MOVE_UP,
    ord("k"): Action.MOVE_UP,
    curses.KEY_DOWN: Action.MOVE_DOWN,
    ord("j"): Action.MOVE_DOWN,
    curses.KEY_PPAGE: Action.PAGE_UP,
    curses.KEY_NPAGE: Action.PAGE_DOWN,
    10: Action.ACTIVATE,
    13: Action.ACTIVATE,
    curses.KEY_ENTER: Action.ACTIVATE,
    curses.KEY_BACKSPACE: Action.BACK,
    127: Action.BACK,
    curses.KEY_LEFT: Action.BACK,
    ord("u"): Action.BACK,
    ord("q"): Action.QUIT,
    ord("Q"): Action.QUIT,
}


def resolve_action(key: int) -> Optional[Action]:
    return _KEY_ACTIONS.get(key)


def _display_width(text: str) -> int:
    """Terminal column width: East-Asian wide/fullwidth characters count as 2."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _truncate(text: str, max_width: int) -> str:
    result = ""
    for ch in text:
        if _display_width(result + ch) > max_width:
            break
        result += ch
    return result


def _ljust(text: str, width: int) -> str:
    return text + " " * max(width - _display_width(text), 0)


def _rjust(text: str, width: int) -> str:
    return " " * max(width - _display_width(text), 0) + text


def format_row(entry: Entry, name_width: int) -> str:
    name = entry.name
    if _display_width(name) > name_width:
        name = _truncate(name, name_width - 1) + "…"
    return _ljust(name, name_width)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest test_nginx_tui -v`
Expected: all tests PASS (42 total so far).

- [ ] **Step 5: Commit**

```bash
git add nginx_tui.py test_nginx_tui.py
git commit -m "feat: add keypress-to-action mapping and row formatting"
```

---

## Task 6: BrowserApp — interactive browsing and download

**Files:**
- Modify: `nginx_tui.py` (append)

No new automated tests: `curses` needs a real terminal, so this task is verified manually against a local nginx instance (per the design doc's approved testing approach). All logic that *could* be isolated already has unit tests from Tasks 2-5; this task only wires that logic to the terminal.

**Interfaces:**
- Consumes: `Entry`, `parse_index`, `format_size` (Task 2); `fetch_index`, `download_file` (Task 3); `Frame`, `NavigationStack` (Task 4); `Action`, `resolve_action`, `format_row` (Task 5); `time`, `curses`, `urllib.error`, `PROGRESS_THROTTLE_SECONDS` (Task 1)
- Produces: `BrowserApp(stdscr, start_url: str, output_dir: str)` with `.stack: Optional[NavigationStack]`, `.status: str`, `.run() -> None`

- [ ] **Step 1: Append the `BrowserApp` class to `nginx_tui.py`**

```python
class BrowserApp:
    def __init__(self, stdscr, start_url: str, output_dir: str) -> None:
        self.stdscr = stdscr
        self.output_dir = output_dir
        self.status = ""
        self.stack: Optional[NavigationStack] = None
        self.dir_attr = curses.A_BOLD
        self._init_colors()
        curses.curs_set(0)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass
        self.stdscr.keypad(True)
        self._load(start_url, push=False)

    def _init_colors(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLUE, -1)
        self.dir_attr = curses.color_pair(1) | curses.A_BOLD

    def _load(self, url: str, push: bool) -> bool:
        self.status = f"正在加载 {url} ..."
        self._draw()
        try:
            html_text = fetch_index(url)
            entries = parse_index(html_text, url)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.status = f"加载失败 {url}：{exc}"
            return False
        if push and self.stack is not None:
            self.stack.push(url, entries)
        else:
            self.stack = NavigationStack(url, entries)
        self.status = ""
        return True

    def run(self) -> None:
        while True:
            self._draw()
            key = self.stdscr.getch()
            if key == curses.KEY_RESIZE:
                continue
            if key == curses.KEY_MOUSE:
                self._handle_mouse()
                continue
            action = resolve_action(key)
            if action is None:
                continue
            if action == Action.QUIT:
                return
            elif action == Action.MOVE_UP:
                self._move_selection(-1)
            elif action == Action.MOVE_DOWN:
                self._move_selection(1)
            elif action == Action.PAGE_UP:
                self._move_selection(-self._page_size())
            elif action == Action.PAGE_DOWN:
                self._move_selection(self._page_size())
            elif action == Action.BACK:
                self._go_back()
            elif action == Action.ACTIVATE:
                self._activate_selected()

    def _page_size(self) -> int:
        height, _ = self.stdscr.getmaxyx()
        return max(height - 3, 1)

    def _move_selection(self, delta: int) -> None:
        frame = self.stack.current
        if not frame.entries:
            return
        frame.selected = max(0, min(len(frame.entries) - 1, frame.selected + delta))
        self._adjust_offset()

    def _adjust_offset(self) -> None:
        frame = self.stack.current
        visible = self._page_size()
        if frame.selected < frame.offset:
            frame.offset = frame.selected
        elif frame.selected >= frame.offset + visible:
            frame.offset = frame.selected - visible + 1

    def _go_back(self) -> None:
        if not self.stack.pop():
            self.status = "已在根目录"

    def _handle_mouse(self) -> None:
        try:
            _, _, my, _, bstate = curses.getmouse()
        except curses.error:
            return
        if not (bstate & curses.BUTTON1_CLICKED or bstate & curses.BUTTON1_PRESSED):
            return
        height, _ = self.stdscr.getmaxyx()
        if my < 2 or my >= height - 1:
            return
        frame = self.stack.current
        row_index = frame.offset + (my - 2)
        if 0 <= row_index < len(frame.entries):
            frame.selected = row_index
            self._activate_selected()

    def _activate_selected(self) -> None:
        frame = self.stack.current
        if not frame.entries:
            return
        entry = frame.entries[frame.selected]
        if entry.is_dir:
            self._load(entry.url, push=True)
        else:
            self._download(entry)

    def _download(self, entry: Entry) -> None:
        # basename() guards against path traversal via a crafted href
        dest_path = os.path.join(self.output_dir, os.path.basename(entry.name))
        if os.path.exists(dest_path):
            if not self._confirm_overwrite(dest_path):
                self.status = "已取消下载"
                return

        last_draw = 0.0

        def on_progress(downloaded: int, total: Optional[int]) -> None:
            nonlocal last_draw
            now = time.monotonic()
            if now - last_draw < PROGRESS_THROTTLE_SECONDS and (total is None or downloaded < total):
                return
            last_draw = now
            self.status = self._format_progress(entry.name, downloaded, total)
            self._draw()

        try:
            download_file(entry.url, dest_path, progress_cb=on_progress)
        except KeyboardInterrupt:
            self.status = "下载已中断"
        except (urllib.error.URLError, OSError) as exc:
            self.status = f"下载失败：{exc}"
        else:
            self.status = f"已下载到 {dest_path}"

    @staticmethod
    def _format_progress(name: str, downloaded: int, total: Optional[int]) -> str:
        if total:
            percent = int(downloaded * 100 / total)
            bar_width = 20
            filled = int(bar_width * downloaded / total)
            bar = "=" * filled + "-" * (bar_width - filled)
            return f"下载中 {name} [{bar}] {percent}% {format_size(downloaded)}/{format_size(total)}"
        return f"下载中 {name} {format_size(downloaded)}"

    def _confirm_overwrite(self, dest_path: str) -> bool:
        self.status = f"{os.path.basename(dest_path)} 已存在，是否覆盖？(y/n)"
        self._draw()
        key = self.stdscr.getch()
        return key in (ord("y"), ord("Y"))

    def _draw(self) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        if self.stack is None:
            shown = _truncate(self.status, width - 1)
            self.stdscr.addnstr(0, 0, shown, len(shown))
            self.stdscr.refresh()
            return

        frame = self.stack.current
        breadcrumb = _truncate(frame.url, width - 1)
        self.stdscr.addnstr(0, 0, breadcrumb, len(breadcrumb), curses.A_REVERSE)

        size_width, mtime_width = 8, 16
        name_width = max(width - size_width - mtime_width - 3, 8)
        header = f"{_ljust('名称', name_width)} {_rjust('大小', size_width)} {_rjust('修改时间', mtime_width)}"
        self.stdscr.addnstr(1, 0, header, len(header), curses.A_BOLD)

        visible = self._page_size()
        for row, entry in enumerate(frame.entries[frame.offset: frame.offset + visible]):
            y = row + 2
            name_col = format_row(entry, name_width)
            size_col = _rjust(format_size(entry.size_bytes), size_width)
            mtime_col = _rjust(entry.mtime or "", mtime_width)
            line = f"{name_col} {size_col} {mtime_col}"
            attr = curses.A_REVERSE if frame.offset + row == frame.selected else curses.A_NORMAL
            if entry.is_dir:
                attr |= self.dir_attr
            self.stdscr.addnstr(y, 0, line, len(line), attr)

        status = self.status or "↑/↓ 移动   Enter/点击 进入或下载   Backspace 返回上级   q 退出"
        shown_status = _truncate(status, width - 1)
        self.stdscr.addnstr(height - 1, 0, shown_status, len(shown_status), curses.A_DIM)
        self.stdscr.refresh()
```

- [ ] **Step 2: Run the existing automated test suite to confirm no regressions**

Run: `python3 -m unittest test_nginx_tui -v`
Expected: all 42 tests still PASS (this task added no new automated tests, so the count doesn't change).

- [ ] **Step 3: Manually verify against a local nginx instance**

Point your docker nginx at a test directory tree with (create these under the directory it serves, with `autoindex on;` in the matching `location`):
- A nested subdirectory at least 2 levels deep
- A file with a space in its name and a file with Chinese characters in its name
- An empty subdirectory
- A file of at least 5 MB (to see the progress bar move)

Add a temporary bottom-of-file smoke-test entry point so the class is runnable, run it, and check off each item below:

```python
if __name__ == "__main__":
    import curses as _curses
    _curses.wrapper(lambda stdscr: BrowserApp(stdscr, "http://127.0.0.1:8080/", os.getcwd()).run())
```

Run: `python3 nginx_tui.py` (temporary manual entry point above — Task 7 replaces this with the real CLI-driven `main()`)

Checklist:
- [ ] Arrow keys and `j`/`k` move the selection; `PageUp`/`PageDown` jump by a page
- [ ] Enter on a directory enters it; the breadcrumb updates to the new URL
- [ ] Backspace returns to the parent directory instantly (no visible reload/flicker) with the previous selection restored
- [ ] Pressing Backspace repeatedly at the root shows "已在根目录" and does not crash or exit
- [ ] The empty subdirectory shows a header row with no entries, and Backspace still works from there
- [ ] The space-containing and Chinese filenames render correctly and download to the right local names
- [ ] Directories are visually distinguished from files (color/bold)
- [ ] Mouse click on a directory row enters it; mouse click on a file row downloads it (skip this check if your terminal doesn't report mouse events)
- [ ] Downloading the 5 MB+ file shows a moving progress bar with byte counts
- [ ] Re-downloading the same file prompts "已存在，是否覆盖？(y/n)"; `n` (or any non-`y` key) cancels without touching the existing file; `y` re-downloads it
- [ ] Ctrl-C during an in-progress download leaves no `<file>.part` file behind in the output directory
- [ ] Resizing the terminal window while the app is running does not crash it
- [ ] `q` quits and returns the terminal to normal (no leftover garbled state)

- [ ] **Step 4: Remove the temporary manual entry point**

Delete the `if __name__ == "__main__":` block added in Step 3 (Task 7 adds the real one).

- [ ] **Step 5: Commit**

```bash
git add nginx_tui.py
git commit -m "feat: add interactive curses browser and download flow"
```

---

## Task 7: CLI entry point and end-to-end verification

**Files:**
- Modify: `nginx_tui.py` (append + `chmod +x`)

**Interfaces:**
- Consumes: `parse_args` (Task 1), `BrowserApp` (Task 6)
- Produces: `main(argv: Optional[List[str]] = None) -> None`; script becomes directly executable

- [ ] **Step 1: Append `main()` to `nginx_tui.py`**

```python
def main(argv: Optional[List[str]] = None) -> None:
    # Must run before curses.wrapper()/initscr() so the window's encoding
    # resolves to the process locale (needed for the Chinese UI text to render).
    locale.setlocale(locale.LC_ALL, "")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    os.makedirs(args.output_dir, exist_ok=True)
    error_holder: List[str] = []

    def _run(stdscr):
        app = BrowserApp(stdscr, args.url, args.output_dir)
        if app.stack is None:
            error_holder.append(app.status)
            return
        app.run()

    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        sys.exit(130)

    if error_holder:
        print(error_holder[0], file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the full automated test suite**

Run: `python3 -m unittest test_nginx_tui -v`
Expected: all 42 tests PASS.

- [ ] **Step 3: Make the script executable**

```bash
chmod +x nginx_tui.py
```

- [ ] **Step 4: Manually verify the CLI, using the same local nginx instance from Task 6**

- [ ] `python3 nginx_tui.py` (no arguments) prints full help text and exits with status 0
- [ ] `python3 nginx_tui.py -h` prints the same help text
- [ ] `./nginx_tui.py 127.0.0.1:8080` (no scheme, run as an executable) launches the browser against `http://127.0.0.1:8080`
- [ ] `python3 nginx_tui.py http://127.0.0.1:8080/ --output-dir /tmp/nginx-tui-downloads` downloads files into that directory instead of the current one
- [ ] Pointing the script at a URL that refuses the connection (e.g., a closed port) shows an error message on the initial screen instead of a raw Python traceback, and exits non-zero
- [ ] Re-run the full Task 6 manual checklist once end-to-end through the real CLI entry point (not the temporary one) to confirm nothing regressed
- [ ] Ctrl-C while idle (not mid-download) exits cleanly with no traceback printed

- [ ] **Step 5: Commit**

```bash
git add nginx_tui.py
git commit -m "feat: add CLI entry point for nginx TUI browser"
```
