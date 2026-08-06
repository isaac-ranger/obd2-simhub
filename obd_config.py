#!/usr/bin/env python3
"""
obd_config.py — one config file for every tool in this repo
===========================================================

Once a rig works, the COM port stops changing. Typing it every run — or
burying it in a batch file that breaks when the repo moves — is the wrong
place to keep it. config.json in the repo root holds it instead, and any
tool here picks it up at startup:

    {
      "common":     { "port": "COM3", "baud": 115200 },
      "obd_feed":   { "dash_gear": "hold" },
      "supervisor": { "quiet": true }
    }

  - "common" is for values that describe the rig and are shared by more
    than one tool (the port, the baud). A tool applies the common keys it
    understands and leaves the rest for the tools that do.
  - A tool-named section applies to that tool only, and beats "common".
  - The command line beats everything. Precedence, highest first:
        CLI  >  tool section  >  common  >  built-in default
    (To keep that true, option abbreviations are disabled: an abbreviated
    option argparse recognizes but this layer's referee doesn't would let
    the file beat the command line. Spell options out in full.)
  - No config.json is fine: everything behaves exactly as before.
    --config PATH points a tool somewhere else (a missing default file is
    fine; a missing explicit one is an error — you asked for it by name).
  - Windows paths: JSON eats backslashes ("D:\\runs" or "D:/runs", never
    "D:\runs" — \r is an escape and becomes an invisible byte). A value
    that arrives with control characters in it is refused by name.

WHY THE KEYS ARE NOT LISTED ANYWHERE

A key is exactly a tool's command-line option with the dashes turned to
underscores (--dash-gear -> dash_gear), DERIVED from the tool's own
argparse parser at startup. An option added to any tool next year is
configurable the day it lands. A maintained table of keys would drift
from the parsers, and its failure mode is silent; this cannot drift.

Two kinds of option are refused as keys, for the same reason positionals
are: they aren't settings. A positional (learn_gears' drive log) is a
per-run input, and a verb (--register, --list-ports, --write) is a
per-run action — a config file that performed one on every start is a
haunting. Verbs are marked at their add_argument site with
`.per_run = True`, so the knowledge lives with the parser, not in a
second list here.

The one list that does exist is TOOLS below — section names have to map
to files somehow. Six lines that change only when a tool is born are a
different animal from forty-odd that change with every feature.

UNRECOGNIZED MEANS REFUSE TO START

A key nothing recognizes is a hard error, not a shrug. "throtle_floor"
quietly ignored is indistinguishable from "this setting doesn't work",
and that gets debugged over email from a paddock. The same goes for a
section that names no tool, a value outside an option's choices, a value
whose JSON type doesn't fit the option (null, a list, a number where
text belongs), and a non-boolean on an on/off flag. Every error names
the file, the key, and when the typo is close enough to see, the fix.
Keys in a section belonging to a tool that ISN'T running are checked
too, as loud stderr warnings — the day you run that tool it refuses,
but the typo speaks the first time anything runs. (--help is exempt
from all of it: a broken config must never block reading the manual.)

WHY THIS IS NOT calibration.json

calibration.json describes the car and is machine-written —
learn_gears.py --write does a read-modify-write on it. config.json
describes the installation and is yours to hand-edit. Different authors,
different lifetimes: a typo in your hand-edited file must never take a
calibration you paid for with a drive down with it, and re-learning
gears must never rewrite your serial port.

Requires: python 3.9+, stdlib only.
"""

import argparse
import difflib
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_NAME = "config.json"
COMMON = "common"

# section name -> the file whose argparse parser defines that section's
# keys (via its build_parser()), relative to the repo root.
# A new tool is one line here and nothing else.
TOOLS = {
    "obd_feed":    ("extractor", "obd_feed.py"),
    "obd_probe":   ("probe", "obd_probe.py"),
    "learn_gears": ("probe", "learn_gears.py"),
    "learn_throttle": ("probe", "learn_throttle.py"),
    "fake_car":    ("extractor", "fake_car.py"),
    "supervisor":  ("supervisor", "supervisor.py"),
    "report":      ("report.py",),
}

_SURFACE_CACHE = {}


def default_config_path():
    return os.path.join(HERE, CONFIG_NAME)


