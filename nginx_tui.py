#!/usr/bin/env python3
import argparse
import curses
import datetime
import enum
import html.parser
import http.client
import locale
import os
import re
import ssl
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
_CANCEL_HINT = "（Ctrl-C 取消）"
# A directory listing page is plain text with light markup -- even a huge
# directory stays well under this. Bounds how much a URL that turns out to
# not actually be a directory listing (misconfigured or hostile server) can
# force into memory before fetch_index gives up on it.
_MAX_INDEX_BODY_SIZE = 10 * 1024 * 1024


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def normalize_url(raw_url: str) -> str:
    # urlparse().scheme misparses "localhost:8000/x" as scheme="localhost";
    # requiring "://" right after the scheme name avoids that false positive.
    raw_url = raw_url.strip()
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
    parser.add_argument(
        "-k", "--insecure", action="store_true",
        help="跳过 HTTPS 证书校验（用于自签名证书的服务器；有中间人攻击风险，仅在信任目标网络时使用）",
    )
    return parser


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = build_arg_parser()
    if not argv:
        parser.print_help()
        raise SystemExit(0)
    args = parser.parse_args(argv)
    args.url = normalize_url(args.url)
    args.output_dir = os.path.expanduser(args.output_dir)
    return args


@dataclass
class Entry:
    name: str
    href: str
    url: str
    is_dir: bool
    size_bytes: Optional[int]
    mtime: Optional[str]
    # Set only when the server reported a rounded, unit-suffixed size
    # (autoindex_exact_size off, e.g. "24M") instead of an exact byte count.
    # size_bytes stays None in that case; this is shown as-is instead.
    size_raw: Optional[str] = None


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
            if self._current_href is not None:
                # A new <a> opened before the previous one closed (malformed/
                # nested markup) -- flush the still-open outer anchor first
                # instead of silently discarding it.
                self.anchors.append((self._current_href, "".join(self._current_text)))
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
# `size` also accepts autoindex_exact_size off's rounded K/M/G/T-suffixed
# form (e.g. "24M") so that a large file's very real, present date isn't
# lost just because its size isn't an exact byte count -- _parse_meta_by_href
# still discards a unit-suffixed size rather than fabricate a byte count.
_LINE_META_RE = re.compile(
    r'<a\s+href="(?P<href>[^"]*)"[^>]*>.*?</a>'
    r'\s*(?:(?P<date>\d{2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2}))?'
    r'\s*(?P<size>-|\d+(?:\.\d+)?[KMGT]?)?\s*$'
)

_SKIP_HREFS = {"../", "..", "/"}
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
_MTIME_RE = re.compile(
    r"^(?P<day>\d{2})-(?P<mon>[A-Za-z]{3})-(?P<year>\d{4})\s+"
    r"(?P<hour>\d{2}):(?P<minute>\d{2})$"
)


def _parse_meta_by_href(html_text: str) -> Dict[str, Tuple[Optional[str], Optional[int], Optional[str]]]:
    meta: Dict[str, Tuple[Optional[str], Optional[int], Optional[str]]] = {}
    for line in html_text.splitlines():
        match = _LINE_META_RE.search(line)
        if not match or match.group("href") in meta:
            continue
        size_raw = match.group("size")
        # A unit-suffixed size (autoindex_exact_size off, e.g. "24M") is a
        # rounded display value, not an exact byte count -- leave size_bytes
        # unknown rather than fabricate a precise value, but still show the
        # server's own text as-is.
        size_bytes = int(size_raw) if size_raw and size_raw.isdigit() else None
        size_display = size_raw if size_raw and size_bytes is None and size_raw != "-" else None
        # _AnchorExtractor's href comes from HTMLParser, which entity-decodes
        # attribute values -- match that here so a page whose hrefs contain
        # entities (e.g. "a&amp;b.txt") looks up under the same key instead
        # of silently missing its size/mtime.
        meta[html.unescape(match.group("href"))] = (match.group("date"), size_bytes, size_display)
    return meta


