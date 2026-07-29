#!/usr/bin/env python3
"""
NET MANAGER  --  Game Master console for Cyberpunk RED netrunning.

Single-file, stdlib-only. Runs on Windows, macOS and Linux.

    python3 netmanager.py [--port 7717] [--ascii] [--no-beacon]

The GM builds NET Architectures, decides which ones the Netrunner can see,
watches the Netrunner move through them in real time, resolves NET Actions,
and can block a runner out of an architecture at any moment.

Save files live in ./saves/ next to this script.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import socket
import struct
import sys
import threading
import time
import uuid
from datetime import datetime

# --------------------------------------------------------------------------
# Platform / terminal plumbing
# --------------------------------------------------------------------------

IS_WIN = os.name == "nt"

if IS_WIN:
    import msvcrt
else:
    import select
    import termios
    import tty

DEFAULT_TCP_PORT = 7717
BEACON_PORT = 7718     # GM -> players, passive beacon
PROBE_PORT = 7719      # players -> GM, active probe answered by unicast
BEACON_MAGIC = "CPRED_NETRUN_V1"
PROTOCOL = 1


def _prepare_console():
    """Turn on ANSI escape handling and UTF-8 output where we can."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not IS_WIN:
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.SetConsoleOutputCP(65001)
        for handle_id in (-11, -12):  # stdout, stderr
            handle = k32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(handle, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                k32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


class RawInput:
    """Context manager putting the terminal in cbreak mode (Unix no-op on Win)."""

    def __init__(self):
        self.fd = None
        self.saved = None

    def __enter__(self):
        if not IS_WIN and sys.stdin.isatty():
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?25l")  # hide cursor
        sys.stdout.flush()
        return self

    def __exit__(self, *exc):
        sys.stdout.write("\x1b[?25h\x1b[0m\n")
        sys.stdout.flush()
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)


_CSI_KEYS = {
    "A": "up", "B": "down", "C": "right", "D": "left",
    "H": "home", "F": "end",
    "1~": "home", "4~": "end", "3~": "del",
    "5~": "pgup", "6~": "pgdn",
}
_WIN_KEYS = {
    "H": "up", "P": "down", "K": "left", "M": "right",
    "G": "home", "O": "end", "S": "del", "I": "pgup", "Q": "pgdn",
}


def read_key(timeout=None):
    """Return a key name ('up', 'enter', 'a', ...) or None when timeout expires."""
    if IS_WIN:
        return _read_key_win(timeout)
    return _read_key_posix(timeout)


def _read_key_win(timeout):
    deadline = None if timeout is None else time.time() + timeout
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                return _WIN_KEYS.get(msvcrt.getwch(), "")
            return _normalize(ch)
        if deadline is not None and time.time() >= deadline:
            return None
        time.sleep(0.008)


def _posix_getch(timeout):
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    first = os.read(sys.stdin.fileno(), 1)
    if not first:
        return None
    lead = first[0]
    if lead >= 0x80:  # finish the UTF-8 sequence
        extra = 1 if lead >> 5 == 0b110 else 2 if lead >> 4 == 0b1110 else 3
        first += os.read(sys.stdin.fileno(), extra)
    return first.decode("utf-8", "replace")


def _read_key_posix(timeout):
    ch = _posix_getch(timeout)
    if ch is None:
        return None
    if ch != "\x1b":
        return _normalize(ch)
    nxt = _posix_getch(0.05)
    if nxt is None:
        return "esc"
    if nxt == "O":  # application cursor mode
        final = _posix_getch(0.05) or ""
        return _CSI_KEYS.get(final, "esc")
    if nxt != "[":
        return "esc"
    code = ""
    while True:
        c = _posix_getch(0.05)
        if c is None:
            break
        code += c
        if c.isalpha() or c == "~":
            break
    return _CSI_KEYS.get(code, _CSI_KEYS.get(code[-1:], "esc"))


def _normalize(ch):
    if ch in ("\r", "\n"):
        return "enter"
    if ch in ("\x7f", "\b"):
        return "backspace"
    if ch == "\t":
        return "tab"
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch == "\x1b":
        return "esc"
    if ch < " ":
        return ""
    return ch


# --------------------------------------------------------------------------
# Styling
# --------------------------------------------------------------------------

class C:
    RESET = "\x1b[0m"
    DIM = "\x1b[2m"
    BOLD = "\x1b[1m"
    REV = "\x1b[7m"
    RED = "\x1b[38;5;203m"
    GREEN = "\x1b[38;5;84m"
    YELLOW = "\x1b[38;5;222m"
    CYAN = "\x1b[38;5;51m"
    BLUE = "\x1b[38;5;75m"
    MAGENTA = "\x1b[38;5;213m"
    GREY = "\x1b[38;5;245m"
    ORANGE = "\x1b[38;5;215m"


UNICODE_GLYPHS = {
    "tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "h": "─", "v": "│",
    "dot": "•", "arrow": "▶", "block": "█", "vt": "├", "corner": "└",
    "up": "▲", "down": "▼",
}
ASCII_GLYPHS = {
    "tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|",
    "dot": "*", "arrow": ">", "block": "#", "vt": "+", "corner": "\\",
    "up": "^", "down": "v",
}
G = dict(UNICODE_GLYPHS)


def pick_glyphs(force_ascii):
    global G
    if force_ascii:
        G = dict(ASCII_GLYPHS)
        return
    enc = (sys.stdout.encoding or "ascii")
    try:
        "".join(UNICODE_GLYPHS.values()).encode(enc)
    except Exception:
        G = dict(ASCII_GLYPHS)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def vislen(s):
    return len(_ANSI_RE.sub("", s))


def clip(s, width):
    if vislen(s) <= width:
        return s
    out, shown = [], 0
    i = 0
    while i < len(s):
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        if shown >= width:
            break
        out.append(s[i])
        shown += 1
        i += 1
    return "".join(out) + C.RESET


def pad(s, width):
    gap = width - vislen(s)
    return s + " " * gap if gap > 0 else clip(s, width)


# --------------------------------------------------------------------------
# UI toolkit
# --------------------------------------------------------------------------

REFRESH = object()  # returned by menu() when watched state changed
HEADING = object()  # a menu row that is drawn but cannot be selected


