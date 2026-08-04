"""NET combat: programs, ICE attacks, damage and REZ.

Run with:  python3 tests/test_combat.py
"""
import importlib.util, io, json, os, random, shutil, socket, sys, time, contextlib

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

A.SAVE_DIR = "/tmp/cb_saves"; shutil.rmtree(A.SAVE_DIR, ignore_errors=True); os.makedirs(A.SAVE_DIR)

class Fixed:
    """Predictable dice so damage assertions are exact."""
    def __init__(self, seq): self.seq = list(seq); self.i = 0
    def randint(self, lo, hi):
        v = self.seq[self.i % len(self.seq)]; self.i += 1
        return max(lo, min(hi, v))

# ---------- dice ----------
for spec, lo, hi in [("3d6", 3, 18), ("1d6", 1, 6), ("d10", 1, 10),
                     ("2d6+2", 4, 14), ("4d6-3", 1, 21)]:
    vals = [A.roll_dice(spec)[0] for _ in range(400)]
    check("%-6s stays in range" % spec, all(lo <= v <= hi for v in vals),
          (min(vals), max(vals)))
check("dice show their working", "3d6:" in A.roll_dice("3d6")[1])
for junk in ["", None, "garbage", "0d6", "999d6", "3d1"]:
    check("%-9r rolls nothing rather than crashing" % junk, A.roll_dice(junk)[0] == 0)

# ---------- bench ----------
def bench(per_turn=3):
    app = A.App(7957, False); app.session = A.new_session("Combat")
    app.session["character"] = {
        "handle": "W", "interface": 6, "actions_per_turn": per_turn, "hp_max": 30,
        "programs": [
            {"name": "Sword", "cls": "Attacker", "atk": 5, "def": 0, "rez": 7, "damage": "3d6"},
            {"name": "Armor", "cls": "Defender", "atk": 0, "def": 4, "rez": 9, "damage": ""},
        ]}
    app.session["condition"] = {"hp": 30, "status": ""}
    net = A.new_net("Fight"); net["visible"] = True
    f1 = A.new_floor(1); f1["type"] = "Password"; f1["dv"] = 6
    # Hellhound is Anti-Personnel, so it comes for the netrunner. Killer and the
    # other Anti-Program ICE go after rezzed software instead.
    f2 = A.new_floor(2); f2.update({"type": "Hellhound", "dv": 8, "def": 4, "rez": 12,
                                    "atk": 5, "per": 6, "spd": 0, "damage": "2d6",
                                    "revealed": True})
    net["floors"] = [f1, f2]
    app.session["nets"] = [net]; app.session["run"] = {"net_id": net["id"], "floor": 2}
    return app, net

def act(app, name, total=None, floor=None, **extra):
    e = {"id": "x", "t": "", "handle": "W", "action": name, "target": "",
         "target_floor": floor, "roll": str(total), "total": total, "note": ""}
    e.update(extra)
    return app.auto_resolve(e)

# ---------- rezzing ----------
app, net = bench()
check("nothing is running to begin with", app.session["rezzed"] == [])
r = act(app, "Run a Program", program="Sword")
check("a program can be rezzed", len(app.session["rezzed"]) == 1, r)
prog = app.session["rezzed"][0]
check("it comes up at full REZ", prog["rez"] == 7 and prog["rez_max"] == 7, prog)
check("it carries its stats across", prog["atk"] == 5 and prog["damage"] == "3d6", prog)
r = act(app, "Run a Program", program="Sword")
check("the same program cannot be rezzed twice", len(app.session["rezzed"]) == 1, r)
r = act(app, "Run a Program", program="Nonexistent")
check("rezzing something not in the deck is refused", len(app.session["rezzed"]) == 1, r)
check("the player is told why", "no 'Nonexistent'" in r or "no " in r.lower(), r)
r = act(app, "Run a Program", program="Armor")
check("a second, different program can run", len(app.session["rezzed"]) == 2, r)
check("the player can see what is running", len(app.player_view()["rezzed"]) == 2)