def format_size(size_bytes: Optional[int]) -> str:
    if size_bytes is None:
        return "-"
    size = float(size_bytes)
    for unit in ("B", "K", "M", "G"):
        if size < 1024:
            return f"{int(size)}B" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


def _format_entry_size(entry: Entry) -> str:
    if entry.size_bytes is not None:
        return format_size(entry.size_bytes)
    if entry.size_raw:
        return entry.size_raw
    return "-"


def _format_duration(seconds: float) -> str:
    total_seconds = max(int(seconds), 0)
    minutes, secs = divmod(total_seconds, 60)
    return f"{minutes}:{secs:02d}"


def format_mtime(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    # The regex's \s+ between date and time matches a literal tab, which
    # survives into every fallback return below (unrecognized month,
    # non-calendar date) -- sanitize before those can reach curses addstr
    # unsanitized like the other server-controlled display strings.
    raw = _sanitize_display_text(raw)
    match = _MTIME_RE.match(raw)
    if not match:
        return raw
    month = _MONTHS.get(match.group("mon"))
    if month is None:
        return raw
    try:
        parsed = datetime.datetime(
            int(match.group("year")),
            month,
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
        )
    except ValueError:
        # Syntactically matches (2-digit day/hour/minute) but not a real date
        # (e.g. day 31 in February) -- degrade this one entry, don't let it
        # abort parsing of the whole directory listing.
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
        date_raw, size_bytes, size_raw = meta_by_href.get(href, (None, None, None))
        entries.append(Entry(
            name=_sanitize_display_text(urllib.parse.unquote(href)),
            href=href,
            url=urllib.parse.urljoin(base_url, href),
            is_dir=is_dir,
            size_bytes=None if is_dir else size_bytes,
            mtime=format_mtime(date_raw),
            size_raw=None if is_dir else size_raw,
        ))
    return entries


def _ssl_context(insecure: bool) -> Optional[ssl.SSLContext]:
    if not insecure:
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def fetch_index(url: str, timeout: float = CONNECT_TIMEOUT, insecure: bool = False) -> Tuple[str, str]:
    # Returns (html, final_url): nginx 301-redirects a directory request that's
    # missing its trailing slash, and relative hrefs in the listing must be
    # resolved against that final URL, not the one originally requested.
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context(insecure)) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        chunks = []
        total = 0
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_INDEX_BODY_SIZE:
                raise ValueError(
                    f"目录列表页过大（超过 {_MAX_INDEX_BODY_SIZE // (1024 * 1024)}MB），可能不是目录列表"
                )
        html_text = b"".join(chunks).decode(charset, errors="replace")
        return html_text, response.geturl()


def download_file(
    url: str,
    dest_path: str,
    progress_cb: Optional[Callable[[int, Optional[int]], None]] = None,
    timeout: float = CONNECT_TIMEOUT,
    insecure: bool = False,
) -> None:
    part_path = dest_path + ".part"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    opened_part = False
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context(insecure)) as response:
            total_header = response.headers.get("Content-Length")
            total_bytes = int(total_header) if total_header is not None else None
            if total_bytes is not None and total_bytes < 0:
                # A malformed negative value isn't a real byte count -- treat
                # it as unknown, or the completeness check below would flag
                # every successful download as incomplete and delete it.
                total_bytes = None
            downloaded = 0
            with open(part_path, "wb") as out_file:
                # Only set once open() has actually succeeded -- a failed
                # open() must never trigger the cleanup below to delete a
                # pre-existing file this call never touched. (A signal
                # landing in the handful of bytecodes between open()
                # returning and this line executing is a real but
                # astronomically unlikely race, not worth trading this
                # guarantee away for.)
                opened_part = True
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    out_file.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb is not None:
                        progress_cb(downloaded, total_bytes)
            # HTTPResponse.read() returns b"" (not an exception) when the
            # connection closes before Content-Length bytes arrive -- without
            # this check a dropped connection silently commits a truncated
            # file as if the download succeeded.
            if total_bytes is not None and downloaded != total_bytes:
                raise OSError(f"下载不完整：已接收 {downloaded}/{total_bytes} 字节，连接可能提前断开")
        os.replace(part_path, dest_path)
    except BaseException:
        # Only remove part_path if this call actually created/truncated it --
        # never delete a pre-existing file we never touched (e.g. the request
        # itself failed before open() ran).
        if opened_part and os.path.exists(part_path):
            os.remove(part_path)
        raise


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


