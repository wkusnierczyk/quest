"""HTTP downloads: parallel ranged download into a single preallocated file, verified, then moved into place."""
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .common import sha256_file

CHUNK = 256 * 1024
PARALLEL_MIN_SIZE = 32 * 1024 * 1024


class DownloadError(Exception):
    pass


def _ssl_context():
    """System trust store, or certifi's bundle when available (python.org builds on macOS ship without one)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CONTEXT = _ssl_context()
CERT_HINT = ("TLS certificate verification failed. On macOS with the python.org installer, run "
             "'Install Certificates.command' from the Python folder in Applications (or: pip install certifi).")


def _urlopen(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT)


class _RangeNotHonored(Exception):
    pass


def download(url, dest, console, expected_sha256=None, threads=4, timeout=30, retries=4, label=None):
    """
    Downloads url to dest atomically: data goes to "<dest>.part" and is renamed into place only
    after the size (and expected_sha256, when given) check out. Raises DownloadError on failure.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    label = label or dest.name

    try:
        size, ranges = _probe(url, timeout)
        if size and ranges and threads > 1 and size >= PARALLEL_MIN_SIZE:
            try:
                _parallel(url, tmp, size, threads, timeout, retries, console, label)
            except _RangeNotHonored:
                console.warn("Server ignored byte ranges; falling back to a single connection.")
                _single(url, tmp, size, timeout, retries, console, label)
        else:
            _single(url, tmp, size, timeout, retries, console, label)

        if size is not None and tmp.stat().st_size != size:
            raise DownloadError(f"size mismatch: got {tmp.stat().st_size} bytes, expected {size}")
        if expected_sha256:
            actual = sha256_file(tmp, console, f"Verifying {label}")
            if actual != expected_sha256.lower():
                raise DownloadError(f"SHA256 mismatch for {label}: got {actual}, expected {expected_sha256.lower()}")
        os.replace(tmp, dest)
    except DownloadError:
        _remove(tmp)
        raise
    except (OSError, urllib.error.URLError, ValueError) as e:
        _remove(tmp)
        if isinstance(getattr(e, "reason", e), ssl.SSLCertVerificationError):
            raise DownloadError(CERT_HINT) from e
        raise DownloadError(f"{label}: {e}") from e
    finally:
        console.progress_done()


def _remove(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _probe(url, timeout):
    """Returns (content_length or None, supports_byte_ranges)."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with _urlopen(req, timeout) as resp:
            length = resp.headers.get("Content-Length")
            ranges = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
            return (int(length) if length and length.isdigit() else None), ranges
    except urllib.error.HTTPError as e:
        if e.code in (403, 405, 501):  # HEAD not allowed; fall back to a plain GET.
            return None, False
        raise


def _single(url, tmp, size, timeout, retries, console, label):
    for attempt in range(1, retries + 1):
        try:
            done = 0
            with _urlopen(url, timeout) as resp, open(tmp, "wb") as f:
                total = size or int(resp.headers.get("Content-Length") or 0)
                while data := resp.read(CHUNK):
                    f.write(data)
                    done += len(data)
                    console.progress(label, done, total)
            if total and done < total:
                raise OSError(f"connection closed early ({done} of {total} bytes)")
            return
        except (OSError, urllib.error.URLError) as e:
            if attempt == retries:
                raise DownloadError(f"{label}: {e}") from e
            console.warn(f"Download interrupted ({e}); retrying ({attempt}/{retries - 1})...")
            time.sleep(2 * attempt)


def _parallel(url, tmp, size, threads, timeout, retries, console, label):
    with open(tmp, "wb") as f:
        f.truncate(size)

    chunk_size = size // threads
    ranges = [(i * chunk_size, size - 1 if i == threads - 1 else (i + 1) * chunk_size - 1) for i in range(threads)]
    progress = [0] * threads
    errors = []
    lock = threading.Lock()
    cancel = threading.Event()

    def worker(idx, start, end):
        pos = start
        attempt = 0
        try:
            with open(tmp, "r+b") as f:
                while pos <= end and not cancel.is_set():
                    try:
                        req = urllib.request.Request(url, headers={"Range": f"bytes={pos}-{end}"})
                        with _urlopen(req, timeout) as resp:
                            content_range = resp.headers.get("Content-Range", "")
                            if resp.status != 206 or not content_range.startswith(f"bytes {pos}-"):
                                raise _RangeNotHonored()
                            f.seek(pos)
                            while pos <= end and not cancel.is_set():
                                data = resp.read(min(CHUNK, end - pos + 1))
                                if not data:
                                    break
                                f.write(data)
                                pos += len(data)
                                with lock:
                                    progress[idx] = pos - start
                                console.progress(label, sum(progress), size)
                        if pos <= end and not cancel.is_set():
                            raise OSError("connection closed early")
                    except (OSError, urllib.error.URLError) as e:
                        attempt += 1
                        if attempt >= retries:
                            raise
                        console.warn(f"Segment {idx + 1} interrupted ({e}); resuming ({attempt}/{retries - 1})...")
                        time.sleep(2 * attempt)
        except BaseException as e:  # noqa: BLE001 - propagated to the caller below
            with lock:
                errors.append(e)
            cancel.set()

    workers = [threading.Thread(target=worker, args=(i, s, e), daemon=True) for i, (s, e) in enumerate(ranges)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    if errors:
        if any(isinstance(e, _RangeNotHonored) for e in errors):
            raise _RangeNotHonored()
        raise DownloadError(f"{label}: {errors[0]}")
