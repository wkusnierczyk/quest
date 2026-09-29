"""Host-side tools: Android platform-tools, payload-dumper-go, and (Windows only) the Oculus USB driver."""
import json
import platform
import shutil
import tarfile
import zipfile

from .common import BIN_EXT, DOWNLOADS_DIR, IS_WINDOWS, PLATFORM_TOOLS_DIR, TOOLS_ROOT, Refused, run, sha256_file
from .downloader import DownloadError, download

# Pinned releases. SHA256 values were computed from the published files (platform-tools also
# matched the SHA-1 in Google's repository2-3.xml; payload-dumper-go matched its checksums file).
PLATFORM_TOOLS = {
    "darwin": ("https://dl.google.com/android/repository/platform-tools_r37.0.1-darwin.zip",
               "ee39ad5967e95c2a07f04dbcbde96b1a0c916ba376096db5d2f498b7727a5d1d"),
    "linux": ("https://dl.google.com/android/repository/platform-tools_r37.0.1-linux.zip",
              "d230f13842f60f782a8645f9c813f8f845bf36089ea7289f28c48f17979313f1"),
    "windows": ("https://dl.google.com/android/repository/platform-tools_r37.0.1-win.zip",
                "45f4d63113e895ebde0c90f194099a4676b6ac653bd28d54314a9e022bbc1a99"),
}

PAYLOAD_DUMPER_BASE = "https://github.com/ssut/payload-dumper-go/releases/download/1.3.0/"
PAYLOAD_DUMPER = {
    ("darwin", "amd64"): ("payload-dumper-go_1.3.0_darwin_amd64.tar.gz",
                          "5fdcf8cb07183c7cd1670427b3a5aa9d99563f9a3e15d6b92cc5f8db24e0b566"),
    ("darwin", "arm64"): ("payload-dumper-go_1.3.0_darwin_arm64.tar.gz",
                          "e6b95df4b08e4bf452077e35cc2c0d644ce8fd454696d1aceedde6887ef0df84"),
    ("linux", "amd64"): ("payload-dumper-go_1.3.0_linux_amd64.tar.gz",
                         "1b973d521cda71268ddbaa0388fe28d6a94b370c58e21352137c5f9077b0bfe7"),
    ("linux", "arm64"): ("payload-dumper-go_1.3.0_linux_arm64.tar.gz",
                         "2512b2a57556f12c24304898363fa9640b8485b6ead3f2184e253c4890fe6de5"),
    # Windows on ARM runs the amd64 build under emulation.
    ("windows", "amd64"): ("payload-dumper-go_1.3.0_windows_amd64.tar.gz",
                           "0f96e07477963327f7f50a03bf2aa9dac5c76dba110ab332dc759321ae345d52"),
    ("windows", "arm64"): ("payload-dumper-go_1.3.0_windows_amd64.tar.gz",
                           "0f96e07477963327f7f50a03bf2aa9dac5c76dba110ab332dc759321ae345d52"),
}

# oculus-adb-driver-2.0.zip (Last-Modified 2019-09-04); SHA256 computed from the published file.
OCULUS_DRIVER_URL = "https://securecdn.oculus.com/binaries/download/?id=2987319634674616"
OCULUS_DRIVER_SHA256 = "0b075f305bfc50ccd0a145c3247781a82aed12382a63f53bc00f0efd74ad7927"

_ARCH_ALIASES = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}


def host_os():
    return platform.system().lower()


def host_arch():
    return _ARCH_ALIASES.get(platform.machine().lower(), platform.machine().lower())


