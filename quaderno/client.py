"""HTTPS transport and login for the device's Web API."""

import base64
import http.client
import json
import logging
import os
import ssl
import uuid
from urllib.parse import quote

from . import crypto
from .errors import (CertificateError, DeviceUnreachableError, LoginError, NotFoundError,
                     ProtocolError, QuadernoError, RequestError)
from .util import HTTPS_PORT

log = logging.getLogger("quaderno")
CHUNK = 8 * 1024 * 1024  # upload piece size (the one tested)
_STALE = (http.client.RemoteDisconnected, http.client.CannotSendRequest,
          http.client.ResponseNotReady, BrokenPipeError, ConnectionResetError)


def _unreachable(err):
    return DeviceUnreachableError(f"lost contact with the device ({err.__class__.__name__}: "
                                  f"{err}); is it still connected and awake?")


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a self-signed device: accept exactly the certificate obtained at
    pairing (when there is one); checked on every (re)connect."""

    def __init__(self, *args, pin=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pin = pin

    def connect(self):
        super().connect()
        if self._pin is not None and self.sock.getpeercert(binary_form=True) != self._pin:
            self.sock.close()
            raise CertificateError("device TLS certificate differs from the one received at "
                                   "pairing (devicecert.bin); if the firmware was updated, "
                                   "run `quaderno pair --force`")


class Client:
    """One device session: a kept-alive HTTPS connection plus the login cookie."""

    def __init__(self, addr, client_id, key, timeout=60, pinned_cert=None):
        self.addr = addr.strip("[]")
        self.client_id = client_id
        self.key = key
        self.timeout = timeout
        self.pinned_cert = pinned_cert
        self.cookies = {}
        self._conn = None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # self-signed; pinned_cert does the checking
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        self.ctx = ctx

    # --- connection ------------------------------------------------------------
    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _send(self, method, path, body, hdrs):
        """Send on the kept-alive connection; if that connection had gone stale,
        reconnect and send once more (a fresh connection is never retried)."""
        for _ in range(2):
            reused = self._conn is not None
            if not reused:
                self._conn = PinnedHTTPSConnection(self.addr, HTTPS_PORT, context=self.ctx,
                                                   timeout=self.timeout, pin=self.pinned_cert)
            try:
                self._conn.request(method, path, body=body, headers=hdrs)
                return self._conn.getresponse()
            except _STALE as err:
                self.close()
                if not reused:
                    raise _unreachable(err) from err
            except OSError as err:  # refused, timed out, interface gone...
                self.close()
                raise _unreachable(err) from err
            except BaseException:
                self.close()
                raise
        raise DeviceUnreachableError(f"{method} {path}: lost the connection to the device")

    def request(self, method, path, body=None, headers=None, ok=(200, 204), raw=False,
                stream_to=None):
        """Call the Web API. JSON bodies are encoded, JSON answers decoded; with
        `stream_to`, the response body is written there and its size returned."""
        hdrs = {"Host": f"digitalpaper.local:{HTTPS_PORT}", "Accept": "application/json"}
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        resp = self._send(method, path, body, hdrs)
        try:
            for h in resp.headers.get_all("Set-Cookie") or []:
                name, _, rest = h.partition("=")
                self.cookies[name.strip()] = rest.split(";", 1)[0]
            if resp.status not in ok:
                data = resp.read()
                code = None
                try:
                    err = json.loads(data)
                    code = err.get("error_code")
                    msg = f"{code} {err.get('message')}"
                except ValueError:
                    msg = data[:200].decode(errors="replace")
                cls = NotFoundError if resp.status == 404 else RequestError
                raise cls(f"{method} {path} -> HTTP {resp.status}: {msg}", resp.status, code)
            if stream_to is not None:
                total = 0
                while chunk := resp.read(1 << 20):
                    stream_to.write(chunk)
                    total += len(chunk)
                result = total
            else:
                data = resp.read()
                result = data if raw else (self._json(method, path, data) if data else None)
        except QuadernoError:
            if resp.will_close:
                self.close()
            raise
        except OSError as err:  # dropped while reading the answer
            self.close()
            raise _unreachable(err) from err
        except BaseException:
            self.close()  # body may be half-read: the connection can't be reused
            raise
        if resp.will_close:
            self.close()
        return result

    @staticmethod
    def _json(method, path, data):
        try:
            return json.loads(data)
        except ValueError as err:
            raise ProtocolError(f"{method} {path}: the device's answer isn't JSON ({err}): "
                                f"{data[:80]!r}") from err

    # --- login -----------------------------------------------------------------
    def authenticate(self):
        try:
            nonce = self.request("GET", "/auth/nonce/" + quote(self.client_id))["nonce"]
            sig = crypto.sign(self.key, nonce.encode("latin-1"))
            self.request("PUT", "/auth", {"client_id": self.client_id,
                                          "nonce_signed": base64.b64encode(sig).decode()})
        except (RequestError, NotFoundError) as err:  # 404: the device doesn't know this client
            if err.status in (400, 401, 403, 404):
                raise LoginError(f"the device rejected this client ({err}); it may have been "
                                 "unpaired on the device: run `quaderno pair --force`",
                                 err.status) from err
            raise

    # --- raw document transfer ---------------------------------------------------
    def download_to(self, doc_id, fh):
        return self.request("GET", f"/documents/{doc_id}/file", stream_to=fh,
                            headers={"Accept": "*/*"})

    def upload_file(self, local, folder_id, name):
        """Create a document `name` in folder `folder_id` with the contents of
        `local`; returns its id. A failed transfer deletes the half-made entry."""
        doc_id = self.request("POST", "/documents", {"parent_folder_id": folder_id,
                                                     "file_name": name,
                                                     "document_source": ""})["document_id"]
        try:
            self._send_file(local, doc_id)
            return doc_id
        except BaseException:
            try:
                self.request("DELETE", f"/documents/{doc_id}")
            except (QuadernoError, OSError):
                log.warning("warning: could not remove the incomplete upload %s", name)
            raise

    def _send_file(self, local, doc_id):
        size = os.path.getsize(local)
        with open(local, "rb") as fh:
            offset = 0
            while True:
                chunk = fh.read(CHUNK)
                boundary = uuid.uuid4().hex
                body = (f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="file"; filename="altair.pdf"\r\n'
                        f"Content-Type: application/octet-stream\r\n\r\n").encode() + chunk + \
                    f"\r\n--{boundary}--\r\n".encode()
                r = self.request(
                    "PUT", f"/documents/{doc_id}/file?offset_bytes={offset}&total_bytes={size}",
                    body, {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    ok=(200, 206))
                offset = int(r["current_bytes"])
                if offset >= size:
                    if r.get("completed") != "yes":
                        raise RequestError("upload finished but device did not mark it complete")
                    return
                if not chunk:
                    raise RequestError("upload stalled")
                fh.seek(offset)

    def create_folder(self, parent_id, name):
        return self.request("POST", "/folders", {"parent_folder_id": parent_id,
                                                 "folder_name": name})["folder_id"]
