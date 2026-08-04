"""Tests for the config layer. Run: python test_config.py

The layer's one promise is that nothing about it is silent: the file's
values land, the command line beats them, and anything unrecognized —
key, section, choice, type — stops the tool with an error that names it.
So most of these tests are typos, and the assertion is that the typo
speaks. Stdlib only, no car, no adapter; parsers are the real tools'
own build_parser() output, because a config layer tested against toy
parsers would only prove it works on toys.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import obd_config
from obd_config import parse_with_config, default_config_path

# The tools import parser-clean by design (that is half the point of
# build_parser); pulling them in here is also the registry tripwire —
# a tool in TOOLS without a working build_parser fails the import test.
sys.path.insert(0, os.path.join(HERE, "extractor"))
sys.path.insert(0, os.path.join(HERE, "probe"))
sys.path.insert(0, os.path.join(HERE, "supervisor"))
import obd_feed
import obd_probe
import learn_gears
import supervisor as supervisor_mod

FAILED = []


def ok(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILED.append(name)


TMP = tempfile.mkdtemp(prefix="obd2_config_test_")
_n = 0


def cfg_file(cfg):
    """Write a config dict (or raw string) to a fresh temp file."""
    global _n
    _n += 1
    path = os.path.join(TMP, f"config_{_n}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(cfg if isinstance(cfg, str) else json.dumps(cfg))
    return path


def parse(builder, tool, cfg, argv=(), known=False):
    return parse_with_config(builder(), tool,
                             argv=list(argv) + ["--config", cfg_file(cfg)],
                             known=known)


def refuses(name, builder, tool, cfg, argv=(), saying=()):
    """The parse must die, and the error must name the problem."""
    try:
        parse(builder, tool, cfg, argv)
    except SystemExit as e:
        msg = str(e)
        missing = [s for s in saying if s not in msg]
        ok(name, not missing,
           f"error is missing the words {missing!r}: {msg!r}" if missing else msg)
        return
    ok(name, False, "parsed happily — the typo was silent")


# --- values land, and in the right order ----------------------------------------

print("precedence:")

args = parse(obd_feed.build_parser, "obd_feed", {"common": {"port": "COM9"}})
ok("file beats built-in default", args.port == "COM9", f"port={args.port!r}")
ok("untouched options keep their defaults", args.baud == 115200)

args = parse(obd_feed.build_parser, "obd_feed",
             {"common": {"port": "COM9"}}, argv=["--port", "COM1"])
ok("command line beats file", args.port == "COM1", f"port={args.port!r}")

args = parse(obd_feed.build_parser, "obd_feed",
             {"common": {"baud": 115200}, "obd_feed": {"baud": 500000}})
ok("tool section beats common", args.baud == 500000, f"baud={args.baud!r}")

args = parse(obd_probe.build_parser, "obd_probe",
             {"obd_probe": {"baud": "230400"}})
ok("string value reaches the option's type",
   args.baud == 230400 and isinstance(args.baud, int), f"baud={args.baud!r}")

args = parse(obd_feed.build_parser, "obd_feed", {"obd_feed": {"debug": True}})
ok("boolean lands on an on/off flag", args.debug is True)

args = parse(learn_gears.build_parser, "learn_gears",
             {"common": {"tcp": 40000}}, argv=["drive.csv"])
ok("common key for a different tool is left for it",
   not hasattr(args, "tcp"))

# --- the file loses arguments with the command line, never wins them ------------

print("mutual exclusion:")

args = parse(obd_feed.build_parser, "obd_feed",
             {"common": {"port": "COM3"}}, argv=["--replay", "d.csv"])
ok("configured port yields to --replay on the CLI",
   args.replay == "d.csv" and args.port is None,
   f"port={args.port!r} replay={args.replay!r}")

refuses("port AND replay in one file is refused",
        obd_feed.build_parser, "obd_feed",
        {"obd_feed": {"port": "COM3", "replay": "d.csv"}},
        saying=["mutually exclusive"])

# --- everything unrecognized speaks ---------------------------------------------

print("refusals:")

refuses("unknown key in a tool section",
        obd_feed.build_parser, "obd_feed",
        {"obd_feed": {"throtle_floor": 1}}, saying=["throtle_floor", "obd_feed"])

refuses("near-miss key suggests the fix",
        obd_feed.build_parser, "obd_feed",
        {"obd_feed": {"dash_gera": "hold"}}, saying=["dash_gera", "dash_gear"])

refuses("unknown section, with suggestion",
        obd_feed.build_parser, "obd_feed",
        {"obd_fed": {"port": "COM3"}}, saying=["obd_fed", "obd_feed"])

refuses("value outside an option's choices",
        obd_feed.build_parser, "obd_feed",
        {"obd_feed": {"units": "metricc"}}, saying=["units", "metricc"])

refuses("junk on an on/off flag",
        obd_feed.build_parser, "obd_feed",
        {"obd_feed": {"debug": "yes"}}, saying=["on/off", "debug"])

refuses("unconvertible value names the type",
        obd_probe.build_parser, "obd_probe",
        {"obd_probe": {"baud": "fast"}}, saying=["baud", "int"])

refuses("a positional is an input, not a setting",
        learn_gears.build_parser, "learn_gears",
        {"learn_gears": {"log": "x.csv"}}, argv=["drive.csv"],
        saying=["log"])

refuses("common key no tool recognizes",
        obd_feed.build_parser, "obd_feed",
        {"common": {"blorp": 1}}, saying=["blorp", "no tool"])

refuses("the file cannot choose its own location",
        obd_feed.build_parser, "obd_feed",
        {"common": {"config": "other.json"}}, saying=["config"])

refuses("invalid JSON says where",
        obd_feed.build_parser, "obd_feed", '{"common": {', saying=["JSON", "line"])

refuses("top level must be sections",
        obd_feed.build_parser, "obd_feed", "[1, 2]", saying=["object"])

refuses("a section must be an object",
        obd_feed.build_parser, "obd_feed", {"common": 3}, saying=["common"])

try:
    parse_with_config(obd_feed.build_parser(), "obd_feed",
                      argv=["--config", os.path.join(TMP, "nope.json")])
    ok("explicitly named missing file is an error", False, "parsed happily")
except SystemExit as e:
    ok("explicitly named missing file is an error", "does not exist" in str(e))

if not os.path.exists(default_config_path()):
    args = parse_with_config(obd_probe.build_parser(), "obd_probe", argv=[])
    ok("no config.json at all changes nothing", args.baud == 115200)
else:
    print("  (skip: repo has a real config.json — default-missing case not testable here)")

try:
    parse_with_config(obd_probe.build_parser(), "obd_probe",
                      argv=["--conf", cfg_file({"common": {"port": "COM9"}})])
    ok("abbreviated --config is refused", False,
       "parsed against the wrong defaults")
except SystemExit as e:
    ok("abbreviated --config is refused", "spell --config" in str(e), str(e))

try:
    parse_with_config(obd_feed.build_parser(), "not_a_tool", argv=[])
    ok("unregistered tool name is a programmer error", False)
except ValueError as e:
    ok("unregistered tool name is a programmer error", "TOOLS" in str(e))

# --- the supervisor: parse_known_args, and the file it hands its child ----------

print("supervisor:")

custom = cfg_file({"supervisor": {"stall_seconds": 5}})
args, rest = parse_with_config(supervisor_mod.build_parser(), "supervisor",
                               argv=["--config", custom, "--",
                                     "--port", "socket://127.0.0.1:35000"],
                               known=True)
ok("config lands through parse_known_args", args.stall_seconds == 5,
   f"stall_seconds={args.stall_seconds!r}")
ok("feed args pass through untouched",
   rest[-2:] == ["--port", "socket://127.0.0.1:35000"], f"rest={rest!r}")

argv = supervisor_mod.build_feed_argv(args, ["--port", "x"])
ok("non-default config is forwarded to the feed",
   "--config" in argv and custom in argv, f"argv={argv!r}")

args2, _ = parse_with_config(supervisor_mod.build_parser(), "supervisor",
                             argv=[], known=True)
argv2 = supervisor_mod.build_feed_argv(args2, ["--port", "x"])
ok("default config is not forwarded (the feed finds it itself)",
   "--config" not in argv2, f"argv={argv2!r}")

argv3 = supervisor_mod.build_feed_argv(args, ["--config", "theirs.json"])
ok("an explicit feed --config is not overridden",
   argv3.count("--config") == 1 and "theirs.json" in argv3, f"argv={argv3!r}")

# --- the registry: every section name maps to a parser that answers -------------

print("registry:")

for tool in obd_config.TOOLS:
    try:
        dests = obd_config._tool_dests(tool)
        ok(f"{tool} answers for its keys", bool(dests),
           "" if dests else "no configurable options?")
    except Exception as e:
        ok(f"{tool} answers for its keys", False, f"{type(e).__name__}: {e}")


print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all good.")