class Action(enum.Enum):
    MOVE_UP = "move_up"
    MOVE_DOWN = "move_down"
    PAGE_UP = "page_up"
    PAGE_DOWN = "page_down"
    REFRESH = "refresh"
    ACTIVATE = "activate"
    ENTER_DIR = "enter_dir"
    BACK = "back"
    QUIT = "quit"


_KEY_ACTIONS = {
    curses.KEY_UP: Action.MOVE_UP,
    ord("k"): Action.MOVE_UP,
    curses.KEY_DOWN: Action.MOVE_DOWN,
    ord("j"): Action.MOVE_DOWN,
    curses.KEY_PPAGE: Action.PAGE_UP,
    curses.KEY_NPAGE: Action.PAGE_DOWN,
    ord("r"): Action.REFRESH,
    ord("R"): Action.REFRESH,
    10: Action.ACTIVATE,
    13: Action.ACTIVATE,
    curses.KEY_ENTER: Action.ACTIVATE,
    curses.KEY_RIGHT: Action.ENTER_DIR,
    curses.KEY_BACKSPACE: Action.BACK,
    127: Action.BACK,
    curses.KEY_LEFT: Action.BACK,
    ord("u"): Action.BACK,
    27: Action.BACK,  # Esc; unrelated to _confirm_overwrite, which has its own y/n getch loop
    ord("q"): Action.QUIT,
    ord("Q"): Action.QUIT,
}

if hasattr(curses, "KEY_F5"):
    _KEY_ACTIONS[curses.KEY_F5] = Action.REFRESH


def resolve_action(key: int) -> Optional[Action]:
    return _KEY_ACTIONS.get(key)


def _display_width(text: str) -> int:
    """Terminal column width: East-Asian wide/fullwidth chars count 2, combining marks 0."""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_display_text(text: str) -> str:
    # Percent-decoded server data (a filename, a URL path segment) can smuggle
    # C0 control bytes -- newline, tab, carriage return -- straight into
    # curses addstr, which treats them as real cursor motion instead of
    # literal characters and corrupts the layout. Replace with a visible
    # placeholder instead.
    return _CONTROL_CHAR_RE.sub("�", text)


def _flush_stale_input() -> None:
    # Keystrokes/clicks made during a blocking load/refresh/download sit in
    # the input queue and otherwise get "replayed" against the just-drawn
    # screen the moment it returns -- e.g. a stray click landing on whatever
    # entry now happens to be under those coordinates. curses.flushinp()
    # requires initscr() to have run first, which unit tests building a
    # BrowserApp around a fake stdscr never do -- degrade silently there.
    try:
        curses.flushinp()
    except curses.error:
        pass


def _truncate(text: str, max_width: int) -> str:
    result = ""
    for ch in text:
        if _display_width(result + ch) > max_width:
            break
        result += ch
    return result


def _truncate_end(text: str, max_width: int) -> str:
    """Like _truncate, but keeps the tail of the string instead of the head."""
    result = ""
    for ch in reversed(text):
        if _display_width(ch + result) > max_width:
            break
        result = ch + result
    return result


def _truncate_middle(text: str, max_width: int) -> str:
    # For a path, the head (scheme/host) and the tail (the deepest, most
    # specific directory) are both more useful than whatever's in between --
    # cutting from the middle keeps both instead of only ever showing the head.
    if _display_width(text) <= max_width:
        return text
    if max_width <= 1:
        return _truncate(text, max_width)
    available = max_width - 1  # reserve 1 column for the ellipsis
    head_width = (available + 1) // 2
    tail_width = available - head_width
    return f"{_truncate(text, head_width)}…{_truncate_end(text, tail_width)}"


