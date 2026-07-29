# Cyberpunk RED — Netrunner Terminal

A two-program setup for running NET architectures at the table:

- **`admin/netmanager.py`** — the Game Master console. Build architectures, decide what the player can see, watch their run, resolve NET Actions, block them out. Architectures can be saved to a reusable library and reset between runs.
- **`netrunner/netrunner.py`** — the player's terminal. Finds your session on the local network, jacks in, and updates live as you change things.

Both are single files with no dependencies beyond Python itself.

---

## Requirements

- **Python 3.7 or newer** on every machine. Check with `python3 --version` (Windows: `python --version`).
- Both machines on the **same local network** (same Wi-Fi or same switch).
- Any terminal that handles ANSI colour: Terminal.app, iTerm, GNOME Terminal, Konsole, Windows Terminal, PowerShell, or VS Code's terminal. The old `cmd.exe` works but looks worse — prefer Windows Terminal.

Nothing to install. No `pip`, no virtualenv.

---

## Running it

### Game Master

```bash
cd admin
python3 netmanager.py          # Windows: python netmanager.py
```

You land on the main menu: **Start a new session**, **Continue a session**, or quit.

Once a session is open the console starts listening and announces itself on the local network. The header shows the address the player needs (for example `192.168.1.24:7717`) and whether anyone is connected.

### Player

```bash
cd netrunner
python3 netrunner.py           # Windows: python netrunner.py
```

Open sessions on the network appear in the list within a couple of seconds. Pick one, enter a handle the first time, and you are in. Your handle and last server are remembered.

If the list stays empty (some networks block broadcast traffic), choose **Enter an address manually** and type the address from the GM's header. Or skip the menu entirely:

```bash
python3 netrunner.py --host 192.168.1.24
```

---

## Getting around

Every screen works the same way:

| Key | Does |
| --- | --- |
| `↑` `↓` | Move the highlight |
| **Type any letters** | Filters the list — type `zap` to jump straight to Zap |
| `Enter` | Choose the highlighted entry |
| `Backspace` | Delete a character from the filter or a text field |
| `Esc` | Back out one screen (clears the filter first if you typed one) |
| `Home` / `End` | Jump to the first or last entry |
| `Ctrl+C` | Quit; the session is saved first |

When a list is taller than the window it scrolls, and `▲ n more above` / `▼ n more below` show what's off-screen.

A few screens add a single-key shortcut, always listed along the bottom of the screen — in the architecture list `v` toggles a NET's visibility and `r` resets it; in the NET editor `r` toggles a floor's revealed flag; in the import list `d` removes a library entry.

---

## GM walkthrough

**1. Build an architecture.** Session menu → **NET Architectures** → **Create a new NET**. Give it a name, then add floors from the top down. Each floor has:

- a **Type** — Password, File, Control Node, Black ICE (Hellhound, Killer, Asp, Raven, …), a Demon, or anything you type in
- a **DV** the player rolls against — set this and the program resolves the check for you
- **DEF** and **REZ** for Black ICE and Demons — what a Zap has to beat, and how much damage kills it
- a **Label** the player sees once the floor is revealed
- **GM notes**, which are *never* sent to the player's machine
- a **State** — Intact, Defeated, Alerted, Controlled, Virused, Destroyed, Rezzed

**2. Decide what they can see.** Two independent switches:

- **NET visible** — whether the architecture appears on the player's map at all. Hidden by default.
- **Floor revealed** — whether a specific floor's details are sent. Unrevealed floors below the deepest revealed one are not transmitted, so the player genuinely cannot see how deep the architecture goes. They just get *"deeper floors unknown — run Pathfinder."*

Reveal a floor and it appears on their screen instantly.

**The floor they're standing on is always visible**, whatever its revealed flag says — you see your own feet without scanning. It's revealed on entry and on every move, so Pathfinder does what it should: look *ahead*.

**3. Run the session.** When the player jacks in, **Run control** shows where they are. From there you can move them between floors, reveal the floor they're standing on, or change a floor's state.

**4. Most actions resolve themselves.** You set a DV on each floor, so the program compares the player's roll to it and applies the result without asking you:

| Action | On a success |
| --- | --- |
| Backdoor | floor marked **Defeated** — the way is open |
| Pathfinder | the next unknown floor below is revealed |
| Eye-Dee | the floor's contents are revealed |
| Control | floor marked **Controlled** |
| Virus | floor marked **Virused** |
| Zap | 1d6 off the target's REZ; at 0 it's **Destroyed** and the floor is clear |
| Download | a copy of the File lands on their deck for the rest of the session |

**Movement is automatic too**, and always **one floor at a time**. Move Down just happens, unless something is actually in the way:

- an **intact Password** — they're told to Backdoor it
- **live Black ICE or a Demon** — they're told to kill it or get past it
- the **bottom of the architecture** — there's nothing below

A floor counts as dealt with once it's Defeated, Controlled, Virused or Destroyed. Files, Control Nodes and empty floors never block on their own. Move Up always works, and stops at the entry.

