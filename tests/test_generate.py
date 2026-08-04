"""The architecture generator: structure rules, scaling, and GM approval.

Run with:  python3 tests/test_generate.py
"""
import importlib.util, io, json, os, shutil, sys, contextlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
A = load("nm", os.path.join(ROOT, "admin", "netmanager.py"))

fails = []
def check(label, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + ((" -> " + str(extra)) if not cond else ""))
    if not cond: fails.append(label)

A.SAVE_DIR = "/tmp/gn_saves"; shutil.rmtree(A.SAVE_DIR, ignore_errors=True); os.makedirs(A.SAVE_DIR)
TIERS = A.DIFFICULTIES
OBSTACLE = lambda t: t not in A.REWARD_TYPES
SAMPLE = {t: [A.generate_architecture(t) for _ in range(300)] for t in TIERS}

# ---------- sane output at every tier ----------
for t in TIERS:
    nets = SAMPLE[t]; lo, hi = A.GEN_TIERS[t]["floors"]
    check("%s: floor count stays in range" % t,
          all(lo <= len(n["floors"]) <= hi for n in nets),
          sorted({len(n["floors"]) for n in nets}))
    check("%s: every floor has a type and a DV" % t,
          all(f["type"] and f["dv"] for n in nets for f in n["floors"]))
    check("%s: the tier is recorded" % t, all(n["difficulty"] == t for n in nets))
    check("%s: arrives hidden and unlocked" % t,
          all(not n["visible"] and not n["locked"] for n in nets))
    check("%s: floors numbered from 1" % t,
          all([f["n"] for f in n["floors"]] == list(range(1, len(n["floors"]) + 1)) for n in nets))

# ---------- structure: valuables sit behind obstacles ----------
for t in TIERS:
    nets = SAMPLE[t]; bad = []
    for n in nets:
        kinds = [f["type"] for f in n["floors"]]
        for i, k in enumerate(kinds):
            if k in A.REWARD_TYPES and (i == 0 or not OBSTACLE(kinds[i - 1])):
                bad.append(kinds); break
    check("%s: every File/Control Node sits directly below an obstacle" % t, not bad, bad[:2])
    check("%s: the way in is always locked" % t,
          all(n["floors"][0]["type"] == "Password" for n in nets))
    check("%s: the bottom floor is always worth taking" % t,
          all(n["floors"][-1]["type"] in A.REWARD_TYPES for n in nets))
    check("%s: no two valuables are adjacent" % t,
          all(not (a["type"] in A.REWARD_TYPES and b["type"] in A.REWARD_TYPES)
              for n in nets for a, b in zip(n["floors"], n["floors"][1:])))
    check("%s: every architecture has something to find" % t,
          all(any(f["type"] in A.REWARD_TYPES for f in n["floors"]) for n in nets))

# ---------- difficulty grows with depth ----------
for t in TIERS:
    nets = SAMPLE[t]
    check("%s: DVs never drop as you descend" % t,
          all(all(a["dv"] <= b["dv"] for a, b in zip(n["floors"], n["floors"][1:])) for n in nets))
    deep = [n for n in nets if len(n["floors"]) >= 4]
    tops = [n["floors"][0]["dv"] for n in deep]; bots = [n["floors"][-1]["dv"] for n in deep]
    check("%s: the bottom is harder than the top on average" % t,
          sum(bots) / len(bots) > sum(tops) / len(tops),
          (sum(tops) / len(tops), sum(bots) / len(bots)))
    ice = [[f for f in n["floors"] if f.get("rez")] for n in nets]
    ice = [l for l in ice if len(l) >= 2]
    check("%s: ICE gets tougher deeper in" % t,
          all(all(a["rez"] <= b["rez"] for a, b in zip(l, l[1:])) for l in ice))

# ---------- difficulty grows with tier ----------
mean = lambda v: sum(v) / len(v) if v else 0
avg_dv = {t: mean([f["dv"] for n in SAMPLE[t] for f in n["floors"]]) for t in TIERS}
check("average DV climbs with every tier",
      avg_dv["Basic"] < avg_dv["Standard"] < avg_dv["Uncommon"] < avg_dv["Advanced"], avg_dv)
avg_floors = {t: mean([len(n["floors"]) for n in SAMPLE[t]]) for t in TIERS}
check("architectures get deeper with every tier",
      avg_floors["Basic"] < avg_floors["Standard"] < avg_floors["Uncommon"] < avg_floors["Advanced"],
      avg_floors)
avg_rez = {t: mean([f["rez"] for n in SAMPLE[t] for f in n["floors"] if f.get("rez")]) for t in TIERS}
check("ICE gets tougher with every tier",
      avg_rez["Basic"] < avg_rez["Standard"] < avg_rez["Uncommon"] < avg_rez["Advanced"], avg_rez)
ice_share = {t: mean([1.0 if f["type"] in A.BLACK_ICE | A.DEMONS else 0.0
                      for n in SAMPLE[t] for f in n["floors"]]) for t in TIERS}
check("higher tiers are more ICE and fewer plain locks",
      ice_share["Basic"] < ice_share["Advanced"], ice_share)
check("Basic never fields a Demon",
      not any(f["type"] in A.DEMONS for n in SAMPLE["Basic"] for f in n["floors"]))
check("Advanced never fields the weakest ICE",
      not any(f["type"] in ("Wisp", "Skunk") for n in SAMPLE["Advanced"] for f in n["floors"]))

