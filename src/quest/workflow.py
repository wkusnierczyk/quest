"""
The conversion steps. Each public function is one CLI subcommand's worth of work and raises
Refused (device untouched) or ConversionFailed (device mid-conversion, with recovery steps).
"""
import re
import shutil
from dataclasses import dataclass
from datetime import datetime

from .common import AlreadyConverted, ConversionFailed, Refused, runnable_cmd
from .safety import (
    SUPPORTED_HARDWARE,
    already_converted,
    compatibility_problems,
    hal_is_safe,
    is_pvt11,
    is_safe_partition_name,
    match_target,
    parse_incremental,
)

FASTBOOT_WAIT = 60
SIDELOAD_WAIT = 180
CONFIRM_PHRASE = "CONVERT"


@dataclass
class DeviceInfo:
    serial: str
    props: dict
    device_name: str       # e.g. "Eureka"
    incremental: int
    target: str            # "v85" or "v201"
    rooted: bool


def describe_device(props):
    return {
        "Hardware": SUPPORTED_HARDWARE.get(props.get("ro.hardware", ""), f"Unknown ({props.get('ro.hardware', '?')})"),
        "Revision": props.get("ro.vendor.product.model_extended", "unknown"),
        "Serial": props.get("ro.serialno", "unknown"),
        "Build": f"{props.get('ro.build.branch', 'unknown')} ({props.get('ro.build.version.incremental', '?')})",
        "Build type": props.get("ro.build.type", "unknown"),
        "Bootloader": "unlocked" if props.get("ro.boot.flash.locked") == "0" else "locked",
        "Boot state": props.get("ro.boot.verifiedbootstate", "unknown"),
        "Active slot": props.get("ro.boot.slot_suffix", "unknown"),
    }


def inspect_device(dev, console, wait_seconds=0):
    """Selects the device and runs every compatibility and safety check. Returns DeviceInfo."""
    serial = dev.select_device(wait_seconds)
    console.ok(f"Device {serial} connected.")

    props = dev.get_props()
    # Production (user) builds never grant adb root; don't try, and don't warn about it.
    rooted = False
    if props.get("ro.build.type") != "user":
        rooted = dev.try_root()
        if rooted:
            console.ok("adbd is running as root.")
        else:
            console.warn("Could not get adb root (root-dependent checks such as the PVT1.1 HAL check will fail).")

    for key, value in describe_device(props).items():
        console.info(f"  {key + ':':<13}{value}")

    converted = already_converted(props)
    if converted:
        raise AlreadyConverted(f"Already on user firmware ({converted}, "
                               f"{props.get('ro.build.version.incremental')}). Nothing to convert.")

    problems = compatibility_problems(props)
    if problems:
        raise Refused("Device is not compatible:\n  - " + "\n  - ".join(problems))

    if is_pvt11(props):
        console.info("PVT1.1 board detected; running HAL safety check...")
        addresses, res = dev.read_i2c_bus4()
        if not res.ok:
            hint = "" if rooted else " adb root was unavailable, so the bus could not be read."
            raise Refused(f"PVT1.1 HAL check could not read i2c bus 4 ({res.describe_failure()}).{hint} "
                          "Conversion blocked.")
        console.info(f"  i2c bus 4 responding: {', '.join(hex(a) for a in sorted(addresses)) or 'none'}")
        if not hal_is_safe(addresses):
            raise Refused("PVT1.1 board failed the HAL safety check. It may be damaged; converting "
                          "it could cause a permanent black screen on boot. Conversion blocked.")
        console.ok("PVT1.1 HAL check passed.")

    incremental = parse_incremental(props.get("ro.build.version.incremental"))
    target = match_target(incremental)
    console.ok(f"Compatible. Conversion target for incremental {incremental}: {target}")
    return DeviceInfo(serial, props, SUPPORTED_HARDWARE[props["ro.hardware"]], incremental, target, rooted)


# --- Backup ------------------------------------------------------------------------------

BY_NAME_DIR = "/dev/block/bootdevice/by-name"
MAPPER_DIR = "/dev/block/mapper"


