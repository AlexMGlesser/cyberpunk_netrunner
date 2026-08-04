"""Checks the mechanics against how the Cyberpunk RED rules actually work.

Each check names the rule it is enforcing. Run with:
    python3 tests/test_rulebook.py
"""
import importlib.util, os, random, shutil, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def load(n, p):
    s = importlib.util.spec_from_file_location(n, p)
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
A = load("nm", os.path.join(ROOT, "admin", "netmanager.py"))
N = load("nr", os.path.join(ROOT, "netrunner", "netrunner.py"))

fails = []
def check(l, c, e=""):
    print(("  OK   " if c else "  FAIL ") + l + ((" -> " + str(e)) if not c else ""))
    if not c: fails.append(l)

A.SAVE_DIR = "/tmp/rb_saves"; shutil.rmtree(A.SAVE_DIR, ignore_errors=True); os.makedirs(A.SAVE_DIR)

def bench(dvs=(6, 9, 12, 15)):
    app = A.App(7953, False); app.session = A.new_session("Rules")
    app.session["character"] = {"handle": "W", "interface": 7, "actions_per_turn": 3,
                                "hp_max": 30, "programs": [
        {"name": "Sword", "cls": "Attacker", "atk": 3, "def": 0, "rez": 7, "damage": "3d6"}]}
    app.session["condition"] = {"hp": 30, "status": ""}
    net = A.new_net("Book"); net["visible"] = True
    for i, dv in enumerate(dvs):
        f = A.new_floor(i + 1); f["dv"] = dv
        f["type"] = "Password" if i == 0 else ("Killer" if i == 2 else "File")
        if f["type"] == "Killer":
            f.update({"def": 2, "rez": 12, "atk": 5, "per": 6, "spd": 0, "damage": "3d6"})
        net["floors"].append(f)
    net["floors"][0]["revealed"] = True
    app.session["nets"] = [net]; app.session["run"] = {"net_id": net["id"], "floor": 1}
    return app, net

def act(app, name, total=None, floor=None, **kw):
    e = {"id": "x", "t": "", "handle": "W", "action": name, "target": "",
         "target_floor": floor, "roll": str(total), "total": total, "note": ""}
    e.update(kw); return app.auto_resolve(e)

# --- Interface Abilities are Interface + 1d10 vs DV ------------------------
for name in ("Backdoor", "Control", "Eye-Dee", "Virus"):
    entry = [a for a in A.NET_ACTIONS if a["name"] == name][0]
    check("%s rolls Interface + 1d10 vs a DV" % name,
          "Interface + 1d10" in entry["check"] and "DV" in entry["check"], entry["check"])

# --- Pathfinder reads down until a floor's DV beats the check --------------
app, net = bench(dvs=(6, 9, 12, 15))
r = act(app, "Pathfinder", total=10, floor=1)
seen = [f["n"] for f in net["floors"] if f["revealed"]]
check("Pathfinder reveals floors whose DV it beats", 2 in seen, (r, seen))
check("Pathfinder stops at the first floor it cannot read", 3 not in seen and 4 not in seen,
      (r, seen))
check("it says which floor blocked the read", "beyond your read" in r, r)
app, net = bench(dvs=(6, 20, 20, 20))
r = act(app, "Pathfinder", total=10, floor=1)
check("a roll under the next floor's DV reveals nothing",
      [f["n"] for f in net["floors"] if f["revealed"]] == [1], r)
app, net = bench(dvs=(6, 2, 3, 4))
act(app, "Pathfinder", total=30, floor=1)
check("a big roll reads the whole way down",
      all(f["revealed"] for f in net["floors"]), [f["revealed"] for f in net["floors"]])
app, net = bench()
act(app, "Pathfinder", total=30, floor=1)
check("Pathfinder never reveals a DV -- 'but not the DV of anything'",
      not any(f.get("dv_known") for f in net["floors"]),
      [f.get("dv_known") for f in net["floors"]])
view = app.player_view()
check("and the player is sent no DVs from a scan",
      all(f["dv"] is None for f in view["nets"][0]["floors"]), view["nets"][0]["floors"])

# --- Zap: Interface + 1d10 vs DEF + 1d10, 1d6 damage ----------------------
entry = [a for a in A.NET_ACTIONS if a["name"] == "Zap"][0]
check("Zap is contested against DEF + 1d10", "DEF + 1d10" in entry["check"], entry["check"])
app, net = bench(); ice = net["floors"][2]
before = ice["rez"]
random.seed(1)
act(app, "Zap", total=99, floor=3)
check("Zap does 1d6 to REZ", 1 <= before - ice["rez"] <= 6, (before, ice["rez"]))

# --- Attack: Interface + Program ATK + 1d10 vs DEF + 1d10 -----------------
entry = [a for a in A.NET_ACTIONS if a["name"] == "Attack"][0]
check("Attack adds Interface AND the program's ATK",
      "Interface + Program ATK + 1d10" in entry["check"], entry["check"])
