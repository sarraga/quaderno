# quaderno.py

A command-line client for the Fujitsu QUADERNO (FMV-DP41, a rebadged Sony DPT-CP1/RP1) connected to a Mac over USB.
It is a single file that uses only the Python standard library: no venv, nothing to `pip install`, and no native code for Santa to block.
It pairs with the device by itself and does not need the QUADERNO PC App.

Tested against: QUADERNO serial 850000312345678, firmware 2.2.09.11030FP, Web API 2.1, macOS 27.2, Python 3.14. The full cycle (plug in, USB mode switch, pairing, login, file operations) works with the PC App closed.

## Usage

```
./quaderno.py pair [--force] [--pin-file F]  pair with the device (PIN shown on its screen)
./quaderno.py ls [PATH] [-l] [-R]            list a folder (default: Document)
./quaderno.py get REMOTE... [-o DEST] [-r]   download documents; -r for whole folders
./quaderno.py put LOCAL... [-d DEST] [-r]    upload PDFs; -r for local directories
./quaderno.py mkdir REMOTE... [-p]           create folders; -p creates parents, ok if exists
./quaderno.py mv SRC DST                     move or rename a document or folder
./quaderno.py rm REMOTE... [-r]              delete documents / empty folders; -r with contents
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

How the commands behave:
- **`put` never overwrites.** If a document with the same name already exists in the target folder, `put` skips it and prints `skip ...` on stderr. This makes `put -r` safe to re-run: it uploads only what's missing. Non-PDF files and hidden files are ignored. A remote folder is created only where at least one PDF will go.
- **`rm` on a folder only deletes it when it's empty.** `rm -r` deletes the documents first, then the folders from the deepest up. The CLI never sends a delete for a folder that still has contents, because the device's behaviour in that case isn't documented. It refuses to delete or move the root folder.
- **`get -r` keeps the folder layout.** Partial downloads are written to `*.part` and renamed when complete.

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