class UI:
    """Full-screen redraw + menu/prompt widgets. All calls are blocking."""

    def __init__(self, app=None):
        self.app = app
        self.last_index = 0   # where the highlight rested when menu() returned

    # -- low level ---------------------------------------------------------

    def size(self):
        w, h = shutil.get_terminal_size((80, 24))
        return max(60, w), max(16, h)

    def draw(self, lines):
        w, h = self.size()
        buf = ["\x1b[H"]
        for line in lines[: h - 1]:
            buf.append(clip(line, w) + "\x1b[K\n")
        buf.append("\x1b[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def banner(self, title, subtitle=""):
        w, _ = self.size()
        bar = G["h"] * (w - 2)
        out = [
            C.CYAN + G["tl"] + bar + G["tr"] + C.RESET,
            C.CYAN + G["v"] + C.RESET + pad(" " + C.BOLD + C.CYAN + title + C.RESET, w - 2) + C.CYAN + G["v"] + C.RESET,
        ]
        if subtitle:
            out.append(C.CYAN + G["v"] + C.RESET + pad(" " + C.GREY + subtitle + C.RESET, w - 2) + C.CYAN + G["v"] + C.RESET)
        out.append(C.CYAN + G["bl"] + bar + G["br"] + C.RESET)
        return out

    def rule(self, label=""):
        w, _ = self.size()
        if not label:
            return C.GREY + G["h"] * w + C.RESET
        text = G["h"] * 2 + " " + label + " "
        return C.GREY + text + G["h"] * max(0, w - vislen(text)) + C.RESET

    # -- menu --------------------------------------------------------------

    def menu(self, head, items, foot=None, hotkeys=None, index=0, body=None, watch=None):
        """
        head    : lines above the menu, or a zero-arg callable returning them
        items   : list of (label, value); a bare None inserts a blank spacer
        hotkeys : {key: (description, token)} -- fired only when the type-ahead
                  buffer is empty; returns ('hotkey', token, current_value)
        body    : lines rendered below the menu (feed panes etc.), or a callable
        watch   : zero-arg callable returning a comparable token; when it changes
                  the menu returns REFRESH so the caller can rebuild
        Returns the chosen value, ('hotkey', token, value), REFRESH, or None on Esc.
        """
        hotkeys = hotkeys or {}
        typed = ""
        token0 = watch() if watch else None
        selectable = [i for i, it in enumerate(items)
                      if it is not None and it[1] is not HEADING]
        index = index if index in selectable else (selectable[0] if selectable else 0)
        scroll = 0

        while True:
            head_lines = head() if callable(head) else list(head)
            body_lines = (body() if callable(body) else list(body)) if body else []

            visible = self._filter(items, typed)
            vis_sel = [i for i, it in visible
                       if it is not None and it[1] is not HEADING]
            if vis_sel and index not in vis_sel:
                index = vis_sel[0]

            _, h = self.size()
            lines = list(head_lines)
            # Budget rows precisely: whatever the header and the feed pane do not
            # use is available for entries, less the scroll markers and footer.
            # Row budget. The footer and scroll markers are non-negotiable; the
            # body pane gives up rows before the list does, so a cramped window
            # trims the feed rather than hiding entries behind a footer that
            # scrolled off the bottom.
            footer = 3 + (1 if foot else 0)         # blank + rule + hint (+ foot)
            budget = h - 1 - len(head_lines) - 2 - footer
            if body_lines:
                max_body = budget - 4               # keep >= 3 list rows + a blank
                if len(body_lines) > max_body:
                    body_lines = ([body_lines[0]] + body_lines[-(max_body - 1):]
                                  if max_body >= 2 else [])
                if body_lines:
                    budget -= 1 + len(body_lines)
            room = max(1, budget)

            # Scrolling must be measured in `visible` coordinates. `vis_sel`
            # indexes only selectable rows, and mixing the two spaces pushes the
            # highlight outside the rendered window as soon as spacers are in
            # play -- which is what made off-screen entries unreachable once a
            # tall header or feed pane shrank the list.
            at = {real_i: k for k, (real_i, _it) in enumerate(visible)}
            if vis_sel:
                pos = at.get(index, 0)
                if pos < scroll:
                    scroll = pos
                elif pos >= scroll + room:
                    scroll = pos - room + 1
            scroll = max(0, min(scroll, max(0, len(visible) - room)))
            window = visible[scroll : scroll + room]

            if not visible:
                lines.append("   " + C.GREY + "(no matches)" + C.RESET)
            if scroll > 0:
                lines.append("   " + C.GREY + G["up"] + " %d more above" % scroll + C.RESET)
            for real_i, item in window:
                if item is None:
                    lines.append("")
                    continue
                label = item[0]
                if real_i == index:
                    lines.append(C.CYAN + " " + G["arrow"] + " " + C.RESET + C.BOLD + label + C.RESET)
                else:
                    lines.append("   " + label)
            below = len(visible) - (scroll + len(window))
            if below > 0:
                lines.append("   " + C.GREY + G["down"] + " %d more below" % below + C.RESET)

            if body_lines:
                lines.append("")
                lines.extend(body_lines)

            lines.append("")
            hint = []
            if typed:
                hint.append(C.YELLOW + "type: " + typed + "_" + C.RESET)
            hint.append(C.GREY + "arrows/type to pick" + C.RESET)
            hint.append(C.GREY + "enter select" + C.RESET)
            for key, (desc, _tok) in hotkeys.items():
                hint.append(C.MAGENTA + key + C.RESET + C.GREY + " " + desc + C.RESET)
            hint.append(C.GREY + "esc back" + C.RESET)
            lines.append(self.rule())
            lines.append(" " + (C.GREY + " " + G["dot"] + " " + C.RESET).join(hint))
            if foot:
                lines.append(" " + foot)
            self.draw(lines)

            key = read_key(0.15)
            self.last_index = index
            if key is None:
                if watch and watch() != token0:
                    return REFRESH
                continue
            current = items[index][1] if (vis_sel and items[index] is not None) else None

            if key == "up" and vis_sel:
                index = vis_sel[(vis_sel.index(index) - 1) % len(vis_sel)]
            elif key == "down" and vis_sel:
                index = vis_sel[(vis_sel.index(index) + 1) % len(vis_sel)]
            elif key in ("pgup", "home") and vis_sel:
                index = vis_sel[0]
            elif key in ("pgdn", "end") and vis_sel:
                index = vis_sel[-1]
            elif key == "enter":
                if vis_sel:
                    return current
            elif key == "esc":
                if typed:
                    typed = ""
                else:
                    return None
            elif key == "backspace":
                typed = typed[:-1]
            elif not typed and key in hotkeys:
                return ("hotkey", hotkeys[key][1], current)
            elif len(key) == 1 and key >= " ":
                typed += key
                scroll = 0

    @staticmethod
    def _filter(items, typed):
        pairs = list(enumerate(items))
        if not typed:
            return pairs
        needle = typed.lower()
        return [
            (i, it) for i, it in pairs
            if it is None or needle in _ANSI_RE.sub("", it[0]).lower()
        ]

    # -- prompts -----------------------------------------------------------

    def prompt(self, head, label, default="", allow_empty=True):
        buf = str(default)
        sys.stdout.write("\x1b[?25h")
        try:
            while True:
                lines = list(head() if callable(head) else head)
                lines += [
                    "",
                    " " + C.YELLOW + label + C.RESET,
                    " " + C.CYAN + "> " + C.RESET + buf + C.GREY + "_" + C.RESET,
                    "",
                    self.rule(),
                    " " + C.GREY + "enter confirm " + G["dot"] + " esc cancel" + C.RESET,
                ]
                self.draw(lines)
                key = read_key(0.2)
                if key is None:
                    continue
                if key == "enter":
                    if buf or allow_empty:
                        return buf
                elif key == "esc":
                    return None
                elif key == "backspace":
                    buf = buf[:-1]
                elif len(key) == 1 and key >= " ":
                    buf += key
        finally:
            sys.stdout.write("\x1b[?25l")

    def prompt_int(self, head, label, default=None, lo=None, hi=None):
        raw = self.prompt(head, label, "" if default is None else str(default))
        if raw is None:
            return None
        raw = raw.strip()
        if not raw:
            return default
        try:
            val = int(raw)
        except ValueError:
            return default
        if lo is not None:
            val = max(lo, val)
        if hi is not None:
            val = min(hi, val)
        return val

    def alert(self, head, lines, color=C.YELLOW):
        out = list(head() if callable(head) else head)
        out.append("")
        for line in lines:
            out.append(" " + color + line + C.RESET)
        out.append("")
        out.append(self.rule())
        out.append(" " + C.GREY + "press any key" + C.RESET)
        self.draw(out)
        while read_key(0.2) is None:
            pass

    def confirm(self, head, question, danger=False):
        color = C.RED if danger else C.YELLOW
        head = list(head() if callable(head) else head) + ["", " " + color + question + C.RESET, ""]
        return self.menu(head, [("No", False), ("Yes", True)]) is True


# --------------------------------------------------------------------------
# Cyberpunk RED reference data
# --------------------------------------------------------------------------

FLOOR_TYPES = [
    ("Password", "Lock floor. Beaten with Backdoor."),
    ("File", "Data sitting on the floor. Eye-Dee to identify, Virus to infect."),
    ("Control Node", "Wired to a real-world device. Control to seize it."),
    ("--- BLACK ICE (Anti-Personnel) ---", ""),
    ("Hellhound", "Anti-personnel Black ICE."),
    ("Sabertooth", "Anti-personnel Black ICE."),
    ("Kraken", "Anti-personnel Black ICE."),
    ("Dragon", "Anti-personnel Black ICE."),
    ("Killer", "Anti-personnel Black ICE."),
    ("Liche", "Anti-personnel Black ICE."),
    ("--- BLACK ICE (Anti-Program) ---", ""),
    ("Asp", "Anti-program Black ICE."),
    ("Giant", "Anti-program Black ICE."),
    ("Raven", "Anti-program Black ICE."),
    ("Scorpion", "Anti-program Black ICE."),
    ("Skunk", "Anti-program Black ICE."),
    ("Wisp", "Anti-program Black ICE."),
    ("--- DEMONS ---", ""),
    ("Imp", "Demon. Low-tier system guardian."),
    ("Efreet", "Demon. Mid-tier system guardian."),
    ("Balron", "Demon. High-tier system guardian."),
    ("--- OTHER ---", ""),
    ("Empty", "Nothing here. Yet."),
    ("Custom", "Whatever you want it to be."),
]

FLOOR_STATES = ["Intact", "Defeated", "Alerted", "Controlled", "Destroyed", "Rezzed"]

DIFFICULTIES = ["Basic", "Standard", "Uncommon", "Advanced"]

NET_ACTIONS = [
    {"name": "Pathfinder", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs Floor DV",
     "desc": "Scan the floors below you. On a success the GM reveals what is waiting down there."},
    {"name": "Backdoor", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs Password DV",
     "desc": "Cut through a Password floor. Blowing the roll can wake up the system."},
    {"name": "Slide", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs attacker's roll",
     "desc": "Duck an incoming attack from Black ICE."},
    {"name": "Cloak", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs Demon PER",
     "desc": "Hide your presence from a Demon or a rival Netrunner."},
    {"name": "Control", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs Control Node DV",
     "desc": "Seize a Control Node and run whatever hardware is bolted to it."},
    {"name": "Eye-Dee", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs File DV",
     "desc": "Work out what a File actually holds before you touch it."},
    {"name": "Virus", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs Floor DV",
     "desc": "Plant a virus on a File or a Control Node."},
    {"name": "Zap", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs target DEF",
     "desc": "Hit Black ICE or a Demon for 1d6 damage against its REZ."},
]

OPERATIONS = [
    {"name": "Move Down a Floor", "cost": "Movement", "check": "--",
     "desc": "Drop to the next floor of the architecture. GM confirms."},
    {"name": "Move Up a Floor", "cost": "Movement", "check": "--",
     "desc": "Climb back toward the entry point."},
    {"name": "Run a Program", "cost": "1 NET Action", "check": "--",
     "desc": "Rez an Attacker, Defender or Booster from your deck."},
    {"name": "Speak to the GM", "cost": "--", "check": "--",
     "desc": "Describe something your Netrunner does that isn't on this list."},
    {"name": "Jack Out", "cost": "--", "check": "--",
     "desc": "Pull the plug and leave the architecture."},
]


# --------------------------------------------------------------------------
# Session model
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR = os.path.join(HERE, "saves")


def now_stamp():
    return datetime.now().strftime("%H:%M:%S")


def today():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "session"


def new_session(name):
    return {
        "schema": 1,
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "created": today(),
        "updated": today(),
        "character": {
            "handle": "",
            "interface": 1,
            "actions_per_turn": 1,
            "hp": 20,
            "hp_max": 20,
            "programs": [],
            "notes": "",
        },
        "nets": [],
        "run": None,          # {"net_id":..,"floor":int}
        "feed": [],           # player-visible messages
        "log": [],            # GM-only log
        "pending": [],        # unresolved actions from the player
    }


def new_net(name):
    return {
        "id": uuid.uuid4().hex[:6],
        "name": name,
        "description": "",
        "difficulty": "Standard",
        "visible": False,
        "locked": False,
        "reset_on_entry": True,   # wipe progress automatically when they jack in
        "floors": [],
    }


def new_floor(number):
    return {
        "n": number,
        "type": "Password",
        "dv": 6,
        "label": "",
        "state": "Intact",
        "revealed": False,
        "gm_notes": "",
    }


def save_path(session):
    return os.path.join(SAVE_DIR, "%s-%s.json" % (slugify(session["name"]), session["id"]))


def save_session(session):
    os.makedirs(SAVE_DIR, exist_ok=True)
    session["updated"] = today()
    path = save_path(session)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(session, fh, indent=2)
    os.replace(tmp, path)
    return path


def list_saves():
    if not os.path.isdir(SAVE_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(SAVE_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SAVE_DIR, fn), encoding="utf-8") as fh:
                data = json.load(fh)
            out.append((os.path.join(SAVE_DIR, fn), data))
        except Exception:
            continue
    out.sort(key=lambda p: p[1].get("updated", ""), reverse=True)
    return out


# -- reset -----------------------------------------------------------------

def reset_net(net):
    """Put an architecture back the way it was built: every floor Intact and
    unrevealed, nothing locked. The design (types, DVs, labels, GM notes) is
    untouched -- only the netrunner's progress through it is wiped.
    Returns a short description of what changed."""
    touched = 0
    for f in net["floors"]:
        if f.get("state", "Intact") != "Intact" or f.get("revealed"):
            touched += 1
        f["state"] = "Intact"
        f["revealed"] = False
    was_locked = net.get("locked", False)
    net["locked"] = False
    bits = ["%d floor(s) cleared" % touched] if touched else ["already pristine"]
    if was_locked:
        bits.append("unlocked")
    return ", ".join(bits)


# -- architecture library --------------------------------------------------

LIBRARY_DIR = os.path.join(HERE, "library")


def net_template(net):
    """A pristine, session-independent copy of an architecture, ready to reuse.
    Design and GM notes are kept; run progress and visibility are not."""
    return {
        "kind": "cpred_net_architecture",
        "schema": 1,
        "name": net["name"],
        "description": net.get("description", ""),
        "difficulty": net.get("difficulty", "Standard"),
        "exported": today(),
        "floors": [
            {
                "n": i + 1,
                "type": f.get("type", "Empty"),
                "dv": f.get("dv"),
                "label": f.get("label", ""),
                "gm_notes": f.get("gm_notes", ""),
            }
            for i, f in enumerate(net.get("floors", []))
        ],
    }


def net_from_template(data, name=None):
    """Build a fresh in-session architecture from an exported template."""
    net = new_net(name or data.get("name", "Imported NET"))
    net["description"] = data.get("description", "")
    net["difficulty"] = data.get("difficulty", "Standard")
    for i, f in enumerate(data.get("floors", [])):
        floor = new_floor(i + 1)
        floor["type"] = f.get("type", "Empty")
        floor["dv"] = f.get("dv")
        floor["label"] = f.get("label", "")
        floor["gm_notes"] = f.get("gm_notes", "")
        net["floors"].append(floor)
    return net


def library_path(name):
    return os.path.join(LIBRARY_DIR, slugify(name) + ".json")


def export_net(net, overwrite_path=None):
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    path = overwrite_path or library_path(net["name"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(net_template(net), fh, indent=2)
    os.replace(tmp, path)
    return path


def list_library():
    if not os.path.isdir(LIBRARY_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(LIBRARY_DIR)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(LIBRARY_DIR, fn)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "floors" in data:
                out.append((path, data))
        except Exception:
            continue
    out.sort(key=lambda p: p[1].get("name", "").lower())
    return out


def unique_net_name(session, name):
    taken = {n["name"] for n in session["nets"]}
    if name not in taken:
        return name
    i = 2
    while "%s (%d)" % (name, i) in taken:
        i += 1
    return "%s (%d)" % (name, i)


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

# Discovery runs two ways, because different networks block different things:
#
#   passive -- the GM broadcasts a beacon on BEACON_PORT and players listen
#   active  -- a player broadcasts a probe on PROBE_PORT and the GM answers
#              with a unicast reply, which stateful firewalls let back through
#              because it matches the player's own outbound packet
#
# Both go out once per local interface. A machine with Wi-Fi and Ethernet, or a
# VPN, or Docker bridges, has several; a single unbound socket would only ever
# reach whichever one happens to own the default route.


def _iface_addrs():
    """(address, broadcast) for each up IPv4 interface. Linux only; [] elsewhere."""
    if IS_WIN:
        return []
    try:
        import fcntl
    except ImportError:
        return []
    try:
        names = [name for _idx, name in socket.if_nameindex()]
    except (OSError, AttributeError):
        return []
    out = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            packed = struct.pack("256s", name.encode()[:15])
            try:
                addr = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x8915, packed)[20:24])
                brd = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x8919, packed)[20:24])
            except OSError:
                continue
            if addr.startswith("127.") or brd in ("0.0.0.0", ""):
                continue
            out.append((addr, brd))
    finally:
        probe.close()
    return out


