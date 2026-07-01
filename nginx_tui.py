#!/usr/bin/env python3
import argparse
import curses
import datetime
import enum
import html.parser
import locale
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
        if height < 4 or width < 20:
            message = _truncate("终端窗口太小", max(width - 1, 0))
            self.stdscr.addstr(0, 0, message)
            self.stdscr.refresh()
            return
        if self.stack is None:
            shown = _truncate(self.status, width - 1)
            self.stdscr.addstr(0, 0, shown)
            self.stdscr.refresh()
            return

        frame = self.stack.current
        breadcrumb = _truncate(frame.url, width - 1)
        self.stdscr.addstr(0, 0, breadcrumb, curses.A_REVERSE)

        size_width, mtime_width = 8, 16
        name_width = max(width - size_width - mtime_width - 3, 8)
        header = f"{_ljust('名称', name_width)} {_rjust('大小', size_width)} {_rjust('修改时间', mtime_width)}"
        self.stdscr.addstr(1, 0, header, curses.A_BOLD)

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
            self.stdscr.addstr(y, 0, line, attr)

        status = self.status or "↑/↓ 移动   Enter/点击 进入或下载   Backspace 返回上级   q 退出"
        shown_status = _truncate(status, width - 1)
        self.stdscr.addstr(height - 1, 0, shown_status, curses.A_DIM)
        self.stdscr.refresh()


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
