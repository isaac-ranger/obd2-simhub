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
      "obd_feed":   { "dash_gear": "honest" },
      "supervisor": { "quiet": true }
    }

  - "common" is for values that describe the rig and are shared by more
    than one tool (the port, the baud). A tool applies the common keys it
    understands and leaves the rest for the tools that do.
  - A tool-named section applies to that tool only, and beats "common".
  - The command line beats everything. Precedence, highest first:
        CLI  >  tool section  >  common  >  built-in default
  - No config.json is fine: everything behaves exactly as before.
    --config PATH points a tool somewhere else (a missing default file is
    fine; a missing explicit one is an error — you asked for it by name).

WHY THE KEYS ARE NOT LISTED ANYWHERE

A key is exactly a tool's command-line option with the dashes turned to
underscores (--dash-gear -> dash_gear), DERIVED from the tool's own
argparse parser at startup. An option added to any tool next year is
configurable the day it lands. A maintained table of keys would drift
from the parsers, and its failure mode is silent; this cannot drift.

The one list that does exist is TOOLS below — section names have to map
to files somehow. Six lines that change only when a tool is born are a
different animal from forty-odd that change with every feature.

UNRECOGNIZED MEANS REFUSE TO START

A key nothing recognizes is a hard error, not a shrug. "throtle_floor"
quietly ignored is indistinguishable from "this setting doesn't work",
and that gets debugged over email from a paddock. The same goes for a
section that names no tool, a value outside an option's choices, and a
non-boolean on an on/off flag. Every error names the file, the key, and
when the typo is close enough to see, the fix.

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
    "fake_car":    ("extractor", "fake_car.py"),
    "supervisor":  ("supervisor", "supervisor.py"),
    "report":      ("report.py",),
}

_DESTS_CACHE = {}


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

    parser.add_argument("--config", default=default_config_path(),
                        metavar="CONFIG_JSON",
                        help="settings file (default: config.json in the "
                             "repo root; sections: common plus one per tool, "
                             "keys are option names with underscores)")

    path, explicit = _config_path_from(argv)
    cfg = _load(path, explicit)
    if cfg:
        _apply(parser, tool, cfg, path, argv)

    result = parser.parse_known_args(argv) if known else parser.parse_args(argv)
    ns = result[0] if known else result

    # argparse abbreviates option names (--conf), our pre-scan does not.
    # If the two disagree about which file governs, the parse we just did
    # was against the wrong defaults — refuse rather than guess.
    if getattr(ns, "config", path) != path:
        sys.exit("config error: spell --config out in full — it decides "
                 "what the rest of the command line means")
    return result


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
    return path, explicit


def _load(path, explicit):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        if explicit:
            sys.exit(f"config error: {path} does not exist (named by --config)")
        return None
    except OSError as e:
        sys.exit(f"config error: cannot read {path}: {e}")
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"config error ({path}): not valid JSON — line {e.lineno} "
                 f"column {e.colno}: {e.msg}")
    if not isinstance(cfg, dict):
        sys.exit(f"config error ({path}): the top level must be an object "
                 'of sections, e.g. {"common": {...}}')
    return cfg


def _configurable_actions(parser):
    """dest -> action for everything a config file may set.

    Optionals only: a positional (learn_gears' drive log) is a per-run
    input, not a setting; --help is not a value; and the config file does
    not get to choose its own location."""
    out = {}
    for a in parser._actions:
        if not a.option_strings:
            continue
        if isinstance(a, argparse._HelpAction):
            continue
        if a.dest in ("config", argparse.SUPPRESS):
            continue
        out[a.dest] = a
    return out