def local_ipv4s():
    """Every non-loopback IPv4 address this machine answers on."""
    found = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # nothing is sent; this just picks a route
            found.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        found.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    for addr, _brd in _iface_addrs():
        found.add(addr)
    return sorted(a for a in found if not a.startswith("127.") and a != "0.0.0.0")


def lan_ip():
    addrs = local_ipv4s()
    return addrs[0] if addrs else "127.0.0.1"


def broadcast_plan():
    """[(source address or None, destination)] reaching every interface."""
    plan = []
    seen = set()

    def add(src, dst):
        if dst and (src, dst) not in seen:
            seen.add((src, dst))
            plan.append((src, dst))

    add(None, "255.255.255.255")             # whatever owns the default route
    per_iface = dict(_iface_addrs())
    for addr in local_ipv4s():
        add(addr, "255.255.255.255")         # pinned to this interface
        brd = per_iface.get(addr)
        if not brd:
            parts = addr.split(".")
            brd = ".".join(parts[:3] + ["255"]) if len(parts) == 4 else None
        add(addr, brd)                       # subnet-directed broadcast
        add(None, brd)
    return plan


class Broadcaster:
    """Sends a datagram out of every interface, one socket per source address."""

    def __init__(self):
        self.socks = {}
        self.plan = []
        self.refreshed = 0.0

    def refresh(self, force=False):
        now = time.time()
        if not force and self.plan and now - self.refreshed < 10:
            return
        self.refreshed = now
        self.plan = broadcast_plan()
        for src, _dst in self.plan:
            if src in self.socks:
                continue
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                if src:
                    sock.bind((src, 0))
                self.socks[src] = sock
            except OSError:
                self.socks[src] = None

    def send(self, payload, port):
        self.refresh()
        sent = 0
        for src, dst in self.plan:
            sock = self.socks.get(src)
            if sock is None:
                continue
            try:
                sock.sendto(payload, (dst, port))
                sent += 1
            except OSError:
                pass
        return sent

    def sockets(self):
        return [s for s in self.socks.values() if s is not None]

    def close(self):
        for sock in self.socks.values():
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self.socks = {}
        self.plan = []


