# quaderno

A command-line client for the Fujitsu QUADERNO (FMV-DP41, a rebadged Sony DPT-CP1/RP1) connected over **USB** or **Wi-Fi** to a Mac or a Linux computer.
It is a Python package with two faces: a **Python API** (`import quaderno`) and the **`quaderno` command** built on it. The only dependency is [`cryptography`](https://cryptography.io).
It pairs with the device by itself, so the official QUADERNO app isn't needed.

**New here? Start with the [manual](docs/MANUAL.md)**: what the tool can do, task by task, written for someone who has never used it.

Tested end-to-end against a QUADERNO A4 (FMV-DP41) with firmware 2.2.09.11030FP and Web API 2.1 over USB on both **macOS** and **Linux** (systemd-udevd + NetworkManager), and over **Wi-Fi** (zero-dependency mDNS/Bonjour discovery with TLS 1.2 certificate pinning) on macOS. The full cycle (cold plug-in, USB mode switch, Wi-Fi discovery, pairing, login, info, config, search, screenshot, download, and file operations) was exercised with the official app closed. The code differs between macOS and Linux only in OS discovery hooks (`ioreg`/Bonjour on macOS, `sysfs` on Linux), and the offline test suite covers both.

## Install

You need macOS or Linux, Python 3.11 or newer, and a USB cable that carries data. Clone the repository first:

```sh
git clone https://github.com/sarraga/quaderno.git ~/quaderno
cd ~/quaderno
```

Install in editable mode (`-e .`, or `-e '.[test]'` for development with `pytest` and `mypy`) so changes to the code take effect immediately. `pip` also installs `cryptography`. Without `quaderno` on your `PATH`, `.venv/bin/python -m quaderno` works the same.

### macOS

macOS needs no extra drivers, permissions, or network configuration—the logged-in user can open `/dev/cu.usbmodem*` directly, and the kernel creates an Ethernet interface (`enN`, e.g. `en8`) with an IPv6 link-local address automatically once `quaderno` switches the device out of serial mode:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'   # or -e . if you don't need pytest/mypy
mkdir -p ~/bin
ln -sf ~/quaderno/.venv/bin/quaderno ~/bin/quaderno
quaderno --help
```

### Linux

#### 1. Install the package

Create a virtual environment and symlink the CLI into `~/.local/bin` (standard on Linux desktops, without putting `.venv`'s `python` ahead of system Python):

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'   # or -e . if you don't need pytest/mypy
mkdir -p ~/.local/bin
ln -sf ~/quaderno/.venv/bin/quaderno ~/.local/bin/quaderno
which quaderno
```

#### 2. Why Linux needs extra permissions and network setup

Unlike macOS, Linux enforces strict multi-user permissions on serial devices and hands new wired USB interfaces to NetworkManager's default DHCP policy. Both steps in the USB bring-up need a one-time root configuration:

```
  Plug in USB cable
         │
         ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │ 1. CDC-ACM serial mode  (VID 04c5, PID 1656 → /dev/ttyACM0)         │
  │    Default node: crw-rw---- 1 root dialout  (/dev/ttyACM0)          │
  │      • ordinary users have "---" → PermissionError on open()        │
  │      • ModemManager probes new ttyACM* ports with AT commands       │
  │                                                                     │
  │    Fixed by 60-quaderno.rules:                                      │
  │      • TAG+="uaccess"  → systemd-logind adds ACL user:<you>:rw-     │
  │      • ID_MM_DEVICE_IGNORE="1" → ModemManager leaves it alone       │
  └─────────────────────────────────────────────────────────────────────┘
         │  quaderno writes 10-byte SWITCH_TO_CDC_ECM to /dev/ttyACM0
         ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │ 2. USB re-enumeration gap (~1–2 s)                                  │
  │    04c5:1656 drops off /sys/bus/usb/devices before 04c5:1657        │
  │    appears; usb.discover() waits through this gap.                  │
  └─────────────────────────────────────────────────────────────────────┘
         │
         ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │ 3. CDC-ECM Ethernet mode  (VID 04c5, PID 1657 → eth0 / usb0)        │
  │    Default NM wired profile waits for DHCP + IPv6 RA (neither is    │
  │    sent by the tablet) and can drop the link on timeout.            │
  │                                                                     │
  │    Fixed by 60-quaderno.rules + quaderno.nmconnection:              │
  │      • udev renames interface → quaderno0 and brings link UP        │
  │      • NM matches quaderno* with ipv4=disabled, ipv6=link-local     │
  └─────────────────────────────────────────────────────────────────────┘
         │
         ▼
  quaderno0 UP (fe80::.../64) ──► HTTPS :8443 to fe80::...%quaderno0
```

**Understanding the `/dev/ttyACM0` permission issue (`uaccess` vs. `dialout`)**

On Linux, USB serial devices (`/dev/ttyACM0`) inherit Unix's multi-user model for modems and hardware terminals:

```sh
$ ls -l /dev/ttyACM0
crw-rw---- 1 root dialout 166, 0 ... /dev/ttyACM0
# ^^^      ^ owner  group
# ││└─ other: --- (no access)
# │└── group: rw- (dialout on Debian/Ubuntu, uucp on Arch)
# └─── owner: rw- (root)
```

Because normal desktop accounts are not in `dialout`, opening `/dev/ttyACM0` raises `PermissionError`. There are two ways Linux grants access, and which one applies depends on **where your terminal session lives**:

- **Local desktop / console session (`TAG+="uaccess"`, recommended):** `60-quaderno.rules` tags only `04c5:1656` with `uaccess`. Because the filename sorts before systemd's `73-seat-late.rules`, `systemd-logind` dynamically attaches a POSIX ACL (`user:<you>:rw-`, shown as a trailing `+` in `crw-rw----+` and visible via `getfacl /dev/ttyACM0`) exclusively to the user seated at the local console—without granting access to every other serial device on the machine or requiring a re-login.
- **Remote SSH or headless session (`dialout` group):** An SSH session has no local `logind` seat, so `uaccess` ACLs are not applied to it. In that case, add your user to the group that owns `/dev/ttyACM0` (`dialout` on Debian/Ubuntu/Fedora, `uucp` on Arch), then **start a new login session** (already-open shells do not pick up new group memberships):

  ```sh
  sudo usermod -aG dialout $USER
  ```

#### 3. Install the udev rule and NetworkManager profile

Both files ship with the package in [`quaderno/linux/`](quaderno/linux). `quaderno linux-setup` prints the commands that install them; inspect it, then run it (`sudo` asks for your password):

```sh
quaderno linux-setup
quaderno linux-setup | sh
```

Then **unplug the QUADERNO and plug it back in** so udev applies `60-quaderno.rules` to the device.

#### 4. Verifying the Linux stack (worked example)

```sh
# 1. Confirm serial mode (04c5:1656) and the udev uaccess ACL before switching
lsusb -d 04c5:                 # Bus ... ID 04c5:1656 Fujitsu, Ltd FMVDP41
ls -l /dev/ttyACM0             # crw-rw----+ 1 root dialout ... (note the '+')
getfacl /dev/ttyACM0           # includes user:<you>:rw-

# 2. Cold switch: serial (1656) -> ~1-2 s USB re-enumeration -> Ethernet (1657)
quaderno -v addr               # ~4 s -> fe80::c8d3:84ff:fec2:323b%quaderno0

# 3. Confirm interface rename, IPv6 link-local, and NetworkManager profile
lsusb -d 04c5:                 # now ID 04c5:1657 Fujitsu, Ltd FMVDP41
ip -br addr show quaderno0     # quaderno0  UP  fe80::.../64
nmcli device status            # quaderno0  ethernet  connected  QUADERNO

# 4. Pair, exercise the API, and run the offline test suite + strict type check
quaderno pair
quaderno ping && quaderno info
.venv/bin/pytest
.venv/bin/mypy
```

### Tab completion (zsh and bash)

`completions/_quaderno` (zsh) and `completions/quaderno.bash` (bash) complete subcommands, options, local files, and **paths on the device**. Paths are fetched live from the device, about 0.4 s per Tab:

```
quaderno sc⇥                         ->  quaderno screenshot
quaderno power s⇥                    ->  shutdown  sleep
quaderno search -t m⇥                ->  mark  meta
quaderno ls Bo⇥                      ->  quaderno ls Books/
quaderno --wifi ls Bo⇥               ->  quaderno --wifi ls Books/
quaderno get Books/comp⇥             ->  quaderno get Books/Computer\ engineering/
quaderno get Books/Computer\ engineering/ru⇥  ->  quaderno get Books/Computer\ engineering/Rust/
quaderno put x.pdf -d no⇥            ->  quaderno put x.pdf -d Note/   (folders only after -d)
quaderno mv 'Books/comp⇥             ->  quaderno mv 'Books/Computer engineering/   (quoted or escaped)
```

How it behaves:
- **Folders** complete with a trailing `/` and no space, so pressing Tab again goes one level deeper.
- **Letter case** is ignored in the part you're completing.
- **Patterns** containing `*`, `?` or `[` are left untouched.
- **With the device unplugged**, nothing is offered, without any delay.
- **`--config DIR`, `--addr ADDR`, `--wifi`, and `--usb`** typed before the subcommand apply to completion too. Over `--wifi`, completion only probes the cached IP in `wifi_addr.dat` (short timeout) and never blocks on an mDNS scan; run one `quaderno --wifi ping` first if the cache doesn't exist yet.
- **Right after plugging in**, the first Tab switches the device to USB networking, which takes about 4 s. Later Tabs take about 0.4 s.

`pip install` doesn't install completion files: your shell reads them straight from the checkout.

- **bash (Linux / macOS):** link into the standard user `bash-completion` directory (loaded automatically on demand, no `~/.bashrc` edit needed):

  ```sh
  mkdir -p ~/.local/share/bash-completion/completions
  ln -sf ~/quaderno/completions/quaderno.bash ~/.local/share/bash-completion/completions/quaderno
  ```

  Or source it directly from `~/.bashrc`:

  ```sh
  source ~/quaderno/completions/quaderno.bash
  ```

- **zsh (macOS / Linux):** add to `~/.zshrc` (`fpath` must come before `compinit`; if `~/.zshrc` already calls `compinit`, add only the `fpath` line above it):

  ```sh
  fpath=(~/quaderno/completions $fpath)
  autoload -Uz compinit && compinit
  ```

  After updating the checkout, reload in already-open zsh shells with `unfunction _quaderno && autoload -Uz _quaderno` (or `source ~/quaderno/completions/quaderno.bash` in bash).

## Usage

```
quaderno pair [--force] [--pin-file F]  pair with the device (PIN shown on its screen)
quaderno ls [PATH] [-l] [-R]            list a folder (default: Document)
quaderno get REMOTE... [-o DEST] [-r]   download documents; -r for whole folders
quaderno put LOCAL... [-d DEST] [-r]    upload PDFs; -r for local directories
quaderno mkdir REMOTE... [-p]           create folders; -p creates parents, ok if exists
quaderno mv SRC... DST                  move/rename; several sources -> into folder DST
quaderno rm REMOTE... [-r] [-y]         delete documents / empty folders; -r with contents
quaderno sync LOCAL [REMOTE] [--delete] [-n]  mirror a device folder here (incremental)
quaderno open DOC [-p PAGE]             open a document on the device's screen
quaderno search KEYWORD [-t TARGET] [-f DIR]  find documents by name, text, or annotation mark
quaderno cp SRC... DST                  copy documents on the device; several -> into folder DST
quaderno replace LOCAL REMOTE           replace a document's PDF content in place
quaderno annotations DOC PAGE [-o OUT]  download raw binary annotations for a page
quaderno viewing                        show which document is open on the device
quaderno marks                          list preset annotation mark names
quaderno ping                           check authenticated connection to the device
quaderno templates {ls,add,rename,rm}   manage custom PDF note templates
quaderno wifi {status,on,off,scan,ls,add,rm}  inspect or configure Wi-Fi
quaderno power {sleep,wake,reboot,shutdown}   device power controls
quaderno firmware {precheck,upload,update}    firmware update workflow
quaderno pen-buttons {ls,set}           show or assign pen button functions
quaderno certs {ls,add-ca,add-client,clear}   manage CA and client certificates
quaderno pastlog [OUT]                  download encrypted diagnostic pastlog archive
quaderno config [--all] [KEY [VALUE]]   show device settings, or change one
quaderno time                           set the device clock to this computer's time
quaderno info [--json]                  firmware, storage, battery, MAC, owner, Wi-Fi
quaderno screenshot [OUT] [--format F]  PNG or JPEG of the current screen
quaderno addr                           print the device address (switches USB mode if needed)
quaderno linux-setup                    print the one-time Linux setup script (see Linux setup)
```

Global options:
- `--addr ADDR` (or `$QUADERNO_ADDR`) skips discovery.
- `--wifi` opts in to Wi-Fi mDNS discovery instead of USB (`--usb` overrides `$QUADERNO_TRANSPORT`; set `$QUADERNO_TRANSPORT=wifi` to default to Wi-Fi, or `auto` for USB-then-Wi-Fi fallback).
- `--config DIR` (or `$QUADERNO_CONFIG`) keeps the credentials in another directory, for every command including `pair`.
- `-v` prints which credentials, address and TLS mode are in use.

### Paths and folders

The device has a single tree rooted at the folder `Document`. Remote paths are relative to it, so `foo.pdf`, `/foo.pdf` and `Document/foo.pdf` all refer to the same file. Folder names cannot contain `/`, and the device stores only PDFs.

```
quaderno ls -l Note
quaderno get "Note/Meeting Notes_20240506.pdf" -o ~/Downloads
quaderno get -r Note -o ~/Backup                 # -> ~/Backup/Note/...
quaderno put paper.pdf -d Books                  # into an existing folder
quaderno put paper.pdf -d "Books/Better name.pdf"
quaderno put -r ~/Papers -d Books                # -> Books/Papers/... (created as needed)
quaderno put -r ~/Papers -d Books/Reading        # Reading is new -> it becomes the copy of ~/Papers
quaderno mkdir -p Books/2026/Q4
quaderno mv Books/2026 Archive/                  # into an existing folder
quaderno mv Books/2026 Books/Twenty26            # rename
quaderno rm -r Books/Old
```

### Patterns

`ls`, `get`, `mv`, `cp`, `rm` and `open` accept patterns in remote paths, so you don't have to type long names. **Quote patterns** so your shell doesn't try to expand them against local files (zsh fails with `no matches found`).

```
quaderno ls 'fighter*'                    # glob: * ? [abc]; letter case is ignored
quaderno mv 'Fighter*' Books              # move the long-named book into Books
quaderno mv 'Note/2024-0[56]*' Archive    # several matches -> DST must be an existing folder
quaderno get 'Note/*meeting*' -o ~/Downloads
quaderno ls '*/sample_*'                  # patterns work in any part of the path
quaderno ls -E 'naval institute'          # -E: regular expression, matched anywhere in the name
quaderno open -E '^dhs660'
quaderno rm 'Received/*'                  # lists the matches and asks first; -y skips the question
```

How matching works:
- **An exact path always wins.** If `weird [draft].pdf` exists, it is used as-is. Otherwise the `[...]` would be read as a glob character class.
- **Patterns match one level at a time.** `*` doesn't cross a `/`.
- **A matched folder is listed itself.** `ls 'B*'` shows the folder `Books/`, not what's in it; add `-R` to list its contents too.
- **`open` needs exactly one match.** Otherwise it lists the candidates.
- **`rm` checks before deleting.** With a pattern, it shows what matched and asks. Without a terminal, it refuses unless you pass `--yes`. All checks, such as non-empty folders without `-r`, happen before anything is deleted.
- **`-E` regex mode** uses Python syntax, ignores letter case, and matches anywhere in the name; anchor with `^` and `$` when you need to. It can't match across a `/`.

How the commands behave:
- **`put` never overwrites.** If a document with the same name already exists in the target folder, `put` skips it and prints `skip ...` on stderr. This makes `put -r` safe to re-run: it uploads only what's missing. A remote folder is created only where at least one PDF will go.
- **`mv` never overwrites.** Before moving anything, it checks every destination. If a name is already taken there, or two sources would end up with the same name, it stops with an error and moves nothing.
- **What `-d` means depends on what exists.**
  - If `-d` names an existing folder, the files go into it.
  - If it names something that doesn't exist and ends in `.pdf`, that becomes the uploaded file's new name.
  - Otherwise `put` stops before uploading anything and suggests folders with that name, for example `did you mean: 'Books/Military History'`. Use the full path from the top (`-d 'Books/Military History'`), or create the folder with `mkdir`.
  - With `put -r` and a single folder, a `-d` folder that doesn't exist yet is created.
- **A failed upload leaves nothing behind.** If an upload fails partway, the half-created document is deleted from the device.
- **`put` skips what it can't upload and carries on.** Each file or folder you name that can't be uploaded gets a `skip` line on stderr: not a PDF, a folder without `-r`, or a file that doesn't exist. Everything else is still uploaded. So `put -r dir/* -d DEST` works even when `dir` mixes PDFs and EPUBs. Inside folders uploaded with `-r`, non-PDF files are left out silently and counted in the summary line (`… 2 non-PDF ignored`); hidden files are ignored and not counted. As with `cp`, the exit status is 1 if anything you named was skipped this way, so a script can tell that something wasn't uploaded. Files skipped because they're already on the device don't count, nor do non-PDFs inside folders.
- **`rm` on a folder only deletes it when it's empty.** `rm -r` deletes the documents first, then the folders from the deepest up. On the device, `DELETE /folders/{id}` rejects non-empty folders with HTTP 400 (`40018`) unless `{"force_delete_flag": "true"}` is sent; deleting individually gives per-file progress events. It refuses to delete or move the root folder.
- **`get -r` keeps the folder layout.** Partial downloads are written to `*.part` and renamed when complete.

## Sync (device → computer)

```
quaderno sync ~/QUADERNO                # whole device
quaderno sync ~/QUADERNO-notes Note     # one folder
quaderno sync -n ~/QUADERNO             # dry run: show what would happen
quaderno sync --delete ~/QUADERNO       # also remove local copies of documents deleted on the device
```

`sync` copies one way, from the device to this computer. Each run downloads only documents that are new or changed. It decides by the device's `file_revision`, which changes whenever a document is edited, including handwriting added on the device. Opening or reading a document doesn't change it.

What each run does:
- **New or edited on the device:** downloaded. The local file's date is set to the device's modification date.
- **Moved or renamed on the device:** the local file is moved too, without downloading it again.
- **Deleted on the device:** the local copy is kept and reported as `gone`. It is removed only with `--delete`, and empty folders left behind are removed too.
- **Missing locally:** downloaded again.
- **Name clash:** two device names that differ only in letter case would land on the same file on a case-insensitive volume, as macOS volumes usually are. The second one is skipped and reported as `CLASH`. Linux does the same, so a synced folder has the same contents on either system.

**Local files are never overwritten.** `sync` only replaces, moves or deletes a file that is exactly as the last sync left it, checked by size and date. A file you edited, or one that wasn't created by sync, is kept and reported as `KEEP`.

The state is stored in `LOCAL/.quaderno-sync.json`. Each local folder mirrors exactly one device folder. A first sync into a non-empty folder needs `--force`, and existing files there are kept.

## open and time

- **`open DOC`** shows the document on the device at the page you last read. `open DOC -p N` opens it at page N. The device reports the new page a few seconds later.
- **`time`** sets the device clock to this computer's current time. Run it now and then to keep the clock right. The device clock can be set but not read.

  The device's timezone is separate from the clock. `time` prints a note whenever the device's timezone and this computer's give different local times. It doesn't change the timezone itself: the device offers only 11 zones (UTC, Amsterdam, Helsinki, 6 US zones, Tokyo, Honolulu), so pick the closest one with `config timezone` (see below).

## search

`search` asks the device to find documents, far faster than listing everything and filtering it yourself. The keyword is required.

```
quaderno search rust                      # names, titles and authors (the default)
quaderno search rust -l                    # with size, date and pages, like ls -l
quaderno search --docs borrow -t body      # text inside documents, excluding notebooks
quaderno search star -t mark               # documents carrying the "star" annotation mark
quaderno search a --sort read --desc       # most recently read first
```

- **`-t` chooses where to look:** `meta` (names, titles and authors; the default), `body` (the text inside documents), or `mark` (by annotation-mark name — run `quaderno marks` to list valid mark names).
- **`-f` / `--folder`** restricts the search to one device folder subtree (`origin_folder_id`).
- **`--notes` / `--docs`** limit the results to notebooks, or to documents (everything else).
- **`--sort`** orders by `name`, `title`, `author`, `date`, `read` (last read), `size` or `created`; add `--desc` to reverse it.
- **`--mark`** (only with `-t mark`) narrows to `hand` (handwriting) or `highlight`; the default is both.
- Results are printed as full paths, like `ls -R`. When nothing matches, nothing is printed and a short note goes to stderr.

## cp, replace, viewing, config and device management

```
quaderno cp Books/a.pdf Note              # copy into a folder (keeping the name)
quaderno cp Books/a.pdf Books/a-copy.pdf   # copy to a new name
quaderno cp 'Books/*.pdf' Archive          # patterns; several sources -> DST must be a folder
quaderno replace updated.pdf Books/a.pdf   # replace PDF contents in place (keeps ID)
quaderno annotations Books/a.pdf 1 -o p1.bin  # download raw annotation bytes for page 1
quaderno viewing                           # what's open on the screen right now
quaderno marks                             # list preset annotation marks
quaderno ping                              # verify authenticated session
quaderno config                            # list all scalar device settings
quaderno config --all                      # full /system/configs3 JSON
quaderno config timeout_to_standby         # read one setting
quaderno config owner "Santiago"           # change one setting
quaderno screenshot screen.jpg             # JPEG screenshot (or --format jpeg|png)
quaderno templates ls                      # list note templates
quaderno templates add grid.pdf --name Grid
quaderno wifi status                       # Wi-Fi state (also: on, off, scan, ls, add, rm)
quaderno power sleep                       # sleep, wake, reboot, or shutdown
quaderno firmware precheck                 # firmware update: precheck, upload, update --yes
quaderno pen-buttons ls                    # show or set pen button functions
quaderno certs ls                          # manage CA and client certificates
quaderno pastlog quaderno-log.bin          # download encrypted diagnostic archive
```

- **`cp` copies on the device itself**, with no download and re-upload, so it's quick even for large files. Only documents can be copied, not folders. Like `mv`, several sources need an existing folder as the destination, while a single source may instead name a new file; name clashes are checked before anything is copied.
- **`replace` overwrites an existing document's PDF bytes in place** (`PUT /documents/{id}/file`), preserving its document ID; pass `--revision REV` for optimistic concurrency control (`40017` on mismatch).
- **`viewing`** prints the open document's path and page, for example `Document/Books/x.pdf  page 59 of 736`, or `nothing is open on the device`. It pairs with `open`.
- **`config`** with no arguments lists all scalar settings (via `/system/configs3`, normalizing `timeout_to_sleep` to `timeout_to_standby`); `--all` prints the raw `/system/configs3` JSON. Settable keys: `owner`, `timezone`, `date_format`, `time_format`, `timeout_to_standby`, `mtp` (`0`/`1`), `regulation_voltage`, `use_mode` (`personal`/`shared`/`others`), `annotations_permission` (`yes`/`no`), `additional_client_registration` (`accept_all`/`accept_dcss_only`/`refuse`), `control_servers_editable` (`yes`/`no`), `initialized_flag`, `wifi` (`on`/`off`), `wifi_apmode` (`on`/`off`), `wifi_sleep_policy` (`disabled`/`enabled_always`/`enabled_if_plugged`).
- **`templates`, `wifi`, `power`, `firmware`, `pen-buttons`, `certs`, `pastlog`** expose the remaining device Web API controls directly from the command line. See [`docs/API.md`](docs/API.md) for the complete endpoint-by-endpoint reference.

## Python API

Everything the command does — plus low-level v2 document/folder operations, document action attributes, on-screen dialogs (`indicate`), transfer wizards, notifications, and custom app installation — is available from Python with full `TypedDict` structs for every request and response (`quaderno.models`).

```python
import quaderno
from quaderno import DialogParameters, WifiApConfig

with quaderno.connect() as dev:                  # USB by default; pass transport="wifi" or "auto" for Wi-Fi
    dev.ping()
    for e in dev.entries("Books", v2=True):      # Entry: name, path, relpath, ext_id, size, pages, ...
        print(e.relpath, e.size)
    dev.get("Books/Sailing/*.pdf", "~/Downloads")      # patterns work as in the CLI
    res = dev.put(["a.pdf", "b.pdf"], "Books")         # PutResult(uploaded=, skipped=, missing=, ...)
    dev.upload("c.pdf", "Books")                       # one file; raises if the name exists
    dev.replace_document("Books/c.pdf", "c_v2.pdf")    # in-place PDF update -> FileUploadResult
    dev.move("Fighter*", "Books")                      # -> [(old_path, new_path), ...]
    dev.copy("Books/x.pdf", "Archive")                 # server-side copy -> [(old, new), ...]
    dev.rm("Received/*", confirm=lambda entries: True) # confirm is asked only when a pattern matched
    dev.sync("~/QUADERNO", "Note")                     # -> counts per action
    for e in dev.search("rust", target="meta", origin_folder="Books"):
        print(e.relpath)
    dev.open("Books/x.pdf", page=5)
    print(dev.current_viewing()["views"])              # CurrentViewing struct
    dev.set_time()
    dev.set_config("owner", "Santiago")                # dev.configs() / dev.configs_v3() / dev.get_config(key)
    dev.add_wifi_accesspoint(config=WifiApConfig(ssid="Home", security="psk", passwd="secret"))
    dev.indicate(DialogParameters(title="Sync", message="Working...", button_caption="OK"))
    print(dev.info()["storage"])
```

- **Structs:** [`quaderno/models.py`](quaderno/models.py) defines `TypedDict` structs and `Literal` enums for every request and response model (`DeviceInformation`, `CurrentViewing`, `NoteTemplate`, `FileUploadResult`, `ConfigsV2`, `ConfigsV3`, `WifiApConfig`, `WifiApInfo`, `PenButtonsInfo`, `CertificateInfo`, `DocumentProcessStatus`, `UpdateFirmwarePrecheck`, `DialogParameters`, `DocumentAttribute`, `TransferWizardNextScreen`, etc.). They are plain `dict`s at runtime and statically checked under `mypy --strict`.
- **Session renewal:** device sessions expire after 30 minutes idle; `Client` automatically re-authenticates once on HTTP `401`.
- **Progress:** operations that change things take `on_event=`, a function that receives an `Event(kind, message, path, warning)` for each step. Kinds include `uploaded`, `skipped`, `created`, `moved`, `deleted` and `downloaded`. The CLI simply prints `event.message`, sending warnings to stderr.
- **Errors:** every failure on the device side is a `quaderno.QuadernoError`; local file problems (a directory that doesn't exist, no permission) are ordinary `OSError`s. Subclasses separate unreachable devices (`ConnectError`), credential and identity problems (`AuthError`), pairing (`PairingError`), errors from operations (`NotFoundError`, `ExistsError`, `NotEmptyError`, `RequestError`, `LockedError`, `StorageFullError`), and malformed answers from the device (`ProtocolError`). The full tree is under [Errors and exit codes](#errors-and-exit-codes).
- **Pairing:** `quaderno.pair(read_pin)` pairs from code. `read_pin` is a function that returns the PIN shown on the device.
- **Types:** the package is fully annotated and ships `py.typed`, so mypy and editors check code that uses it. Answers from the device are checked where they come in: a reply that isn't a JSON object, or lacks an id the API needs, raises `ProtocolError` rather than surfacing later as a `KeyError`.

## Pairing

```
quaderno pair
```

1. Connect the device by USB and wake it.
2. Run `pair`. It generates a 2048-bit RSA key and starts pairing. The QUADERNO shows a PIN.
3. Type the PIN at the prompt within about 2 minutes. To supply it from another process instead, use `--pin-file F`: `pair` waits for the file `F` to appear and reads the PIN from it.

The credentials are stored in `~/.config/quaderno/` (set `$QUADERNO_CONFIG` to change the location):

| File | Contents | Mode |
|---|---|---|
| `deviceid.dat` | client ID (random UUID) registered on the device | 644 |
| `privatekey.dat` | RSA private key, PKCS#1 PEM | **600** |
| `publickey.dat` | public key sent to the device | 644 |
| `devicecert.bin` | the device's TLS certificate, received during pairing | 644 |
| `serial.dat` | device serial number, used to compute its USB address and match over mDNS | 644 |
| `wifi_addr.dat` | last TLS-verified Wi-Fi IP address, cached for fast `--wifi` reconnects | 644 |

`pair` refuses to replace an existing pairing unless you pass `--force`.

**USB vs. Wi-Fi pairing:** pair over USB whenever possible. Once paired, the same credentials and pinned certificate (`devicecert.bin`) work over both USB and Wi-Fi. Pairing over Wi-Fi (`quaderno --wifi pair`) works, but because the tablet's registration endpoint uses unencrypted HTTP (port `8080`), an active attacker on a shared network could MITM the PIN exchange; `quaderno` prints a warning when pairing over a Wi-Fi IP.

**Where credentials come from**, in order: `--config DIR`, then `$QUADERNO_CONFIG`, then `~/.config/quaderno`.

## TLS

The device uses a self-signed certificate for `digitalpaper.local` (valid until 2035), and hands that same certificate over during pairing. Every HTTPS connection must present exactly that certificate. Anything else is refused with an error.

If pairing received a certificate it couldn't read, none is stored and connections are not verified; every command then prints a warning on stderr, and `pair --force` fixes it. If a firmware update replaces the certificate, run `pair --force` as well.

## How the connection works

1. **USB mode switch.** On plug-in, the device appears as a CDC-ACM serial port: USB VID `0x04C5`, PID `0x1656`, `/dev/cu.usbmodem*` on macOS and `/dev/ttyACM*` on Linux. Writing `01 00 00 01 00 00 00 01 01 04` to that port makes it disconnect and re-enumerate as CDC-ECM Ethernet (PID `0x1657`), which appears as a new network interface:
   - **macOS:** `enN` (e.g. `en8`), discovered via `ioreg -a -r -c IOUSBHostDevice -l`.
   - **Linux:** `ethN` or `usbN`, renamed to `quaderno0` by `60-quaderno.rules` and activated with IPv6 link-local by `quaderno.nmconnection`; discovered via `/sys/bus/usb/devices/<dev>/<dev>:<cfg>.<if>/net/`. Because Linux briefly drops the device from sysfs (~1–2 s) between `0x1656` disappearing and `0x1657` enumerating, `usb.discover()` waits out that gap within `answer_timeout` (~4 s end-to-end on a cold plug-in).
   The script writes only to a tty whose USB VID/PID match.
2. **Address.** The device's IPv6 link-local address is derived from its serial number: `('1'+serial) * 0x9e37fffffffc0001`, masked, with `fffe` inserted in the middle. For example, serial `850000312345678` gives `fe80::c8d3:84ff:fec2:323b`. It is used only on the network interface the system created for the device. On macOS, the I/O Registry (`ioreg`) lists that interface (e.g. `en8`) and its link status under the USB device. On Linux, sysfs does: `/sys/bus/usb/devices/<device>/<interface>/net/`, with the link status in `/sys/class/net/<interface>/carrier`. On Linux, an error also says when the interface is down or has no IPv6 link-local address. The link status is used only to word an error (missing interface, link down, or no answer), never to skip trying: right after one plug-in it read "not valid" while the device answered. With several QUADERNOs attached, the serial saved at pairing picks the right one.
3. **Pairing.** Plain HTTP on port 8080, in five calls:
   - `POST /register/pin`: the device sends nonce `n1`, its MAC and its Diffie-Hellman public value.
   - `POST /register/hash`: both sides compute a shared secret (DH, RFC 3526 2048-bit group). PBKDF2-SHA256 with 10,000 iterations turns it into a 32-byte HMAC key and a 16-byte AES-128-CBC key. The PIN enters as `HMAC(key, pin)`, so each side proves it knows the PIN without sending it.
   - `POST /register/ca`: the device returns its certificate, encrypted.
   - `POST /register`: the client sends its ID and public key, encrypted the same way.
   - `PUT /register/cleanup`.

   All messages are checked with HMAC.
4. **Login.** The script calls `GET /auth/nonce/{client_id}`, signs the nonce with RSA-SHA256 (PKCS#1 v1.5), sends the signature with `PUT /auth`, and then uses the session cookie.
5. **API.** HTTPS on port 8443:
   - `/resolve/entry/{path}`
   - `/folders/{id}/entries?offset&limit`
   - `GET /documents/{id}/file` to download
   - `POST /documents` followed by `PUT /documents/{id}/file?offset_bytes&total_bytes` in 8 MB pieces to upload
   - `POST /folders`
   - `PUT /documents/{id}` and `PUT /folders/{id}` to move or rename
   - `DELETE /documents/{id}` and `DELETE /folders/{id}`
   - `/system/status/*`

RSA key generation, signing, key files and AES come from the `cryptography` package; HMAC and PBKDF2 from the standard library. The one Diffie-Hellman exchange is done with Python's `pow()`. The device requires classic finite-field Diffie-Hellman (RFC 3526 group 14), which `cryptography` has deprecated for removal, and no other well-maintained Python library offers it. It is two modular exponentiations, so no library is needed. The tests check all of this against `/usr/bin/openssl` and a simulated device.

## Tests

```
.venv/bin/pip install -e '.[test]'   # once: adds pytest
.venv/bin/python -m pytest           # all offline tests, about 3 s, including mypy --strict
.venv/bin/python -m mypy             # type checking alone (settings in pyproject.toml)
.venv/bin/python -m pytest tests/test_sync.py -k guards   # one file, or one test
```

| File | Covers |
|---|---|
| `tests/test_api.py` | the Python API used directly |
| `tests/test_fake.py` | CLI folder commands (put/get/mv/rm/mkdir) |
| `tests/test_sync.py` | sync: edits, moves, deletes, local changes, name clashes, dry run |
| `tests/test_patterns.py` | globs/regex in ls, get, mv, rm, open; rm confirmation |
| `tests/test_pair.py` | crypto wiring vs `/usr/bin/openssl`; pairing vs a simulated device |
| `tests/test_errors.py` | discovery states (stuck serial mode...), reading `ioreg` and a simulated Linux sysfs, `linux-setup`, HTTP/connection errors, malformed answers, local file errors, Tab completion options, exit codes |
| `tests/test_mdns.py` | pure-stdlib DNS-SD/mDNS packet encoding/decoding, macOS Bonjour & Linux Avahi fast-paths + UDP fallback, VPN interface filtering, dual-stack dedup, cached Wi-Fi IP probe, USB → Wi-Fi fallback, CLI `--usb`/`--wifi` |
| `tests/test_hardening.py` | adversarial tests: mDNS SSRF/DoS/flood/pin-mismatch defenses, sync path traversal and symlink guards, protocol loop/size bounds, X.509 cert validation, terminal escape sanitization |
| `tests/test_bash_completion.py` | bash Tab completion (`completions/quaderno.bash`): subcommands, options, remote path escaping, global flag forwarding |
| `tests/test_types.py` | `mypy --strict` over the package and the tests (skipped if mypy isn't installed) |

None of them needs the device: `tests/fakedevice.py` is a `quaderno.Device` with only the HTTP layer replaced, so the tests run the real API and CLI code. Two manual checks do need it, and pytest doesn't collect them:

```
.venv/bin/python tests/hw_replug_watch.py   # re-plug the device; the CLI must do the USB switch
zsh tests/hw_zsh_completion.zsh             # presses Tab in a real zsh against the device
```

## Code layout

```
quaderno/
  __init__.py     public API: connect, pair, Device, Entry, Event, PutResult, models, errors
  device.py       Device: files, search, viewer, templates, configs, Wi-Fi, certs, power, firmware, sync
  models.py       TypedDict structs and literal enums for every JSON request and response
  sync.py         one-way incremental sync (device -> computer)
  client.py       HTTPS transport (kept-alive, certificate pinning, auto re-auth) and binary transfer
  pairing.py      PIN pairing protocol
  crypto.py       RSA/AES via `cryptography`, HMAC, Diffie-Hellman, PEM/DER
  credentials.py  where credentials live; loading and saving them (including cached Wi-Fi IP)
  usb.py          finding the device on USB (`ioreg -a` on macOS, sysfs on Linux), USB mode switch, address
  mdns.py         finding the device on Wi-Fi via stdlib mDNS (`_dp_fujitsu._tcp.local`) and TLS pin check
  bonjour.py      macOS Bonjour (`mDNSResponder` via `libSystem.B.dylib`) leaf resolver
  discovery.py    unified discovery across USB and Wi-Fi (with TLS-pinned cached Wi-Fi fast path)
  entry.py        Entry: a document or folder
  events.py       Event: progress reports from API operations
  errors.py       the exception hierarchy
  util.py         paths, patterns, terminal sanitization, time and size formatting
  cli.py          the `quaderno` command: argument parsing and printing only
  linux/          udev rule and NetworkManager profile, installed by `quaderno linux-setup`
docs/MANUAL.md    user manual: what you can do, task by task
docs/API.md       complete firmware Web API v2.1 endpoint and error code reference
completions/_quaderno       zsh Tab completion
completions/quaderno.bash   bash Tab completion
tests/                  offline tests (fake device) and manual device checks
```

## Wi-Fi

By default `quaderno` connects over USB only (no network traffic or mDNS delay when unplugged). Pass `--wifi` before the subcommand to connect over Wi-Fi using the **same USB-paired credentials and pinned TLS certificate**:

```sh
# Basic Wi-Fi commands (unplug USB, wake the tablet on the same Wi-Fi network)
quaderno --wifi addr                     # discover & print Wi-Fi IP (e.g. 192.168.0.99)
quaderno --wifi -v ping                  # verify TLS-pinned login over Wi-Fi
quaderno --wifi info                     # firmware, storage, battery, Wi-Fi SSID
quaderno --wifi ls -l Books
quaderno --wifi put paper.pdf -d Books
quaderno --wifi get -r Note -o ~/Backup
quaderno --wifi sync ~/QUADERNO-notes Note

# Transport selection via environment variable
export QUADERNO_TRANSPORT=auto           # try USB first, fall back to Wi-Fi if unplugged
quaderno ping                            # uses USB if plugged in, else Wi-Fi
QUADERNO_TRANSPORT=wifi quaderno ls      # default to Wi-Fi
QUADERNO_TRANSPORT=wifi quaderno --usb addr  # --usb overrides $QUADERNO_TRANSPORT

# Skip discovery completely with an explicit IP
quaderno --addr 192.168.0.99 ping
```

From Python:

```python
import quaderno

with quaderno.connect(transport="wifi") as dev:   # or transport="auto"
    print(dev.addr, dev.info()["battery"])
```

Adding a Wi-Fi network to the tablet from the CLI (prompts interactively on a TTY or reads from stdin so the password doesn't land in shell history):

```sh
quaderno wifi scan
quaderno wifi add "MyWiFi" --security psk              # prompts securely for password
printf '%s\n' "$WIFI_PASS" | quaderno wifi add "MyWiFi" --password-stdin
quaderno config wifi_sleep_policy disabled             # keep Wi-Fi awake while idle
```

How Wi-Fi discovery works (zero external dependencies):
- **Fast path (`wifi_addr.dat`, ~0.3 s):** `--wifi` first probes the last verified IP cached in `~/.config/quaderno/wifi_addr.dat` with a TLS 1.2 certificate-pin check (`devicecert.bin`). If the IP is stale or changed by DHCP, it automatically falls back to mDNS and updates the cache after login succeeds.
- **macOS Bonjour fast-path (~15 ms lookup):** on macOS, `quaderno/mdns.py` first queries the system `mDNSResponder` cache via `libSystem.B.dylib` (`DNSServiceBrowse` / `DNSServiceResolve` / `DNSServiceGetAddrInfo`). This finds an idle tablet immediately even when Wi-Fi power-save drops raw multicast probes.
- **Linux Avahi fast-path (when installed):** on Linux, `quaderno/mdns.py` first queries `avahi-daemon`'s cache via `avahi-browse -c -r -t -p` (with the same port, probe-cap, serial, and TLS-pin checks). If `avahi-browse` isn't installed, the daemon isn't running, or the cache misses, it falls back silently to raw UDP multicast.
- **Cross-platform stdlib UDP mDNS fallback:** browses `_dp_fujitsu._tcp.local` over `224.0.0.251:5353`, pinning `IP_MULTICAST_IF` / `IP_ADD_MEMBERSHIP` to active non-loopback, non-point-to-point LAN interfaces (skipping VPN tunnels like `utun*`) and resending queries every `0.5 s` up to `5 s`.
- **Hardening on untrusted LANs:** discovery refuses non-`8080` SRV ports and loopback/multicast/link-local/unspecified IPs, caps probe counts and response sizes, and verifies `devicecert.bin` over TLS 1.2 *before* accepting or caching an IP. If a cached IP fails TLS pinning, `wifi_addr.dat` is automatically cleared.
- **SSID display:** the tablet's Wi-Fi endpoints return `ssid` Base64-encoded on the wire; `quaderno info` and `quaderno wifi status|ls|scan` decode canonical Base64 UTF-8 SSIDs automatically.
- **Tab completion:** `quaderno --wifi ls Bo⇥` probes only the cached `wifi_addr.dat` IP (short timeout) and never blocks on an mDNS scan.
- **Standby note:** check `quaderno --wifi config wifi_sleep_policy`. With sleep enabled, the tablet drops off Wi-Fi after sitting idle; wake it with its power button or set `wifi_sleep_policy` to `disabled`.

## If you also use the official QUADERNO app

`quaderno` never reads or changes the app's files, and the two can be used side by side:
- **Pairing:** `quaderno pair` adds a second client on the device. The app's own pairing keeps working; both were checked after pairing this unit. Moving over from the app takes one `quaderno pair`.
- **USB switch:** when the app is running, it switches the device to USB networking itself.
- **Clock:** the app sets the device clock each time it connects; `quaderno time` does the same on demand.
- **App errors such as `EADDRINUSE:8081` or status 0** come from the app's own local port forwarding. They don't affect the USB link or `quaderno`.

## Alternative: dpt-rp1-py

[dpt-rp1-py](https://github.com/janten/dpt-rp1-py) implements the same Sony/Fujitsu Web API and supports Sony DPT-RP1/CP1, Readmoo mooInk Pro 2, and Windows, adding bidirectional `sync`, a FUSE filesystem mounter (`dptmount`), and a macOS PDF print service. It requires `pycryptodome`, `zeroconf`, and other packages, and uses a separate Linux-only helper (`dptusb`) for USB switching. It can share the same credentials:

```
dptrp1 --client-id ~/.config/quaderno/deviceid.dat --key ~/.config/quaderno/privatekey.dat --addr "[$(quaderno addr)]" list-documents
```

## Errors and exit codes

Every failure is a `quaderno.QuadernoError`. The subclasses tell you what went wrong; scripts using the command get the same distinction through the exit code.

```
QuadernoError                    base: .status (HTTP status, when there is one)              exit 1
├── ConnectError                 can't reach device, unknown transport, or USB list failed   exit 3
│   ├── DeviceNotFoundError      no QUADERNO on USB or Wi-Fi, wrong serial, or multiple
│   ├── SerialModeError          stuck in USB serial mode (see below)
│   └── DeviceUnreachableError   interface/link down, port 8443 refuses/fails pin, or connection dropped
├── AuthError                    credentials or device identity problems                     exit 4
│   ├── NotPairedError           no credentials or incomplete pairing directory
│   ├── LoginError               the device rejected this client's RSA signature
│   └── CertificateError         TLS certificate differs from devicecert.bin (or cert corrupt)
├── PairingError                 PIN pairing protocol or crypto failure                      exit 4
│   └── WrongPinError            HTTP 403/40301: entered PIN did not match
├── RequestError                 HTTP error (.status, .code) or stalled chunked upload       exit 1
│   ├── LockedError              HTTP 423: resource is locked (e.g. open in viewer)
│   └── StorageFullError         HTTP 507: insufficient storage on the device
├── ProtocolError                malformed, non-JSON, oversized, or out-of-range answer      exit 1
├── NotFoundError                path doesn't exist (HTTP 404) or pattern matched nothing    exit 1
├── ExistsError                  target already exists (upload, mkdir, move, cp)             exit 1
└── NotEmptyError                folder not empty (rm without recursive)                     exit 1
```

A local file problem, such as `get -o` into a directory that doesn't exist, prints `quaderno: PATH: reason` and exits 1. Exit code 2 is a command-line usage error, and 130 means you pressed Ctrl-C. Network failures in the middle of a command, such as the cable being pulled, become `DeviceUnreachableError`, not a Python traceback.

A malformed value that only affects what is shown or a file's date doesn't stop a command: an unreadable size or page count shows as missing, and `get` saves the file without the device's modification time. Both print a warning on stderr.

### The device stuck in serial mode

When the USB link restarts, the QUADERNO starts in serial mode; this happens when you plug it in or when the computer wakes from sleep. `quaderno` then sends the command that switches it to USB networking. While the device is in standby, it can ignore that command.

`quaderno` re-sends the command every 3 seconds. If the device is still in serial mode after 15 seconds, it stops with:

```
quaderno: the QUADERNO is connected but stayed in USB serial mode: it ignored the command that
switches it to USB networking for 15 s. This happens when it is in standby, e.g. after the
computer slept. Wake it with its power button, or unplug and re-plug it, then try again.
```

## Troubleshooting

- **`no QUADERNO on USB`**: look for the device in the system's USB list: `ioreg -p IOUSB` on macOS (look for `FMVDP41`), `lsusb -d 04c5:` on Linux. If it's missing, check the cable: it must carry data, not only power.
- **`stayed in USB serial mode`**: wake the device or re-plug it, as described above.
- **`lists no network interface for it`**: the system didn't set up networking for the device after the switch. Unplug and re-plug it. On Linux, also check that the `cdc_ether` driver loads: `sudo modprobe cdc_ether`.
- **`USB network link (enN) is down`**: the interface exists but has no link. Wake or unlock the device; if that doesn't help, re-plug it.
- **`its link is up, but it doesn't answer`**: the network is fine but the device's server isn't responding. Wake or unlock the device.
- **`no permission to open /dev/ttyACM0`** (Linux): run `quaderno linux-setup | sh`, then re-plug the device. Over SSH, add yourself to the group the message names instead, as described in [Install → Linux](#linux).
- **`that interface is down`, `has no IPv6 link-local address` or `IPv6 is turned off on that interface`** (Linux): run `quaderno linux-setup | sh`, then re-plug the device. `ip addr show quaderno0` should then list an `inet6 fe80::` address. With NetworkManager, `nmcli device status` should show `quaderno0` connected with the `QUADERNO` profile.
- **`isn't the one these credentials were paired with`**: a different QUADERNO is plugged in. Plug in the paired one, or pair this one into its own folder with `quaderno --config DIR pair` and use `--config DIR` with it.
- **`couldn't read the list of USB devices`**: macOS's `ioreg` failed or timed out, or Linux's `/sys/bus/usb/devices` couldn't be read; the message includes why. This is not the same as the device being absent.
- **`finding the QUADERNO on USB isn't supported on ...`**: only macOS and Linux can find the device on USB. On other systems, give its address with `--addr`.
- **`no QUADERNO found on Wi-Fi via mDNS`**: make sure the tablet is awake, Wi-Fi is turned on (`quaderno wifi status` over USB, or on the tablet's settings), and both devices are on the same subnet (some guest networks or AP isolation settings block multicast/peer traffic). If you know the tablet's IP from your router, pass `--addr 192.168.x.y` to test direct reachability.
- **`found QUADERNO (...) on Wi-Fi at ..., but it doesn't answer on port 8443`**: the tablet answered mDNS/port 8080 but HTTPS port 8443 refused or didn't match your pinned `devicecert.bin`. Wake or unlock the tablet; if you recently updated firmware, re-pair with `quaderno pair --force`.
- **`2 QUADERNOs on Wi-Fi`**: unpaired discovery found multiple tablets; pass `--addr IP` to pick one, or pair first so `serial.dat` selects yours automatically.
- **`pairing failed: wrong PIN`**: run `pair` again. The device shows a new PIN for each attempt.
- **`device TLS certificate differs ...`**: this is expected after a firmware update; run `pair --force`. Otherwise, something other than your QUADERNO answered.
- **`the device rejected this client`**: the device no longer knows this client. Run `pair --force`.

## License

Copyright (C) 2026 Santiago Arraga González

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; see [LICENSE](LICENSE) for the full text.

QUADERNO is a trademark of its owner. This project is not affiliated with or endorsed by Fujitsu or Sony.
