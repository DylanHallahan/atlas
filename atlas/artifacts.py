"""Each team's static pages: the `artifacts` folder beside its `items` folder, served at /a/<team>/<path> and listed on
the Teams tab. Read on request, opened by descriptors so no symlink is followed; no table and no source."""
import html
import os
import re
import threading
from datetime import UTC, datetime
from html.parser import HTMLParser

from .files import open_below, read_fd
from .reports import skipped, walk
from .structure import expand

MAX = 5 << 20  # a larger file is not served
LABEL = 200  # characters kept of a title or an author
HEAD = 64 << 10  # the part of a page read for its title and author
HOST = re.compile(r"[A-Za-z0-9.\-]+(?::\d+)?|\[[0-9a-fA-F:.]+\](?::\d+)?")
CACHE, LOCK = {}, threading.Lock()  # (folder, path) to ((mtime_ns, size), (title, author)): kept until either changes


def folder(t):
    """A team's artifacts folder, beside its items folder as the briefs folder is; None without an items folder."""
    items = t.get("items")
    return os.path.join(os.path.dirname(os.path.normpath(expand(items))), "artifacts") if isinstance(items, str) and items else None


def csp(host, team):
    """The Content-Security-Policy of a page: sandboxed, and loading only from its own team's folder. A sandboxed page has
    an opaque origin, and 'self' there would let a page reach every Atlas URL, so the source names the request's own host and
    the team's path (a path ending in / matches everything below it). It has no scheme: Chromium refuses a scheme-relative
    //host source, and a source without one takes the page's own scheme. connect-src and form-action are none: no fetch, XHR, WebSocket, beacon or form post."""
    if not HOST.fullmatch(host or ""):
        host = None
    own = f"{host}/a/{team}/" if host else "'none'"
    return ("sandbox allow-scripts; default-src 'none'; "
            f"script-src {own} 'unsafe-inline'; style-src {own} 'unsafe-inline'; img-src {own} data:; font-src {own} data:; "
            "media-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'")


def read(root, relative, types):
    """The bytes and type of one page, or None. A page is a regular file of a known type with no hidden component, opened by
    open_below, at most MAX bytes. OSError and ValueError are for the caller: they mean not found."""
    parts = relative.split("/")
    suffix = os.path.splitext(parts[-1])[1]
    if suffix not in types or any(skipped(p) for p in parts):
        return None
    body = read_fd(open_below(root, relative), MAX + 1)
    return (body, types[suffix]) if len(body) <= MAX else None


class Head(HTMLParser):
    """The title and the author meta of a page, from its first bytes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.author, self.inside = None, None, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title" and self.title is None:
            self.title, self.inside = "", True
        elif tag == "meta" and (a.get("name") or "").lower() == "author" and self.author is None:
            self.author = a.get("content")

    def handle_endtag(self, tag):
        self.inside = self.inside and tag != "title"

    def handle_data(self, data):
        if self.inside:
            self.title += data


def heading(root, relative):
    """(title, author) from the first 64 KB of a page; either None when the page does not give it."""
    head = Head()
    try:
        head.feed(read_fd(open_below(root, relative), HEAD).decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        pass
    clean = lambda s: " ".join(html.unescape(s or "").split())[:LABEL] or None  # noqa: E731
    return clean(head.title), clean(head.author)


def listing(t):
    """A team's pages, newest first: [{path, title, author, modified}], with the title the file name and the author None when
    the page gives none. Each page is read again only when its modified time or size changes. [] without a folder."""
    root = folder(t)
    if root is None:
        return []
    try:
        found, _ = walk(root, ".html")
    except OSError:
        return []
    found.sort(key=lambda f: (-f[1].st_mtime_ns, f[0]))
    pages, keep = [], set()
    for rel, st in found:
        key, stamp = (root, rel), (st.st_mtime_ns, st.st_size)
        keep.add(key)
        with LOCK:
            cached = CACHE.get(key)
        if cached is None or cached[0] != stamp:
            cached = (stamp, heading(root, rel))
            with LOCK:
                CACHE[key] = cached
        title, author = cached[1]
        pages.append({"path": rel, "title": title or os.path.basename(rel), "author": author,
                      "modified": time_iso(st.st_mtime_ns)})
    with LOCK:
        for key in [k for k in CACHE if k[0] == root and k not in keep]:
            del CACHE[key]
    return pages


def time_iso(ns):
    return datetime.fromtimestamp(ns / 1e9, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