class ToolManager:
    def __init__(self, console):
        self.console = console
        self.adb_path = PLATFORM_TOOLS_DIR / f"adb{BIN_EXT}"
        self.fastboot_path = PLATFORM_TOOLS_DIR / f"fastboot{BIN_EXT}"
        self.dumper_path = PLATFORM_TOOLS_DIR / f"payload-dumper-go{BIN_EXT}"
        # Records which pinned archive each installed binary came from, plus the binary's own SHA256.
        self.provenance_path = TOOLS_ROOT / "provenance.json"

    # --- Provenance: binaries are trusted only if we installed them from the current pin ---

    def _load_provenance(self):
        try:
            return json.loads(self.provenance_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _record(self, name, pin, paths):
        provenance = self._load_provenance()
        provenance[name] = {"pin": pin, "files": {p.name: sha256_file(p) for p in paths}}
        self.provenance_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.provenance_path.with_name(self.provenance_path.name + ".tmp")
        tmp.write_text(json.dumps(provenance, indent=1), encoding="utf-8")
        tmp.replace(self.provenance_path)

    def _verified(self, name, pin, paths):
        """True only if every file exists and matches what was recorded when installed from `pin`."""
        entry = self._load_provenance().get(name)
        if not entry or entry.get("pin") != pin or not all(p.is_file() for p in paths):
            return False
        return all(entry["files"].get(p.name) == sha256_file(p) for p in paths)

    # --- platform-tools ---

    def _platform_tools_pin(self):
        entry = PLATFORM_TOOLS.get(host_os())
        return entry[1] if entry else None

    def platform_tools_installed(self):
        pin = self._platform_tools_pin()
        return pin is not None and self._verified("platform-tools", pin, [self.adb_path, self.fastboot_path])

    def install_platform_tools(self):
        entry = PLATFORM_TOOLS.get(host_os())
        if not entry:
            raise Refused(f"No platform-tools build for this OS ({host_os()}).")
        url, sha256 = entry
        archive = DOWNLOADS_DIR / url.rsplit("/", 1)[1]
        self.console.info("Downloading Android platform-tools...")
        try:
            download(url, archive, self.console, expected_sha256=sha256)
        except DownloadError as e:
            raise Refused(f"platform-tools download failed: {e}") from e

        staging = TOOLS_ROOT / "platform-tools.tmp"
        shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(staging)  # zipfile strips absolute paths and ".." components.
        archive.unlink()
        extracted = staging / "platform-tools"
        if not (extracted / self.adb_path.name).is_file():
            shutil.rmtree(staging, ignore_errors=True)
            raise Refused("platform-tools archive did not contain adb.")

        # Keep an already-installed payload-dumper-go when replacing the directory.
        if self.dumper_path.is_file():
            shutil.copy2(self.dumper_path, extracted / self.dumper_path.name)
        shutil.rmtree(PLATFORM_TOOLS_DIR, ignore_errors=True)
        extracted.rename(PLATFORM_TOOLS_DIR)
        shutil.rmtree(staging, ignore_errors=True)
        if not IS_WINDOWS:
            for binary in (self.adb_path, self.fastboot_path):
                binary.chmod(0o755)
        self._record("platform-tools", sha256, [self.adb_path, self.fastboot_path])
        self.console.ok(f"platform-tools installed to {PLATFORM_TOOLS_DIR}")

    # --- payload-dumper-go ---

    def _dumper_pin(self):
        entry = PAYLOAD_DUMPER.get((host_os(), host_arch()))
        return entry[1] if entry else None

    def dumper_installed(self):
        # The pin is per OS/architecture, so a binary for another host never verifies.
        pin = self._dumper_pin()
        return pin is not None and self._verified("payload-dumper-go", pin, [self.dumper_path])

    def install_payload_dumper(self):
        entry = PAYLOAD_DUMPER.get((host_os(), host_arch()))
        if not entry:
            raise Refused(f"No payload-dumper-go build for {host_os()}/{host_arch()}.")
        filename, sha256 = entry
        archive = DOWNLOADS_DIR / filename
        self.console.info("Downloading payload-dumper-go...")
        try:
            download(PAYLOAD_DUMPER_BASE + filename, archive, self.console, expected_sha256=sha256)
        except DownloadError as e:
            raise Refused(f"payload-dumper-go download failed: {e}") from e

        # Copy only the binary's bytes out of the archive; never let tarfile choose paths.
        PLATFORM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.dumper_path.with_name(self.dumper_path.name + ".tmp")
        with tarfile.open(archive, "r:gz") as tar:
            member = next((m for m in tar.getmembers()
                           if m.isfile() and m.name.rsplit("/", 1)[-1] == self.dumper_path.name), None)
            if member is None:
                raise Refused("payload-dumper-go archive did not contain the expected binary.")
            with tar.extractfile(member) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
        archive.unlink()
        if not IS_WINDOWS:
            tmp.chmod(0o755)
        tmp.replace(self.dumper_path)
        self._record("payload-dumper-go", sha256, [self.dumper_path])
        self.console.ok("payload-dumper-go installed.")

    # --- Windows USB driver ---

    def drivers_installed(self):
        if not IS_WINDOWS:
            return True
        res = run(["pnputil", "/enum-drivers"], timeout=60, console=self.console)
        return res.ok and ("Oculus" in res.stdout or "Meta" in res.stdout)

    def install_drivers(self):
        if not IS_WINDOWS:
            return
        archive = DOWNLOADS_DIR / "oculus_drivers.zip"
        extract_dir = DOWNLOADS_DIR / "oculus_drivers"
        self.console.info("Downloading Oculus ADB driver...")
        try:
            download(OCULUS_DRIVER_URL, archive, self.console, expected_sha256=OCULUS_DRIVER_SHA256, threads=1)
        except DownloadError as e:
            raise Refused(f"Driver download failed: {e}") from e
        shutil.rmtree(extract_dir, ignore_errors=True)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_dir)
        archive.unlink()
        inf = next((p for p in extract_dir.rglob("*") if p.name.lower() == "android_winusb.inf"), None)
        if inf is None:
            raise Refused("Driver package did not contain android_winusb.inf.")
        res = run(["pnputil", "/add-driver", inf, "/install"], timeout=300, console=self.console, show=True)
        if not res.ok:
            raise Refused(f"Driver installation failed ({res.describe_failure()}). "
                          "Try again from an elevated (Administrator) terminal.")
        self.console.ok("Oculus ADB driver installed.")

    # --- Aggregate ---

    def ensure(self, install=True, need_dumper=False, need_drivers=True):
        """Makes sure required tools exist, installing missing ones when allowed."""
        if not self.platform_tools_installed():
            if not install:
                raise Refused("platform-tools are missing or not a verified install. Run: quest setup")
            if self.adb_path.exists():
                self.console.warn("Existing platform-tools are not a verified pinned install; reinstalling.")
            self.install_platform_tools()
        else:
            self.console.ok("platform-tools present.")

        if need_drivers and IS_WINDOWS:
            if self.drivers_installed():
                self.console.ok("Oculus ADB driver present.")
            elif install:
                self.install_drivers()
            else:
                raise Refused("Oculus ADB driver is not installed. Run: quest setup")

        if need_dumper:
            self.ensure_dumper(install)

    def ensure_dumper(self, install=True):
        """payload-dumper-go is only needed to extract firmware, so callers ensure it separately and late."""
        if self.dumper_installed():
            self.console.ok("payload-dumper-go present.")
        elif install:
            if self.dumper_path.exists():
                self.console.warn("Existing payload-dumper-go is not a verified pinned install; reinstalling.")
            self.install_payload_dumper()
        else:
            raise Refused("payload-dumper-go is missing or not a verified install. Run: quest setup")
