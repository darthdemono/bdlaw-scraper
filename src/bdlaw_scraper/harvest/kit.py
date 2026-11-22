"""Polite, resumable fetching for the bulk harvests (gazettes, court judgments, law reports).

Rules it enforces: robots.txt is read and obeyed (a missing file means allowed), one request at a time per host
with a fixed delay, an identifying User-Agent, every page cached on disk (a cached page is never fetched again),
every file written to a ".part" name and renamed only when complete (so an interrupted run never leaves a broken
PDF), and a hard stop after repeated 403 or 429 answers. Anything can be killed at any moment and started again.
"""

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path

UA = "Mozilla/5.0 (compatible; bdlaw-harvest; +https://bdlaw.darthdemono.com)"


class Stop(Exception):
    """The host said no (403/429 repeatedly) or robots.txt forbids the path: stop the job, do not push."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Status:
    """A small JSON file a person can read: job, state, counters, last update. Written atomically."""

    def __init__(self, path: Path, job: str):
        self.path, self.job, self.started, self.data = path, job, now(), {}

    def update(self, state: str, **counts) -> None:
        self.data.update(counts)
        body = {"job": self.job, "state": state, "started": self.started, "updated": now(), **self.data}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=1))
        os.replace(tmp, self.path)


def db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    return con


class Polite:
    def __init__(self, root: Path, host: str, delay: float = 1.0):
        self.root, self.host, self.delay, self.last, self.fails = root, host, delay, 0.0, 0
        (root / "cache").mkdir(parents=True, exist_ok=True)
        self.log = open(root / "requests.jsonl", "a", buffering=1)
        self.robots = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(f"https://{host}/robots.txt", headers={"User-Agent": UA})
            body = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
            if body.lstrip().lstrip(chr(0xFEFF)).startswith("<"):
                self.robots.parse([])  # an HTML error page is not a robots file
            else:
                self.robots.parse(body.splitlines())
        except Exception:
            self.robots.parse([])
        wait = self.robots.crawl_delay(UA) or self.robots.crawl_delay("*")
        if wait:
            self.delay = max(self.delay, float(wait))  # the site's own Crawl-delay wins when it is longer

    def _wait(self) -> None:
        gap = self.delay - (time.time() - self.last)
        if gap > 0:
            time.sleep(gap)
        self.last = time.time()

    def _open(self, url: str, data: dict | None, headers: dict | None = None):
        parts = urllib.parse.urlsplit(url)  # a space or a non-ASCII letter in a link is legal on a page, not in a request
        path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
        query = urllib.parse.quote(parts.query, safe="=&%:@!$'()*+,;/?-._~")
        url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, ""))
        if not self.robots.can_fetch(UA, url):
            raise Stop(f"robots.txt forbids {url}")
        h = {"User-Agent": UA, **(headers or {})}
        body = urllib.parse.urlencode(data).encode() if data else None
        if body:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        return urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=60)

    def fetch(self, url: str, data: dict | None = None, cache: bool = True) -> bytes:
        """A page, from the disk cache if it was ever fetched. Empty bytes for a 404.

        cache=False for an answer that must not be written to disk (a response that carries something it should not).
        """
        key = hashlib.sha1((url + (json.dumps(data, sort_keys=True) if data else "")).encode()).hexdigest()
        path = self.root / "cache" / key[:2] / key
        if cache and path.exists():
            return path.read_bytes()
        blocked = 0
        for attempt in range(5):
            self._wait()
            try:
                body = self._open(url, data).read()
                self.fails = 0
                if cache:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".part")
                    tmp.write_bytes(body)
                    os.replace(tmp, path)
                self.log.write(json.dumps({"t": now(), "url": url, "post": bool(data), "bytes": len(body)}) + "\n")
                return body
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return b""
                if e.code in (403, 429):
                    blocked = e.code
                    time.sleep(30 * (attempt + 1))  # a single refusal is not a ban: wait and try again
                else:
                    time.sleep(2 ** (attempt + 1))
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(2 ** (attempt + 1))
            except ValueError:  # an invalid URL (http.client.InvalidURL is one): nothing to fetch
                return b""
        if blocked:
            # this URL stays refused after every try: skip it; only three different URLs in a row stop the job
            self.fails += 1
            if self.fails >= 3:
                raise Stop(f"{blocked} from {self.host}")
            return b""
        raise Stop(f"gave up on {url}")

    def download(self, url: str, dest: Path, magic: bytes | None = b"%PDF") -> tuple[str, int, str]:
        """A file, resumable. Returns (state, size, sha256); state is exists, ok or bad (not a PDF, too small)."""
        if dest.exists() and dest.stat().st_size > 0:
            return "exists", dest.stat().st_size, ""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        blocked = 0
        for attempt in range(4):
            have = part.stat().st_size if part.exists() else 0
            self._wait()
            try:
                resp = self._open(url, None, {"Range": f"bytes={have}-"} if have else None)
                if have and resp.status != 206:
                    have = 0  # the server ignored the range: start over
                with open(part, "ab" if have else "wb") as f:
                    while chunk := resp.read(1 << 16):
                        f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
                self.fails = 0
                break
            except urllib.error.HTTPError as e:
                if e.code == 416:  # range past the end: the .part is already whole
                    break
                if e.code == 404:
                    return "bad", 0, ""
                if e.code in (403, 429):
                    blocked = e.code
                    time.sleep(30 * (attempt + 1))  # a single refusal is not a ban: wait and try again
                else:
                    time.sleep(2 ** (attempt + 1))
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(2 ** (attempt + 1))
        else:
            if blocked:
                self.fails += 1  # this file stays refused: skip it; only three different files in a row stop the job
                if self.fails >= 3:
                    raise Stop(f"{blocked} from {self.host}")
            return "bad", 0, ""
        size = part.stat().st_size
        head = part.open("rb").read(len(magic)) if magic else b""
        if size < (1024 if magic else 1) or (magic and head != magic):
            part.unlink()
            return "bad", size, ""
        sha = hashlib.sha256()
        with part.open("rb") as f:
            while chunk := f.read(1 << 20):
                sha.update(chunk)
        os.replace(part, dest)
        self.log.write(json.dumps({"t": now(), "url": url, "file": str(dest), "bytes": size}) + "\n")
        return "ok", size, sha.hexdigest()