def _url_label(url: str) -> str:
    # A short name for status messages ("正在加载 subdir ...") instead of the
    # full URL, which can be arbitrarily long for deeply nested paths.
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    name = urllib.parse.unquote(path.rsplit("/", 1)[-1]) if path else ""
    return _sanitize_display_text(name or urllib.parse.unquote(url))


def _ljust(text: str, width: int) -> str:
    return text + " " * max(width - _display_width(text), 0)


def _rjust(text: str, width: int) -> str:
    return " " * max(width - _display_width(text), 0) + text


def format_row(entry: Entry, name_width: int) -> str:
    name = entry.name
    if _display_width(name) > name_width:
        name = _truncate(name, name_width - 1) + "…"
    return _ljust(name, name_width)


_SIZE_COL_WIDTH = 8
_MTIME_COL_WIDTH = 16
_MIN_NAME_COL_WIDTH = 8
# Below this, the name column would be squeezed below its floor and the row
# no longer fits -- render a centered "too small" message instead of a
# garbled, wrapped listing. The trailing +1 reserves the same 1-column right
# margin the breadcrumb and footer already keep (both truncate to width - 1);
# without it, the one width where the name column sits exactly at its floor
# would be the only row that fills the terminal edge to edge.
_MIN_TERMINAL_WIDTH = _MIN_NAME_COL_WIDTH + 1 + _SIZE_COL_WIDTH + 1 + _MTIME_COL_WIDTH + 1


