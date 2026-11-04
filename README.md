# quaderno.py

A command-line client for the Fujitsu QUADERNO (FMV-DP41, a rebadged Sony DPT-CP1/RP1) connected to a Mac over USB.
It is a single file that uses only the Python standard library: no venv, nothing to `pip install`, and no native code for Santa to block.
It pairs with the device by itself and does not need the QUADERNO PC App.

Tested against: QUADERNO serial 850000312345678, firmware 2.2.09.11030FP, Web API 2.1, macOS 27.2, Python 3.14. The full cycle (plug in, USB mode switch, pairing, login, file operations) works with the PC App closed.

## Install

It's set up the same way as `ytchan`: a pyproject package installed in editable mode into its own `.venv`, so changes to `quaderno.py` take effect immediately.

```
cd ~/quaderno
/opt/homebrew/opt/python@3.14/bin/python3.14 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/quaderno --help
```

There are no dependencies. `./quaderno.py` also works directly, without the venv. To run plain `quaderno` from anywhere, link it into a folder on your PATH. On this Mac that folder is `~/bin`:

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

To enable it, add to `~/.zshrc`. `fpath` must come before `compinit`:

```
fpath=(~/quaderno/completions $fpath)
autoload -Uz compinit && compinit
```

`compinit` turns on zsh's completion system for every command, not just this one. If your `~/.zshrc` already runs it, add only the `fpath` line, above it.

## Usage

```
./quaderno.py pair [--force] [--pin-file F]  pair with the device (PIN shown on its screen)
./quaderno.py ls [PATH] [-l] [-R]            list a folder (default: Document)
./quaderno.py get REMOTE... [-o DEST] [-r]   download documents; -r for whole folders
./quaderno.py put LOCAL... [-d DEST] [-r]    upload PDFs; -r for local directories
./quaderno.py mkdir REMOTE... [-p]           create folders; -p creates parents, ok if exists
./quaderno.py mv SRC... DST                  move/rename; several sources -> into folder DST
./quaderno.py rm REMOTE... [-r] [-y]         delete documents / empty folders; -r with contents
./quaderno.py sync LOCAL [REMOTE] [--delete] [-n]  mirror a device folder to the Mac (incremental)
./quaderno.py open DOC [-p PAGE]             open a document on the device's screen
./quaderno.py time                           set the device clock from the Mac
./quaderno.py info [--json]                  firmware, storage, battery, MAC, owner
./quaderno.py screenshot [OUT.png]           PNG of the current screen
./quaderno.py addr                           print the device address (switches USB mode if needed)
```

Global options:
- `--addr ADDR` (or `$QUADERNO_ADDR`) skips discovery.
- `--workspace DIR` uses the credentials in another directory.
- `-v` prints which credentials, address and TLS mode are in use.

### Paths and folders

The device has a single tree rooted at the folder `Document`. Remote paths are relative to it, so `foo.pdf`, `/foo.pdf` and `Document/foo.pdf` all refer to the same file. Folder names cannot contain `/`, and the device stores only PDFs.

