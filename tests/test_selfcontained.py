"""Proves both programs need nothing but Python.

Guards the promise in RUN_INSTRUCTIONS: no pip, no third-party packages, no
extra files, and either script works alone in an empty folder.

Run with:  python3 tests/test_selfcontained.py
"""
import ast
import importlib.util
import io
import contextlib
import json
import os
import re
import shutil
import sys
import sysconfig
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = {
    "admin/netmanager.py": os.path.join(ROOT, "admin", "netmanager.py"),
    "netrunner/netrunner.py": os.path.join(ROOT, "netrunner", "netrunner.py"),
}
STDLIB = sysconfig.get_paths()["stdlib"]

fails = []
def check(label, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + ((" -> " + str(extra)) if not cond else ""))
    if not cond:
        fails.append(label)


def imported_modules(path):
    """Every top-level module name imported anywhere in the file."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    mods, relative = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.append(node.module or "")
            elif node.module:
                mods.add(node.module.split(".")[0])
    return mods, relative


# ---------- every import is standard library ----------
for name, path in SCRIPTS.items():
    mods, relative = imported_modules(path)
    check("%s: no relative imports (would need a package)" % name, not relative, relative)
    third_party = []
    for mod in sorted(mods):
        if mod == "__future__" or mod in sys.builtin_module_names:
            continue
        try:
            spec = importlib.util.find_spec(mod)
        except Exception:
            spec = None
        if spec is None:
            continue                      # platform-gated stdlib, e.g. msvcrt on Linux
        origin = spec.origin or ""
        if "site-packages" in origin or "dist-packages" in origin:
            third_party.append((mod, origin))
    check("%s: every import is stdlib, nothing from pip" % name, not third_party, third_party)
    check("%s: does not import the other script" % name,
          not any(m in ("netmanager", "netrunner") for m in mods), mods & {"netmanager", "netrunner"})

# ---------- platform-only modules stay behind their guard ----------
for name, path in SCRIPTS.items():
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    win, posix, top = set(), set(), set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.If) and "IS_WIN" in ast.dump(node.test):
            for arm, bucket in ((node.body, win), (node.orelse, posix)):
                for stmt in arm:
                    if isinstance(stmt, ast.Import):
                        bucket.update(a.name.split(".")[0] for a in stmt.names)
    check("%s: msvcrt only on Windows" % name, "msvcrt" in win and "msvcrt" not in top, (win, top))
    check("%s: termios/tty/select only on POSIX" % name,
          {"termios", "tty", "select"} <= posix and not ({"termios", "tty"} & top), (posix, top))
    check("%s: ctypes imported lazily, inside the Windows-only path" % name,
          "ctypes" not in top and "        import ctypes" in src)

# ---------- nothing newer than Python 3.7 ----------
NEWER = [(r"(?<![\w.])list\[", "3.9"), (r"(?<![\w.])dict\[", "3.9"),
         (r"\.removeprefix\(", "3.9"), (r"\.removesuffix\(", "3.9"),
         (r"functools\.cache\b", "3.9"), (r"\bzoneinfo\b", "3.9"),
         (r"sys\.stdlib_module_names", "3.10")]
for name, path in SCRIPTS.items():
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    modern = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.NamedExpr)]
    if hasattr(ast, "Match"):
        modern += [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Match)]
    check("%s: no 3.8+ syntax (walrus, match)" % name, not modern, modern[:3])
    late = [(p, v) for p, v in NEWER if re.search(p, src)]
    check("%s: no 3.9+ library calls" % name, not late, late)

# ---------- each script runs alone in an empty folder ----------
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

work = tempfile.mkdtemp(prefix="selfcontained-")
try:
    gm_dir = os.path.join(work, "gm"); os.makedirs(gm_dir)
    shutil.copy(SCRIPTS["admin/netmanager.py"], gm_dir)
    check("GM folder holds nothing but the one script", os.listdir(gm_dir) == ["netmanager.py"],
          os.listdir(gm_dir))
    A = load("solo_nm", os.path.join(gm_dir, "netmanager.py"))
    A.SAVE_DIR = os.path.join(gm_dir, "saves")
    A.LIBRARY_DIR = os.path.join(gm_dir, "library")
    A.RULES_FILE = os.path.join(gm_dir, "rules.json")
    keys = iter(["enter"] + list("Solo") + ["enter"] + ["end", "enter"] + list("quit") + ["enter"])
    A.read_key = lambda timeout=None: next(keys, "esc")
    app = A.App(7959, False)
    with contextlib.redirect_stdout(io.StringIO()):
        app.run()
    check("GM script runs a whole session with no other files", len(A.list_saves()) == 1,
          A.list_saves())
    check("GM script creates its own saves folder", os.path.isdir(A.SAVE_DIR))

    pl_dir = os.path.join(work, "pl"); os.makedirs(pl_dir)
    shutil.copy(SCRIPTS["netrunner/netrunner.py"], pl_dir)
    check("player folder holds nothing but the one script", os.listdir(pl_dir) == ["netrunner.py"],
          os.listdir(pl_dir))
    N = load("solo_nr", os.path.join(pl_dir, "netrunner.py"))
    N.SAVE_DIR = os.path.join(pl_dir, "saves")
    N.PROFILE = os.path.join(N.SAVE_DIR, "profile.json")
    prof = N.load_profile()
    N.save_profile(prof)
    check("player script writes its profile with no other files",
          os.path.isfile(N.PROFILE) and json.load(open(N.PROFILE))["character"]["programs"])
    check("player script carries its own action catalogue", len(N.FALLBACK_ACTIONS) >= 9,
          len(N.FALLBACK_ACTIONS))

    # ---------- every optional file really is optional ----------
    A.RULES_FILE = os.path.join(work, "no-such-rules.json")
    check("a missing rules.json falls back to the built-ins", len(A.load_rules()[0]) >= 9)
    broken = os.path.join(work, "broken.json")
    open(broken, "w").write("{ not json")
    A.RULES_FILE = broken
    check("a corrupt rules.json is ignored rather than fatal", len(A.load_rules()[0]) >= 9)
    A.LIBRARY_DIR = os.path.join(work, "no-such-library")
    check("a missing library folder is fine", A.list_library() == [])
    A.SAVE_DIR = os.path.join(work, "no-such-saves")
    check("a missing saves folder is fine", A.list_saves() == [])
    N.PROFILE = os.path.join(work, "no-such-profile.json")
    check("a missing profile builds a usable default character",
          len(N.load_profile()["character"]["skills"]) > 0)
    N.PROFILE = broken
    check("a corrupt profile recovers instead of crashing",
          isinstance(N.load_profile().get("character"), dict))
finally:
    shutil.rmtree(work, ignore_errors=True)

print("\n" + ("ALL CHECKS PASSED" if not fails else "FAILURES: " + repr(fails)))
sys.exit(1 if fails else 0)
