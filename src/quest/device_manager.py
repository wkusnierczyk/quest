"""adb / fastboot access, always pinned to a single device serial once one is selected."""
import hashlib
import subprocess
import threading
import time

from .common import Refused, run
from .safety import is_valid_slot_suffix, parse_device_list, parse_fastboot_getvar, parse_getprop, parse_i2c_addresses

QUERY_TIMEOUT = 60
FLASH_TIMEOUT = 600
SIDELOAD_TIMEOUT = 3600
STREAM_STALL_TIMEOUT = 60  # seconds without data before a backup stream is considered hung


class DeviceManager:
    def __init__(self, adb_path, fastboot_path, console, serial=None):
        self.adb_path = adb_path
        self.fastboot_path = fastboot_path
        self.console = console
        self.serial = serial

    # --- Raw command access ---

    def adb(self, *args, timeout=QUERY_TIMEOUT, show=False, stream=False, serial=True):
        cmd = [self.adb_path] + (["-s", self.serial] if serial and self.serial else []) + list(args)
        return run(cmd, timeout=timeout, console=self.console, show=show, stream=stream)

    def fastboot(self, *args, timeout=QUERY_TIMEOUT, show=False, stream=False, serial=True):
        cmd = [self.fastboot_path] + (["-s", self.serial] if serial and self.serial else []) + list(args)
        return run(cmd, timeout=timeout, console=self.console, show=show, stream=stream)

    # --- Device selection ---

    def adb_devices(self):
        res = self.adb("devices", serial=False)
        if res.error:
            raise Refused(res.error)
        return parse_device_list(res.stdout)

    def fastboot_devices(self):
        res = self.fastboot("devices", serial=False)
        return parse_device_list(res.stdout) if res.ok else []

    def select_device(self, wait_seconds=0):
        """
        Waits up to wait_seconds for exactly one usable adb device (or the one named by --serial)
        and pins all further commands to it. Returns its serial.
        """
        deadline = time.monotonic() + wait_seconds
        announced = False
        while True:
            devices = self.adb_devices()
            if self.serial:
                devices = [d for d in devices if d[0] == self.serial]
            ready = [d for d in devices if d[1] == "device"]
            if len(ready) == 1 and len(devices) == 1:
                self.serial = ready[0][0]
                return self.serial
            if len(devices) > 1:
                listing = ", ".join(f"{s} ({st})" for s, st in devices)
                raise Refused(f"More than one device connected: {listing}. "
                              "Disconnect the others or pass --serial.")
            if time.monotonic() >= deadline:
                break
            if not announced:
                self.console.info("Waiting for an ADB device (connect the headset and allow USB debugging)...")
                announced = True
            time.sleep(2)

        if devices:
            state = devices[0][1]
            hints = {
                "unauthorized": "Put on the headset and accept the USB debugging prompt.",
                "offline": "Reconnect the USB cable.",
                "no": "USB permission problem (on Linux, check udev rules).",
            }
            raise Refused(f"Device {devices[0][0]} is '{state}'. {hints.get(state, '')}".strip())
        raise Refused("No ADB device found." + (f" (looking for serial {self.serial})" if self.serial else ""))

    # --- Queries (normal OS) ---

    def get_props(self):
        res = self.adb("shell", "getprop")
        if not res.ok:
            raise Refused(f"Could not read device properties ({res.describe_failure()}).")
        props = parse_getprop(res.stdout)
        if not props:
            raise Refused("Device returned no properties.")
        return props

    def try_root(self):
        """Restarts adbd as root. Returns True when the shell is confirmed to run as root."""
        res = self.adb("root", show=True)
        lowered = res.output.lower()
        if not res.ok or "cannot run as root" in lowered or "production builds" in lowered:
            return False
        if "already running as root" not in lowered:
            # adbd restarts; give the old connection time to drop before waiting for the new one.
            time.sleep(2)
            self.adb("wait-for-device", timeout=30)
        for _ in range(5):
            uid = self.adb("shell", "id", "-u")
            if uid.ok:
                return uid.stdout.strip() == "0"
            time.sleep(1)
        return False

    def get_slot_suffix(self):
        res = self.adb("shell", "getprop", "ro.boot.slot_suffix")
        suffix = res.stdout.strip() if res.ok else ""
        return suffix if is_valid_slot_suffix(suffix) else None

    def read_i2c_bus4(self):
        """Returns (responding i2c addresses, CmdResult) for the PVT1.1 HAL check."""
        res = self.adb(
            "shell",
            "ls /sys/bus/i2c/devices/4-0056/driver 2>&1; "
            "ls /sys/bus/i2c/devices/4-0010/driver 2>&1; "
            "i2cdetect -y 4",
        )
        return parse_i2c_addresses(res.stdout), res

    def shell(self, command, timeout=QUERY_TIMEOUT):
        return self.adb("shell", command, timeout=timeout)

    def stream_to_file(self, shell_command, dest, total, label, stall_timeout=STREAM_STALL_TIMEOUT):
        """
        Streams stdout of a device shell command (via `adb exec-out`, which is binary-safe)
        into dest, hashing on the fly. The process is killed if no data arrives for
        stall_timeout seconds. Returns (bytes_written, sha256_hex, exited_cleanly).
        """
        cmd = [str(self.adb_path), "-s", self.serial, "exec-out", shell_command]
        self.console.debug(f"> adb -s {self.serial} exec-out {shell_command}")
        digest = hashlib.sha256()
        written = 0
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        last_data = [time.monotonic()]
        stalled = threading.Event()
        finished = threading.Event()

        def watchdog():
            while not finished.wait(1):
                if time.monotonic() - last_data[0] > stall_timeout:
                    stalled.set()
                    proc.kill()
                    return

        threading.Thread(target=watchdog, daemon=True).start()
        try:
            with open(dest, "wb") as f:
                while block := proc.stdout.read1(1024 * 1024):
                    last_data[0] = time.monotonic()
                    f.write(block)
                    digest.update(block)
                    written += len(block)
                    self.console.progress(label, written, total)
            proc.wait()
        finally:
            finished.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            self.console.progress_done()
        if stalled.is_set():
            self.console.warn(f"{label}: no data for {stall_timeout}s; stream aborted.")
        return written, digest.hexdigest(), proc.returncode == 0 and not stalled.is_set()

    # --- State transitions ---

    def reboot_bootloader(self):
        return self.adb("reboot", "bootloader", show=True)

    def wait_for_fastboot(self, timeout):
        """Waits for this device's serial to appear in `fastboot devices`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(s == self.serial for s, _ in self.fastboot_devices()):
                return True
            time.sleep(1)
        return False

    def fastboot_getvar(self, name):
        """Returns the variable's value, or None if the query failed or timed out."""
        res = self.fastboot("getvar", name)
        return parse_fastboot_getvar(res.output, name) if res.ok else None

    def flash(self, partition, image_path):
        return self.fastboot("flash", partition, image_path, timeout=FLASH_TIMEOUT, stream=True)

    def oem_reboot_sideload(self):
        return self.fastboot("oem", "reboot-sideload", show=True)

    def wait_for_sideload(self, timeout):
        """
        Waits for the pinned device to appear in 'sideload' state. Returns its serial, or None on timeout.
        Recovery can report a different serial; that is accepted only when it is the one and only
        device connected, so a second device can never be mistaken for the headset.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            devices = self.adb_devices()
            if (self.serial, "sideload") in devices:
                return self.serial
            if len(devices) == 1 and devices[0][1] == "sideload":
                self.console.warn(f"Sideload device reports serial {devices[0][0]} (expected {self.serial}).")
                return devices[0][0]
            time.sleep(2)
        return None

    def sideload(self, zip_path, serial):
        cmd = [self.adb_path, "-s", serial, "sideload", zip_path]
        return run(cmd, timeout=SIDELOAD_TIMEOUT, console=self.console, stream=True)
