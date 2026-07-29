#!/usr/bin/env python3
"""
NETRUNNER  --  player terminal for Cyberpunk RED netrunning.

Single-file, stdlib-only. Runs on Windows, macOS and Linux.

    python3 netrunner.py [--host 192.168.1.20] [--port 7717] [--ascii]

Open it, pick a session the GM is running on your LAN, jack in, and work
the architecture. Everything the GM changes shows up here live.

Your handle and the last server you used are kept in ./saves/profile.json.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import socket
import sys
import textwrap
import threading
import time
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
BEACON_PORT = 7718
BEACON_MAGIC = "CPRED_NETRUN_V1"
PROTOCOL = 1


def _prepare_console():
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
        for handle_id in (-11, -12):
            handle = k32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(handle, ctypes.byref(mode)):
                k32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


class RawInput:
    def __init__(self):
        self.fd = None
        self.saved = None

    def __enter__(self):
        if not IS_WIN and sys.stdin.isatty():
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?25l")
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
    if lead >= 0x80:
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
    if nxt == "O":
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
    out, shown, i = [], 0, 0
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


def wrap(text, width):
    return textwrap.wrap(text, width) or [""]


# --------------------------------------------------------------------------
# UI toolkit
# --------------------------------------------------------------------------

REFRESH = object()  # returned by menu() when watched state changed
HEADING = object()  # a menu row that is drawn but cannot be selected


class UI:
    last_index = 0   # where the highlight rested when menu() returned

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
            C.MAGENTA + G["tl"] + bar + G["tr"] + C.RESET,
            C.MAGENTA + G["v"] + C.RESET + pad(" " + C.BOLD + C.MAGENTA + title + C.RESET, w - 2)
            + C.MAGENTA + G["v"] + C.RESET,
        ]
        if subtitle:
            out.append(C.MAGENTA + G["v"] + C.RESET + pad(" " + C.GREY + subtitle + C.RESET, w - 2)
                       + C.MAGENTA + G["v"] + C.RESET)
        out.append(C.MAGENTA + G["bl"] + bar + G["br"] + C.RESET)
        return out

    def rule(self, label=""):
        w, _ = self.size()
        if not label:
            return C.GREY + G["h"] * w + C.RESET
        text = G["h"] * 2 + " " + label + " "
        return C.GREY + text + G["h"] * max(0, w - vislen(text)) + C.RESET

    def menu(self, head, items, foot=None, hotkeys=None, index=0, body=None, watch=None):
        """head/body may be lists or zero-arg callables returning lists.
        watch: zero-arg callable returning a comparable token; when it changes
        the menu returns REFRESH so the caller can rebuild."""
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
            # play -- which is what made off-screen entries unreachable once the
            # architecture map or feed pane grew tall.
            at = {real_i: k for k, (real_i, _it) in enumerate(visible)}
            if vis_sel:
                pos = at.get(index, 0)
                if pos < scroll:
                    scroll = pos
                elif pos >= scroll + room:
                    scroll = pos - room + 1
            scroll = max(0, min(scroll, max(0, len(visible) - room)))
            window = visible[scroll: scroll + room]

            if not visible:
                lines.append("   " + C.GREY + "(no matches)" + C.RESET)
            if scroll > 0:
                lines.append("   " + C.GREY + G["up"] + " %d more above" % scroll + C.RESET)
            for real_i, item in window:
                if item is None:
                    lines.append("")
                    continue
                if real_i == index:
                    lines.append(C.CYAN + " " + G["arrow"] + " " + C.RESET + C.BOLD + item[0] + C.RESET)
                else:
                    lines.append("   " + item[0])
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
        return [(i, it) for i, it in pairs
                if it is None or needle in _ANSI_RE.sub("", it[0]).lower()]

    def prompt(self, head, label, default="", allow_empty=True):
        buf = str(default)
        sys.stdout.write("\x1b[?25h")
        try:
            while True:
                lines = (head() if callable(head) else list(head))
                lines = list(lines) + [
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
            return default
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
# Fallback reference data (the GM's copy wins once connected)
# --------------------------------------------------------------------------

FALLBACK_ACTIONS = [
    {"name": "Pathfinder", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs Floor DV",
     "dv": "DV of the floor being scanned",
     "success": "The next unrevealed floor below you is identified -- you learn "
                "what it is before you have to stand on it.",
     "failure": "You learn nothing. The floor stays unknown.",
     "desc": "Scan ahead into the architecture. This is how you avoid walking "
             "blind into Black ICE; a Netrunner who never runs Pathfinder is "
             "discovering floors the hard way."},
    {"name": "Backdoor", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs Password DV",
     "dv": "DV of the Password floor",
     "success": "The Password floor is defeated and you can move past it.",
     "failure": "The lock holds. The system may register the attempt -- the GM "
                "decides whether something wakes up.",
     "desc": "Force a Password floor. Only Password floors can be Backdoored; "
             "other floor types need their own action."},
    {"name": "Slide", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs the attacker's roll",
     "dv": "the attacking Black ICE's roll, not a fixed number",
     "success": "The attack misses you entirely.",
     "failure": "The attack lands and its effect applies in full.",
     "desc": "Dodge an incoming attack from Black ICE. Contested rather than "
             "fixed-DV, which is why the GM resolves it rather than the program."},
    {"name": "Cloak", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs the Demon's PER",
     "dv": "the Demon's PER, or a rival Netrunner's roll",
     "success": "You go unnoticed for now.",
     "failure": "You are spotted and whatever is hunting you knows where to look.",
     "desc": "Mask your presence from a Demon or an enemy Netrunner. Contested, "
             "so the GM resolves it."},
    {"name": "Control", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs Control Node DV",
     "dv": "DV of the Control Node",
     "success": "The node is yours. You can operate the hardware wired to it -- "
                "doors, cameras, turrets, lifts, whatever it runs.",
     "failure": "The node rejects you and stays under its own control.",
     "desc": "Seize a Control Node. This is the action that turns a NET run into "
             "a physical advantage for the rest of the crew."},
    {"name": "Eye-Dee", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs File DV",
     "dv": "DV of the File",
     "success": "You learn what the File actually holds before touching it.",
     "failure": "The contents stay unreadable.",
     "desc": "Identify a File. Worth doing before you copy or infect something "
             "you cannot describe."},
    {"name": "Virus", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs Floor DV",
     "dv": "DV of the File or Control Node",
     "success": "The virus installs and does whatever you and the GM agreed it does.",
     "failure": "The install fails and the target is untouched.",
     "desc": "Plant a virus on a File or a Control Node. What the virus does is "
             "set when you write it, not when you install it."},
    {"name": "Download", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs File DV",
     "dv": "DV of the File",
     "success": "A copy lands in your deck. It stays readable for the rest of "
                "the session, whether or not you are still in the architecture.",
     "failure": "The copy is corrupted and you get nothing.",
     "desc": "Pull a copy of a File out of the architecture. Eye-Dee tells you "
             "what something is; this is what actually takes it with you."},
    {"name": "Zap", "cost": "1 NET Action",
     "check": "Interface + 1d10 vs the target's DEF",
     "dv": "the target's DEF",
     "success": "1d6 damage to the target's REZ. At 0 REZ it is destroyed and "
                "the floor is clear.",
     "failure": "No damage -- the target is untouched and still in your way.",
     "desc": "Your built-in attack -- no program required. Weak next to a real "
             "Attacker program, but it is always available. Damage means the GM "
             "resolves the result rather than the program."},
]

FALLBACK_OPS = [
    {"name": "Move Down a Floor", "cost": "Movement", "check": "--",
     "desc": "Drop to the next floor of the architecture. GM confirms."},
    {"name": "Move Up a Floor", "cost": "Movement", "check": "--",
     "desc": "Climb back toward the entry point."},
    {"name": "Run a Program", "cost": "1 NET Action", "check": "--",
     "desc": "Rez an Attacker, Defender or Booster from your deck."},
    {"name": "Speak to the GM", "cost": "--", "check": "--",
     "desc": "Describe something your Netrunner does that isn't on this list."},
]


def roll_d10():
    """Cyberpunk RED d10: 10 explodes upward, 1 explodes downward."""
    first = random.randint(1, 10)
    if first == 10:
        bonus = random.randint(1, 10)
        return first + bonus, "10 crit +%d" % bonus
    if first == 1:
        penalty = random.randint(1, 10)
        return first - penalty, "1 fumble -%d" % penalty
    return first, str(first)


# --------------------------------------------------------------------------
# Discovery + connection
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR = os.path.join(HERE, "saves")
PROFILE = os.path.join(SAVE_DIR, "profile.json")


# The netrunner owns their own sheet. It lives here, not in the GM's session
# file, so it survives between games and follows you to any table. The GM only
# tracks what happens TO you during a run -- current HP and status -- which is
# kept separately as "condition" so the two can never overwrite each other.

PROGRAM_CLASSES = [
    "Attacker", "Defender", "Booster", "Anti-Program Attacker", "Black ICE",
]

DEFAULT_SKILLS = [
    {"name": "Interface", "level": 4},
    {"name": "Electronics/Security Tech", "level": 2},
    {"name": "Cryptography", "level": 0},
    {"name": "Library Search", "level": 0},
    {"name": "Perception", "level": 2},
]


def default_character():
    return {
        "handle": "",
        "role_rank": 4,
        "interface": 4,
        "actions_per_turn": 1,
        "hp_max": 25,
        "deck": {"name": "Cyberdeck", "slots": 7, "hardware": []},
        "programs": [
            {"name": "Sword", "cls": "Attacker", "atk": 3, "def": 0, "rez": 7,
             "effect": "3d6 damage to a program or Black ICE."},
            {"name": "Armor", "cls": "Defender", "atk": 0, "def": 4, "rez": 7,
             "effect": "Reduces damage you take by 4."},
        ],
        "skills": [dict(s) for s in DEFAULT_SKILLS],
        "notes": "",
    }


def new_program():
    return {"name": "", "cls": "Attacker", "atk": 0, "def": 0, "rez": 7, "effect": ""}


def load_profile():
    prof = {"handle": "", "last_host": "", "last_port": DEFAULT_TCP_PORT}
    try:
        with open(PROFILE, encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            prof.update(stored)
    except Exception:
        pass
    # Fill in anything a profile written by an older build is missing, so an
    # existing file keeps working instead of blowing up on a lookup.
    character = default_character()
    saved = prof.get("character")
    if isinstance(saved, dict):
        character.update(saved)
        deck = default_character()["deck"]
        if isinstance(saved.get("deck"), dict):
            deck.update(saved["deck"])
        character["deck"] = deck
    character.setdefault("handle", prof.get("handle", ""))
    if not character["handle"]:
        character["handle"] = prof.get("handle", "")
    prof["character"] = character
    return prof


def save_profile(prof):
    os.makedirs(SAVE_DIR, exist_ok=True)
    tmp = PROFILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(prof, fh, indent=2)
    os.replace(tmp, PROFILE)


class Discovery(threading.Thread):
    """Listens for the GM's UDP beacons and keeps a live table of sessions."""

    daemon = True

    def __init__(self):
        super().__init__()
        self.found = {}
        self.lock = threading.Lock()
        self.running = True
        self.error = None

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind(("", BEACON_PORT))
        except OSError as exc:
            self.error = str(exc)
            return
        sock.settimeout(0.5)
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
            if msg.get("magic") != BEACON_MAGIC:
                continue
            host = msg.get("ip") or addr[0]
            key = (host, msg.get("port"), msg.get("id"))
            with self.lock:
                self.found[key] = (msg, time.time())
        sock.close()

    def sessions(self):
        cutoff = time.time() - 6
        with self.lock:
            live = [(k, v[0]) for k, v in self.found.items() if v[1] > cutoff]
        live.sort(key=lambda kv: kv[1].get("session", ""))
        return live


