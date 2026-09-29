"""Shared plumbing: data paths, exit codes, console output, command execution, hashing."""
import hashlib
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


def _user_data_dir():
    """Per-user data directory for downloaded tools and firmware. QUEST_HOME overrides it."""
    if override := os.environ.get("QUEST_HOME"):
        # Absolute, so tool and firmware paths (and printed recovery commands) work from any directory.
        return Path(override).expanduser().absolute()
    home = Path.home()
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "quest"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "quest"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "quest"


# Writable state lives in the user data directory, never next to the installed package or in the CWD.
DATA_DIR = _user_data_dir()
TOOLS_ROOT = DATA_DIR / "tools"
PLATFORM_TOOLS_DIR = TOOLS_ROOT / "platform-tools"
DOWNLOADS_DIR = TOOLS_ROOT / "downloads"
FIRMWARE_ROOT = DATA_DIR / "firmware"
# Pinned hash lists ship inside the package (read-only).
HASH_ROOT = resources.files("quest") / "data" / "hash"

BIN_EXT = ".exe" if IS_WINDOWS else ""

EXIT_OK = 0
EXIT_REFUSED = 1        # Preconditions not met; nothing was written to the device.
EXIT_DEVICE_FAILED = 2  # Failed after the device left its normal state; recovery steps were printed.
EXIT_USAGE = 64         # Bad command line (sysexits EX_USAGE); argparse's default 2 would read as EXIT_DEVICE_FAILED.
EXIT_INTERRUPTED = 130


class Refused(Exception):
    """A precondition or safety check failed before the device was modified."""


class AlreadyConverted(Exception):
    """The device already runs a conversion target's user firmware; there is nothing to do."""


class ConversionFailed(Exception):
    """A step failed after the device left its normal state. Carries recovery instructions."""

    def __init__(self, message, recovery):
        super().__init__(message)
        self.recovery = recovery


@contextmanager
def data_dir_lock(directory=None):
    """
    Exclusive, non-blocking inter-process lock on the shared data directory, held by commands that
    install tools or download/extract firmware so concurrent `quest` runs cannot interleave writes.
    The OS releases it automatically if the process dies.
    """
    directory = Path(directory or DATA_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / ".lock", "a+b")
    locked = False
    try:
        try:
            if IS_WINDOWS:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise Refused(f"Another quest command is using {directory}. Wait for it to finish and retry.") from None
        yield
    finally:
        if locked and IS_WINDOWS:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def discard_stdout():
    """
    Points the stdout file descriptor at the null device, so later writes (and the interpreter's
    final flush) succeed after the reader of a pipe has gone away, e.g. `quest help | head`.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError):  # stdout is not a real file (e.g. replaced in tests)
        pass


class Console:
    """Line-oriented console output with an optional plain-text log file."""

    PROGRESS_INTERVAL = 0.1  # seconds between progress redraws

    def __init__(self, verbose=False, log_path=None, stream=None):
        self.verbose = verbose
        self.stream = stream or sys.stdout
        self.is_tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        self._log = open(log_path, "a", encoding="utf-8") if log_path else None
        self._lock = threading.Lock()
        self._progress_active = False
        self._last_progress = 0.0
        self._last_progress_decile = -1
        self._output_closed = False

    def close(self):
        if self._log:
            self._log.close()
            self._log = None

    def _write_log(self, text):
        if self._log:
            self._log.write(text + "\n")
            self._log.flush()

    def _write(self, text):
        """
        Writes to the screen. A closed output pipe (e.g. `quest convert | head`) must never
        interrupt the work - least of all between flash steps - so from then on screen output
        is dropped while the log file, if any, still receives everything.
        """
        if self._output_closed:
            return
        try:
            self.stream.write(text)
            self.stream.flush()
        except BrokenPipeError:
            self._output_closed = True
            if self.stream is sys.stdout:
                discard_stdout()

    def _emit(self, text):
        with self._lock:
            self._end_progress_line()
            self._write(text + "\n")
            self._write_log(text)

    def _end_progress_line(self):
        if self._progress_active:
            if self.is_tty:
                self._write("\n")
            self._progress_active = False
            self._last_progress_decile = -1

    def step(self, title):
        self._emit(f"\n=== {title} ===")

    def info(self, msg):
        self._emit(msg)

    def ok(self, msg):
        self._emit(f"[OK] {msg}")

    def warn(self, msg):
        self._emit(f"[WARN] {msg}")

    def error(self, msg):
        self._emit(f"[ERROR] {msg}")

    def debug(self, msg):
        if self.verbose:
            self._emit(f"  {msg}")
        else:
            with self._lock:
                self._write_log(f"  {msg}")

    def raw(self, text):
        """Pass-through for live output of long-running tools (flash, sideload, extraction)."""
        with self._lock:
            self._end_progress_line()
            self._write(text)
            if self._log:
                self._log.write(text)
                self._log.flush()

    def progress(self, label, done, total):
        """Throttled single-line progress. Safe to call from worker threads."""
        with self._lock:
            now = time.monotonic()
            finished = total and done >= total
            if not finished and now - self._last_progress < self.PROGRESS_INTERVAL:
                return
            self._last_progress = now
            pct = (done / total * 100) if total else 0.0
            text = f"{label}: {pct:5.1f}% ({done / 1e6:,.1f} / {total / 1e6:,.1f} MB)"
            if self.is_tty:
                self._write("\r" + text.ljust(78))
                self._progress_active = True
            else:
                # Non-interactive output (pipes, CI, log capture): one line per 10%.
                decile = int(pct // 10)
                if decile != self._last_progress_decile:
                    self._last_progress_decile = decile
                    self._write(text + "\n")
                self._progress_active = True
            if finished:
                self._end_progress_line()

    def progress_done(self):
        with self._lock:
            self._end_progress_line()

    def confirm_phrase(self, prompt, phrase, input_fn=input):
        """Require the user to type an exact phrase. Refuses when stdin is not interactive."""
        if input_fn is input and not sys.stdin.isatty():
            raise Refused("Interactive confirmation is required, but stdin is not a terminal.")
        self._emit(prompt)
        if self._output_closed:
            raise Refused("The output was closed, so the summary could not be shown. Not asking for confirmation.")
        try:
            answer = input_fn(f"Type {phrase} to continue, anything else aborts: ")
        except EOFError:
            answer = ""
        self._write_log(f"(answer: {answer!r})")
        return answer.strip() == phrase


@dataclass
class CmdResult:
    args: list
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None  # Set when the command could not be started at all.

    @property
    def ok(self):
        return self.returncode == 0 and not self.timed_out and self.error is None

    @property
    def output(self):
        return (self.stdout + "\n" + self.stderr).strip()

    def describe_failure(self):
        if self.error:
            return self.error
        if self.timed_out:
            return "timed out"
        tail = self.output.splitlines()[-3:]
        detail = f": {' | '.join(tail)}" if tail else ""
        return f"exit code {self.returncode}{detail}"


def shell_quote(argv, windows=None):
    """
    Renders argv as a command line that can be pasted into the platform's default shell and
    runs exactly argv: POSIX sh quoting, or PowerShell on Windows (call operator + single quotes).
    `windows` defaults to the current platform, looked up at call time.
    """
    argv = [str(a) for a in argv]
    if windows is None:
        windows = IS_WINDOWS
    if not windows:
        return shlex.join(argv)

    def ps(arg):
        return arg if re.fullmatch(r"[A-Za-z0-9_./:\\=+-]+", arg) else "'" + arg.replace("'", "''") + "'"

    return "& " + " ".join(ps(a) for a in argv)


def display_cmd(cmd):
    """Short echo of a command being run: the binary's name plus quoted arguments."""
    return shell_quote([os.path.basename(str(cmd[0]))] + [str(c) for c in cmd[1:]])