def _list_partitions(dev, directory, predicate):
    res = dev.shell(f"ls {directory}")
    if not res.ok:
        raise Refused(f"Could not list {directory} ({res.describe_failure()}).")
    names = [n for n in res.stdout.split() if predicate(n)]
    skipped = [n for n in res.stdout.split() if not is_safe_partition_name(n)]
    if skipped:
        dev.console.warn(f"Skipping entries with unexpected names in {directory}: {skipped}")
    return [n for n in names if is_safe_partition_name(n)]


def _partition_size(dev, path):
    res = dev.shell(f"blockdev --getsize64 {path}")
    try:
        return int(res.stdout.strip())
    except ValueError:
        return None


def _new_backup_dir(dest_root, stem):
    """Atomically reserves a fresh directory so concurrent or back-to-back backups never share one."""
    for n in range(1, 100):
        dest = dest_root / (stem if n == 1 else f"{stem}-{n}")
        try:
            dest.mkdir()
            return dest
        except FileExistsError:
            continue
    raise Refused(f"Could not create a unique backup directory under {dest_root}.")


def backup(dev, console, dest_root, include_mapper=True, verify_on_device=False, wait_seconds=0):
    """
    Streams every partition (except userdata) straight to the host with `adb exec-out cat`,
    so nothing is staged on the device. Sizes are checked against the block device and a
    SHA256 manifest is written. Returns the backup directory. Raises Refused if anything failed.
    """
    serial = dev.select_device(wait_seconds)
    if not dev.try_root():
        raise Refused("Backup requires adb root (reading block devices needs root).")

    partitions = [(f"{BY_NAME_DIR}/{n}", "by-name", n)
                  for n in _list_partitions(dev, BY_NAME_DIR, lambda n: n != "userdata")]
    if include_mapper:
        partitions += [(f"{MAPPER_DIR}/{n}", "mapper", n)
                       for n in _list_partitions(dev, MAPPER_DIR, lambda n: n.endswith(("_a", "_b")))]
    if not partitions:
        raise Refused("No partitions found to back up.")

    sizes = {}
    for path, _, _ in partitions:
        size = _partition_size(dev, path)
        if size is None:
            raise Refused(f"Could not read the size of {path}.")
        sizes[path] = size
    total = sum(sizes.values())

    dest_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dest_root).free
    if free < total * 1.02:
        raise Refused(f"Not enough free space in {dest_root}: need {total / 1e9:.1f} GB, have {free / 1e9:.1f} GB.")
    dest = _new_backup_dir(dest_root, f"{serial}_{datetime.now():%Y%m%d-%H%M%S}")

    console.info(f"Backing up {len(partitions)} partitions ({total / 1e9:.1f} GB) to {dest}")
    manifest = []
    failures = []
    for i, (path, group, name) in enumerate(partitions, 1):
        out = dest / group / f"{name}.img"
        out.parent.mkdir(parents=True, exist_ok=True)
        label = f"[{i}/{len(partitions)}] {group}/{name}"
        written, digest, clean = dev.stream_to_file(f"cat {path} 2>/dev/null", out, sizes[path], label)
        if not clean or written != sizes[path]:
            failures.append(f"{group}/{name}: got {written} of {sizes[path]} bytes")
            continue
        if verify_on_device:
            res = dev.shell(f"sha256sum {path}", timeout=1800)
            device_digest = res.stdout.split()[0].lower() if res.ok and res.stdout.split() else ""
            if device_digest != digest:
                failures.append(f"{group}/{name}: on-device SHA256 differs")
                continue
        manifest.append((digest, f"{group}/{name}.img"))
        console.debug(f"{label}: {digest}")

    with open(dest / "SHA256SUMS.csv", "w", encoding="utf-8", newline="\n") as f:
        f.write('"Algorithm","Hash","File"\n')
        for digest, rel in manifest:
            f.write(f'"SHA256","{digest.upper()}","{rel}"\n')

    if failures:
        raise Refused("Backup incomplete:\n  - " + "\n  - ".join(failures) + f"\nPartial backup left in {dest}")
    console.ok(f"Backup of {len(manifest)} partitions saved to {dest}")
    console.info("Keep it private: it contains device-specific partitions (calibration and security data).")
    return dest


# --- Conversion ----------------------------------------------------------------------------