class Client:
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.handle = "unknown"
        self.alive = True
        self.buf = b""

    def send(self, obj):
        if not self.alive:
            return
        try:
            self.sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except Exception:
            self.alive = False

    def close(self):
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class Server:
    def __init__(self, app, port, beacon=True):
        self.app = app
        self.port = port
        self.beacon = beacon
        self.clients = []
        self.lock = threading.Lock()
        self.running = False
        self.sock = None
        self.threads = []
        self.ip = lan_ip()
        self.beacon_sent = 0        # interfaces reached by the last beacon
        self.probes_answered = 0
        self.probe_error = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.listen(5)
        # A timeout keeps accept() from pinning the listening socket open; without
        # it the port stays bound after stop() and the next session cannot start.
        self.sock.settimeout(0.4)
        self.running = True
        self.threads = [threading.Thread(target=self._accept_loop, daemon=True)]
        if self.beacon:
            self.threads.append(threading.Thread(target=self._beacon_loop, daemon=True))
            self.threads.append(threading.Thread(target=self._probe_loop, daemon=True))
        for t in self.threads:
            t.start()

    def stop(self):
        self.running = False
        with self.lock:
            for c in list(self.clients):
                c.send({"type": "server_closing"})
                c.close()
            self.clients = []
        for t in self.threads:
            t.join(timeout=2.0)
        self.threads = []
        try:
            self.sock.close()
        except Exception:
            pass

    # -- threads -----------------------------------------------------------

    def _accept_loop(self):
        while self.running:
            try:
                sock, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    time.sleep(0.2)
                continue
            sock.settimeout(None)
            client = Client(sock, addr)
            with self.lock:
                self.clients.append(client)
            threading.Thread(target=self._client_loop, args=(client,), daemon=True).start()

    def _client_loop(self, client):
        client.send({"type": "hello", "protocol": PROTOCOL,
                     "session": self.app.session["name"]})
        self.push_state()
        try:
            while self.running and client.alive:
                data = client.sock.recv(4096)
                if not data:
                    break
                client.buf += data
                while b"\n" in client.buf:
                    raw, client.buf = client.buf.split(b"\n", 1)
                    if not raw.strip():
                        continue
                    try:
                        msg = json.loads(raw.decode("utf-8"))
                    except Exception:
                        continue
                    self.app.handle_client_message(client, msg)
        except Exception:
            pass
        finally:
            client.alive = False
            with self.lock:
                if client in self.clients:
                    self.clients.remove(client)
            self.app.log("%s disconnected." % client.handle)
            self.app.bump()

    def announcement(self):
        return json.dumps({
            "magic": BEACON_MAGIC,
            "protocol": PROTOCOL,
            "session": self.app.session["name"],
            "id": self.app.session["id"],
            "port": self.port,
            "ip": self.ip,
            "players": len(self.clients),
            "nets": sum(1 for n in self.app.session["nets"] if n.get("visible")),
        }).encode("utf-8")

    def _beacon_loop(self):
        caster = Broadcaster()
        try:
            while self.running:
                self.beacon_sent = caster.send(self.announcement(), BEACON_PORT)
                for _ in range(15):  # ~1.5s, but stay responsive to stop()
                    if not self.running:
                        break
                    time.sleep(0.1)
        finally:
            caster.close()

    def _probe_loop(self):
        """Answer 'who is out there' probes with a unicast reply."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind(("", PROBE_PORT))
        except OSError as exc:
            self.probe_error = str(exc)
            sock.close()
            return
        sock.settimeout(0.4)
        while self.running:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if msg.get("magic") != BEACON_MAGIC or not msg.get("probe"):
                continue
            self.probes_answered += 1
            try:
                sock.sendto(self.announcement(), addr)
            except OSError:
                pass
        sock.close()

    # -- outbound ----------------------------------------------------------

    def broadcast(self, obj):
        with self.lock:
            targets = list(self.clients)
        for c in targets:
            c.send(obj)

    def push_state(self):
        self.broadcast({"type": "state", "state": self.app.player_view()})

    def connected(self):
        with self.lock:
            return [c for c in self.clients if c.alive]


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

class App:
    def __init__(self, port, beacon):
        self.ui = UI(self)
        self.session = None
        self.server = None
        self.port = port
        self.beacon = beacon
        self.dirty = threading.Event()
        self.version = 0
        self.state_lock = threading.RLock()

    # -- helpers -----------------------------------------------------------

    def bump(self):
        """Signal every open screen that something changed underneath it."""
        self.version += 1
        self.dirty.set()

    def log(self, text):
        if not self.session:
            return
        self.session["log"].append({"t": now_stamp(), "text": text})
        del self.session["log"][:-500]

    def feed(self, text, kind="gm"):
        """Add a player-visible message."""
        self.session["feed"].append({"t": now_stamp(), "kind": kind, "text": text})
        del self.session["feed"][:-100]

    def touch(self, push=True):
        """Persist + push to the netrunner. Call after every mutation."""
        save_session(self.session)
        if push and self.server:
            self.server.push_state()
        self.bump()

    def net_by_id(self, net_id):
        for net in self.session["nets"]:
            if net["id"] == net_id:
                return net
        return None

    # -- player-facing projection -----------------------------------------

    def player_view(self):
        with self.state_lock:
            s = self.session
            nets = []
            for net in s["nets"]:
                if not net.get("visible"):
                    continue
                floors = net["floors"]
                deepest = -1
                for i, f in enumerate(floors):
                    if f.get("revealed"):
                        deepest = i
                shown = []
                for i, f in enumerate(floors[: deepest + 1]):
                    if f.get("revealed"):
                        shown.append({
                            "n": f["n"], "type": f["type"], "dv": f.get("dv"),
                            "label": f.get("label", ""), "state": f.get("state", "Intact"),
                            "revealed": True,
                        })
                    else:
                        shown.append({"n": f["n"], "type": "???", "dv": None,
                                      "label": "", "state": "", "revealed": False})
                nets.append({
                    "id": net["id"], "name": net["name"],
                    "description": net["description"],
                    "difficulty": net["difficulty"],
                    "locked": net.get("locked", False),
                    "floors": shown,
                    "more": len(floors) > deepest + 1,
                })
            run = None
            if s.get("run"):
                net = self.net_by_id(s["run"]["net_id"])
                if net and net.get("visible"):
                    run = {"net_id": net["id"], "net_name": net["name"],
                           "floor": s["run"].get("floor", 1)}
            return {
                "session": s["name"],
                "nets": nets,
                "run": run,
                "character": s["character"],
                "feed": s["feed"][-40:],
                "actions": NET_ACTIONS,
                "operations": OPERATIONS,
                "pending": [p["id"] for p in s["pending"]],
            }

    # -- inbound from player ----------------------------------------------

    def handle_client_message(self, client, msg):
        kind = msg.get("type")
        with self.state_lock:
            if kind == "join":
                client.handle = msg.get("handle") or "netrunner"
                if not self.session["character"].get("handle"):
                    self.session["character"]["handle"] = client.handle
                self.log("%s jacked into the session." % client.handle)
                self.feed("%s connected." % client.handle, "sys")
                self.touch()

            elif kind == "enter_run":
                net = self.net_by_id(msg.get("net_id"))
                if not net or not net.get("visible"):
                    client.send({"type": "denied", "reason": "That architecture is not on the map."})
                elif net.get("locked"):
                    client.send({"type": "denied", "reason": "That architecture is locked down."})
                else:
                    if net.get("reset_on_entry"):
                        summary = reset_net(net)
                        self.log("Auto-reset %s on entry (%s)." % (net["name"], summary))
                    self.session["run"] = {"net_id": net["id"], "floor": 1}
                    self.log("%s started a run on %s." % (client.handle, net["name"]))
                    self.feed("Jacked into %s." % net["name"], "sys")
                    self.touch()

            elif kind == "leave_run":
                if self.session.get("run"):
                    net = self.net_by_id(self.session["run"]["net_id"])
                    self.log("%s jacked out of %s." % (client.handle, net["name"] if net else "?"))
                    self.feed("Jacked out.", "sys")
                    self.session["run"] = None
                    self.touch()

            elif kind == "action":
                entry = {
                    "id": uuid.uuid4().hex[:6],
                    "t": now_stamp(),
                    "handle": client.handle,
                    "action": msg.get("action", "?"),
                    "target": msg.get("target", ""),
                    "roll": msg.get("roll"),
                    "note": msg.get("note", ""),
                }
                self.session["pending"].append(entry)
                roll = (" [rolled %s]" % entry["roll"]) if entry["roll"] else ""
                self.log("ACTION %s: %s -> %s%s" % (entry["handle"], entry["action"], entry["target"], roll))
                self.feed("%s: %s%s -- waiting on the GM." % (entry["handle"], entry["action"], roll), "action")
                self.touch()

            elif kind == "chat":
                text = (msg.get("text") or "").strip()
                if text:
                    self.log("%s says: %s" % (client.handle, text))
                    self.feed("<%s> %s" % (client.handle, text), "player")
                    self.touch()

            elif kind == "ping":
                client.send({"type": "pong"})
        self.bump()

    # ======================================================================
    # Screens
    # ======================================================================

    def run(self):
        while True:
            head = self.ui.banner("NET MANAGER", "Cyberpunk RED  " + G["dot"] + "  Game Master console")
            saves = list_saves()
            head += ["", " " + C.GREY + "%d saved session(s) in ./saves" % len(saves) + C.RESET, ""]
            choice = self.ui.menu(head, [
                ("Start a new session", "new"),
                ("Continue a session", "load"),
                None,
                ("Netrunning reference", "ref"),
                ("Quit", "quit"),
            ])
            if choice in (None, "quit"):
                return
            if choice == "new":
                self.screen_new_session()
            elif choice == "load":
                self.screen_load_session()
            elif choice == "ref":
                self.screen_reference(self.ui.banner("NETRUNNING REFERENCE"))

    def screen_new_session(self):
        head = self.ui.banner("NEW SESSION")
        name = self.ui.prompt(head, "Name this session (e.g. 'Arasaka Tower Job'):", "")
        if not name:
            return
        self.session = new_session(name.strip())
        self.log("Session created.")
        save_session(self.session)
        self.screen_session()

    def screen_load_session(self):
        saves = list_saves()
        if not saves:
            self.ui.alert(self.ui.banner("CONTINUE SESSION"), ["No saves found in ./saves yet."])
            return
        items = []
        for path, data in saves:
            label = "%s  %s  %s%d NETs  %s  last played %s%s" % (
                pad(C.BOLD + data.get("name", "?") + C.RESET, 32),
                C.GREY + G["dot"] + C.RESET,
                C.GREY, len(data.get("nets", [])),
                G["dot"], data.get("updated", "?"), C.RESET,
            )
            items.append((label, path))
        items.append(None)
        items.append((C.RED + "Delete a save" + C.RESET, "__delete__"))
        head = self.ui.banner("CONTINUE SESSION")
        choice = self.ui.menu(head, items)
        if choice is None:
            return
        if choice == "__delete__":
            return self.screen_delete_save()
        with open(choice, encoding="utf-8") as fh:
            self.session = json.load(fh)
        self.session.setdefault("pending", [])
        self.session.setdefault("feed", [])
        self.session.setdefault("run", None)
        self.log("Session loaded.")
        self.screen_session()

    def screen_delete_save(self):
        saves = list_saves()
        items = [("%s  (%s)" % (d.get("name", "?"), d.get("updated", "?")), p) for p, d in saves]
        head = self.ui.banner("DELETE SAVE", "this cannot be undone")
        path = self.ui.menu(head, items)
        if not path:
            return
        if self.ui.confirm(head, "Permanently delete %s ?" % os.path.basename(path), danger=True):
            try:
                os.remove(path)
            except OSError as exc:
                self.ui.alert(head, ["Could not delete: %s" % exc], C.RED)

    # -- live session ------------------------------------------------------

    def screen_session(self):
        self.server = Server(self, self.port, self.beacon)
        try:
            self.server.start()
        except OSError as exc:
            self.ui.alert(self.ui.banner("NETWORK ERROR"),
                          ["Could not open port %d: %s" % (self.port, exc),
                           "Another copy may already be running.",
                           "Try:  python netmanager.py --port 7718"], C.RED)
            return
        self.log("Server listening on %s:%d" % (self.server.ip, self.port))
        try:
            keep = 0
            while True:
                head = self.session_header
                pending = len(self.session["pending"])
                pend_label = "Pending NET Actions"
                if pending:
                    pend_label = C.RED + G["block"] + " Pending NET Actions (%d)" % pending + C.RESET
                run = self.session.get("run")
                run_label = "Run control"
                if run:
                    net = self.net_by_id(run["net_id"])
                    run_label = C.ORANGE + "Run control -- IN %s, floor %d" % (
                        net["name"] if net else "?", run.get("floor", 1)) + C.RESET
                choice = self.ui.menu(head, [
                    ("NET Architectures (%d)" % len(self.session["nets"]), "nets"),
                    (pend_label, "pending"),
                    (run_label, "run"),
                    None,
                    ("Send a message to the netrunner", "msg"),
                    ("Netrunner character sheet", "char"),
                    ("Session log", "log"),
                    ("Netrunning reference", "ref"),
                    None,
                    ("Save & return to main menu", "back"),
                ], index=keep, body=lambda: self.feed_pane(6), watch=lambda: self.version)
                keep = self.ui.last_index
                if choice is REFRESH:
                    continue
                if choice is None or choice == "back":
                    save_session(self.session)
                    return
                if choice == "nets":
                    self.screen_nets()
                elif choice == "pending":
                    self.screen_pending()
                elif choice == "run":
                    self.screen_run_control()
                elif choice == "msg":
                    self.screen_message()
                elif choice == "char":
                    self.screen_character()
                elif choice == "log":
                    self.screen_log()
                elif choice == "ref":
                    self.screen_reference(self.session_header())
        finally:
            self.server.stop()
            self.server = None

    def session_header(self):
        s = self.session
        clients = self.server.connected() if self.server else []
        if clients:
            who = ", ".join(c.handle for c in clients)
            status = C.GREEN + "ONLINE " + G["dot"] + " " + who + C.RESET
        else:
            status = C.RED + "no netrunner connected" + C.RESET
        beacon = "broadcasting" if self.beacon else "beacon off"
        sub = "%s:%d  %s  %s" % (self.server.ip if self.server else "?", self.port, G["dot"], beacon)
        head = self.ui.banner(s["name"].upper(), sub)
        head.append(" " + status)
        visible = sum(1 for n in s["nets"] if n.get("visible"))
        head.append(" " + C.GREY + "%d NET(s), %d visible to the netrunner" % (len(s["nets"]), visible) + C.RESET)
        head.append("")
        return head

    def feed_pane(self, count):
        entries = self.session["feed"][-count:]
        lines = [self.ui.rule("FEED")]
        if not entries:
            lines.append(" " + C.GREY + "(nothing yet)" + C.RESET)
        for e in entries:
            color = {"gm": C.CYAN, "sys": C.GREY, "action": C.YELLOW,
                     "player": C.MAGENTA, "alert": C.RED}.get(e.get("kind"), C.RESET)
            lines.append(" " + C.GREY + e["t"] + C.RESET + " " + color + e["text"] + C.RESET)
        return lines

    # -- nets --------------------------------------------------------------

    def screen_nets(self):
        keep = 0
        while True:
            items = []
            for net in self.session["nets"]:
                if net.get("visible"):
                    flag = C.GREEN + "[VISIBLE]" + C.RESET
                elif net.get("locked"):
                    flag = C.RED + "[LOCKED] " + C.RESET
                else:
                    flag = C.GREY + "[hidden] " + C.RESET
                items.append((
                    "%s %s %s%s  %d floors  %s  %s%s" % (
                        flag, pad(C.BOLD + net["name"] + C.RESET, 28),
                        C.GREY, G["dot"], len(net["floors"]), G["dot"],
                        net["difficulty"], C.RESET),
                    net["id"]))
            if items:
                items.append(None)
            items.append((C.CYAN + "+ Create a new NET Architecture" + C.RESET, "__new__"))
            items.append((C.CYAN + "+ Import an architecture" + C.RESET +
                          C.GREY + "  from the library or another session" + C.RESET, "__import__"))
            if self.session["nets"]:
                items.append(("Reset every architecture" + C.GREY +
                              "  clears all run progress" + C.RESET, "__resetall__"))
            head = self.ui.banner("NET ARCHITECTURES", "enter to edit  " + G["dot"] +
                                  "  v toggles visibility  " + G["dot"] + "  r resets progress")
            choice = self.ui.menu(head, items, index=keep,
                                  hotkeys={"v": ("toggle visible", "vis"),
                                           "r": ("reset", "reset")},
                                  watch=lambda: self.version)
            keep = self.ui.last_index
            if choice is REFRESH:
                continue
            if choice is None:
                return
            if isinstance(choice, tuple):  # hotkey
                _, token, net_id = choice
                net = self.net_by_id(net_id) if net_id else None
                if net and token == "vis":
                    net["visible"] = not net["visible"]
                    self.log("%s is now %s." % (net["name"], "VISIBLE" if net["visible"] else "hidden"))
                    if net["visible"]:
                        self.feed("New architecture on your map: %s" % net["name"], "sys")
                    self.touch()
                elif net and token == "reset":
                    self.reset_one(net, head)
                continue
            if choice == "__import__":
                self.screen_import()
                continue
            if choice == "__resetall__":
                if self.ui.confirm(head, "Reset all %d architectures? Every floor goes back to "
                                         "Intact and unrevealed." % len(self.session["nets"])):
                    for net in self.session["nets"]:
                        reset_net(net)
                    self.log("Reset every architecture.")
                    self.feed("The architectures have been restored.", "sys")
                    self.touch()
                continue
            if choice == "__new__":
                name = self.ui.prompt(head, "Name the architecture:", "")
                if name and name.strip():
                    net = new_net(name.strip())
                    self.session["nets"].append(net)
                    self.log("Created NET '%s'." % net["name"])
                    self.touch()
                    self.screen_net(net)
                continue
            net = self.net_by_id(choice)
            if net:
                self.screen_net(net)

    def screen_net(self, net):
        keep = 0
        while True:
            head = self.ui.banner("NET: " + net["name"].upper(),
                                  net["description"] or "(no description)")
            head.append(" " + (C.GREEN + "VISIBLE to the netrunner" if net["visible"]
                               else C.GREY + "hidden from the netrunner") + C.RESET)
            if net.get("locked"):
                head.append(" " + C.RED + "LOCKED -- the netrunner cannot start a run here" + C.RESET)
            if net.get("reset_on_entry"):
                head.append(" " + C.CYAN + "AUTO-RESET -- wipes clean every time they jack in" + C.RESET)
            head.append("")

            items = []
            for i, f in enumerate(net["floors"]):
                eye = C.GREEN + "o" + C.RESET if f.get("revealed") else C.GREY + "." + C.RESET
                state = f.get("state", "Intact")
                state_col = {"Intact": C.RESET, "Defeated": C.GREEN, "Alerted": C.RED,
                             "Controlled": C.CYAN, "Destroyed": C.GREY, "Rezzed": C.ORANGE}.get(state, C.RESET)
                items.append((
                    "%s  %s %s  DV %s  %s%s%s  %s%s%s" % (
                        eye, pad(C.GREY + "FLOOR %d" % f["n"] + C.RESET, 16),
                        pad(C.BOLD + f["type"] + C.RESET, 22),
                        pad(str(f.get("dv") or "--"), 4),
                        state_col, pad(state, 12), C.RESET,
                        C.GREY, f.get("label", ""), C.RESET),
                    ("floor", i)))
            if items:
                items.append(None)
            items += [
                (C.CYAN + "+ Add a floor" + C.RESET, ("add", None)),
                None,
                (("Hide from netrunner" if net["visible"] else "Make VISIBLE to netrunner"), ("vis", None)),
                (("Unlock architecture" if net.get("locked") else "Lock architecture"), ("lock", None)),
                ("Rename", ("rename", None)),
                ("Edit description", ("desc", None)),
                ("Set difficulty (%s)" % net["difficulty"], ("diff", None)),
                ("Reveal all floors to the netrunner", ("revealall", None)),
                ("Hide all floors from the netrunner", ("hideall", None)),
                None,
                ("Reset this architecture" + C.GREY + "  every floor back to Intact and unrevealed"
                 + C.RESET, ("reset", None)),
                (("Stop auto-resetting when they jack in" if net.get("reset_on_entry")
                  else "Auto-reset every time the netrunner jacks in"), ("autoreset", None)),
                None,
                ("Save to the architecture library", ("export", None)),
                None,
                (C.RED + "Delete this architecture" + C.RESET, ("delete", None)),
            ]
            choice = self.ui.menu(head, items, index=keep,
                                  hotkeys={"r": ("toggle floor revealed", "rev")})
            keep = self.ui.last_index
            if choice is None:
                return
            if isinstance(choice, tuple) and choice[0] == "hotkey":
                _, token, value = choice
                if token == "rev" and isinstance(value, tuple) and value[0] == "floor":
                    f = net["floors"][value[1]]
                    f["revealed"] = not f.get("revealed")
                    self.log("Floor %d of %s %s." % (f["n"], net["name"],
                             "revealed" if f["revealed"] else "hidden"))
                    self.touch()
                continue

            kind, arg = choice
            if kind == "floor":
                self.screen_floor(net, arg)
            elif kind == "add":
                f = new_floor(len(net["floors"]) + 1)
                net["floors"].append(f)
                self.touch()
                self.screen_floor(net, len(net["floors"]) - 1)
            elif kind == "vis":
                net["visible"] = not net["visible"]
                self.log("%s is now %s." % (net["name"], "VISIBLE" if net["visible"] else "hidden"))
                if net["visible"]:
                    self.feed("New architecture on your map: %s" % net["name"], "sys")
                self.touch()
            elif kind == "lock":
                net["locked"] = not net.get("locked")
                self.touch()
            elif kind == "rename":
                v = self.ui.prompt(head, "New name:", net["name"])
                if v and v.strip():
                    net["name"] = v.strip()
                    self.touch()
            elif kind == "desc":
                v = self.ui.prompt(head, "Description (the netrunner sees this):", net["description"])
                if v is not None:
                    net["description"] = v
                    self.touch()
            elif kind == "diff":
                v = self.ui.menu(head, [(d, d) for d in DIFFICULTIES])
                if v:
                    net["difficulty"] = v
                    self.touch()
            elif kind in ("revealall", "hideall"):
                for f in net["floors"]:
                    f["revealed"] = (kind == "revealall")
                self.touch()
            elif kind == "reset":
                self.reset_one(net, head)
            elif kind == "autoreset":
                net["reset_on_entry"] = not net.get("reset_on_entry")
                self.log("%s: auto-reset on entry %s." % (
                    net["name"], "ON" if net["reset_on_entry"] else "off"))
                self.touch()
            elif kind == "export":
                self.export_one(net, head)
            elif kind == "delete":
                if self.ui.confirm(head, "Delete '%s' and all its floors?" % net["name"], danger=True):
                    run = self.session.get("run")
                    if run and run["net_id"] == net["id"]:
                        self.session["run"] = None
                    self.session["nets"].remove(net)
                    self.log("Deleted NET '%s'." % net["name"])
                    self.touch()
                    return

    # -- reset / library ---------------------------------------------------

    def reset_one(self, net, head, ask=True):
        """Wipe the netrunner's progress through one architecture."""
        run = self.session.get("run")
        in_use = bool(run and run["net_id"] == net["id"])
        if ask:
            warn = "Reset '%s'? Every floor goes back to Intact and unrevealed." % net["name"]
            if in_use:
                warn += " The netrunner is inside it right now."
            if not self.ui.confirm(head, warn):
                return False
        summary = reset_net(net)
        if in_use:
            self.session["run"]["floor"] = 1
            self.feed("The architecture reshapes itself around you. You are back at the entry.", "sys")
        self.log("Reset %s (%s)." % (net["name"], summary))
        self.touch()
        return True

    def export_one(self, net, head):
        existing = library_path(net["name"])
        if os.path.exists(existing):
            if not self.ui.confirm(head, "'%s' is already in the library. Overwrite it?" % net["name"]):
                return
        try:
            path = export_net(net)
        except OSError as exc:
            self.ui.alert(head, ["Could not write to the library:", str(exc)], C.RED)
            return
        self.log("Exported '%s' to the library." % net["name"])
        self.ui.alert(head, [
            "Saved to %s" % os.path.relpath(path, HERE),
            "",
            "%d floor(s) with their types, DVs, labels and GM notes." % len(net["floors"]),
            "Run progress and visibility are not stored -- an imported copy",
            "always starts pristine and hidden.",
        ], C.GREEN)

    def screen_import(self):
        """Bring an architecture in from the library or from another session."""
        while True:
            head = self.ui.banner("IMPORT AN ARCHITECTURE",
                                  "copies are independent -- editing one will not touch the other")
            items = []
            library = list_library()
            if library:
                items.append((C.GREY + "-- library --" + C.RESET, HEADING))
                for path, data in library:
                    items.append((
                        "%s %s%s  %d floors  %s  saved %s%s" % (
                            pad(C.BOLD + data.get("name", "?") + C.RESET, 30),
                            C.GREY, G["dot"], len(data.get("floors", [])), G["dot"],
                            data.get("exported", "?"), C.RESET),
                        ("lib", path, data)))

            others = []
            for path, sess in list_saves():
                if sess.get("id") == self.session["id"]:
                    continue
                for net in sess.get("nets", []):
                    others.append((sess.get("name", "?"), net))
            if others:
                items.append(None)
                items.append((C.GREY + "-- other sessions --" + C.RESET, HEADING))
                for sess_name, net in others:
                    items.append((
                        "%s %s%s  %d floors  %s  from %s%s" % (
                            pad(C.BOLD + net["name"] + C.RESET, 30),
                            C.GREY, G["dot"], len(net.get("floors", [])), G["dot"],
                            sess_name, C.RESET),
                        ("sess", None, net_template(net))))

            if not items:
                self.ui.alert(head, [
                    "Nothing to import yet.",
                    "",
                    "Open any architecture and choose 'Save to the architecture",
                    "library' to build up a set you can reuse across sessions.",
                    "Architectures from your other saved sessions show up here too.",
                ], C.GREY)
                return

            choice = self.ui.menu(head, items,
                                  hotkeys={"d": ("delete from library", "del")})
            if choice is None:
                return
            if isinstance(choice, tuple) and choice[0] == "hotkey":
                _, token, value = choice
                if token == "del" and value and value[0] == "lib":
                    if self.ui.confirm(head, "Remove '%s' from the library? The copy in this "
                                             "session is untouched." % value[2].get("name", "?"),
                                       danger=True):
                        try:
                            os.remove(value[1])
                        except OSError as exc:
                            self.ui.alert(head, ["Could not delete: %s" % exc], C.RED)
                continue
            if not isinstance(choice, tuple):
                continue

            _, _, data = choice
            name = unique_net_name(self.session, data.get("name", "Imported NET"))
            typed = self.ui.prompt(head, "Name it in this session:", name)
            if typed is None:
                continue
            net = net_from_template(data, unique_net_name(self.session, typed.strip() or name))
            self.session["nets"].append(net)
            self.log("Imported NET '%s' (%d floors)." % (net["name"], len(net["floors"])))
            self.touch()
            self.ui.alert(head, ["'%s' imported, hidden from the netrunner." % net["name"],
                                 "Make it visible when you are ready."], C.GREEN)
            self.screen_net(net)
            return

    def screen_floor(self, net, index):
        keep = 0
        while True:
            f = net["floors"][index]
            head = self.ui.banner("%s  %s  FLOOR %d" % (net["name"].upper(), G["dot"], f["n"]))
            head.append(" " + (C.GREEN + "revealed to the netrunner" if f.get("revealed")
                               else C.GREY + "not yet revealed") + C.RESET)
            head.append("")
            items = [
                ("Type          %s" % (C.BOLD + f["type"] + C.RESET), "type"),
                ("DV            %s" % (f.get("dv") or "--"), "dv"),
                ("Label         %s" % (f.get("label") or C.GREY + "(none)" + C.RESET), "label"),
                ("State         %s" % f.get("state", "Intact"), "state"),
                ("GM notes      %s" % (C.GREY + (f.get("gm_notes") or "(none)") + C.RESET), "notes"),
                None,
                (("Hide from netrunner" if f.get("revealed") else "Reveal to netrunner"), "reveal"),
                None,
                ("Move floor up", "up"),
                ("Move floor down", "down"),
                (C.RED + "Delete this floor" + C.RESET, "delete"),
            ]
            choice = self.ui.menu(head, items, index=keep)
            keep = self.ui.last_index
            if choice is None:
                return
            if choice == "type":
                opts = []
                for name, blurb in FLOOR_TYPES:
                    if name.startswith("---"):
                        opts.append(None)
                        opts.append((C.GREY + name + C.RESET, None))
                        continue
                    opts.append((pad(C.BOLD + name + C.RESET, 22) + C.GREY + blurb + C.RESET, name))
                opts = [o for o in opts if o is None or o[1] is not None]
                v = self.ui.menu(head, opts)
                if v:
                    if v == "Custom":
                        v = self.ui.prompt(head, "Custom floor type:", f["type"]) or f["type"]
                    f["type"] = v
                    self.touch()
            elif choice == "dv":
                v = self.ui.prompt_int(head, "Difficulty Value (blank for none):", f.get("dv"), 0, 30)
                f["dv"] = v
                self.touch()
            elif choice == "label":
                v = self.ui.prompt(head, "Label shown to the netrunner once revealed:", f.get("label", ""))
                if v is not None:
                    f["label"] = v
                    self.touch()
            elif choice == "state":
                v = self.ui.menu(head, [(s, s) for s in FLOOR_STATES])
                if v:
                    f["state"] = v
                    self.touch()
            elif choice == "notes":
                v = self.ui.prompt(head, "GM notes (never sent to the netrunner):", f.get("gm_notes", ""))
                if v is not None:
                    f["gm_notes"] = v
                    self.touch()
            elif choice == "reveal":
                f["revealed"] = not f.get("revealed")
                self.touch()
            elif choice in ("up", "down"):
                j = index - 1 if choice == "up" else index + 1
                if 0 <= j < len(net["floors"]):
                    net["floors"][index], net["floors"][j] = net["floors"][j], net["floors"][index]
                    for k, fl in enumerate(net["floors"]):
                        fl["n"] = k + 1
                    index = j
                    self.touch()
            elif choice == "delete":
                if self.ui.confirm(head, "Delete floor %d?" % f["n"], danger=True):
                    net["floors"].pop(index)
                    for k, fl in enumerate(net["floors"]):
                        fl["n"] = k + 1
                    self.touch()
                    return

    # -- pending actions ---------------------------------------------------

    def screen_pending(self):
        while True:
            pend = self.session["pending"]
            head = self.ui.banner("PENDING NET ACTIONS",
                                  "%d waiting on your call" % len(pend))
            if not pend:
                self.ui.alert(head, ["Nothing waiting. The netrunner has not sent an action."],
                              C.GREY)
                return
            items = []
            for p in pend:
                roll = C.YELLOW + ("rolled %s" % p["roll"] if p.get("roll") else "no roll") + C.RESET
                items.append((
                    "%s %s  %s  %s  %s" % (
                        C.GREY + p["t"] + C.RESET,
                        pad(C.BOLD + p["action"] + C.RESET, 16),
                        pad(C.CYAN + (p.get("target") or "-") + C.RESET, 26),
                        roll,
                        C.GREY + (p.get("note") or "") + C.RESET),
                    p["id"]))
            items.append(None)
            items.append((C.RED + "Clear all pending" + C.RESET, "__clear__"))
            choice = self.ui.menu(head, items, watch=lambda: self.version)
            if choice is REFRESH:
                continue
            if choice is None:
                return
            if choice == "__clear__":
                if self.ui.confirm(head, "Discard all pending actions?", danger=True):
                    self.session["pending"] = []
                    self.touch()
                continue
            entry = next((p for p in pend if p["id"] == choice), None)
            if entry:
                self.resolve_action(entry)

    def resolve_action(self, entry):
        head = self.ui.banner("RESOLVE: " + entry["action"].upper())
        head += [
            " " + C.GREY + "from " + C.RESET + entry["handle"] +
            C.GREY + "  target " + C.RESET + (entry.get("target") or "-"),
            " " + C.GREY + "roll  " + C.RESET + str(entry.get("roll") or "--"),
        ]
        if entry.get("note"):
            head.append(" " + C.GREY + "note  " + C.RESET + entry["note"])
        head.append("")
        verdict = self.ui.menu(head, [
            (C.GREEN + "Success" + C.RESET, "success"),
            (C.RED + "Failure" + C.RESET, "fail"),
            (C.YELLOW + "Partial / complication" + C.RESET, "partial"),
            ("Custom result only", "custom"),
            None,
            ("Leave it pending", None),
        ])
        if verdict is None:
            return
        detail = self.ui.prompt(head, "What happens? (shown to the netrunner)", "")
        if detail is None:
            return
        plain = {"success": "SUCCESS", "fail": "FAILURE",
                 "partial": "PARTIAL", "custom": ""}[verdict]
        text = "%s -- %s%s%s" % (entry["action"], plain, ": " if plain and detail else "", detail)
        self.session["pending"] = [p for p in self.session["pending"] if p["id"] != entry["id"]]
        self.feed(text, "gm")
        self.log("Resolved %s as %s. %s" % (entry["action"], plain or "custom", detail))
        self.touch()

    # -- run control -------------------------------------------------------

    def screen_run_control(self):
        keep = 0
        while True:
            run = self.session.get("run")
            head = self.ui.banner("RUN CONTROL")
            if not run:
                self.ui.alert(head, ["The netrunner is not inside an architecture right now."], C.GREY)
                return
            net = self.net_by_id(run["net_id"])
            if not net:
                self.session["run"] = None
                self.touch()
                return
            head += [
                " " + C.ORANGE + "IN: " + net["name"] + C.RESET,
                " " + C.GREY + "current floor: " + C.RESET + C.BOLD + str(run.get("floor", 1)) + C.RESET +
                C.GREY + " of %d" % len(net["floors"]) + C.RESET,
                "",
            ]
            choice = self.ui.menu(head, [
                ("Move netrunner DOWN a floor", "down"),
                ("Move netrunner UP a floor", "up"),
                ("Set floor directly", "set"),
                None,
                ("Reveal the floor they are standing on", "reveal"),
                ("Set state of their current floor", "state"),
                None,
                ("Reset this architecture around them" + C.GREY +
                 "  back to floor 1, nothing revealed" + C.RESET, "reset"),
                None,
                (C.RED + G["block"] + " BLOCK THEM OUT OF THIS RUN" + C.RESET, "block"),
                ("End the run quietly (they jack out clean)", "endrun"),
                None,
                (C.RED + "Disconnect the netrunner entirely" + C.RESET, "disconnect"),
            ], index=keep, body=lambda: self.feed_pane(5), watch=lambda: self.version)
            keep = self.ui.last_index
            if choice is REFRESH:
                continue
            if choice is None:
                return
            floor_i = run.get("floor", 1) - 1
            if choice in ("down", "up"):
                delta = 1 if choice == "down" else -1
                run["floor"] = max(1, min(len(net["floors"]) or 1, run.get("floor", 1) + delta))
                self.feed("You move to floor %d." % run["floor"], "sys")
                self.touch()
            elif choice == "set":
                v = self.ui.prompt_int(head, "Floor number:", run.get("floor", 1), 1,
                                       max(1, len(net["floors"])))
                if v:
                    run["floor"] = v
                    self.feed("You are on floor %d." % v, "sys")
                    self.touch()
            elif choice == "reveal":
                if 0 <= floor_i < len(net["floors"]):
                    net["floors"][floor_i]["revealed"] = True
                    self.feed("Floor %d resolves on your screen." % (floor_i + 1), "sys")
                    self.touch()
            elif choice == "state":
                if 0 <= floor_i < len(net["floors"]):
                    v = self.ui.menu(head, [(s, s) for s in FLOOR_STATES])
                    if v:
                        net["floors"][floor_i]["state"] = v
                        self.touch()
            elif choice == "reset":
                self.reset_one(net, head)
            elif choice == "block":
                self.block_runner(net)
                return
            elif choice == "endrun":
                self.session["run"] = None
                self.feed("Run ended. You jack out clean.", "sys")
                self.log("GM ended the run on %s." % net["name"])
                self.touch()
                return
            elif choice == "disconnect":
                if self.ui.confirm(head, "Drop the netrunner's connection entirely?", danger=True):
                    reason = self.ui.prompt(head, "Message to show before they drop:",
                                            "The GM closed your link.")
                    self.server.broadcast({"type": "disconnect", "reason": reason or ""})
                    time.sleep(0.3)
                    for c in self.server.connected():
                        c.close()
                    self.session["run"] = None
                    self.log("Disconnected the netrunner.")
                    self.touch()
                    return

    def block_runner(self, net):
        head = self.ui.banner("BLOCK OUT", "boot the netrunner from " + net["name"])
        preset = self.ui.menu(head, [
            ("System lockout -- the architecture slams shut on you.", 1),
            ("Black ICE flatlines your connection. You are OUT.", 2),
            ("A Demon traces your signal and severs the link.", 3),
            ("The sysop pulls the plug. Hard.", 4),
            None,
            ("Write my own message", 0),
        ])
        if preset is None:
            return
        messages = {
            1: "SYSTEM LOCKOUT -- the architecture slams shut on you.",
            2: "BLACK ICE FLATLINES YOUR CONNECTION. You are OUT.",
            3: "A DEMON TRACED YOUR SIGNAL and severed the link.",
            4: "THE SYSOP PULLED THE PLUG. Hard.",
        }
        if preset == 0:
            msg = self.ui.prompt(head, "Message shown to the netrunner:", "")
            if msg is None:
                return
        else:
            msg = messages[preset]
        after = self.ui.menu(head + ["", " " + C.GREY + "What happens to the architecture?" + C.RESET, ""], [
            ("Leave it exactly as they left it", "keep"),
            ("Reset it -- a fresh run if they come back", "reset"),
            ("Lock it so they cannot re-enter", "lock"),
            ("Reset it AND lock it", "both"),
        ])
        if after is None:
            return
        if after in ("lock", "both"):
            net["locked"] = True
        self.session["run"] = None
        self.session["pending"] = []
        self.feed(msg, "alert")
        self.log("BLOCKED the netrunner out of %s. (%s)" % (net["name"], msg))
        if after in ("reset", "both"):
            # reset_net clears `locked`, so re-apply it afterwards
            summary = reset_net(net)
            if after == "both":
                net["locked"] = True
            self.log("Reset %s after the block (%s)." % (net["name"], summary))
        self.touch(push=False)
        self.server.broadcast({"type": "blocked", "reason": msg, "net": net["name"]})
        self.server.push_state()
        notes = ["Netrunner ejected from %s." % net["name"]]
        if after in ("reset", "both"):
            notes.append("The architecture is pristine again.")
        if after in ("lock", "both"):
            notes.append("It is locked -- unlock it when you want them back in.")
        self.ui.alert(head, notes, C.GREEN)

    # -- misc screens ------------------------------------------------------

    def screen_message(self):
        head = self.ui.banner("MESSAGE THE NETRUNNER")
        text = self.ui.prompt(head, "Message:", "")
        if text and text.strip():
            self.feed(text.strip(), "gm")
            self.log("GM message: %s" % text.strip())
            self.touch()

    def screen_character(self):
        while True:
            ch = self.session["character"]
            head = self.ui.banner("NETRUNNER SHEET", ch.get("handle") or "(no handle yet)")
            head.append("")
            items = [
                ("Handle             %s" % (ch.get("handle") or "-"), "handle"),
                ("Interface rank     %s" % ch.get("interface", 1), "interface"),
                ("NET Actions/turn   %s" % ch.get("actions_per_turn", 1), "apt"),
                ("HP                 %s / %s" % (ch.get("hp", 0), ch.get("hp_max", 0)), "hp"),
                ("Notes              %s" % (C.GREY + (ch.get("notes") or "(none)") + C.RESET), "notes"),
                None,
                ("Programs (%d)" % len(ch.get("programs", [])), "programs"),
            ]
            choice = self.ui.menu(head, items)
            if choice is None:
                return
            if choice == "handle":
                v = self.ui.prompt(head, "Handle:", ch.get("handle", ""))
                if v is not None:
                    ch["handle"] = v
            elif choice == "interface":
                ch["interface"] = self.ui.prompt_int(head, "Interface rank:", ch.get("interface", 1), 0, 10)
            elif choice == "apt":
                ch["actions_per_turn"] = self.ui.prompt_int(head, "NET Actions per turn:",
                                                            ch.get("actions_per_turn", 1), 1, 9)
            elif choice == "hp":
                ch["hp"] = self.ui.prompt_int(head, "Current HP:", ch.get("hp", 0), -50, 200)
                ch["hp_max"] = self.ui.prompt_int(head, "Max HP:", ch.get("hp_max", 0), 1, 200)
            elif choice == "notes":
                v = self.ui.prompt(head, "Notes (netrunner can see these):", ch.get("notes", ""))
                if v is not None:
                    ch["notes"] = v
            elif choice == "programs":
                self.screen_programs()
                continue
            self.touch()

    def screen_programs(self):
        ch = self.session["character"]
        ch.setdefault("programs", [])
        while True:
            head = self.ui.banner("PROGRAMS IN THE DECK")
            items = [(p, ("edit", i)) for i, p in enumerate(ch["programs"])]
            if items:
                items.append(None)
            items.append((C.CYAN + "+ Add a program" + C.RESET, ("add", None)))
            choice = self.ui.menu(head, items)
            if choice is None:
                return
            kind, i = choice
            if kind == "add":
                v = self.ui.prompt(head, "Program (e.g. 'Sword  Attacker  ATK 3  DMG 3d6'):", "")
                if v and v.strip():
                    ch["programs"].append(v.strip())
                    self.touch()
            else:
                v = self.ui.prompt(head, "Edit (blank to delete):", ch["programs"][i])
                if v is None:
                    continue
                if v.strip():
                    ch["programs"][i] = v.strip()
                else:
                    ch["programs"].pop(i)
                self.touch()

    def screen_log(self):
        head = self.ui.banner("SESSION LOG", "newest last")
        lines = []
        _, h = self.ui.size()
        for e in self.session["log"][-(h - 10):]:
            lines.append(" " + C.GREY + e["t"] + C.RESET + "  " + e["text"])
        if not lines:
            lines = [" " + C.GREY + "(empty)" + C.RESET]
        self.ui.draw(head + lines + ["", self.ui.rule(), " " + C.GREY + "press any key" + C.RESET])
        while read_key(0.2) is None:
            pass

    def screen_reference(self, head):
        while True:
            items = [(pad(C.BOLD + a["name"] + C.RESET, 16) + C.GREY + a["desc"] + C.RESET, a)
                     for a in NET_ACTIONS]
            items.append(None)
            items += [(pad(C.BOLD + o["name"] + C.RESET, 22) + C.GREY + o["desc"] + C.RESET, o)
                      for o in OPERATIONS]
            choice = self.ui.menu(list(head) + [self.ui.rule("NET ACTIONS")], items)
            if choice is None:
                return
            self.ui.alert(self.ui.banner(choice["name"].upper()), [
                "Cost:  " + choice["cost"],
                "Check: " + choice["check"],
                "",
            ] + wrap(choice["desc"], 70), C.CYAN)