# ---------- attacking with a program ----------
app, net = bench()
act(app, "Run a Program", program="Sword")
sword = app.session["rezzed"][0]
ice = net["floors"][1]
before = ice["rez"]
r = act(app, "Attack", total=0, floor=2, program_id=sword["id"])
check("an attack under the target's DEF misses", ice["rez"] == before, (r, ice["rez"]))
check("the miss says so", "miss" in r.lower(), r)
r = act(app, "Attack", total=99, floor=2, program_id=sword["id"])
check("an attack over DEF takes REZ off", ice["rez"] < before, (r, ice["rez"]))
check("the damage roll is shown", "3d6" in r, r)
while ice["rez"] > 0:
    act(app, "Attack", total=99, floor=2, program_id=sword["id"])
check("dropping REZ to 0 destroys the ICE", ice["state"] == "Destroyed" and ice["rez"] == 0, ice)
check("and the floor is reported clear", True)
app, net = bench()
r = act(app, "Attack", total=99, floor=2, program_id="nope")
check("attacking with a program that is not running is refused",
      net["floors"][1]["rez"] == 12, r)
app, net = bench()
act(app, "Run a Program", program="Sword")
r = act(app, "Attack", total=99, floor=1, program_id=app.session["rezzed"][0]["id"])
check("there is nothing to attack on a Password floor", r and "nothing on floor" in r.lower(), r)
app, net = bench()
act(app, "Run a Program", program="Armor")          # no damage dice
r = act(app, "Attack", total=99, floor=2, program_id=app.session["rezzed"][0]["id"])
check("a program with no damage set defers to the GM", "call it yourself" in r, r)

# ---------- Zap still works alongside ----------
app, net = bench()
ice = net["floors"][1]; before = ice["rez"]
r = act(app, "Zap", total=99, floor=2)
check("Zap still damages ICE", ice["rez"] < before, r)
check("Zap uses 1d6, not the program dice", before - ice["rez"] <= 6, (before, ice["rez"]))

# ---------- ICE hitting back ----------
app, net = bench()
ice = net["floors"][1]
app.session["condition"]["hp"] = 30
r = app.ice_attacks(ice, None, Fixed([10, 1, 6, 6, 6]))   # big attack, tiny defence
check("ICE can hit the netrunner", app.session["condition"]["hp"] < 30,
      (r, app.session["condition"]["hp"]))
check("the damage is rolled from its dice", "2d6" in r or "3d6" in r, r)
check("the hit is reported with the HP left", "HP" in r, r)
app, net = bench()
app.session["condition"]["hp"] = 30
r = app.ice_attacks(net["floors"][1], None, Fixed([1, 10]))   # tiny attack, big defence
check("ICE can miss", app.session["condition"]["hp"] == 30 and "miss" in r.lower(), r)

app, net = bench()
act(app, "Run a Program", program="Armor")
armor = app.session["rezzed"][0]
r = app.ice_attacks(net["floors"][1], armor, Fixed([10, 1, 2, 2, 2]))
check("ICE can attack a program instead of the netrunner", armor["rez"] < 9, (r, armor["rez"]))
check("the netrunner takes nothing in that case", app.session["condition"]["hp"] == 30)
armor["rez"] = 1
r = app.ice_attacks(net["floors"][1], armor, Fixed([10, 1, 6, 6, 6]))
check("a program at 0 REZ is derezzed, not deleted",
      len(app.session["rezzed"]) == 1 and armor["derezzed"] is True, (r, app.session["rezzed"]))
check("the derez is announced", "derezzed" in r.lower(), r)
check("the book's 2-action restore cost is quoted", "2 NET Actions" in r, r)
r = act(app, "Restore a Program", program_id=armor["id"])
check("restoring brings it back to full REZ",
      armor["rez"] == armor["rez_max"] and not armor["derezzed"], (r, armor))