def runnable_cmd(cmd):
    """Copy-paste-safe command: the binary's full path and every argument quoted."""
    return shell_quote(cmd)


def run(cmd, timeout=60, console=None, show=False, stream=False):
    """
    Runs a command without a shell and returns a CmdResult (never raises for command failures).

    show:   echo the command line and its output to the console.
    stream: relay output live while it runs (for long operations with their own progress output).
    """
    cmd = [str(c) for c in cmd]
    if console:
        (console.info if show or stream else console.debug)(f"> {display_cmd(cmd)}")
    if stream:
        return _run_streaming(cmd, timeout, console)
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
        result = CmdResult(cmd, res.returncode, res.stdout, res.stderr)
    except subprocess.TimeoutExpired as e:
        result = CmdResult(cmd, None, _decode(e.stdout), _decode(e.stderr), timed_out=True)
    except OSError as e:
        result = CmdResult(cmd, None, error=f"could not run {os.path.basename(cmd[0])}: {e}")
    if console and result.output:
        if show:
            console.info(result.output)
        else:
            console.debug(_abbreviate(result.output))
    return result


def _abbreviate(text, max_lines=15):
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"])


def _decode(data):
    if data is None:
        return ""
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else data


def _run_streaming(cmd, timeout, console):
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except OSError as e:
        return CmdResult(cmd, None, error=f"could not run {os.path.basename(cmd[0])}: {e}")

    timed_out = threading.Event()

    def kill():
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, kill) if timeout else None
    if timer:
        timer.daemon = True
        timer.start()
    chunks = []
    try:
        for chunk in iter(lambda: proc.stdout.read1(4096), b""):
            text = chunk.decode("utf-8", "replace")
            chunks.append(text)
            if console:
                console.raw(text)
        proc.wait()
    finally:
        if timer:
            timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if console:
        console.raw("\n")
    return CmdResult(cmd, None if timed_out.is_set() else proc.returncode, "".join(chunks),
                     timed_out=timed_out.is_set())


def sha256_file(path, console=None, label=None):
    path = Path(path)
    total = path.stat().st_size
    digest = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while block := f.read(4 * 1024 * 1024):
            digest.update(block)
            done += len(block)
            if console and label and total > 64 * 1024 * 1024:
                console.progress(label, done, total)
    if console and label and total > 64 * 1024 * 1024:
        console.progress_done()
    return digest.hexdigest()