def parse_with_config(parser, tool, argv=None, known=False):
    """Drop-in replacement for parser.parse_args().

    Adds --config, folds the file's settings into the parser's defaults,
    then parses argv as normal — which is what makes the command line win
    without a single per-option line of plumbing. With known=True it
    stands in for parse_known_args() and returns (args, rest).
    """
    if tool not in TOOLS:
        raise ValueError(f"unregistered tool {tool!r} — one line in "
                         "obd_config.TOOLS is the price of a section name")
    if argv is None:
        argv = sys.argv[1:]

    # Abbreviations off, deliberately: argparse would accept --po for
    # --port while the exclusive-group referee below matches full names
    # only — and that mismatch let a config-file replay beat a typed
    # port. One rule ("spell it out") is cheaper than two resolvers
    # agreeing forever.
    parser.allow_abbrev = False

    parser.add_argument("--config", default=default_config_path(),
                        metavar="CONFIG_JSON",
                        help="settings file (default: config.json in the "
                             "repo root; sections: common plus one per tool, "
                             "keys are option names with underscores)")

    # A broken config must never block the manual: --help skips the file
    # entirely, so the command that lists the legal keys always runs.
    pre = argv[:argv.index("--")] if "--" in argv else argv
    if "-h" not in pre and "--help" not in pre:
        path, explicit = _config_path_from(argv)
        cfg = _load(path, explicit)
        if cfg:
            _apply(parser, tool, cfg, path, argv)
    else:
        path = default_config_path()

    result = parser.parse_known_args(argv) if known else parser.parse_args(argv)
    ns = result[0] if known else result

    # Belt to the allow_abbrev suspenders: if argparse and our pre-scan
    # somehow disagree about which file governs, the parse we just did
    # was against the wrong defaults — refuse rather than guess.
    if getattr(ns, "config", path) != path:
        sys.exit("config error: spell --config out in full — it decides "
                 "what the rest of the command line means")
    return result


def resolved_defaults(tool, argv):
    """Parse argv exactly as `tool` will at its own startup — same config
    file, same precedence — and return the namespace. For a parent that
    needs to know what its child will decide before spawning it (the
    supervisor asking whether the feed is a replay). Unknown argv entries
    are tolerated here; the child's own strict parse still owns them."""
    parser = _tool_module(tool).build_parser()
    return parse_with_config(parser, tool, argv=list(argv), known=True)[0]


# --------------------------------------------------------------------------
# the pieces
# --------------------------------------------------------------------------

def _config_path_from(argv):
    """Find --config before argparse runs, because the file it names has
    to be loaded before the real parse. Stops at `--`: everything after
    that belongs to somebody else's command line (see supervisor.py)."""
    path, explicit = default_config_path(), False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            break
        if a == "--config" and i + 1 < len(argv):
            path, explicit = argv[i + 1], True
            i += 2
            continue
        if a.startswith("--config="):
            path, explicit = a.split("=", 1)[1], True
        i += 1
    if explicit and not path:
        sys.exit("config error: --config needs a path")
    return path, explicit


def _no_json_constants(name):
    raise ValueError(f"{name} is not a number a settings file can hold")


def _no_duplicate_keys(pairs):
    """JSON silently last-wins on duplicate keys; 'silently' is the part
    this layer exists to kill."""
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"duplicate key {k!r} — the second value "
                             "would silently win")
        seen[k] = v
    return seen


def _load(path, explicit):
    try:
        # utf-8-sig: accepts what Notepad writes (UTF-8 with BOM) as well
        # as plain UTF-8.
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        if explicit:
            sys.exit(f"config error: {path} does not exist (named by --config)")
        return None
    except UnicodeDecodeError:
        sys.exit(f"config error ({path}): not saved as UTF-8 — re-save it "
                 "as UTF-8 in your editor (Notepad: Save As -> "
                 "Encoding: UTF-8)")
    except OSError as e:
        sys.exit(f"config error: cannot read {path}: {e}")
    try:
        # parse_constant: Python's json quietly accepts NaN/Infinity, and a
        # NaN dwell would freeze the gear readout forever with no error.
        cfg = json.loads(text, object_pairs_hook=_no_duplicate_keys,
                         parse_constant=_no_json_constants)
    except json.JSONDecodeError as e:
        sys.exit(f"config error ({path}): not valid JSON — line {e.lineno} "
                 f"column {e.colno}: {e.msg}")
    except RecursionError:
        sys.exit(f"config error ({path}): nested too deeply to be a "
                 "settings file")
    except ValueError as e:
        sys.exit(f"config error ({path}): {e}")
    if not isinstance(cfg, dict):
        sys.exit(f"config error ({path}): the top level must be an object "
                 'of sections, e.g. {"common": {...}}')
    return cfg


