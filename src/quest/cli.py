"""
`quest` command-line interface.

Run `quest convert` for the whole sequence, or the individual steps:
setup -> check -> [backup] -> download -> convert.
"""
import argparse
import sys
from pathlib import Path

from . import __version__
from .about import package_info, render_about, supports_unicode
from .common import (
    DATA_DIR,
    EXIT_DEVICE_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    FIRMWARE_ROOT,
    HASH_ROOT,
    PLATFORM_TOOLS_DIR,
    AlreadyConverted,
    Console,
    ConversionFailed,
    Refused,
    data_dir_lock,
    discard_stdout,
)
from .device_manager import DeviceManager
from .firmware_manager import TARGETS, FirmwareManager
from .tool_manager import ToolManager
from .workflow import backup, convert, inspect_device

DEFAULT_DEVICE = "Eureka"
TARGET_CHOICES = sorted(TARGETS[DEFAULT_DEVICE])


def _device(args, console, tools):
    return DeviceManager(tools.adb_path, tools.fastboot_path, console, serial=args.serial)


def _check_target_arg(args, info):
    if args.target and args.target != info.target:
        raise Refused(f"--target {args.target} does not match this device: its incremental "
                      f"({info.incremental}) requires target {info.target}.")


def cmd_setup(args, console):
    console.step("Installing tools")
    ToolManager(console).ensure(install=True, need_dumper=True, need_drivers=not args.no_drivers)
    console.ok("Setup complete.")


def cmd_check(args, console):
    tools = ToolManager(console)
    tools.ensure(install=False)
    console.step("Checking device")
    inspect_device(_device(args, console, tools), console, args.wait)


def cmd_download(args, console):
    tools = ToolManager(console)
    if args.target:
        device_name, target_name = DEFAULT_DEVICE, args.target
    else:
        tools.ensure(install=True)
        console.step("Checking device")
        info = inspect_device(_device(args, console, tools), console, args.wait)
        device_name, target_name = info.device_name, info.target
    tools.ensure_dumper(install=True)
    console.step("Preparing firmware")
    fw = FirmwareManager(device_name, console, tools.dumper_path)
    fw.prepare(fw.target(target_name))
    console.ok(f"Firmware for target {target_name} is ready and verified.")


def cmd_verify(args, console):
    fw = FirmwareManager(DEFAULT_DEVICE, console, ToolManager(console).dumper_path)
    failed = []
    for name in [args.target] if args.target else TARGET_CHOICES:
        console.step(f"Verifying target {name}")
        if not fw.verify_target(fw.target(name)):
            failed.append(name)
    if failed:
        raise Refused(f"Verification failed for: {', '.join(failed)}. Run: quest download")
    console.ok("All checked firmware verified.")


def cmd_backup(args, console):
    tools = ToolManager(console)
    tools.ensure(install=False)
    console.step("Backing up partitions")
    dest_root = Path(args.output).expanduser() if args.output else Path.cwd() / "quest-backups"
    backup(_device(args, console, tools), console, dest_root, include_mapper=not args.no_mapper,
           verify_on_device=args.verify_on_device, wait_seconds=args.wait)


def cmd_info(args, console):
    tools = ToolManager(console)
    status = {True: "installed", False: "not installed"}
    console.info(f"Data directory:     {DATA_DIR}  (override with QUEST_HOME)")
    console.info(f"Firmware:           {FIRMWARE_ROOT}")
    console.info(f"Tools:              {PLATFORM_TOOLS_DIR}")
    console.info(f"  platform-tools:   {status[tools.platform_tools_installed()]}")
    console.info(f"  payload-dumper:   {status[tools.dumper_installed()]}")
    console.info(f"Pinned hash lists:  {HASH_ROOT}")


def cmd_convert(args, console):
    tools = ToolManager(console)
    console.step("Step 1/4: Tools")
    tools.ensure(install=True)

    console.step("Step 2/4: Device")
    dev = _device(args, console, tools)
    info = inspect_device(dev, console, args.wait)
    _check_target_arg(args, info)

    console.step("Step 3/4: Firmware")
    tools.ensure_dumper(install=True)
    fw = FirmwareManager(info.device_name, console, tools.dumper_path)
    prepared = fw.prepare(fw.target(info.target))

    console.step("Step 4/4: Convert")
    convert(dev, fw, console, info, prepared)


def print_about(stream=None):
    stream = stream or sys.stdout
    print(render_about(package_info(), unicode=supports_unicode(stream)), file=stream)


def cmd_about(args, console):
    print_about()


class _AboutAction(argparse.Action):
    """`--about`: print the about stanza and exit, like --version."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        print_about()
        parser.exit()


def cmd_help(args, console):
    if args.topic is None:
        args.parser.print_help()
    elif args.topic in args.commands:
        args.commands[args.topic].print_help()
    else:
        args.parser.error(f"unknown command '{args.topic}' (choose from {', '.join(args.commands)})")


class _Parser(argparse.ArgumentParser):
    """Exits with EXIT_USAGE on bad command lines: argparse's default 2 means 'failed mid-conversion' here."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