# ---------- the details are filled in ----------
for t in TIERS:
    nets = SAMPLE[t]
    files = [f for n in nets for f in n["floors"] if f["type"] == "File"]
    check("%s: generated Files are named" % t, all(f["label"] for f in files))
    check("%s: generated Files have something to read" % t, all(f["content"] for f in files))
    nodes = [f for n in nets for f in n["floors"] if f["type"] == "Control Node"]
    check("%s: generated Control Nodes say what they run" % t,
          all("wired to" in f["label"] for f in nodes))
    ice = [f for n in nets for f in n["floors"] if f["type"] in A.BLACK_ICE | A.DEMONS]
    check("%s: generated ICE has DEF and REZ" % t, all(f["def"] and f["rez"] for f in ice))
    check("%s: Passwords carry no ICE stats" % t,
          all(not f.get("rez") for n in nets for f in n["floors"] if f["type"] == "Password"))
    check("%s: nothing starts revealed to the player" % t,
          not any(f["revealed"] or f.get("dv_known") for n in nets for f in n["floors"]))
    check("%s: file names are not reused inside one architecture" % t,
          all(len({f["label"] for f in n["floors"] if f["type"] == "File"})
              == len([f for f in n["floors"] if f["type"] == "File"]) for n in nets))
check("names vary between rolls", len({n["name"] for n in SAMPLE["Standard"]}) > 20)
check("the same tier does not produce the same layout twice",
      len({tuple(f["type"] for f in n["floors"]) for n in SAMPLE["Standard"]}) > 50)
check("no ICE type repeats back to back",
      not any(a["type"] == b["type"] and a["type"] in A.BLACK_ICE
              for n in SAMPLE["Advanced"] for a, b in zip(n["floors"], n["floors"][1:])))

# ---------- nothing is added until the GM says so ----------
def screen(app, fn, keys):
    it = iter(keys); A.read_key = lambda timeout=None: next(it, "esc")
    A.UI.draw = lambda self, lines: None
    with contextlib.redirect_stdout(io.StringIO()):
        result = fn()
    return result, list(it)

app = A.App(7965, False); app.session = A.new_session("Gen"); app.server = None
made, _ = screen(app, app.screen_generate, ["esc"])
check("backing out adds nothing", made is None and app.session["nets"] == [])
made, _ = screen(app, app.screen_generate, list("Discard and go back") + ["enter"])
check("'discard' adds nothing", made is None and app.session["nets"] == [])
screen(app, app.screen_generate, list("Roll another") + ["enter", "esc"])
check("rolling again still adds nothing", app.session["nets"] == [])
made, _ = screen(app, app.screen_generate, list("Use this one") + ["enter", "enter", "enter"])
check("accepting adds exactly one architecture", len(app.session["nets"]) == 1)
check("the accepted one is returned", made and made["name"] == app.session["nets"][0]["name"])
check("it is hidden until the GM makes it visible", app.session["nets"][0]["visible"] is False)
check("it is logged", any("Generated" in e["text"] for e in app.session["log"]))
check("it survives to disk", len(json.load(open(A.save_session(app.session)))["nets"]) == 1)

screen(app, app.screen_generate,
       list("Change difficulty") + ["enter"] + list("Advanced") + ["enter"]
       + list("Use this one") + ["enter", "enter", "enter"])
check("the GM can pick the tier before accepting",
      app.session["nets"][-1]["difficulty"] == "Advanced")
check("the chosen tier really is deeper",
      len(app.session["nets"][-1]["floors"]) >= A.GEN_TIERS["Advanced"]["floors"][0])
screen(app, app.screen_generate, list("Use this one") + ["enter"]
       + ["backspace"] * 40 + list("My Own Name") + ["enter", "enter"])
check("the GM can rename it on the way in", app.session["nets"][-1]["name"] == "My Own Name")
before = len(app.session["nets"])
screen(app, app.screen_generate, list("Use this one") + ["enter"]
       + ["backspace"] * 40 + list("My Own Name") + ["enter", "enter"])
check("a clashing name is disambiguated, not overwritten",
      app.session["nets"][-1]["name"] == "My Own Name (2)" and len(app.session["nets"]) == before + 1,
      app.session["nets"][-1]["name"])

# ---------- the preview shows the GM everything ----------
app2 = A.App(7965, False); app2.session = A.new_session("Preview")
net = A.generate_architecture("Uncommon")
text = A._ANSI_RE.sub("", "\n".join(app2.preview_lines(net)))
check("the preview lists every floor", all(f["type"] in text for f in net["floors"]))
check("the preview shows DVs", "DV" in text)
check("the preview shows ICE stats when there is ICE",
      ("REZ" in text) == any(f.get("rez") for f in net["floors"]))

# ---------- generated nets behave like hand-built ones ----------
app3 = A.App(7965, False); app3.session = A.new_session("Use")
gen = A.generate_architecture("Standard"); gen["visible"] = True
app3.session["nets"] = [gen]; app3.session["run"] = {"net_id": gen["id"], "floor": 1}
app3.reveal_current_floor()
view = app3.player_view()
check("a generated architecture is playable", view["nets"][0]["floors"][0]["revealed"] is True)
check("its DVs are still hidden from the player", view["nets"][0]["floors"][0]["dv"] is None)
check("it exports to the library like any other",
      A.net_template(gen)["floors"][0]["type"] == "Password")
A.reset_net(gen)
check("resetting it works", not any(f["revealed"] for f in gen["floors"]))

print("\n" + ("ALL CHECKS PASSED" if not fails else "FAILURES: " + repr(fails)))
sys.exit(1 if fails else 0)
