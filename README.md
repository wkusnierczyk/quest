# Quest User Firmware Installer

`quest` is a command-line tool that converts a **userdebug** Meta Quest 3 (Eureka) back to **user** firmware.

> Flashing firmware can brick a headset. The tool refuses anything it cannot verify, but
> read the summary it prints before typing `CONVERT`, and keep the headset charged and connected.

## Install

Requires [uv](https://docs.astral.sh/uv/) (or pipx) and Python 3.10+.

```sh
uv tool install git+https://github.com/wkusnierczyk/quest
# or, from a local checkout:
uv tool install .
```

This puts a `quest` command on your `PATH` (run `uv tool update-shell` once if it is not found).
On macOS with the python.org Python, install with `uv tool install --with certifi ...`
if downloads fail with a certificate error.

Upgrade with `uv tool upgrade quest-user-firmware-installer`, remove with
`uv tool uninstall quest-user-firmware-installer`.

## Converting a headset, step by step

A successful conversion is one-way: afterwards the headset runs user firmware and is no
longer userdebug. Do steps 0-3 first.

### 0. Prepare the headset

1. Charge it well. The sideload and the install that follows take a while.
2. Boot it **normally** (into its regular OS, not the bootloader).
3. Connect it to the computer with a USB **data** cable.
4. If a USB debugging prompt appears inside the headset, accept it.

Every step below starts from this state. `quest` talks to the running OS first, and reads
the active slot and device properties from it before it reboots anything.

### 1. Install the tools and check the headset (changes nothing)

```sh
quest setup
quest check
```

`check` prints the hardware, build, bootloader state and active slot. It must end with:

```
[OK] Compatible. Conversion target for incremental <N>: v85   (or v201)
```

If it refuses instead, it lists the reasons, for example a locked bootloader, a non-userdebug
build, an unsupported firmware version, or a failed PVT1.1 HAL safety check. Do not continue.

### 2. Check the bootloader lock state (changes nothing)

`quest convert` refuses to flash unless fastboot reports the bootloader as unlocked. To check
this before converting, use the serial that `quest check` printed (the `Serial:` line), so the
commands can only reach the headset even if other devices are connected:

```sh
adb -s <serial> reboot bootloader
fastboot -s <serial> getvar unlocked      # must print: unlocked: yes
fastboot -s <serial> reboot               # back to the normal OS
```

These use the `adb` and `fastboot` that `quest setup` installed. They live in the `Tools:`
directory that `quest info` prints. That path can contain spaces, so quote it, for example on macOS:

```sh
PT="$HOME/Library/Application Support/quest/tools/platform-tools"
"$PT/adb" -s <serial> reboot bootloader
"$PT/fastboot" -s <serial> getvar unlocked
"$PT/fastboot" -s <serial> reboot
```

Any other recent `adb`/`fastboot` on your `PATH` works too.
If it does not print `unlocked: yes`, the conversion would stop before writing anything.

### 3. Back up the headset (recommended; reads only)

This is your only way back if something goes wrong.

```sh
quest backup --verify-on-device
```

- Needs adb root (standard on userdebug builds).
- Needs free space for the whole backup (about 14 GB for a Quest 3) where it is written. The tool checks this before it starts.
- `--verify-on-device` also hashes each partition on the headset and compares. It is slower, but it proves the copy is exact.
- Keep the backup private. It contains device-specific partitions (calibration and security data),
  so don't upload or share it.

Files land in the directory you run the command from, unless you pass `--output DIR`.
Each run creates a new folder:

```
./quest-backups/
└── <serial>_<YYYYMMDD-HHMMSS>/
    ├── by-name/        # physical partitions (boot_a.img, vendor_boot_a.img, vbmeta_a.img, ... - all except userdata)
    ├── mapper/         # logical partitions (system_a.img, vendor_a.img, product_a.img, ...)
    └── SHA256SUMS.csv  # SHA256 of every image
```

A successful backup reports `[OK] Backup of N partitions saved to <folder>` and exits with code 0.
If any partition fails, it exits with code 1, lists the failures, and says where the partial backup is.

### 4. Convert

```sh
quest --log-file convert.log convert
```

This installs any missing tools, re-runs the checks, and downloads and verifies the firmware
(1.4-2.7 GB depending on the target; see [Where the firmware comes from](#where-the-firmware-comes-from)).
It then prints a summary and asks you to type `CONVERT`. **Check the
summary first:**

- Serial: your headset's.
- Target and active slot: the ones `quest check` reported.
- It will flash `boot<slot>` and `vendor_boot<slot>`, then sideload the firmware zip.

After you confirm, it runs these steps. Leave the headset connected throughout:

1. Reboots to the bootloader and waits for fastboot.
2. Checks that fastboot reports `unlocked: yes`.
3. Flashes `boot<slot>`, then `vendor_boot<slot>`.
4. Reboots into sideload mode (detected within 3 minutes).
5. Sideloads the firmware. It finishes either cleanly or with this warning, which is normal:
   `adb reported an error after the transfer completed`. It happens when recovery drops the
   connection to start installing.
6. The headset installs the update and reboots on its own. Leave it connected until it has fully booted.

If `convert` exits with code **2**, it stopped mid-conversion. Follow the recovery commands it
prints (they are specific to the step that failed) and keep `convert.log`.

### 5. Confirm

```sh
quest check
```

Once the headset has fully booted (with USB debugging enabled), this should report:

```
[OK] Already on user firmware (v85, 51764730125000520). Nothing to convert.
```

The target and incremental are the ones you converted to. `check` exits with code 0. Running
`quest convert` again at this point does nothing: no downloads, no reboot.

## Command reference

```sh
quest setup              # install pinned platform-tools, payload-dumper-go, (Windows) USB driver
quest check              # read-only compatibility and PVT1.1 HAL safety checks
quest backup             # stream all partitions (except userdata) to ./quest-backups/
quest download           # download, extract and verify the firmware this headset needs (or --target)
quest verify             # re-verify downloaded firmware (no headset needed)
quest convert            # full run: tools, checks, firmware, flash, sideload
quest info               # where tools and firmware are stored
quest about              # version, developer, source and licence
quest help [COMMAND]     # list the commands, or show the options of one command
```

`quest`, `quest help`, `quest -h` and `quest --help` all list the commands. `quest help COMMAND`
and `quest COMMAND --help` show a command's options. `-?` works as well, but zsh (the default
shell on macOS) treats `?` as a wildcard, so type it as `quest -\?` or `quest '-?'`.

Global options: `--serial SERIAL` (when several devices are connected), `-v/--verbose`,
`--log-file PATH`, `--version`.

Exit codes: `0` success, `1` refused or failed with the headset untouched, `2` failed
mid-conversion (recovery commands are printed), `64` invalid command line, `130` interrupted.

`setup`, `download` and `convert` lock the data directory, so only one of them can run at a time.

Output can be piped (e.g. `quest help | head`). If the reader stops early, `quest` keeps working
and only drops the rest of its screen output, so a conversion is never interrupted this way;
`--log-file` still receives everything.

### Where files go

Tools and firmware (several GB) are stored in a per-user data directory, shared by all
working directories:

| OS      | Location                                  |
|---------|-------------------------------------------|
| macOS   | `~/Library/Application Support/quest`     |
| Linux   | `$XDG_DATA_HOME/quest` (`~/.local/share/quest`) |
| Windows | `%LOCALAPPDATA%\quest`                    |

Set `QUEST_HOME` to use a different directory. Backups go to `./quest-backups` in the
current directory unless `--output` is given.

### Where the firmware comes from

`quest` does not ship any firmware. It downloads Meta's firmware packages from a
community-run mirror (`files.cocaine.trade`), not from Meta. Every file is checked against the
SHA256 hashes pinned in this repository (`src/quest/data/hash/`). A file that does not match is
deleted and never used. If the mirror is unavailable, `download` and `convert` cannot fetch
firmware.

## Develop

```sh
uv sync                  # create .venv with the package (editable) and dev tools
uv run quest --help
uv run pytest
uv run ruff check .
uv build                 # sdist + wheel in dist/
```

Layout: `src/quest/` (package; pinned hash lists in `src/quest/data/hash/`), `tests/`, `scripts/`.

The pinned hash lists are generated from the firmware itself, not typed in. Each list holds the
SHA256 of the firmware zip and, for releases whose boot images are flashed, of `boot.img` and
`vendor_boot.img`. To regenerate or add one (it downloads the zip unless `--zip` is given, and
refuses to overwrite a hash that changed unless `--force` is given):

```sh
uv run python scripts/make_hash_list.py v201
```

## Acknowledgements

Inspired by [Quest Userdebug Converter](https://github.com/Lumince/Quest_Userdebug_Converter)
by Lumince. Thanks for the inspiration.

## About

```sh
$ quest about
Quest User Firmware Installer: Convert a userdebug Meta Quest 3 back to user firmware
├─ version:    1.0.0
├─ developer:  mailto:waclaw.kusnierczyk@gmail.com
├─ source:     https://github.com/wkusnierczyk/quest
└─ licence:    MIT https://opensource.org/licenses/MIT
```