check("Attack is contested against DEF + 1d10", "DEF + 1d10" in entry["check"], entry["check"])
app, net = bench(); act(app, "Run a Program", program="Sword")
sword = app.session["rezzed"][0]; ice = net["floors"][2]; before = ice["rez"]
act(app, "Attack", total=99, floor=3, program_id=sword["id"])
check("a program hits for its own damage dice, not 1d6", before - ice["rez"] >= 3,
      (before, ice["rez"]))
lo = act(app, "Attack", total=0, floor=3, program_id=sword["id"])
check("an attack that loses the contest misses", "miss" in lo.lower(), lo)

# --- Derezzed is not destroyed; restoring costs 2 NET Actions -------------
app, net = bench(); act(app, "Run a Program", program="Sword")
prog = app.session["rezzed"][0]; prog["rez"] = 1
app.ice_attacks(net["floors"][2], prog, type("F", (), {"randint": staticmethod(lambda a, b: b)})())
check("a program at 0 REZ stays on the deck", len(app.session["rezzed"]) == 1)
check("it is marked derezzed rather than deleted", prog["derezzed"] is True, prog)
check("a derezzed program cannot attack",
      "derezzed" in act(app, "Attack", total=99, floor=3, program_id=prog["id"]).lower())
check("restoring costs 2 NET Actions",
      A.App.action_cost(app.catalog_entry("Restore a Program")) == 2)
act(app, "Restore a Program", program_id=prog["id"])
check("restoring returns it to full REZ", prog["rez"] == prog["rez_max"] and not prog["derezzed"])

# --- Slide: Interface + 1d10 vs the ICE's PER, once per turn, no Demons ---
entry = [a for a in A.NET_ACTIONS if a["name"] == "Slide"][0]
check("Slide is contested against PER + 1d10", "PER + 1d10" in entry["check"], entry["check"])
check("Slide is once per turn", entry.get("per_turn") == 1)
app, net = bench(); app.session["run"]["floor"] = 3
r = act(app, "Slide", total=99, floor=3)
check("a successful Slide breaks away", "break away" in r.lower(), r)
check("and pulls back a floor", app.session["run"]["floor"] == 2, app.session["run"])
app, net = bench(); app.session["run"]["floor"] = 3
r = act(app, "Slide", total=0, floor=3)
check("a failed Slide leaves you where you are",
      app.session["run"]["floor"] == 3 and "stays on you" in r.lower(), r)
app, net = bench()
net["floors"][2]["type"] = "Balron"
app.session["run"]["floor"] = 3
r = act(app, "Slide", total=99, floor=3)
check("you cannot Slide away from a Demon", "cannot slide away from a demon" in r.lower(), r)

# --- Moving and saving a file copy are not NET Actions --------------------
app, _ = bench()
check("moving costs no NET Action",
      A.App.action_cost(app.catalog_entry("Move Down a Floor")) == 0)
check("saving a file copy costs no NET Action",
      A.App.action_cost(app.catalog_entry("Download")) == 0)
check("running a program costs one",
      A.App.action_cost(app.catalog_entry("Run a Program")) == 1)

# --- Black ICE carries all five stats ------------------------------------
f = A.new_floor(1)
for stat in ("per", "spd", "atk", "def", "rez"):
    check("floors carry %s" % stat.upper(), stat in f, list(f))
gen = [A.generate_architecture("Advanced") for _ in range(60)]
ice = [f for n in gen for f in n["floors"] if f["type"] in A.BLACK_ICE | A.DEMONS]
check("generated ICE gets PER and SPD too", all(f["per"] and f["spd"] for f in ice))

# --- walking into ICE gives it a free swing if it is faster ---------------
app, net = bench()
net["floors"][1]["type"] = "Killer"
net["floors"][1].update({"def": 2, "rez": 12, "atk": 99, "per": 6, "spd": 99, "damage": "3d6"})
net["floors"][0]["state"] = "Defeated"
hp_before = app.session["condition"]["hp"]
act(app, "Move Down a Floor")
check("very fast ICE gets its hit in as you arrive",
      app.session["condition"]["hp"] < hp_before, app.session["condition"])
app, net = bench()
net["floors"][1].update({"type": "Killer", "def": 2, "rez": 12, "atk": 5,
                         "per": 6, "spd": 0, "damage": "3d6"})
net["floors"][0]["state"] = "Defeated"
hp_before = app.session["condition"]["hp"]
act(app, "Move Down a Floor")
check("ICE with no SPD recorded does not ambush", app.session["condition"]["hp"] == hp_before)

# --- the player's copy matches ------------------------------------------
for name in ("Attack", "Zap", "Slide", "Pathfinder"):
    gm = [a for a in A.NET_ACTIONS if a["name"] == name][0]
    pl = [a for a in N.FALLBACK_ACTIONS if a["name"] == name][0]
    check("%s reads the same on both sides" % name, gm["check"] == pl["check"], (gm["check"], pl["check"]))

print("\n" + ("ALL CHECKS PASSED" if not fails else "FAILURES: " + repr(fails)))
sys.exit(1 if fails else 0)
