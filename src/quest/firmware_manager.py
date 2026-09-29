"""Firmware acquisition: download, extract boot images, and verify everything against pinned hash lists."""
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .common import FIRMWARE_ROOT, HASH_ROOT, Refused, run, sha256_file
from .downloader import DownloadError, download
from .safety import parse_hash_file

BOOT_PARTITIONS = ("boot", "vendor_boot")
EXTRACT_TIMEOUT = 3600


@dataclass(frozen=True)
class Release:
    version: str
    url: str


@dataclass(frozen=True)
class Target:
    name: str
    description: str
    boot_release: Release      # Source of boot.img / vendor_boot.img flashed to the active slot.
    sideload_release: Release  # Full OTA zip sideloaded afterwards.


@dataclass(frozen=True)
class PreparedFirmware:
    target: Target
    boot_images: dict           # partition base name ("boot") -> Path to verified image
    sideload_zip: Path
    expected: dict              # Path -> expected sha256, for re-checking right before use


_RELEASES = {
    "v72": Release("v72", "https://files.cocaine.trade/firmware/meta/Quest%203/q3_50473320084200510.zip"),
    "v85": Release("v85", "https://files.cocaine.trade/firmware/meta/Quest%203/q3_51764730125000520.zip"),
    "v201": Release("v201", "https://files.cocaine.trade/firmware/meta/Quest%203/q3_51943020036500520.zip"),
}

# Per device (hash/firmware directory name): target name -> Target.
TARGETS = {
    "Eureka": {
        "v85": Target("v85", "v72 boot images + v85 sideload", _RELEASES["v72"], _RELEASES["v85"]),
        "v201": Target("v201", "v2.1 boot images + v2.1 sideload", _RELEASES["v201"], _RELEASES["v201"]),
    },
}