def _split_actions(parser):
    """(settings, verbs): dest -> action for what a config may set, and
    the dest set it must refuse with a to-the-point message.

    Settings exclude positionals (per-run inputs), --help, the config
    location itself, and anything its parser marked `.per_run = True`
    (verbs: --register, --list-ports, --write — actions you take, not
    states you hold)."""
    settings, verbs = {}, set()
    for a in parser._actions:
        if not a.option_strings:
            continue
        if isinstance(a, argparse._HelpAction):
            continue
        if a.dest in ("config", argparse.SUPPRESS):
            continue
        if getattr(a, "per_run", False):
            verbs.add(a.dest)
            continue
        settings[a.dest] = a
    return settings, verbs


def _apply(parser, tool, cfg, path, argv):
    own, own_verbs = _split_actions(parser)

    for section, body in cfg.items():
        if section != COMMON and section not in TOOLS:
            sys.exit(f"config error ({path}): unknown section {section!r}."
                     + _did_you_mean(section, [COMMON, *TOOLS]))
        if not isinstance(body, dict):
            sys.exit(f"config error ({path}): section {section!r} must be "
                     "an object of key/value settings")

    merged, merged_src = {}, {}
    for section in (COMMON, tool):                    # tool wins on conflict
        for key, value in cfg.get(section, {}).items():
            if key == "config":
                sys.exit(f"config error ({path}): {section}.config — the "
                         "config file cannot choose its own location")
            if key in own_verbs:
                sys.exit(f"config error ({path}): {section}.{key} — "
                         f"--{key.replace('_', '-')} is an action you take, "
                         "not a setting; pass it on the run that needs it")
            if key in own:
                _check_value(own[key], key, value, section, path)
                merged[key] = value
                merged_src[key] = section
            elif section == tool:
                sys.exit(f"config error ({path}): {tool} has no setting "
                         f"{key!r}." + _did_you_mean(key, own))
            else:
                _some_tool_recognizes(key, tool, path)

    # Sections for tools that AREN'T running: their keys can't change this
    # process, but a typo in them would otherwise stay silent until the
    # day that tool runs — which is the paddock, at the event, with no
    # slack. Check them now, loudly, without refusing this tool's start.
    for section, body in cfg.items():
        if section in (COMMON, tool) or section not in TOOLS:
            continue
        try:
            settings, verbs = _tool_surface(section)
        except (Exception, SystemExit) as e:
            print(f"config warning ({path}): could not check section "
                  f"{section!r} ({e})", file=sys.stderr)
            continue
        for key in body:
            if key == "config" or key in verbs:
                print(f"config warning ({path}): {section}.{key} will "
                      f"refuse to start when {section} runs — it is an "
                      "action, not a setting", file=sys.stderr)
            elif key not in settings:
                print(f"config warning ({path}): {section} has no setting "
                      f"{key!r} — {section} will refuse to start."
                      + _did_you_mean(key, settings), file=sys.stderr)

    if merged:
        _referee_exclusive_groups(parser, merged, merged_src, path, argv)
        parser.set_defaults(**merged)


def _check_value(action, key, value, section, path):
    """The checks argparse skips for defaults. It type-converts a string
    default on its way through, but it never checks choices on one, it
    swallows any JSON type as-is when the value isn't a string, and an
    on/off flag would keep any truthy junk."""
    where = f"config error ({path}): {section}.{key}"
    if value is None:
        sys.exit(f"{where}: null is not a value — delete the key to "
                 "leave the setting alone")
    if action.nargs == 0:                              # store_true / store_false
        if not isinstance(value, bool):
            sys.exit(f"{where} is an on/off switch — true or false, "
                     f"not {value!r}")
        return
    checked = value
    if isinstance(value, str):
        if any(ord(ch) < 32 for ch in value):
            sys.exit(f"{where}: contains an invisible control character — "
                     "JSON turns \\r, \\t, \\b, \\f into real bytes, so "
                     "write Windows paths with \\\\ or forward slashes")
        if callable(action.type):
            try:
                checked = action.type(value)
            except (TypeError, ValueError):
                sys.exit(f"{where}: {value!r} is not a valid "
                         f"{getattr(action.type, '__name__', 'value')}")
    elif isinstance(value, bool):
        sys.exit(f"{where}: true/false doesn't fit this option")
    elif isinstance(value, (list, dict)):
        sys.exit(f"{where}: one value, not "
                 f"{'a list' if isinstance(value, list) else 'an object'}")
    elif action.type is int:
        if not isinstance(value, int):
            sys.exit(f"{where}: {value!r} is not a whole number")
    elif action.type is float:
        if not isinstance(value, (int, float)):
            sys.exit(f"{where}: {value!r} is not a number")
    elif action.type is None:
        sys.exit(f"{where}: this option takes text — quote it "
                 f"(\"{value}\", not {value})")
    elif callable(action.type):
        try:
            checked = action.type(value)
        except (TypeError, ValueError):
            sys.exit(f"{where}: {value!r} is not a valid "
                     f"{getattr(action.type, '__name__', 'value')}")
    if action.choices is not None and checked not in action.choices:
        sys.exit(f"{where}: {value!r} is not one of "
                 f"{', '.join(map(repr, action.choices))}")