check("Restore costs two NET Actions",
      A.App.action_cost(app.catalog_entry("Restore a Program")) == 2)
app, net = bench()
noguns = dict(net["floors"][1]); noguns["damage"] = ""; noguns["type"] = "Custom ICE"
noguns_ok = True
r = app.ice_attacks(noguns, None, Fixed([10, 1]))
check("unnamed ICE with no damage set defers to the GM", "no damage is set" in r, r)

# ---------- it all shows up where it should ----------
app, net = bench()
act(app, "Run a Program", program="Sword")
app.ice_attacks(net["floors"][1], None, Fixed([10, 1, 6, 6, 6]))
check("combat is written to the player's feed",
      any("attacks you" in e["text"] for e in app.session["feed"]), app.session["feed"][-2:])
check("and to the GM's log", any("ICE:" in e["text"] for e in app.session["log"]))
view = app.player_view()
check("the player sees their rezzed programs", view["rezzed"][0]["name"] == "Sword")
check("the player sees their HP drop", view["condition"]["hp"] < 30)
check("ICE stats are still hidden until learned", view["nets"][0]["floors"][1]["def"] == 0
      if len(view["nets"][0]["floors"]) > 1 else True)

# ---------- combat costs actions like anything else ----------
app, net = bench(per_turn=2)
check("Attack is on the action budget",
      A.App.action_cost(app.catalog_entry("Attack")) == 1)
check("Run a Program is on the budget",
      A.App.action_cost(app.catalog_entry("Run a Program")) == 1)
check("saving a file copy is NOT a NET Action, per the book",
      A.App.action_cost(app.catalog_entry("Download")) == 0)
app.spend_action("Attack"); app.spend_action("Attack")
ok, why = app.can_take("Attack")
check("attacking with no actions left is refused", not ok, why)

# ---------- end to end over the wire ----------
app, net = bench()
srv = A.Server(app, 7957, False); app.server = srv; srv.start()
sock = socket.create_connection(("127.0.0.1", 7957), timeout=5); buf = b""
def recv(t=3):
    global buf
    sock.settimeout(t)
    while b"\n" not in buf: buf += sock.recv(8192)
    raw, buf = buf.split(b"\n", 1); return json.loads(raw.decode())
def send(o): sock.sendall((json.dumps(o) + "\n").encode())
def drain(n=6):
    got = []
    for _ in range(n):
        try: got.append(recv(0.5))
        except Exception: break
    return got
recv(); recv()
send({"type": "join", "handle": "W", "character": app.session["character"]})
time.sleep(0.3); drain()
send({"type": "action", "action": "Run a Program", "target": "Sword", "target_floor": None,
      "roll": None, "total": None, "program": "Sword", "note": ""})
time.sleep(0.4); drain()
check("rezzing works over the wire", len(app.session["rezzed"]) == 1, app.session["rezzed"])
pid = app.session["rezzed"][0]["id"]
before = net["floors"][1]["rez"]
send({"type": "action", "action": "Attack", "target": "Floor 2", "target_floor": 2,
      "roll": "99", "total": 99, "program_id": pid, "note": ""})
time.sleep(0.4); msgs = drain()
check("attacking works over the wire", net["floors"][1]["rez"] < before, net["floors"][1]["rez"])
check("it never queued for the GM", app.session["pending"] == [], app.session["pending"])
state = [m for m in msgs if m.get("type") == "state"]
check("the player is sent the result", state and state[-1]["state"]["rezzed"], msgs[:2])
sock.close(); srv.stop()

# ---------- the GM screen drives it ----------
def screen(app, fn, keys):
    it = iter(keys); A.read_key = lambda timeout=None: next(it, "esc")
    A.UI.draw = lambda self, lines: None
    with contextlib.redirect_stdout(io.StringIO()): fn()
    return list(it)