You can still put them anywhere with **Set floor directly** in Run control — that's a GM override and ignores the blocking rules.

Only three things still come to you, because a number comparison can't settle them: **Slide** and **Cloak** (contested against a roll only you know), and anything targeting a floor where you left the DV blank. Those land in **Pending NET Actions** — pick one, choose Success / Failure / Partial, type what happens, and it appears in their feed.

Turn all of it off with **Auto-resolve rolls** on the session menu if you'd rather call every action yourself.

**5. Block them out.** **Run control → BLOCK THEM OUT OF THIS RUN**. Pick a preset (Black ICE flatline, Demon trace, sysop pulls the plug) or write your own. The player gets a full-screen alert and is dumped back to their game menu. You're then asked what happens to the architecture: leave it as they left it, reset it, lock it, or both.

There's also **Disconnect the netrunner entirely** if you want them off the session completely.

---

## Rounds and NET Actions

The netrunner gets a number of NET Actions per round, taken from **NET Actions / turn** on their own sheet. Each action they spend comes off that budget, and when it's gone they can't act again until **you** start the next round. Nothing advances on its own.

**Round N · X of Y NET Actions left** on the session menu opens the turn screen, and **Start the next round** is also on Run control, where you'll actually be mid-fight. Starting a round refills their actions and clears once-per-turn limits.

The same screen lets you fudge it: grant an extra action, spend one on their behalf, set the count directly, refresh the round without advancing the number, or set the round number itself.

**Slide is once per turn.** Even with actions to spare, a second Slide in the same round is refused. That limit is data, not code — `per_turn` in the catalogue, so you can put the same restriction on any action through `rules.json`.

**Movement is free.** Move Down and Move Up cost nothing from the budget, so being out of actions doesn't strand them. Costs are read off each entry's `cost` field, so if your reading of the rules differs, change it in `rules.json` and both sides follow.

The player sees `round 3 · actions 2/3` on their status line, and any action they can't currently take is greyed out with the reason next to it, before they pick it.

---

## Files

A **File** floor can carry actual content the netrunner walks away with.

Set **File contents** in the floor editor. That text is *not* sent with the architecture — the player can see a File is there, and Eye-Dee tells them what it is, but the contents only arrive when they run **Download** on it. That's checked against the floor's DV like anything else; leave the DV blank and the copy just works.

Downloaded files land on their deck and stay there for the whole session. **Saved files** on the player's game menu — and inside a run, so they can read something without jacking out — lists everything they've taken and opens it for reading. Jacking out doesn't lose them, and resetting the architecture doesn't confiscate them.

On your side, **Downloaded files** on the session menu shows everything they hold. From there you can read any of it, press `d` to wipe one off their deck, or **Hand them a file directly** — which drops a file straight onto their deck with no run involved, useful for a fixer's dead drop or an email their crew forwarded.

---

## Resetting an architecture

Reset puts an architecture back the way you built it — every floor **Intact** and **unrevealed**, nothing locked. Your design is untouched: types, DVs, labels and GM notes all survive. Only the netrunner's progress through it is wiped.

Three ways to do it:

- **NET editor → Reset this architecture** — one architecture, right now.
- **Architecture list → `r`** — resets the highlighted one without opening it. **Reset every architecture** in the same list does the lot.
- **NET editor → Auto-reset every time the netrunner jacks in** — the one you probably want for a set piece they'll attempt more than once. With this on, the architecture wipes itself clean the moment they enter, so a second run starts fresh with nothing scanned. The NET editor header shows **AUTO-RESET** while it's active.

Resetting while they're inside is allowed — they're moved back to floor 1 and told the architecture reshaped around them.

---

## The architecture library

Build an architecture once and reuse it in any session.

**NET editor → Save to the architecture library** writes it to `admin/library/<name>.json`. What's stored is a clean template: floor types, DVs, labels, difficulty, description and your GM notes. Run progress and visibility are deliberately not stored, so an imported copy always arrives pristine and hidden.

**Architecture list → Import an architecture** pulls one in. That screen offers two sources:

- everything in your library
- every architecture in your **other saved sessions** — no exporting needed first

Imports are independent copies with fresh IDs. Editing the copy never touches the original, and you're asked to name it — a clash with an existing name gets `(2)` appended rather than silently overwriting. Press `d` in the import list to remove something from the library; the copies already in your sessions are unaffected.

Library files are plain JSON, so you can hand-edit them or share one with another GM by copying the file into their `admin/library/`.

---

## Your character sheet

The netrunner owns their own sheet — the GM never types it in. Open **My cyberdeck**, from the connect screen or the game menu, and set:

- **Interface rank**, which is added to every NET Action roll, plus NET Actions per turn and max HP
- **Programs** — name, class, ATK / DEF / REZ and what it does
- **Skills** — your proficiencies and their levels
- **Cyberdeck** — name, hardware slots, and what's installed

