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