class FirmwareManager:
    CACHE_NAME = ".verified.json"

    def __init__(self, device_name, console, dumper_path):
        if device_name not in TARGETS:
            raise Refused(f"No firmware definitions for {device_name}.")
        self.device_name = device_name
        self.console = console
        self.dumper_path = dumper_path
        self.fw_dir = FIRMWARE_ROOT / device_name
        self.hash_dir = HASH_ROOT / device_name
        self._hash_lists = {}

    def target(self, name):
        try:
            return TARGETS[self.device_name][name]
        except KeyError:
            raise Refused(f"Unknown target '{name}' for {self.device_name}.") from None

    # --- Paths & expected hashes ---

    def zip_path(self, release):
        return self.fw_dir / f"firmware_{release.version}.zip"

    def extracted_dir(self, release):
        return self.fw_dir / f"extracted_{release.version}"

    def hashes_for(self, release):
        """Loads the pinned hash list for a release. Missing or malformed lists are fatal."""
        if release.version not in self._hash_lists:
            path = self.hash_dir / f"{release.version}.txt"
            try:
                self._hash_lists[release.version] = parse_hash_file(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise Refused(f"Hash list missing: {path}") from None
            except ValueError as e:
                raise Refused(f"Hash list {path} is malformed: {e}") from None
        return self._hash_lists[release.version]

    def expected_hash(self, release, filename, required=True):
        digest = self.hashes_for(release).get(filename)
        if digest is None and required:
            raise Refused(f"Hash list {release.version}.txt has no entry for {filename}; "
                          "refusing to use it unverified.")
        return digest

    # --- Verification (with a size+mtime cache so multi-GB zips are not re-hashed every run) ---

    def _load_cache(self):
        try:
            return json.loads((self.fw_dir / self.CACHE_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _store_cache(self, cache):
        try:
            self.fw_dir.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a concurrent reader never sees a half-written cache.
            tmp = self.fw_dir / f"{self.CACHE_NAME}.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(cache, indent=1), encoding="utf-8")
            tmp.replace(self.fw_dir / self.CACHE_NAME)
        except OSError:
            pass  # The cache is an optimisation only.

    def verify_file(self, path, expected, use_cache=True, quiet=False):
        """True only if path exists and its SHA256 equals expected. Never passes vacuously."""
        path = Path(path)
        if not expected:
            return False
        if not path.is_file():
            if not quiet:
                self.console.info(f"[MISSING] {path.name}")
            return False
        stat = path.stat()
        key = str(path.relative_to(self.fw_dir)) if path.is_relative_to(self.fw_dir) else str(path)
        cache = self._load_cache() if use_cache else {}
        entry = cache.get(key)
        if (entry and entry.get("size") == stat.st_size and entry.get("mtime_ns") == stat.st_mtime_ns
                and entry.get("sha256") == expected):
            if not quiet:
                self.console.ok(f"{path.name} (verified earlier, unchanged)")
            return True

        actual = sha256_file(path, None if quiet else self.console, f"Hashing {path.name}")
        if actual != expected:
            if not quiet:
                self.console.error(f"{path.name}: SHA256 mismatch (got {actual}, expected {expected})")
            return False
        if not quiet:
            self.console.ok(path.name)
        if use_cache and path.is_relative_to(self.fw_dir):
            cache = self._load_cache()
            cache[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": actual}
            self._store_cache(cache)
        return True

    def planned_files(self, target):
        """Returns {Path: expected sha256} for every file the conversion will use."""
        files = {}
        boot_dir = self.extracted_dir(target.boot_release)
        for part in BOOT_PARTITIONS:
            files[boot_dir / f"{part}.img"] = self.expected_hash(target.boot_release, f"{part}.img")
        zip_path = self.zip_path(target.sideload_release)
        files[zip_path] = self.expected_hash(target.sideload_release, zip_path.name)
        return files

    def verify_target(self, target):
        """Verifies local files for a target without downloading anything. Returns True if all pass."""
        results = [self.verify_file(p, h) for p, h in self.planned_files(target).items()]
        return all(results)

    # --- Acquisition ---

    def prepare(self, target):
        """Downloads/extracts whatever is missing or corrupt, verifies everything, and returns the file set."""
        self.fw_dir.mkdir(parents=True, exist_ok=True)
        expected = self.planned_files(target)  # Fails early if any required hash entry is missing.

        self.console.info(f"Target {target.name}: {target.description}")
        sideload_zip = self._ensure_zip(target.sideload_release, required_hash=True)
        boot_images = self._ensure_boot_images(target)

        # Drop a boot-source zip that is not needed for sideloading (it is only an extraction input).
        boot_zip = self.zip_path(target.boot_release)
        if boot_zip != sideload_zip and boot_zip.exists():
            self.console.info(f"Removing {boot_zip.name} (no longer needed).")
            boot_zip.unlink()

        return PreparedFirmware(target, boot_images, sideload_zip, expected)

    def _ensure_zip(self, release, required_hash):
        path = self.zip_path(release)
        expected = self.expected_hash(release, path.name, required=required_hash)

        if path.exists():
            if expected is None:
                self.console.warn(f"{path.name} has no pinned hash; the extracted images will be verified instead.")
                return path
            if self.verify_file(path, expected):
                return path
            self.console.warn(f"Deleting corrupt {path.name} and downloading it again.")
            path.unlink()

        self.console.info(f"Downloading {release.version} firmware ({release.url})")
        try:
            download(release.url, path, self.console, expected_sha256=expected, label=path.name)
        except DownloadError as e:
            raise Refused(f"Download of {release.version} failed: {e}") from e
        if expected:
            self.console.ok(f"{path.name} downloaded and verified.")
            self.verify_file(path, expected, quiet=True)  # Record in the cache.
        else:
            self.console.warn(f"{path.name} downloaded (no pinned zip hash; images are verified after extraction).")
        return path

    def _ensure_boot_images(self, target):
        release = target.boot_release
        out_dir = self.extracted_dir(release)
        images = {part: out_dir / f"{part}.img" for part in BOOT_PARTITIONS}
        expected = {part: self.expected_hash(release, f"{part}.img") for part in BOOT_PARTITIONS}

        if all(self.verify_file(images[p], expected[p], quiet=True) for p in BOOT_PARTITIONS):
            for part in BOOT_PARTITIONS:
                self.console.ok(f"{release.version}/{part}.img")
            return images

        if not self.dumper_path.is_file():
            raise Refused("payload-dumper-go is not installed. Run: quest setup")

        zip_path = self._ensure_zip(release, required_hash=False)
        staging = out_dir.with_name(out_dir.name + ".tmp")
        shutil.rmtree(staging, ignore_errors=True)
        self.console.info(f"Extracting {', '.join(BOOT_PARTITIONS)} from {zip_path.name}...")
        # The dumper draws progress bars; relay them live only on an interactive terminal.
        res = run([self.dumper_path, "-o", staging, "-p", ",".join(BOOT_PARTITIONS), zip_path],
                  timeout=EXTRACT_TIMEOUT, console=self.console, stream=self.console.is_tty)
        if not res.ok:
            shutil.rmtree(staging, ignore_errors=True)
            raise Refused(f"Extraction failed ({res.describe_failure()}).")

        staged = {part: staging / f"{part}.img" for part in BOOT_PARTITIONS}
        if not all(self.verify_file(staged[p], expected[p], use_cache=False, quiet=True) for p in BOOT_PARTITIONS):
            shutil.rmtree(staging, ignore_errors=True)
            raise Refused(f"Extracted {release.version} images failed verification. "
                          f"Delete {zip_path} and try again.")

        shutil.rmtree(out_dir, ignore_errors=True)
        staging.rename(out_dir)
        for part in BOOT_PARTITIONS:
            self.verify_file(images[part], expected[part])  # Verifies the final location and records it in the cache.
        return images

    def recheck_boot_images(self, prepared):
        """Full re-hash (no cache) of the images immediately before they are flashed."""
        return all(self.verify_file(path, prepared.expected[path], use_cache=False)
                   for path in prepared.boot_images.values())
