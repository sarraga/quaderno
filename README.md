# quaderno

A command-line client for the Fujitsu QUADERNO (FMV-DP41, a rebadged Sony DPT-CP1/RP1) connected to a Mac over USB.
It is a Python package with two faces: a **Python API** (`import quaderno`) and the **`quaderno` command** built on it. The only dependency is [`cryptography`](https://cryptography.io); its compiled module loads fine under Santa.
It pairs with the device by itself, so the official QUADERNO app isn't needed.

Tested against: QUADERNO serial 850000312345678, firmware 2.2.09.11030FP, Web API 2.1, macOS 27.2, Python 3.14. The full cycle (plug in, USB mode switch, pairing, login, file operations) was tested with the official app closed.

## Install

It's set up the same way as `ytchan`: a pyproject package installed in editable mode into its own `.venv`, so changes to the code take effect immediately.

```
cd ~/quaderno
/opt/homebrew/opt/python@3.14/bin/python3.14 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/quaderno --help
```

`pip install` also installs `cryptography`. Without the `quaderno` command on your PATH, `.venv/bin/python -m quaderno` works the same. To run plain `quaderno` from anywhere, link it into a folder on your PATH. On this Mac that folder is `~/bin`:

```
ln -s ~/quaderno/.venv/bin/quaderno ~/bin/quaderno
```

### Tab completion (zsh)

`completions/_quaderno` completes subcommands, options, local files, and **paths on the device**. Paths are fetched live from the device, about 0.4 s per Tab:

```
quaderno ls Bo⇥                      ->  quaderno ls Books/
quaderno get Books/milit⇥            ->  quaderno get Books/Military\ History/
quaderno put x.pdf -d Books/Mil⇥     ->  only folders are offered after -d
quaderno mv 'Books/Fight⇥            ->  quoting is handled, inside quotes or with \
```

How it behaves:
- **Folders** complete with a trailing `/` and no space, so pressing Tab again goes one level deeper.
- **Letter case** is ignored in the part you're completing.
- **Patterns** containing `*`, `?` or `[` are left untouched.
- **With the device unplugged**, nothing is offered, without any delay.
- **Right after plugging in**, the first Tab switches the device to USB networking, which takes about 4 s. Later Tabs take about 0.4 s.

`pip install` doesn't install this file: zsh reads it straight from the checkout. To enable it, add to `~/.zshrc`. `fpath` must come before `compinit`:

```
fpath=(~/quaderno/completions $fpath)
autoload -Uz compinit && compinit
```

`compinit` turns on zsh's completion system for every command, not just this one. If your `~/.zshrc` already runs it, add only the `fpath` line, above it.

## Usage

```
quaderno pair [--force] [--pin-file F]  pair with the device (PIN shown on its screen)
quaderno ls [PATH] [-l] [-R]            list a folder (default: Document)
quaderno get REMOTE... [-o DEST] [-r]   download documents; -r for whole folders
quaderno put LOCAL... [-d DEST] [-r]    upload PDFs; -r for local directories
quaderno mkdir REMOTE... [-p]           create folders; -p creates parents, ok if exists
quaderno mv SRC... DST                  move/rename; several sources -> into folder DST
quaderno rm REMOTE... [-r] [-y]         delete documents / empty folders; -r with contents
quaderno sync LOCAL [REMOTE] [--delete] [-n]  mirror a device folder to the Mac (incremental)
quaderno open DOC [-p PAGE]             open a document on the device's screen
quaderno time                           set the device clock from the Mac
quaderno info [--json]                  firmware, storage, battery, MAC, owner
quaderno screenshot [OUT.png]           PNG of the current screen
quaderno addr                           print the device address (switches USB mode if needed)
```

Global options:
- `--addr ADDR` (or `$QUADERNO_ADDR`) skips discovery.
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

`ls`, `get`, `mv`, `rm` and `open` accept patterns in remote paths, so you don't have to type long names. **Quote patterns** so your shell doesn't try to expand them against files on the Mac (zsh fails with `no matches found`).

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
- **`rm` on a folder only deletes it when it's empty.** `rm -r` deletes the documents first, then the folders from the deepest up. The CLI never sends a delete for a folder that still has contents, because the device's behaviour in that case isn't documented. It refuses to delete or move the root folder.
- **`get -r` keeps the folder layout.** Partial downloads are written to `*.part` and renamed when complete.

## Sync (device → Mac)

```
quaderno sync ~/QUADERNO                # whole device
quaderno sync ~/QUADERNO-notes Note     # one folder
quaderno sync -n ~/QUADERNO             # dry run: show what would happen
quaderno sync --delete ~/QUADERNO       # also remove local copies of documents deleted on the device
```

`sync` copies one way, from the device to the Mac. Each run downloads only documents that are new or changed. It decides by the device's `file_revision`, which changes whenever a document is edited, including handwriting added on the device. Opening or reading a document doesn't change it.

What each run does:
- **New or edited on the device:** downloaded. The local file's date is set to the device's modification date.
- **Moved or renamed on the device:** the local file is moved too, without downloading it again.
- **Deleted on the device:** the local copy is kept and reported as `gone`. It is removed only with `--delete`, and empty folders left behind are removed too.
- **Missing locally:** downloaded again.
- **Name clash:** two device names that differ only in letter case would land on the same file on a case-insensitive Mac volume. The second one is skipped and reported as `CLASH`.

**Local files are never overwritten.** `sync` only replaces, moves or deletes a file that is exactly as the last sync left it, checked by size and date. A file you edited, or one that wasn't created by sync, is kept and reported as `KEEP`.

The state is stored in `LOCAL/.quaderno-sync.json`. Each local folder mirrors exactly one device folder. A first sync into a non-empty folder needs `--force`, and existing files there are kept.

## open and time

- **`open DOC`** shows the document on the device at the page you last read. `open DOC -p N` opens it at page N. The device reports the new page a few seconds later.
- **`time`** sets the device clock to the Mac's current time. Run it now and then to keep the clock right. The device clock can be set but not read.

  The device's timezone is separate from the clock. Yours is `UTC`, which matches Dublin in winter and is 1 hour behind in summer. `time` prints a note whenever the two differ. It doesn't change the timezone, because the device offers only 11 zones (UTC, Amsterdam, Helsinki, 6 US zones, Tokyo, Honolulu) and none of them follows Irish summer time.

## Python API

Everything the command does is available from Python; the CLI is a thin layer over it.

```python
import quaderno

with quaderno.connect() as dev:                  # find it on USB, log in (closes the connection after)
    for e in dev.entries("Books"):               # Entry: name, path, relpath, size, pages, modified, ...
        print(e.relpath, e.size)
    dev.get("Books/Sailing/*.pdf", "~/Downloads")      # patterns work as in the CLI
    res = dev.put(["a.pdf", "b.pdf"], "Books")         # PutResult(uploaded=, skipped=, missing=, ...)
    dev.upload("c.pdf", "Books")                       # one file; raises if the name exists
    dev.move("Fighter*", "Books")                      # -> [(old_path, new_path), ...]
    dev.rm("Received/*", confirm=lambda entries: True) # confirm is asked only when a pattern matched
    dev.sync("~/QUADERNO", "Note")                     # -> counts per action
    dev.open("Books/x.pdf", page=5)
    dev.set_time()
    print(dev.info()["storage"])
```

- **Progress:** operations that change things take `on_event=`, a function that receives an `Event(kind, message, path, warning)` for each step. Kinds include `uploaded`, `skipped`, `created`, `moved`, `deleted` and `downloaded`. The CLI simply prints `event.message`, sending warnings to stderr.
- **Errors:** every failure is a `quaderno.QuadernoError`. Subclasses separate unreachable devices (`ConnectError`), credential and identity problems (`AuthError`), pairing (`PairingError`), errors from operations (`NotFoundError`, `ExistsError`, `NotEmptyError`, `RequestError`), and malformed answers from the device (`ProtocolError`). The full tree is under [Errors and exit codes](#errors-and-exit-codes).
- **Pairing:** `quaderno.pair(read_pin)` pairs from code. `read_pin` is a function that returns the PIN shown on the device.

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
| `serial.dat` | device serial number, used to compute its address | 644 |

`pair` refuses to replace an existing pairing unless you pass `--force`.

**Where credentials come from**, in order: `--config DIR`, then `$QUADERNO_CONFIG`, then `~/.config/quaderno`.

## TLS

The device uses a self-signed certificate for `digitalpaper.local` (valid until 2035), and hands that same certificate over during pairing. Every HTTPS connection must present exactly that certificate. Anything else is refused with an error.

If pairing received a certificate it couldn't read, none is stored and connections are not verified; every command then prints a warning on stderr, and `pair --force` fixes it. If a firmware update replaces the certificate, run `pair --force` as well.

## How the connection works

1. **USB mode switch.** On plug-in, the device appears as a CDC-ACM serial port: USB VID `0x04C5`, PID `0x1656`, `/dev/cu.usbmodem*`. Writing `01 00 00 01 00 00 00 01 01 04` to that port makes it re-enumerate as CDC-ECM Ethernet (PID `0x1657`), which appears as a new `enN` interface. The script writes only to a tty whose USB VID/PID match.
2. **Address.** The device's IPv6 link-local address is derived from its serial number: `('1'+serial) * 0x9e37fffffffc0001`, masked, with `fffe` inserted in the middle. For this unit it is `fe80::c8d3:84ff:fec2:323b`. It is used only on the network interface macOS created for the device: the I/O Registry lists that interface (e.g. `en8`) and its link status under the USB device. That's also how the errors below tell a missing interface, a link that is down, and a device that doesn't answer apart. With several QUADERNOs attached, the serial saved at pairing picks the right one.
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
.venv/bin/python -m pytest           # all offline tests, about 3 s
.venv/bin/python -m pytest tests/test_sync.py -k guards   # one file, or one test
```

| File | Covers |
|---|---|
| `tests/test_api.py` | the Python API used directly |
| `tests/test_fake.py` | CLI folder commands (put/get/mv/rm/mkdir) |
| `tests/test_sync.py` | sync: edits, moves, deletes, local changes, name clashes, dry run |
| `tests/test_patterns.py` | globs/regex in ls, get, mv, rm, open; rm confirmation |
| `tests/test_pair.py` | crypto wiring vs `/usr/bin/openssl`; pairing vs a simulated device |
| `tests/test_errors.py` | discovery states (stuck serial mode...), HTTP/connection errors, malformed answers, exit codes |

None of them needs the device: `tests/fakedevice.py` is a `quaderno.Device` with only the HTTP layer replaced, so the tests run the real API and CLI code. Two manual checks do need it, and pytest doesn't collect them:

```
.venv/bin/python tests/hw_replug_watch.py   # re-plug the device; the CLI must do the USB switch
zsh tests/hw_zsh_completion.zsh             # presses Tab in a real zsh against the device
```

## Code layout

```
quaderno/
  __init__.py     public API: connect, pair, Device, Entry, Event, PutResult, errors
  device.py       Device: ls/find/walk, get/put/upload, mkdir/move/rm, open, set_time, info, sync
  sync.py         one-way incremental sync (device -> Mac)
  client.py       HTTPS transport (kept-alive, certificate pinning) and login
  pairing.py      PIN pairing protocol
  crypto.py       RSA/AES via `cryptography`, HMAC, Diffie-Hellman, PEM/DER
  credentials.py  where credentials live; loading and saving them
  usb.py          finding the device on USB (`ioreg -a`), USB mode switch, address
  entry.py        Entry: a document or folder
  events.py       Event: progress reports from API operations
  errors.py       the exception hierarchy
  util.py         paths, patterns, time and size formatting
  cli.py          the `quaderno` command: argument parsing and printing only
completions/_quaderno   zsh Tab completion
tests/                  offline tests (fake device) and manual device checks
```

## Wi-Fi

If the device is on Wi-Fi, pass its address explicitly with `--addr 192.168.x.y`. This is untested here. dpt-rp1-py finds Fujitsu units on Wi-Fi by browsing the mDNS service `_dp_fujitsu._tcp`.

## If you also use the official QUADERNO app

`quaderno` never reads or changes the app's files, and the two can be used side by side:
- **Pairing:** `quaderno pair` adds a second client on the device. The app's own pairing keeps working; both were checked after pairing this unit. Moving over from the app takes one `quaderno pair`.
- **USB switch:** when the app is running, it switches the device to USB networking itself.
- **Clock:** the app sets the device clock each time it connects; `quaderno time` does the same on demand.
- **App errors such as `EADDRINUSE:8081` or status 0** come from the app's own local port forwarding. They don't affect the USB link or `quaderno`.

## Alternative: dpt-rp1-py

[dpt-rp1-py](https://github.com/janten/dpt-rp1-py) implements the same API and adds sync, Wi-Fi configuration and note templates. It requires `pycryptodome` (native code) and other packages, and it can't find the device over USB by itself. It should be able to use the same credentials, but the following command has not been tested here:

```
dptrp1 --client-id ~/.config/quaderno/deviceid.dat --key ~/.config/quaderno/privatekey.dat --addr "[$(quaderno addr)]" list-documents
```

## Errors and exit codes

Every failure is a `quaderno.QuadernoError`. The subclasses tell you what went wrong; scripts using the command get the same distinction through the exit code.

```
QuadernoError                    base: .status (HTTP status, when there is one)
├── ConnectError                 can't reach the device, or can't read the USB device list   exit 3
│   ├── DeviceNotFoundError      no QUADERNO on USB, or not the paired one
│   ├── SerialModeError          stuck in serial mode (see below)
│   └── DeviceUnreachableError   network mode but no interface, link down or silent; or the connection dropped
├── AuthError                                                                exit 4
│   ├── NotPairedError           no credentials
│   ├── LoginError               the device rejected this client's key
│   └── CertificateError         TLS certificate differs from the saved one
├── PairingError                                                             exit 4
│   └── WrongPinError
├── RequestError                 the device answered with an HTTP error: .status, .code   exit 1
├── ProtocolError                the device's answer is malformed (not JSON)            exit 1
├── NotFoundError                path doesn't exist, or a pattern matched nothing        exit 1
├── ExistsError                  target already exists                                   exit 1
└── NotEmptyError                folder not empty                                        exit 1
```

Exit code 2 is a command-line usage error, and 130 means you pressed Ctrl-C. Network failures in the middle of a command, such as the cable being pulled, become `DeviceUnreachableError`, not a Python traceback.

A malformed value that only affects what is shown or a file's date doesn't stop a command: an unreadable size or page count shows as missing, and `get` saves the file without the device's modification time. Both print a warning on stderr.

### The device stuck in serial mode

When the USB link restarts, the QUADERNO starts in serial mode; this happens when you plug it in or when the Mac wakes from sleep. `quaderno` then sends the command that switches it to USB networking. While the device is in standby, it can ignore that command.

`quaderno` re-sends the command every 3 seconds. If the device is still in serial mode after 15 seconds, it stops with:

```
quaderno: the QUADERNO is connected but stayed in USB serial mode: it ignored the command that
switches it to USB networking for 15 s. This happens when it is in standby, e.g. after the Mac
slept. Wake it with its power button, or unplug and re-plug it, then try again.
```

## Troubleshooting

- **`no QUADERNO on USB`**: run `ioreg -p IOUSB` and look for `FMVDP41`. If it's missing, check the cable: it must carry data, not only power.
- **`stayed in USB serial mode`**: wake the device or re-plug it, as described above.
- **`macOS lists no network interface for it`**: macOS didn't set up networking for the device after the switch. Unplug and re-plug it.
- **`USB network link (enN) is down`**: the interface exists but has no link. Wake or unlock the device; if that doesn't help, re-plug it.
- **`its link is up, but it doesn't answer`**: the network is fine but the device's server isn't responding. Wake or unlock the device.
- **`isn't the one these credentials were paired with`**: a different QUADERNO is plugged in. Plug in the paired one, or pair this one into its own folder with `quaderno --config DIR pair` and use `--config DIR` with it.
- **`couldn't read the list of USB devices with ioreg`**: macOS's `ioreg` failed or timed out; the message includes why. This is not the same as the device being absent.
- **`pairing failed: wrong PIN`**: run `pair` again. The device shows a new PIN for each attempt.
- **`device TLS certificate differs ...`**: this is expected after a firmware update; run `pair --force`. Otherwise, something other than your QUADERNO answered.
- **`the device rejected this client`**: the device no longer knows this client. Run `pair --force`.