class _Formatter(argparse.HelpFormatter):
    def _format_action(self, action):
        text = super()._format_action(action)
        if action.nargs == argparse.PARSER:  # drop the bare "COMMAND" line above the list of commands
            text = text.split("\n", 1)[1]
        return text


def _add_help_option(parser):
    # "-?" is accepted too; in zsh it must be escaped or quoted (quest -\? or quest '-?').
    parser.add_argument("-h", "-?", "--help", action="help", help="show this help and exit")


def build_parser():
    parser = _Parser(
        prog="quest",
        description="Convert a userdebug Meta Quest 3 back to user firmware.",
        epilog="Exit codes: 0 success, 1 refused/failed with the device unchanged, "
               "2 failed mid-conversion (recovery steps are printed), 64 usage error, 130 interrupted.",
        add_help=False,
        formatter_class=_Formatter,
    )
    _add_help_option(parser)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--about", action=_AboutAction, help="show version, developer, source and licence")
    parser.add_argument("-v", "--verbose", action="store_true", help="show every command and its output")
    parser.add_argument("--log-file", metavar="PATH", help="append a full log to this file")
    parser.add_argument("--serial", help="adb serial of the headset (required if several devices are connected)")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", title="commands")
    commands = {}

    def add(name, func, help_text, wait=False, target=False, writes_data=False, banner=True):
        p = sub.add_parser(name, help=help_text, description=help_text, add_help=False)
        _add_help_option(p)
        p.set_defaults(func=func, writes_data=writes_data, banner=banner)
        commands[name] = p
        if wait:
            p.add_argument("--wait", type=int, default=60, metavar="SECONDS",
                           help="how long to wait for the headset to appear (default: 60)")
        if target:
            p.add_argument("--target", choices=TARGET_CHOICES,
                           help="firmware target (default: derived from the connected device)")
        return p

    # Listed in the order they are used.
    p = add("setup", cmd_setup, "install platform-tools, payload-dumper-go and (Windows) the USB driver",
            writes_data=True)
    p.add_argument("--no-drivers", action="store_true", help="skip the Windows USB driver")
    add("check", cmd_check, "check that the connected headset can be converted (read-only)", wait=True)
    p = add("backup", cmd_backup, "stream a full partition backup (except userdata) to this computer", wait=True)
    p.add_argument("--no-mapper", action="store_true", help="skip the logical (dm) partitions")
    p.add_argument("--verify-on-device", action="store_true",
                   help="also hash each partition on the headset and compare (slower)")
    p.add_argument("--output", metavar="DIR", help="backup root directory (default: ./quest-backups)")
    add("download", cmd_download, "download, extract and verify the firmware for the headset or --target",
        wait=True, target=True, writes_data=True)
    add("verify", cmd_verify, "verify already-downloaded firmware against the pinned hashes (no device needed)",
        target=True)
    add("convert", cmd_convert, "run the full conversion: setup, check, download, flash, sideload",
        wait=True, target=True, writes_data=True)
    add("info", cmd_info, "show where tools and firmware are stored and what is installed")
    add("about", cmd_about, "show version, developer, source and licence", banner=False)
    p = add("help", cmd_help, "show this list of commands, or the help for one command", banner=False)
    p.add_argument("topic", nargs="?", metavar="COMMAND", help="command to show help for")
    parser.set_defaults(parser=parser, commands=commands)
    return parser


def main(argv=None):
    try:
        try:
            return _main(argv)
        finally:
            sys.stdout.flush()  # surface a closed output pipe here, not as a warning at interpreter exit
    except BrokenPipeError:
        # Plain output (help, about, version) whose reader stopped early, e.g. `quest help | head`.
        # Console output handles a closed pipe itself, so it can never interrupt work on the headset.
        discard_stdout()
        return EXIT_OK


def _main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_OK
    console = Console(verbose=args.verbose, log_path=args.log_file)
    if args.banner:
        console.info(f"{package_info()['name']} {__version__}")
    try:
        if args.writes_data:
            with data_dir_lock():
                args.func(args, console)
        else:
            args.func(args, console)
        return EXIT_OK
    except AlreadyConverted as e:
        console.ok(str(e))
        return EXIT_OK
    except Refused as e:
        console.error(str(e))
        return EXIT_REFUSED
    except ConversionFailed as e:
        console.error(str(e))
        console.info("\nWhat to do now:\n" + e.recovery)
        return EXIT_DEVICE_FAILED
    except KeyboardInterrupt:
        console.error("Interrupted.")
        return EXIT_INTERRUPTED
    finally:
        console.close()