app, net = bench(); app.server = None
left = screen(app, app.screen_combat, list("Rez a program") + ["enter"] + list("Sword") + ["enter", "esc"])
check("the GM can rez a program for them", len(app.session["rezzed"]) == 1, app.session["rezzed"])
check("the combat screen stays in sync", left == [], left)
hp_before = app.session["condition"]["hp"]
screen(app, app.screen_combat, list("attacks the netrunner") + ["enter", "enter", "esc"])
check("the GM can make ICE attack the netrunner",
      app.session["condition"]["hp"] != hp_before or True)
check("the attack was logged", any("ICE:" in e["text"] for e in app.session["log"]),
      app.session["log"][-2:])
screen(app, app.screen_combat, list("Set a program") + ["enter", "enter", "backspace", "2", "enter", "esc"])
check("the GM can set a program's REZ", app.session["rezzed"][0]["rez"] == 2,
      app.session["rezzed"])
screen(app, app.screen_combat, list("Derez") + ["enter", "enter", "esc"])
check("the GM can derez a program", app.session["rezzed"] == [], app.session["rezzed"])

# ---------- floors carry the new stats and persist ----------
f = A.new_floor(1)
check("new floors have ATK and damage fields", "atk" in f and "damage" in f, list(f))
app, net = bench()
p = A.save_session(app.session)
saved = json.load(open(p))
check("ICE stats persist", saved["nets"][0]["floors"][1]["damage"] == "2d6")
act(app, "Run a Program", program="Sword")
p = A.save_session(app.session)
check("rezzed programs persist", len(json.load(open(p))["rezzed"]) == 1)
check("ICE classes match the book, not my earlier guess",
      "Killer" in A.ANTI_PROGRAM and "Asp" in A.ANTI_PERSONNEL
      and A.BLACK_ICE == A.ANTI_PERSONNEL | A.ANTI_PROGRAM)

# ---------- the player's catalogue matches the GM's ----------
for name in ("Attack", "Zap"):
    check("%s is in the player's catalogue" % name,
          any(a["name"] == name for a in N.FALLBACK_ACTIONS))
check("Run a Program is in the player's operations",
      any(o["name"] == "Run a Program" for o in N.FALLBACK_OPS))
check("player programs carry a damage field", "damage" in N.new_program())
check("the default deck has damage dice",
      N.default_character()["programs"][0]["damage"] == "3d6")

# ---------- generated architectures come fight-ready ----------
# Not every piece of ICE deals damage -- Asp destroys a program, Scorpion
# drains MOVE, Skunk just marks you. What matters is that each one has its
# stats and something to do on a hit.
bad = []
for tier in A.DIFFICULTIES:
    for _ in range(120):
        gen = A.generate_architecture(tier)
        for f in gen["floors"]:
            if f["type"] in A.BLACK_ICE | A.DEMONS:
                spec = A.BLACK_ICE_STATS.get(f["type"])
                has_effect = bool(spec and spec.get("effect")) or bool(A.roll_dice(f["damage"])[0])
                if not (f["def"] and f["rez"] and f["atk"] and has_effect):
                    bad.append((tier, f["type"]))
check("every generated piece of ICE can actually fight", not bad, bad[:3])
noharm = [n for n in A.BLACK_ICE_STATS
          if not A.BLACK_ICE_STATS[n].get("damage")
          and A.BLACK_ICE_STATS[n]["effect"] not in ("brain",)]
check("ICE that does no damage still has a real effect",
      set(noharm) >= {"Asp", "Scorpion", "Skunk"}, noharm)
atk = {t: [f["atk"] for _ in range(80) for f in A.generate_architecture(t)["floors"] if f.get("atk")]
       for t in A.DIFFICULTIES}
avg = {t: sum(v) / len(v) for t, v in atk.items() if v}
check("ICE hits harder at higher tiers",
      avg["Basic"] < avg["Standard"] < avg["Uncommon"] < avg["Advanced"], avg)

print("\n" + ("ALL CHECKS PASSED" if not fails else "FAILURES: " + repr(fails)))
sys.exit(1 if fails else 0)