Every change saves to `netrunner/saves/profile.json` the moment you make it, so the same character is waiting next session. You can build it before connecting to anything.

While you're in a session, changes go straight to the GM's screen. The one thing you don't control is **current HP** — the GM tracks damage, so a sheet edit can never quietly undo a hit you took.

---

## Player walkthrough

The game menu lists every architecture the GM has made visible. Pick one to see what you know about it, then **JACK IN**.

Inside a run you get the architecture map, your position in it, and the NET Actions from the rulebook — Pathfinder, Backdoor, Slide, Cloak, Control, Eye-Dee, Virus, Zap — plus movement and program operations. Highlight any of them and press Enter to read the full description.

**Saved files** holds everything you've pulled out of the NET this session, readable from the game menu or from inside a run. Files come from running **Download** on a File floor, or from the GM handing you one.

Most actions resolve the moment you send them — the GM set a DV on each floor, so a passed Backdoor opens it, a passed Pathfinder reveals what's below, and killing ICE with Zap clears the floor. Moving down happens on its own unless something is actually blocking you. Slide and Cloak still wait on the GM, since those are contested.

Taking an action walks you through: pick a target, then choose **Roll it** (the program rolls `Interface + 1d10`, exploding on a 10 and fumbling on a 1), send it without a roll, or type in a roll you made with real dice. Add a note if you want. It goes to the GM, and their ruling comes back in the feed.

**Jack Out** leaves cleanly. Esc does the same thing.

---

## Saving

The GM console saves after every single change — creating a NET, revealing a floor, resolving an action. There is no save button to forget.

- GM sessions: `admin/saves/<session-name>-<id>.json`
- Architecture library: `admin/library/<name>.json`
- Player character sheet: `netrunner/saves/profile.json`
- Your own rules text: `admin/rules.json`

**Continue a session** on the main menu picks up exactly where you stopped: architectures, floor states, character sheet, feed and log all intact. Save files are plain JSON, so you can read or hand-edit them between sessions.

To move a campaign to a different machine, copy the JSON file into that machine's `admin/saves/` folder. To move just one architecture, export it and copy that file into `admin/library/`.

---

## Rules text

Every NET Action entry lists its cost, what you roll, what you roll against, and what success and failure each do. Highlight one and press Enter for the full breakdown.

If you'd rather see your rulebook's own wording, the GM's machine has an `admin/rules.json` written on first run. Fill in the `text` field for any action and it appears under a **FROM YOUR RULEBOOK** heading — on the player's terminal as well as yours, since the GM's copy is what gets sent. You can override `cost`, `check`, `success`, `failure` or `desc` the same way. A missing or malformed file is ignored and the built-in text is used.

---

## Options

```
netmanager.py [--port 7717] [--ascii] [--no-beacon]
netrunner.py  [--host ADDRESS] [--port 7717] [--ascii]
```

| Flag | Why |
| --- | --- |
| `--port` | Use a different port. Both sides must match. |
| `--ascii` | Plain `-` and `+` instead of box-drawing characters, for terminals with bad font coverage. Auto-detected, but you can force it. |
| `--no-beacon` | Stop announcing the session on the network. Players then need `--host` or manual entry. |
| `--host` | Connect straight to an address, skipping the session list. |

---

## If something goes wrong

**Player sees no sessions.** Broadcast traffic is blocked on plenty of networks — guest Wi-Fi and corporate networks especially. Use **Enter an address manually** with the address from the GM's header. If that fails too, check the firewall (below).

**"Could not open port 7717."** Another copy is already running, or the port is taken. Close the other copy, or run both sides with `--port 7800`.

**"Could not reach ADDRESS."** Check that the GM has a session actually open (the listening address only appears once you're inside a session, not on the main menu), that the addresses match, and the firewall.

**Firewall.** The first run on Windows pops a Windows Defender prompt — allow it on **Private networks**. On macOS, System Settings → Network → Firewall → Options → allow incoming connections for Python. On Linux with ufw: `sudo ufw allow 7717/tcp && sudo ufw allow 7718/udp`.

**Garbled boxes or `←[38;5` littering the screen.** Your terminal isn't handling ANSI. Use Windows Terminal instead of `cmd.exe`, or run with `--ascii`.

**Layout looks cramped.** Make the window bigger — 90×30 or larger is comfortable. Resizing mid-game is fine; the next redraw picks it up.

---

## How it works

The GM console listens on **TCP 7717** and broadcasts a small discovery packet on **UDP 7718** roughly once a second. The player's terminal listens for those broadcasts to build the session list, then opens a TCP connection carrying newline-delimited JSON.

Every change on the GM side pushes a fresh state snapshot to the player, which is why their screen updates without them doing anything. That snapshot is **built per-player**: hidden architectures, unrevealed floors and GM notes are filtered out on the server and never reach the player's machine. Reading their network traffic won't spoil anything.

All traffic is unencrypted and unauthenticated — it's designed for a table of friends on a home network, not for the open internet. Don't port-forward it.