def _exclusive_groups(parser):
    """All mutually-exclusive groups, including any registered on an
    argument group rather than the parser (argparse files them under the
    group they were created on)."""
    seen, out = set(), []
    for holder in [parser, *parser._action_groups]:
        for g in getattr(holder, "_mutually_exclusive_groups", []):
            if id(g) not in seen:
                seen.add(id(g))
                out.append(g)
    return out


def _referee_exclusive_groups(parser, merged, merged_src, path, argv):
    """argparse referees mutual exclusion between options it sees on the
    command line; a default it never sees is invisible to it. Two cases
    it would miss:

    - the file sets both members of an exclusive pair (port AND replay):
      nonsense, refuse loudly;
    - the file sets one and the command line gives the other: the command
      line wins, so the file's value must not survive into the namespace —
      obd_feed with a configured port and --replay on the CLI is a replay,
      not a coin toss.

    (Abbreviations are disabled in parse_with_config precisely so that
    the full-name matching here sees everything argparse will accept.)
    """
    given = set()
    for a in argv:
        if a == "--":
            break
        if a.startswith("--"):
            given.add(a.split("=", 1)[0])
    for group in _exclusive_groups(parser):
        acts = group._group_actions
        from_file = [a for a in acts if a.dest in merged]
        if len(from_file) > 1:
            names = " and ".join(f"{merged_src[a.dest]}.{a.dest}"
                                 for a in from_file)
            sys.exit(f"config error ({path}): {names} are mutually "
                     "exclusive — pick one")
        cli_hit = [a for a in acts
                   if any(s in given for s in a.option_strings)]
        if cli_hit:
            for a in acts:
                if a not in cli_hit:
                    merged.pop(a.dest, None)


def _some_tool_recognizes(key, running_tool, path):
    """A common key this tool doesn't use is fine — if some tool does.
    Nobody recognizing it is the silent-typo case, and 'silent' is the
    part this layer exists to kill. Lazy on purpose: port/baud match the
    running tool directly and cost nothing; only a stranger key makes us
    go ask the neighbours."""
    unreachable = []
    everyone = set()
    for other in TOOLS:
        if other == running_tool:
            continue
        try:
            settings, verbs = _tool_surface(other)
        except (Exception, SystemExit) as e:
            unreachable.append(f"{other} ({e})")
            continue
        if key in settings:
            return
        if key in verbs:
            sys.exit(f"config error ({path}): common.{key} — "
                     f"--{key.replace('_', '-')} is an action you take, "
                     "not a setting; pass it on the run that needs it")
        everyone.update(settings)
    if unreachable:
        sys.exit(f"config error ({path}): common.{key} is not a setting of "
                 f"any tool that could be checked, and these could not be: "
                 f"{'; '.join(unreachable)} — refusing to guess")
    sys.exit(f"config error ({path}): common.{key} matches no tool's "
             "options." + _did_you_mean(key, everyone))


def _tool_module(tool):
    """Import a tool's file under a scratch name. Every tool here imports
    parser-clean (obd_probe guards pyserial for exactly this reason), so
    this stays cheap — ~10ms — and honest."""
    file_path = os.path.join(HERE, *TOOLS[tool])
    spec = importlib.util.spec_from_file_location(f"_obd_config_scan_{tool}",
                                                  file_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tool_surface(tool):
    """(settings dest set, verbs dest set) for a tool, by asking its own
    build_parser(). Cached per process."""
    if tool not in _SURFACE_CACHE:
        settings, verbs = _split_actions(_tool_module(tool).build_parser())
        _SURFACE_CACHE[tool] = (set(settings), verbs)
    return _SURFACE_CACHE[tool]


def _did_you_mean(word, candidates):
    close = difflib.get_close_matches(str(word), list(candidates), n=1)
    return f" Did you mean {close[0]!r}?" if close else ""