```
./quaderno.py ls -l Note
./quaderno.py get "Note/Meeting Notes_20240506.pdf" -o ~/Downloads
./quaderno.py get -r Note -o ~/Backup                 # -> ~/Backup/Note/...
./quaderno.py put paper.pdf -d Books                  # into an existing folder
./quaderno.py put paper.pdf -d "Books/Better name.pdf"
./quaderno.py put -r ~/Papers -d Books                # -> Books/Papers/... (created as needed)
./quaderno.py put -r ~/Papers -d Books/Reading        # Reading is new -> it becomes the copy of ~/Papers
./quaderno.py mkdir -p Books/2026/Q4
./quaderno.py mv Books/2026 Archive/                  # into an existing folder
./quaderno.py mv Books/2026 Books/Twenty26            # rename
./quaderno.py rm -r Books/Old
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
- **What `-d` means depends on what exists.**
  - If `-d` names an existing folder, the files go into it.
  - If it names something that doesn't exist and ends in `.pdf`, that becomes the uploaded file's new name.
  - Otherwise `put` stops before uploading anything and suggests folders with that name, for example `did you mean: 'Books/Military History'`. Use the full path from the top (`-d 'Books/Military History'`), or create the folder with `mkdir`.
  - With `put -r` and a single folder, a `-d` folder that doesn't exist yet is created.
- **A failed upload leaves nothing behind.** If an upload fails partway, the half-created document is deleted from the device.
- **`put` skips what it can't upload and carries on.** Each file or folder you name that can't be uploaded gets a `skip` line on stderr: not a PDF, a folder without `-r`, or a file that doesn't exist. Everything else is still uploaded. So `put -r dir/* -d DEST` works even when `dir` mixes PDFs and EPUBs. Inside folders uploaded with `-r`, non-PDF files are left out silently and counted in the summary line (`… 2 non-PDF ignored`); hidden files are ignored and not counted. The exit status is 1 only if a file you named doesn't exist.
- **`rm` on a folder only deletes it when it's empty.** `rm -r` deletes the documents first, then the folders from the deepest up. The CLI never sends a delete for a folder that still has contents, because the device's behaviour in that case isn't documented. It refuses to delete or move the root folder.
- **`get -r` keeps the folder layout.** Partial downloads are written to `*.part` and renamed when complete.

## Sync (device → Mac)

```
./quaderno.py sync ~/QUADERNO                # whole device
./quaderno.py sync ~/QUADERNO-notes Note     # one folder
./quaderno.py sync -n ~/QUADERNO             # dry run: show what would happen
./quaderno.py sync --delete ~/QUADERNO       # also remove local copies of documents deleted on the device
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
- **`time`** sets the device clock to the Mac's current time. The PC App does this on every connection; without the app, run it now and then. The device clock can be set but not read.

  The device's timezone is separate from the clock. Yours is `UTC`, which matches Dublin in winter and is 1 hour behind in summer. `time` prints a note whenever the two differ. It doesn't change the timezone, because the device offers only 11 zones (UTC, Amsterdam, Helsinki, 6 US zones, Tokyo, Honolulu) and none of them follows Irish summer time.

## Pairing

```
./quaderno.py pair
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

Pairing adds a client on the device next to the PC App. The PC App's own pairing keeps working; both were checked after pairing this unit. `pair` refuses to replace an existing pairing unless you pass `--force`.

**Where credentials come from**, in order:
1. `--workspace DIR`
2. `~/.config/quaderno`
3. The PC App's pairing under `~/Library/Application Support/Fujitsu/QUADERNO PC App/QUADERNO_PCApp/ws*/`. This is only a fallback for a Mac that hasn't been paired with `pair`, and the files are only read.

## TLS

The device uses a self-signed certificate for `digitalpaper.local` (valid until 2035), and hands that same certificate over during pairing. With our own pairing, every HTTPS connection must present exactly that certificate. Anything else is refused with an error.

When the PC App's credentials are used, there is no stored certificate, so the connection is not verified. If a firmware update replaces the certificate, run `pair --force`.

## How the connection works

1. **USB mode switch.** On plug-in, the device appears as a CDC-ACM serial port: USB VID `0x04C5`, PID `0x1656`, `/dev/cu.usbmodem*`. Writing `01 00 00 01 00 00 00 01 01 04` to that port makes it re-enumerate as CDC-ECM Ethernet (PID `0x1657`), which appears as a new `enN` interface. The script writes only to a tty whose USB VID/PID match. If the PC App is running, it does this switch itself.
2. **Address.** The device's IPv6 link-local address is derived from its serial number: `('1'+serial) * 0x9e37fffffffc0001`, masked, with `fffe` inserted in the middle. For this unit it is `fe80::c8d3:84ff:fec2:323b%en8`. The script tries that address on every `en*` interface.
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

AES-128, RSA key generation and PKCS#1 signing are implemented in pure Python. They are checked against the FIPS-197 test vector and against the system's `/usr/bin/openssl`.

## Tests

```
python3 tests/test_fake.py     # folder commands against an in-memory fake of the device API
python3 tests/test_sync.py     # sync: edits, moves, deletes, local changes, name clashes, dry run
python3 tests/test_patterns.py # globs/regex in ls, get, mv, rm, open; rm confirmation
python3 tests/test_pair.py     # AES/RSA against FIPS-197 and openssl; pairing vs a simulated device
python3 tests/hw_replug_watch.py   # manual: re-plug the device; the CLI must do the USB switch
zsh tests/hw_zsh_completion.zsh    # manual: presses Tab in a real zsh against the device
```

The first three need no device. `tests/fakedevice.py` replaces only the HTTP layer, so the tests run the CLI's real code.

## Wi-Fi

If the device is on Wi-Fi, pass its address explicitly with `--addr 192.168.x.y`. This is untested here. dpt-rp1-py finds Fujitsu units on Wi-Fi by browsing the mDNS service `_dp_fujitsu._tcp`.

## Alternative: dpt-rp1-py

[dpt-rp1-py](https://github.com/janten/dpt-rp1-py) implements the same API and adds sync, Wi-Fi configuration and note templates. It requires `pycryptodome` (native code) and other packages, and it can't find the device over USB by itself. It should be able to use the same credentials, but the following command has not been tested here:

```
dptrp1 --client-id ~/.config/quaderno/deviceid.dat --key ~/.config/quaderno/privatekey.dat --addr "[$(./quaderno.py addr)]" list-documents
```

## Troubleshooting

- **`no QUADERNO found on USB`**: run `ioreg -p IOUSB` and look for `FMVDP41`. If it is missing, check the cable, which must carry data, not only power.
- **`did not answer on port 8443`**: wake or unlock the device. Running `ifconfig` should show an `enN` interface with an `fe80::` address.
- **`pairing failed: wrong PIN`**: run `pair` again. The device shows a new PIN for each attempt.
- **`device TLS certificate differs ...`**: this is expected after a firmware update; run `pair --force`. Otherwise, something other than your QUADERNO answered.
- **HTTP 401 on login**: the device no longer knows this client. Run `pair --force`.
- **PC App errors such as `EADDRINUSE:8081` or status 0**: these come from the app's own local port forwarding and affect only the app, not the USB link or this script.
