"""
Pure, side-effect-free decision logic: output parsers and the rules that decide whether a
device may be converted. Everything safety-critical lives here so it can be unit tested.
"""
import csv
import io
import re

# Supported hardware: ro.hardware value -> device name used for firmware/hash directories.
SUPPORTED_HARDWARE = {
    "eureka": "Eureka",  # Quest 3
}

# v85 target: the device's current incremental must be <= this baseline.
V72_BASELINE_INCREMENTAL = 50473320084200510
# v201 target: the device's current incremental must exactly match one of these.
V201_INCREMENTALS = frozenset({51780870000000521, 51822030000000521})
# Incrementals of the user firmware each target sideloads: a user build on one of these is already converted.
CONVERTED_INCREMENTALS = {
    51764730125000520: "v85",
    51943020036500520: "v201",
}

PVT11_MODEL = "Eureka PVT1.1"
SLOT_SUFFIX_RE = re.compile(r"_[ab]")
PARTITION_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


# --- Parsers ---------------------------------------------------------------------------

def parse_getprop(text):
    """Parses `adb shell getprop` output ("[key]: [value]" per line) into a dict."""
    return dict(re.findall(r"^\[([^\]]+)\]: \[(.*)\]\s*$", text, re.MULTILINE))


def parse_device_list(text):
    """
    Parses `adb devices` / `fastboot devices` output into [(serial, state), ...].
    Header, daemon-startup ("* daemon ...") and blank lines are ignored.
    """
    devices = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def parse_fastboot_getvar(text, name):
    """Extracts a variable from `fastboot getvar <name>` output (printed on stderr as "name: value")."""
    match = re.search(rf"^(?:\(bootloader\)\s*)?{re.escape(name)}:\s*(\S*)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def parse_incremental(value):
    try:
        incremental = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return incremental if incremental > 0 else None


def parse_i2c_addresses(raw_output):
    """
    Parses an `i2cdetect -y <bus>` grid and returns the set of addresses that responded
    (a hex value, or "UU" when a kernel driver holds the address).

    Grid rows are fixed-width: "70: -- -- 72 ..." where each cell occupies 3 characters.
    Unscanned cells (e.g. 0x00-0x02 in row 00) are blank, so cells must be located by
    column position rather than by whitespace splitting.
    """
    addresses = set()
    for line in raw_output.splitlines():
        match = re.match(r"^([0-7][0-9a-fA-F]):(.*)$", line.rstrip())
        if not match:
            continue
        row_base = int(match.group(1), 16)
        if row_base % 16:
            continue
        cells = match.group(2)
        for col in range(16):
            cell = cells[col * 3 + 1: col * 3 + 3]
            if cell == "UU" or re.fullmatch(r"[0-9a-fA-F]{2}", cell):
                addresses.add(row_base + col)
    return addresses


def parse_hash_file(text):
    """
    Parses a hash list ("Algorithm","Hash","File" CSV) into {filename: lowercase sha256}.
    Raises ValueError on anything unexpected rather than silently skipping rows.
    """
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header != ["Algorithm", "Hash", "File"]:
        raise ValueError(f"unexpected header: {header}")
    hashes = {}
    for line_no, row in enumerate(reader, start=2):
        if not row:
            continue
        if len(row) != 3:
            raise ValueError(f"line {line_no}: expected 3 columns, got {len(row)}")
        algorithm, digest, filename = row
        digest = digest.strip().lower()
        if algorithm.upper() != "SHA256" or not SHA256_RE.fullmatch(digest):
            raise ValueError(f"line {line_no}: not a SHA256 entry")
        if filename in hashes and hashes[filename] != digest:
            raise ValueError(f"line {line_no}: conflicting entries for {filename}")
        hashes[filename] = digest
    return hashes


# --- Rules -----------------------------------------------------------------------------

def match_target(incremental):
    """
    Returns the conversion target valid for the device's current OS incremental:
    "v201", "v85", or None when no conversion path exists.
    """
    if incremental is None:
        return None
    if incremental in V201_INCREMENTALS:
        return "v201"
    if incremental <= V72_BASELINE_INCREMENTAL:
        return "v85"
    return None


def is_bootloader_unlocked(props):
    return props.get("ro.boot.flash.locked") == "0" and props.get("ro.boot.verifiedbootstate") == "orange"


def is_pvt11(props):
    return props.get("ro.vendor.product.model_extended", "").strip() == PVT11_MODEL


def already_converted(props):
    """
    Returns the target name if the device is in the state a conversion leaves it in - a target's
    user firmware on supported hardware with the bootloader still unlocked (the tool never relocks
    it) - else None. A stock, locked headset on the same firmware was never converted.
    """
    if (props.get("ro.hardware") not in SUPPORTED_HARDWARE or props.get("ro.build.type") != "user"
            or not is_bootloader_unlocked(props)):
        return None
    return CONVERTED_INCREMENTALS.get(parse_incremental(props.get("ro.build.version.incremental")))


def compatibility_problems(props):
    """Returns a list of human-readable reasons the device cannot be converted (empty = compatible)."""
    problems = []
    hardware = props.get("ro.hardware", "")
    if hardware not in SUPPORTED_HARDWARE:
        problems.append(f"Unsupported hardware ({hardware or 'unknown'}).")
    build_type = props.get("ro.build.type", "")
    if build_type != "userdebug":
        problems.append(f"Build type is '{build_type or 'unknown'}'; 'userdebug' is required.")
    if not is_bootloader_unlocked(props):
        problems.append("Bootloader is locked (requires ro.boot.flash.locked=0 and verifiedbootstate=orange).")
    incremental_raw = props.get("ro.build.version.incremental", "")
    incremental = parse_incremental(incremental_raw)
    if incremental is None:
        problems.append(f"Could not parse the OS incremental ('{incremental_raw}').")
    elif match_target(incremental) is None:
        problems.append(f"OS incremental {incremental} is not supported by any known conversion path.")
    return problems


def hal_is_safe(addresses):
    """
    PVT1.1 HAL safety rule on i2c bus 4. Safe when:
      0x10 and 0x20 both respond, or 0x20 and 0x56 both respond (other addresses may also
      respond), or exactly {0x75}, or exactly {0x74}.
    Everything else - nothing detected, a lone 0x10/0x20/0x56, other combinations - is unsafe.
    """
    return (
        {0x10, 0x20} <= addresses
        or {0x20, 0x56} <= addresses
        or addresses == {0x75}
        or addresses == {0x74}
    )


def is_valid_slot_suffix(suffix):
    return bool(suffix) and bool(SLOT_SUFFIX_RE.fullmatch(suffix))


def is_safe_partition_name(name):
    return bool(PARTITION_NAME_RE.fullmatch(name)) and name not in (".", "..")
