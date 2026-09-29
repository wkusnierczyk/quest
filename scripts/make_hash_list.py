"""
Regenerates a pinned hash list, src/quest/data/hash/<device>/<version>.txt, from the firmware itself.

For every release it records the SHA256 of the firmware zip. For releases whose boot images are
flashed, it also extracts boot.img and vendor_boot.img with payload-dumper-go and records theirs.
The zip is downloaded from the release URL unless --zip points at a local copy.

Before writing, new hashes are compared with the existing list: a hash that differs is reported
and nothing is written unless --force is given.

Usage:
    uv run python scripts/make_hash_list.py v201
    uv run python scripts/make_hash_list.py v85 --zip path/to/firmware_v85.zip
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

from quest.common import Console, Refused, run, sha256_file
from quest.downloader import DownloadError, download
from quest.firmware_manager import BOOT_PARTITIONS, EXTRACT_TIMEOUT, TARGETS
from quest.safety import parse_hash_file
from quest.tool_manager import ToolManager

HASH_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "quest" / "data" / "hash"


def find_release(version):
    """Returns (device name, release, whether its boot images are flashed)."""
    for device, targets in TARGETS.items():
        releases = [t.boot_release for t in targets.values()] + [t.sideload_release for t in targets.values()]
        release = next((r for r in releases if r.version == version), None)
        if release:
            flashed = any(t.boot_release.version == version for t in targets.values())
            return device, release, flashed
    known = sorted({r.version for ts in TARGETS.values() for t in ts.values()
                    for r in (t.boot_release, t.sideload_release)})
    raise Refused(f"Unknown release '{version}'. Known: {', '.join(known)}")


def compute_hashes(release, flashed, zip_path, workdir, console):
    hashes = {f"firmware_{release.version}.zip": sha256_file(zip_path, console, f"Hashing {zip_path.name}")}
    if flashed:
        tools = ToolManager(console)
        tools.ensure_dumper(install=True)
        images = workdir / "images"
        res = run([tools.dumper_path, "-o", images, "-p", ",".join(BOOT_PARTITIONS), zip_path],
                  timeout=EXTRACT_TIMEOUT, console=console, stream=console.is_tty)
        if not res.ok:
            raise Refused(f"Extraction failed ({res.describe_failure()}).")
        for part in BOOT_PARTITIONS:
            hashes[f"{part}.img"] = sha256_file(images / f"{part}.img")
    return hashes


def render(hashes):
    lines = ['"Algorithm","Hash","File"'] + [f'"SHA256","{h.upper()}","{name}"' for name, h in sorted(hashes.items())]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Regenerate a pinned firmware hash list.")
    parser.add_argument("version", help="release version, e.g. v72, v85, v201")
    parser.add_argument("--zip", type=Path, help="use this local firmware zip instead of downloading it")
    parser.add_argument("--workdir", type=Path, help="directory for temporary files (default: system temp)")
    parser.add_argument("--force", action="store_true", help="write even if an existing hash changed")
    args = parser.parse_args(argv)
    console = Console()

    try:
        device, release, flashed = find_release(args.version)
        out = HASH_SOURCE_ROOT / device / f"{release.version}.txt"
        # Read the current list first, so a malformed one fails before any download.
        existing = parse_hash_file(out.read_text(encoding="utf-8")) if out.exists() else {}
        if args.zip is not None and not args.zip.is_file():
            raise Refused(f"No such file: {args.zip}")
        with tempfile.TemporaryDirectory(dir=args.workdir) as tmp:
            workdir = Path(tmp)
            zip_path = args.zip
            if zip_path is None:
                zip_path = workdir / f"firmware_{release.version}.zip"
                console.info(f"Downloading {release.url}")
                download(release.url, zip_path, console, label=zip_path.name)
            hashes = compute_hashes(release, flashed, zip_path, workdir, console)
    except ValueError as e:
        console.error(f"Existing hash list {out} is malformed: {e}")
        return 1
    except (Refused, DownloadError, OSError) as e:
        console.error(str(e))
        return 1

    changed = False
    for name, digest in sorted(hashes.items()):
        old = existing.get(name)
        status = "new" if old is None else "unchanged" if old == digest else "CHANGED (was " + old + ")"
        changed |= status.startswith("CHANGED")
        console.info(f"  {name:<22} {digest}  {status}")
    dropped = sorted(set(existing) - set(hashes))
    if dropped:
        console.info(f"  dropping {len(dropped)} entries the tool does not use: {', '.join(dropped)}")
    if changed and not args.force:
        console.error(f"A pinned hash changed; not writing {out}. Investigate, then rerun with --force.")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.{os.getpid()}.tmp")
    tmp.write_text(render(hashes), encoding="utf-8")
    tmp.replace(out)  # atomic: the list is never left half-written
    console.ok(f"Wrote {out.relative_to(HASH_SOURCE_ROOT.parents[3])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
