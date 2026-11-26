# quaderno manual

This guide is for people who have never used this project. It explains what the `quaderno` command can do for you and walks through the everyday jobs it's good at, one task at a time.

For every option and the technical details, see the [README](../README.md). For the tablet's network protocol, see [API.md](API.md).

- [What this is](#what-this-is)
- [Install it](#install-it)
- [First steps](#first-steps)
- [How the tablet organizes things](#how-the-tablet-organizes-things)
- [Everyday tasks](#everyday-tasks)
- [Using it over Wi-Fi](#using-it-over-wi-fi)
- [Advanced: handle with care](#advanced-handle-with-care)
- [Using it in scripts](#using-it-in-scripts)
- [When something goes wrong](#when-something-goes-wrong)
- [Command cheat sheet](#command-cheat-sheet)
- [Glossary](#glossary)

## What this is

The Fujitsu QUADERNO is an e-paper tablet for reading PDFs and writing notes by hand with a pen. Fujitsu's own way to move files on and off it is a desktop app. `quaderno` is an independent alternative: a command you type in a terminal on your Mac or Linux computer, with a Python library underneath, that talks to the tablet directly over USB or Wi-Fi.

Why use it instead of the app?

- **It's scriptable.** Anything you can type, you can put in a script: a nightly backup of your notes, or sending a whole folder of papers in one go.
- **It's quick and precise.** Copy, move, rename, search and delete by typing a name or a pattern instead of clicking through folders.
- **It covers the tablet's settings too:** owner name, standby time, clock, note templates, pen buttons, Wi-Fi.
- **Everyday commands are safe by default.** They never overwrite a file, they ask before deleting anything matched by a pattern, and they check everything before changing anything.

What you need:

- **A QUADERNO.** It was built and tested on the A4 model (FMV-DP41). Other models are untested.
- **A Mac or a Linux computer.** Linux needs one extra setup step, described under [Install it](#install-it).
- **Python 3.11 or newer.**
- **A USB cable that carries data.** Some cables only charge; with those the tablet never shows up. You need it for initial pairing (after that, you can also work wirelessly over Wi-Fi).

You don't need Fujitsu's app. If you already use it, both can be used side by side.

## Install it

First, clone the repository in a terminal (on a Mac, the Terminal app):

```sh
git clone https://github.com/sarraga/quaderno.git ~/quaderno
cd ~/quaderno
```

Then follow the section for your system.

### macOS

macOS needs no extra drivers, permissions, or network setup. Create a virtual environment, install `quaderno`, and link it onto your `PATH`:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
mkdir -p ~/bin
ln -sf ~/quaderno/.venv/bin/quaderno ~/bin/quaderno
```

When you plug in the tablet, macOS lets your terminal open its USB serial port directly and automatically sets up its USB network link (typically named `en8` or similar).

### Linux

First, create a virtual environment, install `quaderno`, and link it onto your `PATH` (`~/.local/bin` is standard on Linux desktops):

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
mkdir -p ~/.local/bin
ln -sf ~/quaderno/.venv/bin/quaderno ~/.local/bin/quaderno
```

**Why Linux needs one extra setup step**

When you plug in the QUADERNO, it doesn't appear as a network device right away. It starts as a USB serial port (`/dev/ttyACM0`), and `quaderno` sends a short 10-byte command to switch it into USB networking mode:

```
  Plug in USB cable
         │
         ▼
  Serial mode (04c5:1656 → /dev/ttyACM0)
    • Owned by root:dialout (crw-rw----) ──► your user is blocked
    • ModemManager may probe it as a modem
         │
         │  quaderno sends the mode-switch command
         ▼
  Network mode (04c5:1657 → quaderno0)
    • NetworkManager's default wired profile waits for DHCP and drops the link
    • Needs an IPv6 link-local-only profile (fe80::.../64)
         │
         ▼
  Ready: quaderno talks HTTPS to fe80::...%quaderno0
```

Linux protects serial ports like `/dev/ttyACM0` because on multi-user Unix systems they belonged to modems and hardware terminals. By default, `/dev/ttyACM0` looks like this:

```sh
$ ls -l /dev/ttyACM0
crw-rw---- 1 root dialout 166, 0 ... /dev/ttyACM0
```

Only `root` and members of the `dialout` group (`uucp` on Arch) can read or write it (`rw-`); everyone else has no access (`---`). Normal desktop users aren't in `dialout`, so running `quaderno` from a terminal fails with `no permission to open /dev/ttyACM0`.

**Fix it once with `quaderno linux-setup`**

`quaderno linux-setup` prints a short script that installs two system files:

1. **A udev rule** (`60-quaderno.rules`): grants your local desktop session permission on the QUADERNO's serial port only (without giving your account access to every serial port on the machine), tells ModemManager to leave it alone, and names its network interface `quaderno0`.
2. **A NetworkManager profile** (`quaderno.nmconnection`): gives `quaderno0` an IPv6 link-local address and turns off DHCP so NetworkManager doesn't drop the link.

Inspect the script, then run it (`sudo` asks for your password):

```sh
quaderno linux-setup         # show what it will install
quaderno linux-setup | sh    # install both files
```

Then **unplug the tablet and plug it back in**. You do this once per computer.

> **Using Linux over SSH or a non-desktop terminal?** The udev rule's automatic access (`uaccess`) applies only to a user logged in locally at the computer's screen/keyboard. Over SSH, add your user to the `dialout` group (`uucp` on Arch) instead, then log out and back in so the new group takes effect:
>
> ```sh
> sudo usermod -aG dialout $USER
> ```

**Check that Linux sees the tablet**

After re-plugging, you can see the permission ACL (`+` in `ls -l`, `user:<you>:rw-` in `getfacl`) and test the cold switch from serial mode (`04c5:1656`) to network mode (`04c5:1657`):

```sh
lsusb -d 04c5:               # Bus ... ID 04c5:1656 Fujitsu, Ltd FMVDP41
getfacl /dev/ttyACM0         # includes user:<you>:rw-
quaderno -v addr             # switches mode (~4 s) -> fe80::c8d3:84ff:fec2:323b%quaderno0
ip -br addr show quaderno0   # quaderno0  UP  fe80::.../64
nmcli device status          # quaderno0  ethernet  connected  QUADERNO
```

### Tab completion (bash and zsh)

`quaderno` ships Tab completion for both **bash** and **zsh**. Pressing `Tab` completes subcommands, flags, option values, local files, and **folders and PDFs live from the tablet**:

```sh
quaderno sc⇥                             ->  quaderno screenshot
quaderno power s⇥                        ->  shutdown  sleep
quaderno ls bo⇥                          ->  quaderno ls Books/
quaderno --wifi ls Books/comp⇥           ->  quaderno --wifi ls Books/Computer\ engineering/
quaderno get 'Books/comp⇥                ->  quaderno get 'Books/Computer engineering/
quaderno put paper.pdf -d no⇥            ->  quaderno put paper.pdf -d Note/
quaderno search -t m⇥                    ->  mark  meta
```

- Folders end in `/` with no trailing space so pressing `Tab` again steps straight into the subfolder.
- Letter case is ignored when matching names on the tablet, and patterns containing `*`, `?`, or `[` are left untouched.
- With `--wifi`, Tab completion probes only the cached Wi-Fi IP (`wifi_addr.dat`) so it stays fast and never blocks on a slow network scan (run one `quaderno --wifi ping` first if you haven't connected over Wi-Fi yet).

**Enable it in bash (Linux / macOS):**

```sh
mkdir -p ~/.local/share/bash-completion/completions
ln -sf ~/quaderno/completions/quaderno.bash ~/.local/share/bash-completion/completions/quaderno
```

Open a new terminal (or run `source ~/quaderno/completions/quaderno.bash` in an already-open shell). If your system doesn't use `bash-completion`, add `source ~/quaderno/completions/quaderno.bash` to `~/.bashrc` instead.

**Enable it in zsh (macOS / Linux):**

Add these two lines to `~/.zshrc` (`fpath` must come before `compinit`; if `~/.zshrc` already calls `compinit`, add only the `fpath` line above it):

```sh
fpath=(~/quaderno/completions $fpath)
autoload -Uz compinit && compinit
```

## First steps

You do this once, and it takes about five minutes.

### 1. Plug in and wake the tablet

Connect the tablet to your computer, press its power button to wake it, and unlock it if it's locked. Commands only work while the tablet is awake.

The first command after plugging in takes a few seconds longer than usual: `quaderno` switches the tablet's USB connection from a serial port into a small network link between your computer and the tablet. That happens automatically.

### 2. Pair

```
quaderno pair
```

The tablet shows a PIN on its screen. Type it at the prompt within about two minutes.

Pairing is like pairing Bluetooth headphones: it creates a key on your computer and registers it with the tablet, so from now on the tablet recognizes your computer. The key is stored in `~/.config/quaderno/`. If Fujitsu's app was paired before, it keeps working.

### 3. Check that it works

```
quaderno ping
quaderno info
```

`ping` answers `ok` when everything is connected. `info` shows a summary like this (the interface after `%` is `en8` or similar on macOS, and `quaderno0` on Linux):

```
address        fe80::c8d3:84ff:fec2:323b%en8         # %quaderno0 on Linux
api version    2.1
firmware       2.2.09.11030FP
owner          Your Name
mac address    00:11:22:33:44:55
storage        4.4G used / 23.3G (18.9G free)
battery        93% (charging, ac)
```

You're ready.

## How the tablet organizes things

- **One folder tree.** Everything lives under a top folder called `Document`. You can leave that part out: `Books/paper.pdf`, `/Books/paper.pdf` and `Document/Books/paper.pdf` are the same file.
- **Only PDFs.** The tablet stores nothing else. Anything that isn't a PDF is skipped when you upload.
- **Two kinds of PDF:**
  - **Documents** are PDFs you put on the tablet: papers, books, manuals.
  - **Notebooks** are created on the tablet from a *template* (lined, squared, blank…) and filled in by hand. They're PDFs too.
- **Your handwriting is saved with the file.** Notes and highlights you write on a page are part of that document, so when you download it, the handwriting comes along.
- **Names with spaces need quotes** in the terminal: `"Books/My Paper.pdf"`.
- **Patterns save typing.** `'Books/*rust*'` matches every name in `Books` containing "rust", ignoring letter case. Always put patterns in single quotes so your shell doesn't try to expand them itself.

## Everyday tasks

### See what's on the tablet

```
quaderno ls                         # the top folder
quaderno ls Books                   # one folder
quaderno ls -l Books                # with size, date and number of pages
quaderno ls -R Books                # everything inside, all levels down
quaderno ls 'Books/*algorithm*'     # only matching names
```

### Find a document

`search` asks the tablet itself to look, which is much faster than listing everything.

```
quaderno search rust                     # in names, titles and authors
quaderno search rust -l                  # with size, date and pages
quaderno search rust -f Books            # only inside the Books folder
quaderno search meeting --notes          # only notebooks
quaderno search a --sort read --desc     # everything, most recently read first
quaderno search ownership -t body        # the text inside documents
```

Searching the text inside documents relies on the tablet's own index, so it can miss documents.

**Flag pages with a mark and find them later.** The tablet recognizes a few hand-drawn marks. Draw one on a page you want to come back to, then search for it:

```
quaderno marks                      # the marks the tablet knows, e.g. star and asterisk
quaderno search star -t mark        # every document where you drew a star
```

### Back up your notes to your computer

A one-off copy of a whole folder:

```
quaderno get -r Note -o ~/Backup          # creates ~/Backup/Note/...
quaderno get "Note/Meeting Notes_20260105.pdf" -o ~/Downloads
```

**For regular backups, use `sync`.** It keeps a folder on your computer as a mirror of a folder on the tablet:

```
quaderno sync ~/QUADERNO              # the whole tablet
quaderno sync ~/QUADERNO-notes Note   # just the Note folder
quaderno sync -n ~/QUADERNO           # dry run: show what it would do, change nothing
```

What makes `sync` safe to run as often as you like:

- **It only downloads what changed.** New and edited documents (including new handwriting) are downloaded; everything else is left alone.
- **It follows moves and renames** on the tablet without downloading the file again.
- **It never overwrites your own changes.** A file you edited on your computer is kept and reported as `KEEP`.
- **It doesn't delete by itself.** Something deleted on the tablet stays on your computer and is reported as `gone`. Add `--delete` if you want those removed too.

Copying only works in one direction, from the tablet to your computer.

### Send papers and books to the tablet

```
quaderno put paper.pdf -d Books                # into an existing folder
quaderno put paper.pdf -d "Books/Better name.pdf"   # under a new name
quaderno put *.pdf -d Books                    # several files
quaderno put -r ~/Papers -d Books              # a whole folder, sub-folders included
```

- **It never overwrites.** If a file with the same name is already there, it's skipped with a `skip` line. So you can re-run `put -r` and it only sends what's missing.
- **Non-PDFs are skipped,** and so are files that don't exist; everything else is still uploaded.
- **If the destination folder doesn't exist,** `put` stops before sending anything and suggests folders with a similar name. Create it first with `mkdir`.

### Organize your library

```
quaderno mkdir -p Books/2026/Spring              # create folders, parents included
quaderno mv 'Books/*draft*' Archive              # move matching files into the existing Archive folder
quaderno mv Books/old.pdf Books/new.pdf          # rename
quaderno cp Books/paper.pdf Archive              # copy into Archive, done on the tablet itself
quaderno cp Books/paper.pdf Books/paper-copy.pdf # copy under a new name
quaderno rm Books/unwanted.pdf                   # delete one document
quaderno rm 'Received/*'                         # delete matches; it lists them and asks first
quaderno rm -r Books/Old                         # delete a folder with everything inside
```

- **`mv` and `cp` check everything first.** If any destination name is already taken, nothing moves or is copied at all.
- **Moving or copying into a folder needs the folder to exist.** With a single source and a destination that doesn't exist, `mv` and `cp` treat the destination as a new name instead.
- **`cp` doesn't go through your computer,** so copying even a large book is quick. Only documents can be copied, not folders.
- **Be careful with `rm`.** With a pattern it shows the matches and asks before deleting. With an exact name, it deletes straight away. A folder is only deleted if it's empty, unless you add `-r`.

### Open something on the tablet from your computer

```
quaderno open 'Books/*algorithm*'          # opens it where you last stopped reading
quaderno open 'Books/*algorithm*' -p 42    # opens it at page 42
quaderno viewing                           # what's on the screen now, and which page
quaderno screenshot                        # saves the screen as quaderno-screen.png
quaderno screenshot screen.jpg             # or as a JPEG
```

`open` needs a name or pattern that matches exactly one document; if several match, it lists them so you can be more specific.

### Update a PDF you changed on your computer

If you've edited a PDF that's already on the tablet, `replace` swaps in the new file while keeping the same entry on the tablet:

```
quaderno replace paper-v2.pdf Books/paper.pdf
```

Your handwriting on that document is tied to page positions, so if pages moved, notes can end up in the wrong place. Try it on a copy first (`quaderno cp` it, then `replace` the copy).

### Make the tablet yours

**Settings:**

```
quaderno config                          # list all settings
quaderno config owner "Your Name"        # set the owner name
quaderno config timeout_to_standby       # read one setting
quaderno config time_format 24hour
quaderno time                            # set the tablet's clock to your computer's time
```

- **Use the values the tablet's own settings menu offers;** it rejects anything else.
- **The timezone is separate from the clock.** The tablet only offers 11 zones (UTC, Amsterdam, Helsinki, six US zones, Tokyo, Honolulu); set the closest with `config timezone`. `time` tells you when the tablet's local time won't match your computer's.

**Note templates** are the page designs you choose from when you create a notebook. You can add your own, made as a PDF:

```
quaderno templates ls                          # list them, with their IDs
quaderno templates add my-grid.pdf --name "My grid"
quaderno templates rename <id> "Squared paper"
quaderno templates rm <id>                     # deletes it straight away
```

Built-in templates marked `modifiable=false` can't be changed.

**Pen buttons** are the side button and the tail end of the pen:

```
quaderno pen-buttons ls                 # current assignments and the choices
quaderno pen-buttons set side highlight
quaderno pen-buttons set tail eraser
```

The choices are `highlight`, `eraser`, `selection`, `marker`, `secondary_color`, `none` and `expansion`.

### Check on the tablet

```
quaderno info                  # firmware, storage, battery, owner…
quaderno info --json           # the same, for scripts
quaderno firmware precheck     # is it ready for a firmware update? (changes nothing)
quaderno -v ping               # also shows which address and keys are in use
```

If Fujitsu support asks for a diagnostic log, `quaderno pastlog` downloads it as `quaderno-pastlog.bin`. It's encrypted and can be large (over 100 MB); only Fujitsu can read it.

## Using it over Wi-Fi

Once you've paired the tablet over USB and connected it to the same Wi-Fi network as your computer, you can run any `quaderno` command wirelessly by adding `--wifi` before the subcommand:

```sh
quaderno --wifi ping
quaderno --wifi info
quaderno --wifi ls Books
quaderno --wifi put paper.pdf -d Books
quaderno --wifi sync ~/QUADERNO-notes Note
```

You don't need to pair again: Wi-Fi reuses the same keys and pinned TLS certificate in `~/.config/quaderno/`.

### Putting the tablet on Wi-Fi

You can join Wi-Fi in the tablet's own settings menu, or while plugged in over USB:

```sh
quaderno wifi on
quaderno wifi scan
quaderno wifi add "MyWiFi" --security psk   # prompts for the password without saving it in history
quaderno wifi status
```

### Fast reconnects and choosing a default transport

By default, `quaderno` only checks USB so it fails immediately when nothing is plugged in. You can change that with an environment variable in your shell profile (`~/.zshrc` or `~/.bashrc`):

```sh
# Try USB first; if unplugged, automatically fall back to Wi-Fi
export QUADERNO_TRANSPORT=auto

# Or always use Wi-Fi by default (override any single command with --usb)
export QUADERNO_TRANSPORT=wifi
quaderno --usb addr
```

After the first successful `--wifi` login, `quaderno` caches the tablet's IP in `~/.config/quaderno/wifi_addr.dat` so later commands reconnect in about 0.3 seconds. If your router gives the tablet a new IP later, `quaderno` notices automatically, finds the new IP, and updates the cache.

If you already know the tablet's IP, you can also skip discovery with `--addr`:

```sh
quaderno --addr 192.168.0.99 ls
```

### Keeping Wi-Fi responsive & Tab completion

- **Wi-Fi sleep:** check `quaderno --wifi config wifi_sleep_policy`. If the tablet turns Wi-Fi off while idle, wake it with its power button—or keep Wi-Fi on with `quaderno --wifi config wifi_sleep_policy disabled`.
- **Tab completion:** `quaderno --wifi ls Bo⇥` completes remote folders and files just like USB, using the cached IP in `wifi_addr.dat` so pressing Tab never stalls on a network scan.
- **From Python:** pass `transport="wifi"` or `transport="auto"` to `quaderno.connect()`:

  ```python
  import quaderno

  with quaderno.connect(transport="auto") as dev:
      dev.sync("~/QUADERNO-notes", "Note")
  ```

## Advanced: handle with care

These commands change the tablet at a system level. They're there for completeness; most people never need them.

| Command | What it does | Why care |
|---|---|---|
| `power sleep\|wake\|reboot\|shutdown` | Controls the tablet's power | Acts immediately, without asking. After `shutdown`, the USB link drops and you need the power button to start it again. |
| `firmware upload FILE` then `firmware update --yes` | Installs a firmware update from Fujitsu's update file | The tablet reboots into update mode. Use only Fujitsu's own file, keep the tablet charged and plugged in, and don't unplug it until it's done. |
| `wifi on\|off\|scan\|ls\|add\|rm` | Turns Wi-Fi on or off, lists networks and saves new ones | Omit `--password` to be prompted securely (or use `--password-stdin`) so your Wi-Fi password isn't saved in shell history. |
| `certs ls\|add-ca\|add-client\|clear` | Installs certificates for workplace Wi-Fi (EAP) | `clear` removes all of them and needs `--yes`. Omit `--password` or use `--password-stdin` for `add-client`. |
| `config` with `use_mode`, `mtp`, `regulation_voltage`, `additional_client_registration`, `control_servers_editable`, `initialized_flag`, `annotations_permission` | System-level settings | They change how the tablet behaves for every app. Leave them alone unless you know exactly what they do. |
| `annotations DOC PAGE` | Downloads the raw handwriting data of one page | It's a data file for developers, not a picture. |

## Using it in scripts

Every command is meant to be scriptable:

- **Errors go to stderr** and start with `quaderno:`.
- **`info --json` and `config --all`** print machine-readable JSON.
- **Exit codes tell you what happened:**

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | The command failed, for example a file that doesn't exist, or (for `put`) something you named couldn't be uploaded |
| 2 | The command was typed wrong |
| 3 | The tablet can't be reached: not plugged in, asleep, or the cable was pulled |
| 4 | Pairing or login problem: run `quaderno pair --force` |
| 130 | You pressed Ctrl-C |

A minimal backup script:

```sh
#!/bin/sh
quaderno sync ~/QUADERNO-backup Note && echo "notes backed up $(date)"
```

The same things are available from Python:

```python
import quaderno

with quaderno.connect() as dev:
    for e in dev.search("rust"):
        print(e.relpath, e.size)
    dev.get("Note/*.pdf", "~/Backup")
```

The README's Python API section covers the rest.

## When something goes wrong

| Message | What to do |
|---|---|
| `no QUADERNO on USB` | Check the cable carries data, not only power, and that the tablet is on. To connect wirelessly instead, pass `--wifi`. |
| `no QUADERNO found on Wi-Fi via mDNS` | Wake the tablet, check its Wi-Fi is enabled and on the same network as your computer (not a guest/isolated Wi-Fi), or pass `--addr 192.168.x.y`. |
| `found QUADERNO (...) on Wi-Fi ... doesn't answer on port 8443` | Wake or unlock the tablet; after a firmware update, re-pair with `quaderno pair --force`. |
| `stayed in USB serial mode` | The tablet was asleep, often after the computer slept. Wake it with its power button, or unplug and re-plug it. |
| `link is up, but it doesn't answer` | Wake or unlock the tablet. |
| `not paired` | Pair first: `quaderno pair`. |
| `the device rejected this client` | The tablet forgot your computer. Pair again with `quaderno pair --force`. |
| `device TLS certificate differs` | Normal after a firmware update: run `quaderno pair --force`. |
| `pairing failed: wrong PIN` | Run `pair` again; the tablet shows a new PIN each time. |
| `no permission to open /dev/ttyACM0` | Linux only: do the Linux step under [Install it](#install-it), then unplug and re-plug the tablet. |
| `that interface is down`, or `no IPv6 link-local address` | Linux only: the same Linux step. |
| `isn't the one these credentials were paired with` | A different QUADERNO is plugged in. Plug in yours, or pair this one too (see the README's `--config` option). |

The README's Troubleshooting section has the full list.

## Command cheat sheet

| Command | What it does | Changes the tablet? |
|---|---|---|
| `pair` | Pair this computer with the tablet (once) | Registers your computer |
| `ping` | Check the connection | No |
| `info` | Firmware, storage, battery, owner | No |
| `ls` | List folders and documents | No |
| `search` | Find documents by name, text or hand-drawn mark | No |
| `marks` | List the marks the tablet recognizes | No |
| `get` | Download documents or folders | No |
| `sync` | Keep a local folder mirroring a tablet folder | No |
| `put` | Upload PDFs or folders | Adds files |
| `mkdir` | Create folders | Yes |
| `mv` | Move or rename | Yes |
| `cp` | Copy documents | Adds files |
| `rm` | Delete documents or folders | Deletes |
| `replace` | Swap in a new version of a document | Overwrites |
| `open` | Show a document on the tablet's screen | Only what's on screen |
| `viewing` | What's on the screen now | No |
| `screenshot` | Save the screen as an image | No |
| `config` | Show or change settings | When you give a value |
| `time` | Set the tablet's clock to your computer's time | Yes |
| `templates` | List, add, rename or delete note templates | Except `ls` |
| `pen-buttons` | Show or set what the pen buttons do | Only `set` |
| `wifi` | Wi-Fi status, networks and settings | Except `status`, `scan`, `ls` |
| `certs` | Certificates for workplace Wi-Fi | Except `ls` |
| `firmware` | Check for, upload and install a firmware update | Except `precheck` |
| `power` | Sleep, wake, reboot or shut down | Yes |
| `annotations` | Download one page's raw handwriting data | No |
| `pastlog` | Download the encrypted diagnostic log | No |
| `addr` | Print the tablet's network address | No |
| `linux-setup` | Print the one-time Linux setup script | No |

Run `quaderno COMMAND --help` for every option of a command.

## Glossary

- **Document:** a PDF you put on the tablet.
- **Notebook:** a PDF created on the tablet from a template, for writing by hand.
- **Template:** a page design (lines, squares, planner…) a notebook is created from.
- **Annotation:** handwriting or highlighting on a page.
- **Mark:** a symbol the tablet recognizes when you draw it, such as a star, so you can find those pages later with `search -t mark`.
- **Pairing:** the one-time step that lets the tablet recognize your computer.
- **Revision:** a tag the tablet changes every time a document's file changes, including when you write on it. `sync` uses it to know what to download.