class BrowserApp:
    def __init__(self, stdscr, start_url: str, output_dir: str, insecure: bool = False) -> None:
        self.stdscr = stdscr
        self.output_dir = output_dir
        self.insecure = insecure
        self.status = ""
        self.status_expires_at: Optional[float] = None
        self.stack: Optional[NavigationStack] = None
        # Only ever set for the startup load (push=False is exclusive to it) --
        # lets main() tell "user cancelled with Ctrl-C" apart from "load
        # actually failed" when deciding the process exit code.
        self.startup_cancelled = False
        self.dir_attr = curses.A_BOLD
        self.header_attr = curses.A_REVERSE | curses.A_BOLD
        self.footer_attr = curses.A_BOLD
        self._init_colors()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass
        self.stdscr.keypad(True)
        self.stdscr.timeout(100)
        self._load(start_url, push=False)

    def _set_status(self, text: str, timeout: Optional[float] = None) -> None:
        # Exception text (e.g. an HTTPError's reason phrase) can carry a
        # server-controlled literal control character straight through --
        # sanitize at this single choke point rather than at every call site.
        self.status = _sanitize_display_text(text)
        self.status_expires_at = None if timeout is None else time.monotonic() + timeout

    def _clear_status(self) -> None:
        self.status = ""
        self.status_expires_at = None

    def _status_visible(self) -> bool:
        if not self.status:
            return False
        if self.status_expires_at is None:
            return True
        if time.monotonic() <= self.status_expires_at:
            return True
        self._clear_status()
        return False

    def _init_colors(self) -> None:
        if not curses.has_colors():
            return
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_BLUE, -1)
            self.dir_attr = curses.color_pair(1) | curses.A_BOLD
            # Fixed white-on-blue instead of A_REVERSE on the terminal's
            # default colors, which can render low-contrast in some dark
            # color schemes.
            curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLUE)
            self.header_attr = curses.color_pair(2) | curses.A_BOLD
            # Bold cyan instead of A_DIM for the footer hint — dimming
            # reduces contrast further and can render unreadable on dark
            # color schemes.
            curses.init_pair(3, curses.COLOR_CYAN, -1)
            self.footer_attr = curses.color_pair(3) | curses.A_BOLD
        except curses.error:
            # A terminal that claims color support but can't actually set up
            # these pairs falls back to the plain attributes set in __init__.
            pass

    def _load(self, url: str, push: bool, label: Optional[str] = None) -> bool:
        display_label = label if label is not None else _url_label(url)
        self._set_status(f"正在加载 {display_label} ...{_CANCEL_HINT}")
        try:
            self._draw()
            html_text, final_url = fetch_index(url, insecure=self.insecure)
            entries = parse_index(html_text, final_url)
        except KeyboardInterrupt:
            # Cancelling a load never pushed a new frame, so the caller is
            # left showing whatever was already on screen (the parent level
            # when entering a subdirectory, or nothing yet at startup).
            if not push:
                self.startup_cancelled = True
            self._set_status(f"已取消加载 {display_label}", timeout=2.0)
            _flush_stale_input()
            return False
        except (urllib.error.URLError, OSError, ValueError, LookupError, http.client.HTTPException) as exc:
            self._set_status(f"加载失败 {display_label}：{exc}", timeout=2.0)
            _flush_stale_input()
            return False
        _flush_stale_input()
        if push and self.stack is not None:
            self.stack.push(final_url, entries)
        else:
            self.stack = NavigationStack(final_url, entries)
        self._clear_status()
        return True

    def run(self) -> None:
        while True:
            try:
                self._draw()
                key = self.stdscr.getch()
                # The try covers dispatch too, not just draw+getch: load/
                # refresh/download/confirm each already handle their own
                # Ctrl-C internally and never raise out of here, so widening
                # this only closes the gap for the plain navigation calls
                # below (move/page/back), which had no protection at all.
                if key == -1:
                    continue
                if key == curses.KEY_RESIZE:
                    if self.stack is not None:
                        self._clamp_viewport()
                    continue
                if self.stack is None:
                    if resolve_action(key) == Action.QUIT:
                        return
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
                    self._page_move(-1)
                elif action == Action.PAGE_DOWN:
                    self._page_move(1)
                elif action == Action.REFRESH:
                    self._refresh_current()
                elif action == Action.BACK:
                    self._go_back()
                elif action == Action.ACTIVATE:
                    self._activate_selected()
                elif action == Action.ENTER_DIR:
                    self._enter_dir_selected()
            except KeyboardInterrupt:
                # Idle browsing has no in-flight operation for Ctrl-C to
                # cancel -- ignore it rather than letting it fall through to
                # main()'s handler and kill the whole TUI.
                continue

    def _page_size(self) -> int:
        height, _ = self.stdscr.getmaxyx()
        return max(height - 3, 1)

    def _move_selection(self, delta: int) -> None:
        frame = self.stack.current
        if not frame.entries:
            return
        frame.selected = max(0, min(len(frame.entries) - 1, frame.selected + delta))
        self._adjust_offset()

    def _page_move(self, direction: int) -> None:
        # Shift both selected and offset by a full page directly, instead of
        # routing through _adjust_offset()'s minimal-scroll logic (designed
        # for single-line moves) -- that only nudges the viewport by however
        # far selected overshot it, which is just 1 line whenever selected
        # started exactly on the edge closest to the direction of travel
        # (e.g. the very first PageDown from the top of the list).
        frame = self.stack.current
        if not frame.entries:
            return
        visible = self._page_size()
        step = direction * visible
        frame.selected = max(0, min(len(frame.entries) - 1, frame.selected + step))
        max_offset = max(len(frame.entries) - visible, 0)
        frame.offset = max(0, min(max_offset, frame.offset + step))

    def _adjust_offset(self) -> None:
        frame = self.stack.current
        visible = self._page_size()
        if frame.selected < frame.offset:
            frame.offset = frame.selected
        elif frame.selected >= frame.offset + visible:
            frame.offset = frame.selected - visible + 1

    def _clamp_viewport(self) -> None:
        # Fully re-validates selected/offset against the current frame's
        # entry count and page size -- unlike _adjust_offset() (which only
        # ever nudges offset towards an already-known-good selected), this
        # also pulls offset back down when it's scrolled past where the
        # entry count / page size would leave blank rows at the bottom.
        # Needed anywhere selected/offset can go stale relative to entries
        # or page size out from under a single-line move: refresh shrinking
        # the list, a resize, or restoring a frame via _go_back.
        frame = self.stack.current
        if frame.entries:
            frame.selected = max(0, min(len(frame.entries) - 1, frame.selected))
        else:
            frame.selected = 0
        visible = self._page_size()
        max_offset = max(len(frame.entries) - visible, 0)
        frame.offset = max(0, min(max_offset, frame.offset))
        self._adjust_offset()
        frame.offset = max(0, min(max_offset, frame.offset))

    def _go_back(self) -> None:
        if not self.stack.pop():
            self._set_status("已在根目录", timeout=1.5)
            return
        self._clamp_viewport()

    def _refresh_current(self) -> None:
        frame = self.stack.current
        label = _url_label(frame.url)
        self._set_status(f"正在刷新 {label} ...{_CANCEL_HINT}")
        try:
            self._draw()
            html_text, final_url = fetch_index(frame.url, insecure=self.insecure)
            entries = parse_index(html_text, final_url)
        except KeyboardInterrupt:
            # frame.entries is untouched, so the current (stale) listing stays visible.
            self._set_status(f"已取消刷新 {label}", timeout=2.0)
            _flush_stale_input()
            return
        except (urllib.error.URLError, OSError, ValueError, LookupError, http.client.HTTPException) as exc:
            self._set_status(f"刷新失败 {label}：{exc}", timeout=2.0)
            _flush_stale_input()
            return
        _flush_stale_input()
        frame.url = final_url
        frame.entries = entries
        self._clamp_viewport()
        self._clear_status()

    def _handle_mouse(self) -> None:
        try:
            _, _, my, _, bstate = curses.getmouse()
        except curses.error:
            return
        if not (bstate & curses.BUTTON1_CLICKED or bstate & curses.BUTTON1_PRESSED):
            return
        height, width = self.stdscr.getmaxyx()
        # Mirrors _draw_unsafe's own too-small check -- when the listing
        # isn't actually drawn (only the centered "too small" message is),
        # there's nothing visible at these coordinates to click on.
        if height < 4 or width < _MIN_TERMINAL_WIDTH:
            return
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
            self._load(entry.url, push=True, label=entry.name)
        else:
            self._download(entry)

    def _enter_dir_selected(self) -> None:
        frame = self.stack.current
        if not frame.entries:
            return
        entry = frame.entries[frame.selected]
        if entry.is_dir:
            self._load(entry.url, push=True, label=entry.name)

    def _download(self, entry: Entry) -> None:
        # basename() guards against path traversal via a crafted href
        basename = os.path.basename(entry.name)
        if not basename or basename in (".", ".."):
            self._set_status(f"无法下载：文件名无效（{entry.name!r}）", timeout=2.0)
            return
        dest_path = os.path.join(self.output_dir, basename)
        if os.path.isdir(dest_path):
            # Answering the overwrite prompt "y" here would still fail at the
            # final os.replace (IsADirectoryError) only after the full file
            # has already been downloaded -- reject up front instead.
            self._set_status(f"无法下载：{dest_path} 是一个目录", timeout=2.0)
            return
        # lexists (not exists) so a pre-existing broken symlink at dest_path
        # still counts as "something is already there" -- exists() follows
        # symlinks and returns False for a dangling one, which would
        # otherwise skip the prompt entirely.
        # Also guard the ".part" staging path -- download_file truncates it
        # unconditionally, so without this check a pre-existing (unrelated)
        # "<name>.part" file would be silently destroyed with no prompt.
        if os.path.lexists(dest_path) or os.path.lexists(dest_path + ".part"):
            if not self._confirm_overwrite(dest_path):
                self._set_status("已取消下载", timeout=1.5)
                return

        last_draw = 0.0
        start_time = time.monotonic()

        def on_progress(downloaded: int, total: Optional[int]) -> None:
            nonlocal last_draw
            now = time.monotonic()
            if now - last_draw < PROGRESS_THROTTLE_SECONDS and (total is None or downloaded < total):
                return
            last_draw = now
            elapsed = now - start_time
            self._set_status(self._format_progress(entry.name, downloaded, total, elapsed), timeout=1.0)
            self._draw()

        try:
            download_file(entry.url, dest_path, progress_cb=on_progress, insecure=self.insecure)
        except KeyboardInterrupt:
            self._set_status("下载已中断", timeout=1.5)
        except (urllib.error.URLError, OSError, ValueError, LookupError, http.client.HTTPException) as exc:
            self._set_status(f"下载失败：{exc}", timeout=2.0)
        else:
            elapsed = time.monotonic() - start_time
            self._set_status(f"已下载到 {dest_path}（用时 {_format_duration(elapsed)}）", timeout=2.0)
        _flush_stale_input()

    @staticmethod
    def _format_progress(name: str, downloaded: int, total: Optional[int], elapsed: float) -> str:
        duration = _format_duration(elapsed)
        if total:
            percent = int(downloaded * 100 / total)
            bar_width = 20
            filled = int(bar_width * downloaded / total)
            bar = "=" * filled + "-" * (bar_width - filled)
            return (
                f"下载中 {name} [{bar}] {percent}% "
                f"{format_size(downloaded)}/{format_size(total)} 用时{duration}{_CANCEL_HINT}"
            )
        return f"下载中 {name} {format_size(downloaded)} 用时{duration}{_CANCEL_HINT}"

    def _confirm_overwrite(self, dest_path: str) -> bool:
        basename = os.path.basename(dest_path)
        # The caller triggers this confirmation when either dest_path or its
        # ".part" staging file exists -- word the prompt to match which one,
        # or "movie.mkv 已存在" is misleading when only "movie.mkv.part" is
        # actually there and movie.mkv itself doesn't exist yet.
        if os.path.lexists(dest_path):
            prompt = f"{basename} 已存在，是否覆盖？(y/n)"
        else:
            prompt = f"{basename} 的未完成下载（.part）已存在，是否覆盖？(y/n)"
        self._set_status(prompt)
        try:
            self._draw()
        except KeyboardInterrupt:
            return False
        while True:
            try:
                key = self.stdscr.getch()
            except KeyboardInterrupt:
                return False
            if key == -1:
                continue
            if key == curses.KEY_RESIZE:
                # Not a keyboard answer -- reclamp/redraw for the new size and
                # keep waiting (this resize is consumed here, so run()'s own
                # KEY_RESIZE handling never sees it to do this for us).
                self._clamp_viewport()
                try:
                    self._draw()
                except KeyboardInterrupt:
                    return False
                continue
            if key == curses.KEY_MOUSE:
                # Not a keyboard answer either (mouse move/click while
                # confirming) -- still drain it, or it desyncs the mouse
                # event queue and a later click acts on stale coordinates.
                try:
                    curses.getmouse()
                except curses.error:
                    pass
                continue
            return key in (ord("y"), ord("Y"))

    def _draw(self) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        try:
            self._draw_unsafe(height, width)
        except curses.error:
            # A single bad boundary write (resize race, narrow width, quirky
            # terminfo) shouldn't crash the app — skip this frame and retry.
            pass

    def _draw_unsafe(self, height: int, width: int) -> None:
        if height < 4 or width < _MIN_TERMINAL_WIDTH:
            # The footer key-hint line doesn't get drawn in this branch, so
            # fold the one useful action (quit) into the message itself --
            # otherwise there's no way to tell what to do besides guessing.
            # Kept short deliberately: this message is the thing shown when
            # space is already tight, so it needs to survive truncation
            # itself down to a much narrower width than ordinary status text.
            self._draw_center_message(height, width, "窗口太小，按 q 退出")
            self.stdscr.refresh()
            return

        if self.stack is None:
            if self._status_visible():
                self._draw_center_message(height, width, self.status)
            self.stdscr.refresh()
            return

        frame = self.stack.current
        breadcrumb = _truncate_middle(_sanitize_display_text(urllib.parse.unquote(frame.url)), width - 1)
        self.stdscr.addstr(0, 0, breadcrumb, self.header_attr)

        size_width, mtime_width = _SIZE_COL_WIDTH, _MTIME_COL_WIDTH
        name_width = max(width - size_width - mtime_width - 3, _MIN_NAME_COL_WIDTH)
        header = f"{_ljust('名称', name_width)} {_rjust('大小', size_width)} {_rjust('修改时间', mtime_width)}"
        self.stdscr.addstr(1, 0, header, curses.A_BOLD)

        visible = self._page_size()
        for row, entry in enumerate(frame.entries[frame.offset: frame.offset + visible]):
            y = row + 2
            name_col = format_row(entry, name_width)
            # _rjust only pads, never clips -- an oversized value (e.g. a
            # server-reported date the regex accepted but format_mtime
            # couldn't parse, so it fell back to the raw, longer string)
            # would otherwise overflow the column and wrap the row.
            size_col = _rjust(_truncate(_format_entry_size(entry), size_width), size_width)
            mtime_col = _rjust(_truncate(entry.mtime or "", mtime_width), mtime_width)
            line = f"{name_col} {size_col} {mtime_col}"
            attr = curses.A_REVERSE if frame.offset + row == frame.selected else curses.A_NORMAL
            if entry.is_dir:
                attr |= self.dir_attr
            self.stdscr.addstr(y, 0, line, attr)

        status = (
            "↑/↓/j/k 移动  PgUp/PgDn 翻页  Enter/点击 进入或下载  → 进入目录  "
            "r/R/F5 刷新  Backspace/←/u/Esc 返回上级  q/Q 退出"
        )
        # This fixed hint is 114 columns wide -- front-truncating it (the old
        # behavior) cut off "q/Q 退出" at every width below 115, which is
        # effectively always. Middle-truncating keeps the movement hints at
        # the head and the quit hint at the tail instead of losing the tail
        # outright.
        shown_status = _truncate_middle(status, width - 1)
        self.stdscr.addstr(height - 1, 0, shown_status, self.footer_attr)
        if self._status_visible():
            self._draw_center_message(height, width, self.status)
        self.stdscr.refresh()

    def _draw_center_message(self, height: int, width: int, text: str) -> None:
        # Middle-truncate rather than cut the tail off: a long embedded file
        # or directory name would otherwise push a trailing "(y/n)" prompt,
        # cancel hint, or download percentage off the edge of the screen.
        text = _truncate_middle(text, max(width - 4, 0))
        if not text:
            return
        line = f" {text} "
        x = max((width - _display_width(line)) // 2, 0)
        y = height // 2
        try:
            self.stdscr.addstr(y, x, line, curses.A_REVERSE | curses.A_BOLD)
        except curses.error:
            pass


def main(argv: Optional[List[str]] = None) -> None:
    # Must run before curses.wrapper()/initscr() so the window's encoding
    # resolves to the process locale (needed for the Chinese UI text to render).
    # An unsupported locale isn't fatal -- it just risks mis-rendered wide
    # characters, so degrade instead of crashing before --help even runs.
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    # ncurses waits ESCDELAY ms after a lone Esc byte before delivering it,
    # in case more bytes are coming (arrow/function keys are also Esc-prefixed
    # sequences); the 1000ms default makes Esc-to-go-back feel laggy. Only
    # set a default — don't override a value the user already configured.
    os.environ.setdefault("ESCDELAY", "25")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        os.makedirs(args.output_dir, exist_ok=True)
    except OSError as exc:
        print(f"无法创建输出目录 {args.output_dir}：{exc}", file=sys.stderr)
        sys.exit(1)
    startup_failure: Optional[Tuple[str, bool]] = None  # (message, was_cancelled)

    def _run(stdscr):
        nonlocal startup_failure
        app = BrowserApp(stdscr, args.url, args.output_dir, insecure=args.insecure)
        if app.stack is None:
            startup_failure = (app.status, app.startup_cancelled)
            return
        app.run()

    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        sys.exit(130)

    if startup_failure is not None:
        message, cancelled = startup_failure
        print(message, file=sys.stderr)
        sys.exit(130 if cancelled else 1)


if __name__ == "__main__":
    main()
