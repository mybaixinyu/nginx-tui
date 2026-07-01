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