def _apply(parser, tool, cfg, path, argv):
    own = _configurable_actions(parser)

    for section, body in cfg.items():
        if section != COMMON and section not in TOOLS:
            sys.exit(f"config error ({path}): unknown section {section!r}."
                     + _did_you_mean(section, [COMMON, *TOOLS]))
        if not isinstance(body, dict):
            sys.exit(f"config error ({path}): section {section!r} must be "
                     "an object of key/value settings")

    merged = {}
    for section in (COMMON, tool):                    # tool wins on conflict
        for key, value in cfg.get(section, {}).items():
            if key == "config":
                sys.exit(f"config error ({path}): {section}.config — the "
                         "config file cannot choose its own location")
            if key in own:
                _check_value(own[key], key, value, section, path)
                merged[key] = value
            elif section == tool:
                sys.exit(f"config error ({path}): {tool} has no setting "
                         f"{key!r}." + _did_you_mean(key, own))
            else:
                _some_tool_recognizes(key, tool, path)

    if merged:
        _referee_exclusive_groups(parser, tool, merged, path, argv)
        parser.set_defaults(**merged)


def _check_value(action, key, value, section, path):
    """The checks argparse skips for defaults. It type-converts a string
    default on its way through, but it never checks choices on one, and
    an on/off flag would swallow any truthy junk."""
    where = f"config error ({path}): {section}.{key}"
    if action.nargs == 0:                              # store_true / store_false
        if not isinstance(value, bool):
            sys.exit(f"{where} is an on/off switch — true or false, "
                     f"not {value!r}")
        return
    checked = value
    if isinstance(value, str) and callable(action.type):
        try:
            checked = action.type(value)
        except (TypeError, ValueError):
            sys.exit(f"{where}: {value!r} is not a valid "
                     f"{getattr(action.type, '__name__', 'value')}")
    if action.choices is not None and checked not in action.choices:
        sys.exit(f"{where}: {value!r} is not one of "
                 f"{', '.join(map(repr, action.choices))}")


def _referee_exclusive_groups(parser, tool, merged, path, argv):
    """argparse referees mutual exclusion between options it sees on the
    command line; a default it never sees is invisible to it. Two cases
    it would miss:

    - the file sets both members of an exclusive pair (port AND replay):
      nonsense, refuse loudly;
    - the file sets one and the command line gives the other: the command
      line wins, so the file's value must not survive into the namespace —
      obd_feed with a configured port and --replay on the CLI is a replay,
      not a coin toss.
    """
    given = set()
    for a in argv:
        if a == "--":
            break
        if a.startswith("--"):
            given.add(a.split("=", 1)[0])
    for group in parser._mutually_exclusive_groups:
        acts = group._group_actions
        from_file = [a for a in acts if a.dest in merged]
        if len(from_file) > 1:
            names = " and ".join(f"{tool}.{a.dest}" for a in from_file)
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
            dests = _tool_dests(other)
        except Exception as e:                        # noqa: BLE001 — report, don't guess
            unreachable.append(f"{other} ({e})")
            continue
        if key in dests:
            return
        everyone.update(dests)
    if unreachable:
        sys.exit(f"config error ({path}): common.{key} is not a setting of "
                 f"any tool that could be checked, and these could not be: "
                 f"{'; '.join(unreachable)} — refusing to guess")
    sys.exit(f"config error ({path}): common.{key} matches no tool's "
             "options." + _did_you_mean(key, everyone))


def _tool_dests(tool):
    """Import a tool's file under a scratch name and ask its build_parser()
    what it accepts. Every tool here imports parser-clean (obd_probe guards
    pyserial for exactly this reason), so this stays cheap and honest."""
    if tool in _DESTS_CACHE:
        return _DESTS_CACHE[tool]
    file_path = os.path.join(HERE, *TOOLS[tool])
    spec = importlib.util.spec_from_file_location(f"_obd_config_scan_{tool}",
                                                  file_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    dests = set(_configurable_actions(mod.build_parser()))
    _DESTS_CACHE[tool] = dests
    return dests


def _did_you_mean(word, candidates):
    close = difflib.get_close_matches(str(word), list(candidates), n=1)
    return f" Did you mean {close[0]!r}?" if close else ""