def preflight_summary(console, info, slot, prepared):
    console.info("")
    console.info("About to convert this headset:")
    console.info(f"  Serial:        {info.serial}")
    console.info(f"  Incremental:   {info.incremental}")
    console.info(f"  Target:        {prepared.target.name} ({prepared.target.description})")
    console.info(f"  Active slot:   {slot}")
    for part, image in prepared.boot_images.items():
        console.info(f"  {'Flash:':<15}{part}{slot} <- {image}")
    console.info(f"  Then sideload: {prepared.sideload_zip}")
    console.info("")
    console.info("Keep the headset connected and charged. Do not unplug it until the tool says it is done.")


def convert(dev, fw, console, info, prepared, confirm=None):
    """
    Flashes boot images to the active slot and sideloads the full OTA.
    `confirm` is called with the summary shown and must return True to proceed.
    """
    slot = dev.get_slot_suffix()
    if not slot:
        raise Refused("Could not determine the active slot (ro.boot.slot_suffix) from the OS.")

    # Re-validate right before acting: the device must still be the one that passed inspection.
    props = dev.get_props()
    problems = compatibility_problems(props)
    if problems:
        raise Refused("Device state changed since the check:\n  - " + "\n  - ".join(problems))
    same_device = props.get("ro.serialno") == info.props.get("ro.serialno")
    same_target = match_target(parse_incremental(props.get("ro.build.version.incremental"))) == prepared.target.name
    if not (same_device and same_target):
        raise Refused("Device identity or firmware changed since the check. Re-run the command.")

    preflight_summary(console, info, slot, prepared)
    confirm = confirm or (lambda: console.confirm_phrase("This will overwrite the boot partitions.", CONFIRM_PHRASE))
    if not confirm():
        raise Refused("Conversion cancelled; nothing was changed.")

    console.info("Re-verifying files immediately before flashing...")
    if not fw.recheck_boot_images(prepared):
        raise Refused("Boot images changed or are corrupt. Run: quest download")
    if not fw.verify_file(prepared.sideload_zip, prepared.expected[prepared.sideload_zip], use_cache=False):
        raise Refused("Sideload zip changed or is corrupt. Run: quest download")

    runner = _ConversionRunner(dev, console, info.serial, slot, prepared)
    try:
        runner.run()
    except KeyboardInterrupt:
        raise ConversionFailed("Interrupted.", runner.recovery()) from None
    except Refused as e:
        if runner.stage in ("os", "rebooting"):
            raise
        raise ConversionFailed(str(e), runner.recovery()) from None


