# Cyberpunk RED — Netrunner Terminal

A two-program setup for running NET architectures at the table:

- **`admin/netmanager.py`** — the Game Master console. Build architectures, decide what the player can see, watch their run, resolve NET Actions, block them out.
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

A few screens add a single-key shortcut, always listed along the bottom of the screen — `v` toggles a NET's visibility in the architecture list, `r` toggles a floor's revealed flag in the NET editor.

---

## GM walkthrough

**1. Build an architecture.** Session menu → **NET Architectures** → **Create a new NET**. Give it a name, then add floors from the top down. Each floor has:

- a **Type** — Password, File, Control Node, Black ICE (Hellhound, Killer, Asp, Raven, …), a Demon, or anything you type in
- a **DV** the player rolls against
- a **Label** the player sees once the floor is revealed
- **GM notes**, which are *never* sent to the player's machine
- a **State** — Intact, Defeated, Alerted, Controlled, Destroyed, Rezzed

**2. Decide what they can see.** Two independent switches:

- **NET visible** — whether the architecture appears on the player's map at all. Hidden by default.
- **Floor revealed** — whether a specific floor's details are sent. Unrevealed floors below the deepest revealed one are not transmitted, so the player genuinely cannot see how deep the architecture goes. They just get *"deeper floors unknown — run Pathfinder."*

Reveal a floor when a Pathfinder succeeds and it appears on their screen instantly.

**3. Run the session.** When the player jacks in, **Run control** shows where they are. From there you can move them between floors, reveal the floor they're standing on, or change a floor's state.

**4. Resolve their actions.** Every NET Action they take lands in **Pending NET Actions** with the roll they made. Pick it, choose Success / Failure / Partial, type what happens, and it appears in their feed.

**5. Block them out.** **Run control → BLOCK THEM OUT OF THIS RUN**. Pick a preset (Black ICE flatline, Demon trace, sysop pulls the plug) or write your own. The player gets a full-screen alert and is dumped back to their game menu. You'll be asked whether to also lock the architecture so they can't walk straight back in.

There's also **Disconnect the netrunner entirely** if you want them off the session completely.

---

## Player walkthrough

The game menu lists every architecture the GM has made visible. Pick one to see what you know about it, then **JACK IN**.

Inside a run you get the architecture map, your position in it, and the NET Actions from the rulebook — Pathfinder, Backdoor, Slide, Cloak, Control, Eye-Dee, Virus, Zap — plus movement and program operations. Highlight any of them and press Enter to read the full description.

Taking an action walks you through: pick a target, then choose **Roll it** (the program rolls `Interface + 1d10`, exploding on a 10 and fumbling on a 1), send it without a roll, or type in a roll you made with real dice. Add a note if you want. It goes to the GM, and their ruling comes back in the feed.

**Jack Out** leaves cleanly. Esc does the same thing.

---

## Saving

The GM console saves after every single change — creating a NET, revealing a floor, resolving an action. There is no save button to forget.

- GM sessions: `admin/saves/<session-name>-<id>.json`
- Player profile: `netrunner/saves/profile.json`

**Continue a session** on the main menu picks up exactly where you stopped: architectures, floor states, character sheet, feed and log all intact. Save files are plain JSON, so you can read or hand-edit them between sessions.

To move a campaign to a different machine, copy the JSON file into that machine's `admin/saves/` folder.

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