def wrap(text, width):
    import textwrap
    return textwrap.wrap(text, width) or [""]


# --------------------------------------------------------------------------

def diagnostics(port):
    """Print what this machine can see and reach. No TUI, just facts."""
    print("NET MANAGER -- network diagnostics\n")
    addrs = local_ipv4s()
    print("Local IPv4 addresses:")
    for a in addrs:
        print("    %s" % a)
    if not addrs:
        print("    none found -- is this machine on a network?")
    print("\n  Players should be told:  %s:%d" % (lan_ip(), port))

    print("\nBeacons will be sent to:")
    plan = broadcast_plan()
    for src, dst in plan:
        print("    from %-16s -> %s:%d" % (src or "(default route)", dst, BEACON_PORT))

    print("\nPort checks:")
    for label, kind, p in (("TCP session port", socket.SOCK_STREAM, port),
                           ("UDP probe port", socket.SOCK_DGRAM, PROBE_PORT)):
        s = socket.socket(socket.AF_INET, kind)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", p))
            print("    %-18s %-5d OK" % (label, p))
        except OSError as exc:
            print("    %-18s %-5d IN USE (%s)" % (label, p, exc))
        finally:
            s.close()

    caster = Broadcaster()
    payload = json.dumps({"magic": BEACON_MAGIC, "diag": True}).encode("utf-8")
    sent = caster.send(payload, BEACON_PORT)
    caster.close()
    print("\n    test beacon went out on %d of %d route(s)" % (sent, len(plan)))
    if sent < len(plan):
        print("    (some routes refused -- usually harmless if at least one worked)")

    print("""
If a player on another machine cannot see the session:

  1. Make sure you are INSIDE a session. The main menu does not broadcast.
  2. Allow this program through the firewall -- on Windows say yes to the
     Defender prompt for PRIVATE networks; on Linux
       sudo ufw allow %d/tcp && sudo ufw allow %d/udp && sudo ufw allow %d/udp
  3. Confirm both machines share a subnet. If the addresses above do not look
     like the player's address, you are on different networks (guest Wi-Fi and
     a main Wi-Fi are usually separate, and many networks block broadcasts
     between clients entirely).
  4. Failing all that, the player can use 'Enter an address manually' with the
     address printed above -- that needs only the TCP port, no broadcasts.
""" % (port, BEACON_PORT, PROBE_PORT))


def main():
    ap = argparse.ArgumentParser(description="Cyberpunk RED NET Manager (GM console)")
    ap.add_argument("--port", type=int, default=DEFAULT_TCP_PORT, help="TCP port to listen on")
    ap.add_argument("--ascii", action="store_true", help="ASCII-only box drawing")
    ap.add_argument("--no-beacon", action="store_true", help="disable LAN auto-discovery broadcast")
    ap.add_argument("--diag", action="store_true",
                    help="print network diagnostics and exit (no game UI)")
    args = ap.parse_args()

    if args.diag:
        diagnostics(args.port)
        return

    _prepare_console()
    pick_glyphs(args.ascii)
    os.makedirs(SAVE_DIR, exist_ok=True)

    app = App(args.port, not args.no_beacon)
    sys.stdout.write("\x1b[2J")
    try:
        with RawInput():
            app.run()
    except KeyboardInterrupt:
        pass
    finally:
        if app.server:
            app.server.stop()
        if app.session:
            save_session(app.session)
        sys.stdout.write("\x1b[0m\n")
        print("Link closed.")


if __name__ == "__main__":
    main()