class _ConversionRunner:
    """Executes the device-modifying sequence and knows how to explain recovery from each stage."""

    def __init__(self, dev, console, serial, slot, prepared):
        self.dev = dev
        self.console = console
        self.serial = serial
        self.slot = slot
        self.prepared = prepared
        self.stage = "os"
        self.flashed = []
        self.sideload_serial = serial

    def _flash_cmd(self, part):
        image = self.prepared.boot_images[part]
        return runnable_cmd([self.dev.fastboot_path, "-s", self.serial, "flash", f"{part}{self.slot}", image])

    def _sideload_cmd(self, serial=None):
        return runnable_cmd([self.dev.adb_path, "-s", serial or self.serial, "sideload", self.prepared.sideload_zip])

    def _fastboot_cmd(self, *args):
        return runnable_cmd([self.dev.fastboot_path, "-s", self.serial, *args])

    def recovery(self):
        remaining_flash = [self._flash_cmd(p) for p in self.prepared.boot_images if p not in self.flashed]
        if self.stage == "os":
            return "Nothing was written. The headset should still be in its normal OS."
        if self.stage == "rebooting":
            return ("Nothing was written. If the headset is in the bootloader, reboot it with:\n"
                    f"  {self._fastboot_cmd('reboot')}")
        if self.stage == "fastboot" and not self.flashed:
            return ("Nothing was written yet (a flash may have been cut short). Stay in the bootloader and retry\n"
                    "the conversion, or finish manually:\n  " + "\n  ".join(remaining_flash) +
                    f"\n  {self._fastboot_cmd('oem', 'reboot-sideload')}\n  {self._sideload_cmd()}")
        if self.stage == "fastboot":
            return ("The headset is in the bootloader with only some boot partitions written. Do NOT reboot to the\n"
                    "OS. Finish manually:\n  " + "\n  ".join(remaining_flash) +
                    f"\n  {self._fastboot_cmd('oem', 'reboot-sideload')}\n  {self._sideload_cmd()}")
        if self.stage == "sideload-wait":
            return ("Boot partitions are written; the headset should be booting into recovery. If it shows a\n"
                    "recovery menu, choose 'Apply update from ADB', then run:\n"
                    f"  {self._sideload_cmd()}\n"
                    "If it is back in the bootloader, run:\n"
                    f"  {self._fastboot_cmd('oem', 'reboot-sideload')}")
        return ("The sideload did not complete. If the headset is still in recovery/sideload mode, run:\n"
                f"  {self._sideload_cmd(self.sideload_serial)}\n"
                "Otherwise enter the bootloader, run the following, and sideload again:\n"
                f"  {self._fastboot_cmd('oem', 'reboot-sideload')}\n"
                "Do not boot the headset normally until the sideload succeeds.")

    def _fail(self, message):
        raise ConversionFailed(message, self.recovery())

    def run(self):
        dev, console = self.dev, self.console

        console.step("Rebooting to bootloader")
        self.stage = "rebooting"
        res = dev.reboot_bootloader()
        if not res.ok:
            raise Refused(f"'adb reboot bootloader' failed ({res.describe_failure()}); nothing was changed.")
        if not dev.wait_for_fastboot(FASTBOOT_WAIT):
            self._fail(f"Device {self.serial} did not appear in fastboot within {FASTBOOT_WAIT}s.")
        console.ok("Device is in fastboot.")

        unlocked = dev.fastboot_getvar("unlocked")
        if unlocked != "yes":
            state = "locked" if unlocked == "no" else f"unknown ({unlocked or 'query failed'})"
            self._fail(f"Fastboot reports the bootloader lock state as {state}; refusing to flash.")
        self.stage = "fastboot"

        console.step("Flashing boot images")
        for part, image in self.prepared.boot_images.items():
            partition = f"{part}{self.slot}"
            res = dev.flash(partition, image)
            if not res.ok:
                self._fail(f"Flashing {partition} failed ({res.describe_failure()}).")
            self.flashed.append(part)
            console.ok(f"Flashed {partition}.")

        console.step("Booting into sideload mode")
        res = dev.oem_reboot_sideload()
        if not res.ok:
            self._fail(f"'fastboot oem reboot-sideload' failed ({res.describe_failure()}).")
        self.stage = "sideload-wait"
        console.info(f"Waiting for sideload mode (up to {SIDELOAD_WAIT}s)...")
        sideload_serial = dev.wait_for_sideload(SIDELOAD_WAIT)
        if not sideload_serial:
            self._fail("Timed out waiting for the headset to enter sideload mode.")
        console.ok("Device is in sideload mode.")

        console.step("Sideloading firmware")
        self.stage = "sideloading"
        self.sideload_serial = sideload_serial
        res = dev.sideload(self.prepared.sideload_zip, sideload_serial)
        verdict = classify_sideload(res)
        if verdict == "failed":
            self._fail(f"Sideload failed ({res.describe_failure()}).")
        if verdict == "probably-ok":
            console.warn("adb reported an error after the transfer completed. This is normal when recovery "
                         "reboots right after installing. If the headset instead shows an error in recovery, "
                         f"run: {self._sideload_cmd(sideload_serial)}")
        self.stage = "done"
        console.ok("Sideload finished. The headset will install the update and reboot on its own.")
        console.info("Leave it connected until it has fully booted.")


def classify_sideload(res):
    """
    'ok' on a clean exit. 'probably-ok' when adb exits non-zero only after the transfer completed:
    either it reports a complete "Total xfer: N.NNx" ratio (N >= 1), or - with no ratio reported -
    the benign "failed to read command: Success" disconnect that recovery causes when it starts
    installing. A reported ratio below 1 is always 'failed'.
    """
    if res.ok:
        return "ok"
    if res.timed_out or res.error:
        return "failed"
    out = res.output
    ratio = re.search(r"Total xfer:\s*([0-9]+(?:\.[0-9]+)?)x", out)
    if ratio:
        return "probably-ok" if float(ratio.group(1)) >= 1.0 else "failed"
    if "failed to read command: Success" in out:
        return "probably-ok"
    return "failed"