class Connection(threading.Thread):
    daemon = True

    def __init__(self, app, host, port, handle, character=None):
        super().__init__()
        self.app = app
        self.host = host
        self.port = port
        self.handle = handle
        self.character = character or {}
        self.sock = None
        self.alive = False
        self.error = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect((self.host, self.port))
        self.sock.settimeout(None)
        self.alive = True
        self.send({"type": "join", "handle": self.handle, "protocol": PROTOCOL,
                   "character": self.character})
        self.start()

    def send(self, obj):
        if not self.alive:
            return False
        try:
            self.sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))
            return True
        except Exception:
            self.alive = False
            return False

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

    def run(self):
        buf = b""
        try:
            while self.alive:
                data = self.sock.recv(8192)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    if raw.strip():
                        try:
                            self.app.on_message(json.loads(raw.decode("utf-8")))
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            self.alive = False
            self.app.version += 1
            self.app.queue_alert("LINK LOST", ["The connection to the GM dropped."], C.RED)


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

class Client:
    def __init__(self, args):
        self.ui = UI()
        self.args = args
        self.profile = load_profile()
        self.conn = None
        self.state = None
        self.version = 0
        self.alerts = []
        self.lock = threading.Lock()
        self.discovery = Discovery()

    # -- inbound -----------------------------------------------------------

    def on_message(self, msg):
        kind = msg.get("type")
        if kind == "state":
            with self.lock:
                self.state = msg.get("state")
        elif kind == "blocked":
            with self.lock:
                if self.state:
                    self.state["run"] = None
            self.queue_alert(
                "BLOCKED OUT",
                wrap(msg.get("reason") or "You have been ejected.", 66)
                + ["", "You are dumped back to the game menu."],
                C.RED)
        elif kind == "denied":
            self.queue_alert("DENIED", wrap(msg.get("reason") or "Denied.", 66), C.YELLOW)
        elif kind == "disconnect":
            self.queue_alert("DISCONNECTED",
                             wrap(msg.get("reason") or "The GM closed your link.", 66), C.RED)
            if self.conn:
                self.conn.close()
        elif kind == "server_closing":
            self.queue_alert("SESSION CLOSED", ["The GM ended the session."], C.YELLOW)
        self.version += 1

    def queue_alert(self, title, lines, color=C.YELLOW):
        with self.lock:
            self.alerts.append((title, lines, color))
        self.version += 1

    def drain_alerts(self):
        """Show any queued alerts. Returns True if something was shown."""
        shown = False
        while True:
            with self.lock:
                if not self.alerts:
                    break
                title, lines, color = self.alerts.pop(0)
            self.ui.alert(self.ui.banner(title), lines, color)
            shown = True
        return shown

    def connected(self):
        return self.conn is not None and self.conn.alive

    def snap(self):
        with self.lock:
            return self.state

    # ======================================================================
    # Screens
    # ======================================================================

    def run(self):
        self.discovery.start()
        if self.args.host:
            self.try_connect(self.args.host, self.args.port)
            if self.connected():
                self.screen_game()
        while True:
            self.drain_alerts()
            if self.connected():
                self.screen_game()
                continue
            if self.screen_connect() == "quit":
                return

    # -- connect menu ------------------------------------------------------

    def screen_connect(self):
        def head():
            lines = self.ui.banner("NETRUNNER", "cyberdeck offline  " + G["dot"] + "  looking for open sessions")
            handle = self.profile.get("handle") or C.RED + "(not set)" + C.RESET
            lines.append(" " + C.GREY + "handle: " + C.RESET + C.BOLD + handle + C.RESET)
            if self.discovery.error:
                lines.append(" " + C.YELLOW + "auto-discovery unavailable (%s) -- use manual entry"
                             % self.discovery.error + C.RESET)
            lines.append("")
            return lines

        while True:
            if self.drain_alerts():
                continue
            sessions = self.discovery.sessions()
            items = []
            for (host, port, _sid), msg in sessions:
                items.append((
                    "%s %s  %s%s:%s  %s  %d NET(s)  %s  %d connected%s" % (
                        C.GREEN + G["dot"] + C.RESET,
                        pad(C.BOLD + msg.get("session", "?") + C.RESET, 30),
                        C.GREY, host, port, G["dot"], msg.get("nets", 0), G["dot"],
                        msg.get("players", 0), C.RESET),
                    ("connect", host, port)))
            if not items:
                items.append((C.GREY + "(scanning the local net for open sessions...)" + C.RESET, HEADING))
            items.append(None)
            last = self.profile.get("last_host")
            if last:
                items.append(("Reconnect to %s:%s" % (last, self.profile.get("last_port", DEFAULT_TCP_PORT)),
                              ("connect", last, self.profile.get("last_port", DEFAULT_TCP_PORT))))
            items += [
                ("Enter an address manually", ("manual",)),
                ("My cyberdeck" + C.GREY + "  stats, programs and skills" + C.RESET, ("sheet",)),
                ("Set my handle", ("handle",)),
                ("Netrunning reference", ("ref",)),
                None,
                ("Quit", ("quit",)),
            ]

            choice = self.ui.menu(head, items,
                                  watch=lambda: (len(self.discovery.sessions()), self.version))
            if choice is REFRESH:
                continue
            if choice is None:
                continue
            if choice is None or not isinstance(choice, tuple):
                continue
            kind = choice[0]
            if kind == "quit":
                return "quit"
            if kind == "handle":
                self.ask_handle(head)
            elif kind == "sheet":
                self.screen_character()
            elif kind == "ref":
                self.screen_reference(head)
            elif kind == "manual":
                host = self.ui.prompt(head, "GM's IP address:", self.profile.get("last_host", ""))
                if not host:
                    continue
                port = self.ui.prompt(head, "Port:", str(self.profile.get("last_port", DEFAULT_TCP_PORT)))
                try:
                    port = int(port)
                except (TypeError, ValueError):
                    port = DEFAULT_TCP_PORT
                self.try_connect(host.strip(), port)
                if self.connected():
                    return "connected"
            elif kind == "connect":
                self.try_connect(choice[1], choice[2])
                if self.connected():
                    return "connected"

    def ask_handle(self, head):
        v = self.ui.prompt(head, "Your netrunner handle:", self.profile.get("handle", ""))
        if v and v.strip():
            self.profile["handle"] = v.strip()
            self.profile["character"]["handle"] = v.strip()
            self.save_sheet()

    def save_sheet(self):
        """Persist the sheet and, if we are in a session, tell the GM."""
        save_profile(self.profile)
        if self.connected():
            self.conn.send({"type": "character", "character": self.profile["character"]})
        self.version += 1

    def try_connect(self, host, port):
        if not self.profile.get("handle"):
            self.ask_handle(self.ui.banner("IDENTIFY YOURSELF"))
            if not self.profile.get("handle"):
                return
        self.ui.draw(self.ui.banner("CONNECTING", "%s:%s" % (host, port)) +
                     ["", " " + C.CYAN + "negotiating link..." + C.RESET])
        conn = Connection(self, host, int(port), self.profile["handle"],
                          self.profile.get("character"))
        try:
            conn.connect()
        except Exception as exc:
            self.ui.alert(self.ui.banner("CONNECTION FAILED"), [
                "Could not reach %s:%s" % (host, port),
                str(exc),
                "",
                "Check that the GM has a session open and that both",
                "machines are on the same network.",
            ], C.RED)
            return
        self.conn = conn
        self.profile["last_host"] = host
        self.profile["last_port"] = int(port)
        save_profile(self.profile)
        # give the server a moment to push the first state
        for _ in range(30):
            if self.snap():
                break
            time.sleep(0.05)

    # -- game menu ---------------------------------------------------------

    def screen_game(self):
        keep = 0
        while True:
            if self.drain_alerts():
                continue
            if not self.connected():
                return
            state = self.snap() or {}
            if state.get("run"):
                self.screen_run()
                continue

            def head():
                s = self.snap() or {}
                lines = self.ui.banner("SESSION: " + (s.get("session") or "?").upper(),
                                       "%s  %s  jacked in" % (self.profile.get("handle", "?"), G["dot"]))
                lines.append(" " + self.status_strip())
                lines.append("")
                return lines

            nets = state.get("nets", [])
            items = []
            for net in nets:
                lock = C.RED + " [LOCKED]" + C.RESET if net.get("locked") else ""
                known = sum(1 for f in net["floors"] if f.get("revealed"))
                items.append((
                    "%s  %s%s  %s  %s known floor(s)%s%s" % (
                        pad(C.BOLD + net["name"] + C.RESET, 30),
                        C.GREY, net.get("difficulty", ""), G["dot"], known, C.RESET, lock),
                    ("net", net["id"])))
            if not nets:
                items.append((C.GREY + "(the GM has not put any architectures on your map yet)" + C.RESET, HEADING))
            items += [
                None,
                ("Netrunning actions & reference", ("ref",)),
                ("Saved files (%d)" % len((self.snap() or {}).get("files") or []) +
                 C.GREY + "  everything you have pulled out" + C.RESET, ("files",)),
                ("My cyberdeck" + C.GREY + "  stats, programs and skills" + C.RESET, ("char",)),
                ("Say something to the GM", ("chat",)),
                ("Full feed", ("feed",)),
                None,
                ("Disconnect", ("quit",)),
            ]
            choice = self.ui.menu(head, items, index=keep, body=lambda: self.feed_pane(7),
                                  watch=lambda: self.version)
            keep = self.ui.last_index
            if choice is REFRESH:
                continue
            if choice is None:          # esc == the Disconnect entry
                choice = ("quit",)
            if not isinstance(choice, tuple):
                continue
            kind = choice[0]
            if kind == "quit":
                if self.ui.confirm(head, "Disconnect from the session?"):
                    self.conn.close()
                    self.conn = None
                    self.state = None
                    return
            elif kind == "net":
                self.screen_net_detail(choice[1])
            elif kind == "ref":
                self.screen_reference(head)
            elif kind == "char":
                self.screen_character(head)
            elif kind == "files":
                self.screen_files()
            elif kind == "chat":
                text = self.ui.prompt(head, "Say to the GM:", "")
                if text and text.strip():
                    self.conn.send({"type": "chat", "text": text.strip()})
            elif kind == "feed":
                self.screen_feed()

    def status_strip(self):
        """One line of vitals: your stats, the GM's damage tracking."""
        ch = self.profile["character"]
        cond = (self.snap() or {}).get("condition") or {}
        hp = cond.get("hp")
        if hp is None:
            hp = ch.get("hp_max", "-")
        hp_col = C.RESET
        try:
            if ch.get("hp_max"):
                frac = float(hp) / float(ch["hp_max"])
                hp_col = C.RED if frac <= 0.33 else C.YELLOW if frac <= 0.66 else C.GREEN
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        out = (C.GREY + "INT " + C.RESET + str(ch.get("interface", "-")) +
               C.GREY + "   actions/turn " + C.RESET + str(ch.get("actions_per_turn", "-")) +
               C.GREY + "   HP " + C.RESET + hp_col +
               "%s/%s" % (hp, ch.get("hp_max", "-")) + C.RESET)
        if cond.get("status"):
            out += "   " + C.RED + cond["status"] + C.RESET
        return out

    def net_by_id(self, net_id):
        state = self.snap() or {}
        for net in state.get("nets", []):
            if net["id"] == net_id:
                return net
        return None

    def screen_net_detail(self, net_id):
        while True:
            if self.drain_alerts():
                continue
            net = self.net_by_id(net_id)
            if not net:
                return

            def head():
                n = self.net_by_id(net_id) or net
                lines = self.ui.banner("NET: " + n["name"].upper(), n.get("difficulty", ""))
                for line in wrap(n.get("description") or "No intel on this one yet.", 76):
                    lines.append(" " + C.GREY + line + C.RESET)
                lines.append("")
                lines.extend(self.architecture_map(n, None))
                lines.append("")
                return lines

            items = []
            if net.get("locked"):
                items.append((C.RED + "LOCKED -- you cannot get in right now" + C.RESET, HEADING))
            else:
                items.append((C.GREEN + "JACK IN" + C.RESET + C.GREY + "  start a run on this architecture" + C.RESET,
                              "jack"))
            items.append(("Back", None))
            choice = self.ui.menu(head, items, watch=lambda: self.version)
            if choice is REFRESH:
                continue
            if choice == "jack":
                self.conn.send({"type": "enter_run", "net_id": net_id})
                for _ in range(40):
                    if (self.snap() or {}).get("run"):
                        break
                    time.sleep(0.05)
                return
            return

    # -- architecture map --------------------------------------------------

    def architecture_map(self, net, current_floor):
        lines = [self.ui.rule("ARCHITECTURE")]
        floors = net.get("floors", [])
        if not floors:
            lines.append(" " + C.GREY + "Nothing scanned yet. Run Pathfinder." + C.RESET)
        for f in floors:
            here = current_floor is not None and f["n"] == current_floor
            marker = C.ORANGE + G["arrow"] + C.RESET if here else " "
            if not f.get("revealed"):
                lines.append(" %s %s %s" % (marker,
                                            C.GREY + "FLOOR %-3d" % f["n"] + C.RESET,
                                            C.GREY + "??? unscanned" + C.RESET))
                continue
            state = f.get("state") or "Intact"
            state_col = {"Intact": C.RESET, "Defeated": C.GREEN, "Alerted": C.RED,
                         "Controlled": C.CYAN, "Destroyed": C.GREY,
                         "Rezzed": C.ORANGE}.get(state, C.RESET)
            dv = "DV %s" % f["dv"] if f.get("dv") else "DV --"
            label = ("  " + C.GREY + f["label"] + C.RESET) if f.get("label") else ""
            lines.append(" %s %s %s %s  %s%s" % (
                marker,
                C.GREY + "FLOOR %-3d" % f["n"] + C.RESET,
                pad((C.BOLD if here else "") + f["type"] + C.RESET, 20),
                pad(C.YELLOW + dv + C.RESET, 10),
                state_col + pad(state, 11) + C.RESET,
                label))
        if net.get("more"):
            lines.append("   " + C.GREY + G["corner"] + " deeper floors unknown -- run Pathfinder" + C.RESET)
        return lines

    def feed_pane(self, count):
        state = self.snap() or {}
        entries = (state.get("feed") or [])[-count:]
        lines = [self.ui.rule("FEED")]
        if not entries:
            lines.append(" " + C.GREY + "(quiet)" + C.RESET)
        for e in entries:
            color = {"gm": C.CYAN, "sys": C.GREY, "action": C.YELLOW,
                     "player": C.MAGENTA, "alert": C.RED}.get(e.get("kind"), C.RESET)
            lines.append(" " + C.GREY + e.get("t", "") + C.RESET + " " + color + e.get("text", "") + C.RESET)
        return lines

    # -- the run -----------------------------------------------------------

    def screen_run(self):
        keep = 0
        while True:
            if self.drain_alerts():
                continue
            if not self.connected():
                return
            state = self.snap() or {}
            run = state.get("run")
            if not run:
                return
            net = self.net_by_id(run["net_id"])
            if not net:
                return

            def head():
                s = self.snap() or {}
                r = s.get("run") or run
                n = self.net_by_id(r["net_id"]) or net
                lines = self.ui.banner("RUN " + G["dot"] + " " + n["name"].upper(),
                                       "floor %s of the architecture" % r.get("floor", 1))
                lines.append(" " + self.status_strip())
                lines.append("")
                lines.extend(self.architecture_map(n, r.get("floor", 1)))
                lines.append("")
                return lines

            actions = state.get("actions") or FALLBACK_ACTIONS
            ops = state.get("operations") or FALLBACK_OPS
            items = []
            for a in actions:
                items.append((pad(C.BOLD + C.GREEN + a["name"] + C.RESET, 18) +
                              C.GREY + a["desc"] + C.RESET, ("act", a)))
            items.append(None)
            for o in ops:
                items.append((pad(C.BOLD + o["name"] + C.RESET, 24) +
                              C.GREY + o["desc"] + C.RESET, ("act", o)))
            items += [
                None,
                ("Saved files (%d)" % len((self.snap() or {}).get("files") or []), ("files",)),
                ("Say something to the GM", ("chat",)),
                (C.YELLOW + "JACK OUT" + C.RESET + C.GREY + "  leave the architecture" + C.RESET, ("out",)),
            ]

            choice = self.ui.menu(head, items, index=keep, body=lambda: self.feed_pane(6),
                                  watch=lambda: self.version)
            keep = self.ui.last_index
            if choice is REFRESH:
                continue
            if choice is None:          # esc == the Jack Out entry
                choice = ("out",)
            if not isinstance(choice, tuple):
                continue
            kind = choice[0]
            if kind == "out":
                if self.ui.confirm(head, "Jack out of %s?" % net["name"]):
                    self.conn.send({"type": "leave_run"})
                    return
            elif kind == "files":
                self.screen_files()
            elif kind == "chat":
                text = self.ui.prompt(head, "Say to the GM:", "")
                if text and text.strip():
                    self.conn.send({"type": "chat", "text": text.strip()})
            elif kind == "act":
                self.submit_action(head, net, run, choice[1])

    def submit_action(self, head, net, run, action):
        state = self.snap() or {}
        ch = state.get("character") or {}
        interface = ch.get("interface", 0) or 0
        cur = run.get("floor", 1)

        def act_head():
            lines = self.ui.banner(action["name"].upper(), action.get("check", ""))
            for line in wrap(action.get("desc", ""), 76):
                lines.append(" " + C.GREY + line + C.RESET)
            lines.append("")
            return lines

        targets = [("This floor (%d)" % cur, ("Floor %d" % cur, cur))]
        for f in net.get("floors", []):
            if f["n"] == cur:
                continue
            name = f["type"] if f.get("revealed") else "unscanned"
            targets.append(("Floor %d -- %s" % (f["n"], name),
                            ("Floor %d (%s)" % (f["n"], name), f["n"])))
        targets += [
            ("The architecture itself", (net["name"], None)),
            ("Something else (type it)", ("__custom__", None)),
        ]
        picked = self.ui.menu(act_head(), targets)
        if picked is None:
            return
        target, target_floor = picked
        if target == "__custom__":
            target = self.ui.prompt(act_head(), "Target:", "")
            if target is None:
                return
            target_floor = None

        roll_text = None
        roll_total = None
        wants_roll = "1d10" in (action.get("check") or "")
        if wants_roll:
            mode = self.ui.menu(act_head() + [" " + C.GREY + "target: " + C.RESET + target, ""], [
                (C.GREEN + "Roll it" + C.RESET + C.GREY + "  Interface %d + 1d10" % interface + C.RESET, "roll"),
                ("Send without a roll (GM rolls)", "none"),
                ("Enter a roll I made at the table", "manual"),
            ])
            if mode is None:
                return
            if mode == "roll":
                die, detail = roll_d10()
                total = die + interface
                roll_total = total
                roll_text = "%d  (INT %d + d10 %s)" % (total, interface, detail)
                self.ui.alert(act_head(), [
                    "d10 .......... %s" % detail,
                    "Interface .... %d" % interface,
                    "TOTAL ........ %d" % total,
                ], C.GREEN if total >= 10 else C.YELLOW)
            elif mode == "manual":
                roll_text = self.ui.prompt(act_head(), "Your total:", "")
                if roll_text is None:
                    return
                try:
                    roll_total = int(str(roll_text).strip())
                except ValueError:
                    roll_total = None

        note = self.ui.prompt(act_head() + [" " + C.GREY + "target: " + C.RESET + target, ""],
                              "Anything to tell the GM? (optional)", "")
        if note is None:
            note = ""
        self.conn.send({
            "type": "action",
            "action": action["name"],
            "target": target,
            "target_floor": target_floor,
            "roll": roll_text,
            "total": roll_total,
            "note": note.strip(),
        })
        auto = (self.snap() or {}).get("auto_resolve", True)
        self.ui.alert(act_head(), ["Sent. Watch the feed." if auto
                                   else "Sent to the GM. Watch the feed."], C.CYAN)

    # -- info screens ------------------------------------------------------

    def screen_reference(self, head):
        while True:
            state = self.snap() or {}
            actions = state.get("actions") or FALLBACK_ACTIONS
            ops = state.get("operations") or FALLBACK_OPS
            items = [(pad(C.BOLD + a["name"] + C.RESET, 18) + C.GREY + a["desc"] + C.RESET, a)
                     for a in actions]
            items.append(None)
            items += [(pad(C.BOLD + o["name"] + C.RESET, 24) + C.GREY + o["desc"] + C.RESET, o)
                      for o in ops]
            choice = self.ui.menu(head, items, foot=C.GREY + "NET Actions available to you" + C.RESET)
            if choice is None or choice is REFRESH:
                return
            entry = choice
            body = []
            body.append("Cost   " + entry.get("cost", "--"))
            body.append("Roll   " + entry.get("check", "--"))
            if entry.get("dv"):
                body.append("Vs     " + entry["dv"])
            body.append("")
            for line in wrap(entry.get("desc", ""), 72):
                body.append(line)
            if entry.get("success"):
                body.append("")
                body.append("ON A SUCCESS")
                for line in wrap(entry["success"], 68):
                    body.append("  " + line)
            if entry.get("failure"):
                body.append("")
                body.append("ON A FAILURE")
                for line in wrap(entry["failure"], 68):
                    body.append("  " + line)
            if entry.get("text"):          # your own wording from rules.json
                body.append("")
                body.append("FROM YOUR RULEBOOK")
                for para in str(entry["text"]).split("\n"):
                    for line in wrap(para, 68) if para.strip() else [""]:
                        body.append("  " + line)
            self.ui.alert(self.ui.banner(entry["name"].upper()), body, C.CYAN)

    # -- character sheet (yours to edit, saved between games) --------------

    def sheet_head(self, title="MY CYBERDECK", subtitle=None):
        ch = self.profile["character"]
        if subtitle is None:
            subtitle = (ch.get("handle") or "no handle yet") + "  " + G["dot"] + \
                       "  saved in saves/profile.json"
        lines = self.ui.banner(title, subtitle)
        cond = (self.snap() or {}).get("condition") or {}
        if self.connected():
            hp = cond.get("hp")
            hp_txt = ("%s / %s" % (hp, ch.get("hp_max", "-"))) if hp is not None \
                else "%s / %s" % (ch.get("hp_max", "-"), ch.get("hp_max", "-"))
            line = " " + C.GREY + "current HP " + C.RESET + hp_txt + \
                   C.GREY + "  (tracked by the GM)" + C.RESET
            if cond.get("status"):
                line += "   " + C.RED + cond["status"] + C.RESET
            lines.append(line)
            lines.append(" " + C.GREEN + "changes go straight to the GM" + C.RESET)
        else:
            lines.append(" " + C.GREY + "offline -- edits are saved and sent when you connect"
                         + C.RESET)
        lines.append("")
        return lines

    def screen_character(self, _head=None):
        keep = 0
        while True:
            if self.drain_alerts():
                continue
            ch = self.profile["character"]
            deck = ch["deck"]
            items = [
                ("Handle              %s" % (C.BOLD + (ch.get("handle") or "-") + C.RESET), "handle"),
                ("Role rank           %s" % ch.get("role_rank", 4), "rank"),
                None,
                ("Interface rank      %s" % (C.BOLD + str(ch.get("interface", 0)) + C.RESET)
                 + C.GREY + "   added to your NET Action rolls" + C.RESET, "interface"),
                ("NET Actions / turn  %s" % ch.get("actions_per_turn", 1), "apt"),
                ("Max HP              %s" % ch.get("hp_max", 0), "hp"),
                None,
                ("Cyberdeck           %s" % (deck.get("name") or "-") + C.GREY +
                 "  %d slot(s), %d installed" % (deck.get("slots", 0),
                                                 len(deck.get("hardware") or [])) + C.RESET, "deck"),
                ("Programs            %d loaded" % len(ch.get("programs") or []), "programs"),
                ("Skills              %d listed" % len(ch.get("skills") or []), "skills"),
                None,
                ("Notes               %s" % (C.GREY + (ch.get("notes") or "(none)") + C.RESET), "notes"),
            ]
            choice = self.ui.menu(self.sheet_head, items, index=keep,
                                  watch=lambda: self.version)
            keep = self.ui.last_index
            if choice is REFRESH:
                continue
            if choice is None:
                return
            if choice == "handle":
                self.ask_handle(self.sheet_head)
                continue
            elif choice == "rank":
                ch["role_rank"] = self.ui.prompt_int(self.sheet_head, "Netrunner role rank:",
                                                     ch.get("role_rank", 4), 0, 10)
            elif choice == "interface":
                ch["interface"] = self.ui.prompt_int(self.sheet_head, "Interface rank:",
                                                     ch.get("interface", 0), 0, 10)
            elif choice == "apt":
                ch["actions_per_turn"] = self.ui.prompt_int(
                    self.sheet_head, "NET Actions per turn:", ch.get("actions_per_turn", 1), 1, 9)
            elif choice == "hp":
                ch["hp_max"] = self.ui.prompt_int(self.sheet_head, "Maximum HP:",
                                                  ch.get("hp_max", 25), 1, 200)
            elif choice == "notes":
                v = self.ui.prompt(self.sheet_head, "Notes (the GM can see these):",
                                   ch.get("notes", ""))
                if v is None:
                    continue
                ch["notes"] = v
            elif choice == "deck":
                self.screen_deck()
                continue
            elif choice == "programs":
                self.screen_programs()
                continue
            elif choice == "skills":
                self.screen_skills()
                continue
            self.save_sheet()

    def screen_deck(self):
        keep = 0
        while True:
            deck = self.profile["character"]["deck"]
            hardware = deck.setdefault("hardware", [])
            items = [
                ("Deck name       %s" % (deck.get("name") or "-"), ("name", None)),
                ("Hardware slots  %s" % deck.get("slots", 0), ("slots", None)),
                None,
            ]
            for i, hw in enumerate(hardware):
                items.append((" " + C.CYAN + G["dot"] + " " + C.RESET + hw, ("edit", i)))
            items.append((C.CYAN + "+ Install hardware" + C.RESET, ("add", None)))
            head = lambda: self.sheet_head("CYBERDECK", "hardware bolted to the deck")
            choice = self.ui.menu(head, items, index=keep)
            keep = self.ui.last_index
            if choice is None:
                return
            if not isinstance(choice, tuple):
                continue
            kind, i = choice
            if kind == "name":
                v = self.ui.prompt(head, "Deck name:", deck.get("name", ""))
                if v is None:
                    continue
                deck["name"] = v.strip()
            elif kind == "slots":
                deck["slots"] = self.ui.prompt_int(head, "Hardware slots:", deck.get("slots", 7), 0, 20)
            elif kind == "add":
                v = self.ui.prompt(head, "Hardware (e.g. 'Backup Drive' or 'Range Upgrade'):", "")
                if not v or not v.strip():
                    continue
                hardware.append(v.strip())
            elif kind == "edit":
                v = self.ui.prompt(head, "Edit (blank to remove):", hardware[i])
                if v is None:
                    continue
                if v.strip():
                    hardware[i] = v.strip()
                else:
                    hardware.pop(i)
            self.save_sheet()

    def screen_programs(self):
        keep = 0
        while True:
            programs = self.profile["character"].setdefault("programs", [])
            items = []
            for i, p in enumerate(programs):
                items.append((
                    "%s %s  %s  %s  %s" % (
                        pad(C.BOLD + (p.get("name") or "(unnamed)") + C.RESET, 20),
                        pad(C.CYAN + (p.get("cls") or "-") + C.RESET, 24),
                        C.YELLOW + "ATK %-2s DEF %-2s REZ %-2s" % (
                            p.get("atk", 0), p.get("def", 0), p.get("rez", 0)) + C.RESET,
                        C.GREY + (p.get("effect") or "")[:28] + C.RESET, ""),
                    ("edit", i)))
            if items:
                items.append(None)
            items.append((C.CYAN + "+ Load a program" + C.RESET, ("add", None)))
            head = lambda: self.sheet_head("PROGRAMS", "what you can rez during a run")
            choice = self.ui.menu(head, items, index=keep)
            keep = self.ui.last_index
            if choice is None:
                return
            if not isinstance(choice, tuple):
                continue
            kind, i = choice
            if kind == "add":
                programs.append(new_program())
                self.save_sheet()
                self.screen_program(len(programs) - 1)
            else:
                self.screen_program(i)

    def screen_program(self, index):
        keep = 0
        while True:
            programs = self.profile["character"]["programs"]
            if index >= len(programs):
                return
            p = programs[index]
            head = lambda: self.sheet_head("PROGRAM", p.get("name") or "(unnamed)")
            items = [
                ("Name     %s" % (p.get("name") or "-"), "name"),
                ("Class    %s" % (p.get("cls") or "-"), "cls"),
                ("ATK      %s" % p.get("atk", 0), "atk"),
                ("DEF      %s" % p.get("def", 0), "def"),
                ("REZ      %s" % p.get("rez", 0), "rez"),
                ("Effect   %s" % (C.GREY + (p.get("effect") or "(none)") + C.RESET), "effect"),
                None,
                (C.RED + "Unload this program" + C.RESET, "delete"),
            ]
            choice = self.ui.menu(head, items, index=keep)
            keep = self.ui.last_index
            if choice is None:
                return
            if choice == "name":
                v = self.ui.prompt(head, "Program name:", p.get("name", ""))
                if v is None:
                    continue
                p["name"] = v.strip()
            elif choice == "cls":
                v = self.ui.menu(head, [(c, c) for c in PROGRAM_CLASSES])
                if v:
                    p["cls"] = v
            elif choice in ("atk", "def", "rez"):
                p[choice] = self.ui.prompt_int(head, choice.upper() + ":", p.get(choice, 0), 0, 30)
            elif choice == "effect":
                v = self.ui.prompt(head, "What it does:", p.get("effect", ""))
                if v is None:
                    continue
                p["effect"] = v
            elif choice == "delete":
                if self.ui.confirm(head, "Unload '%s'?" % (p.get("name") or "this program")):
                    programs.pop(index)
                    self.save_sheet()
                    return
                continue
            self.save_sheet()

    def screen_skills(self):
        keep = 0
        while True:
            skills = self.profile["character"].setdefault("skills", [])
            items = []
            for i, s in enumerate(skills):
                level = s.get("level", 0)
                colour = C.GREEN if level >= 4 else C.RESET if level > 0 else C.GREY
                items.append((pad(C.BOLD + s.get("name", "?") + C.RESET, 34)
                              + colour + "level %d" % level + C.RESET, ("edit", i)))
            if items:
                items.append(None)
            items.append((C.CYAN + "+ Add a skill" + C.RESET, ("add", None)))
            head = lambda: self.sheet_head("SKILLS", "your proficiencies")
            choice = self.ui.menu(head, items, index=keep)
            keep = self.ui.last_index
            if choice is None:
                return
            if not isinstance(choice, tuple):
                continue
            kind, i = choice
            if kind == "add":
                name = self.ui.prompt(head, "Skill name:", "")
                if not name or not name.strip():
                    continue
                level = self.ui.prompt_int(head, "Level:", 0, 0, 10)
                skills.append({"name": name.strip(), "level": level or 0})
            else:
                s = skills[i]
                name = self.ui.prompt(head, "Skill name (blank to remove):", s.get("name", ""))
                if name is None:
                    continue
                if not name.strip():
                    skills.pop(i)
                else:
                    s["name"] = name.strip()
                    s["level"] = self.ui.prompt_int(head, "Level:", s.get("level", 0), 0, 10)
            self.save_sheet()

    def screen_files(self):
        """Everything you have pulled out of the NET this session."""
        keep = 0
        while True:
            if self.drain_alerts():
                continue
            files = (self.snap() or {}).get("files") or []
            items = []
            for f in files:
                items.append((
                    "%s %s%s%s" % (
                        pad(C.BOLD + f.get("name", "?") + C.RESET, 34),
                        C.GREY, f.get("source", ""), C.RESET),
                    f.get("id")))
            if not items:
                items.append((C.GREY + "(nothing downloaded yet -- run Download on a File)"
                              + C.RESET, HEADING))

            def head():
                n = len((self.snap() or {}).get("files") or [])
                return self.ui.banner("SAVED FILES",
                                      "%d file(s) on your deck  %s  readable anywhere in the session"
                                      % (n, G["dot"]))

            choice = self.ui.menu(head, items, index=keep, watch=lambda: self.version)
            keep = self.ui.last_index
            if choice is REFRESH:
                continue
            if choice is None:
                return
            target = next((f for f in files if f.get("id") == choice), None)
            if not target:
                continue
            body = [C.GREY + target.get("source", "") + C.RESET, ""]
            for line in (target.get("content") or "(this file is empty)").split("\n"):
                body.extend(wrap(line, 74) if line.strip() else [""])
            self.ui.alert(self.ui.banner(target.get("name", "FILE").upper()), body, C.CYAN)

    def screen_feed(self):
        state = self.snap() or {}
        _, h = self.ui.size()
        lines = self.ui.banner("FEED", "newest last")
        for e in (state.get("feed") or [])[-(h - 10):]:
            color = {"gm": C.CYAN, "sys": C.GREY, "action": C.YELLOW,
                     "player": C.MAGENTA, "alert": C.RED}.get(e.get("kind"), C.RESET)
            lines.append(" " + C.GREY + e.get("t", "") + C.RESET + " " + color + e.get("text", "") + C.RESET)
        lines += ["", self.ui.rule(), " " + C.GREY + "press any key" + C.RESET]
        self.ui.draw(lines)
        while read_key(0.2) is None:
            pass


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Cyberpunk RED netrunner terminal")
    ap.add_argument("--host", help="connect straight to this GM address")
    ap.add_argument("--port", type=int, default=DEFAULT_TCP_PORT)
    ap.add_argument("--ascii", action="store_true", help="ASCII-only box drawing")
    args = ap.parse_args()

    _prepare_console()
    pick_glyphs(args.ascii)
    os.makedirs(SAVE_DIR, exist_ok=True)

    app = Client(args)
    sys.stdout.write("\x1b[2J")
    try:
        with RawInput():
            app.run()
    except KeyboardInterrupt:
        pass
    finally:
        if app.conn:
            app.conn.close()
        app.discovery.running = False
        sys.stdout.write("\x1b[0m\n")
        print("Jacked out.")


if __name__ == "__main__":
    main()
