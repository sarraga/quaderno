"""HTTPS transport and login for the device's Web API."""

import base64
import http.client
import json
import logging
import os
import ssl
import uuid
from collections.abc import Collection, Mapping
from typing import Any, BinaryIO, Self, cast
from urllib.parse import quote, urlencode

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from . import crypto
from .errors import (AuthError, CertificateError, DeviceUnreachableError, LockedError, LoginError,
                     NotFoundError, ProtocolError, QuadernoError, RequestError, StorageFullError)
from .models import FileUploadResult
from .util import HTTPS_PORT, JsonObject, json_int, json_str

log = logging.getLogger("quaderno")
CHUNK = 8 * 1024 * 1024  # upload piece size (the one tested)
MAX_JSON_BYTES = 32 * 1024 * 1024  # cap non-streaming JSON responses
MAX_ERR_BYTES = 65536
_STALE: tuple[type[BaseException], ...] = (
    http.client.RemoteDisconnected, http.client.CannotSendRequest,
    http.client.ResponseNotReady, BrokenPipeError, ConnectionResetError)

Body = Mapping[str, Any] | bytes | None  # any JSON mapping/TypedDict is sent as JSON; bytes raw


def _unreachable(err: BaseException) -> DeviceUnreachableError:
    return DeviceUnreachableError(f"lost contact with the device ({err.__class__.__name__}: "
                                  f"{err}); is it still connected and awake?")


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a self-signed device: accept exactly the certificate obtained at
    pairing (when there is one); checked on every (re)connect."""

    def __init__(self, host: str, port: int, *, context: ssl.SSLContext, timeout: float,
                 pin: bytes | None = None) -> None:
        super().__init__(host, port, context=context, timeout=timeout)
        self._pin = pin

    def connect(self) -> None:
        super().connect()
        if self._pin is None:
            return
        sock = self.sock
        if not isinstance(sock, ssl.SSLSocket) or sock.getpeercert(binary_form=True) != self._pin:
            sock.close()
            raise CertificateError("device TLS certificate differs from the one received at "
                                   "pairing (devicecert.bin); if the firmware was updated, "
                                   "run `quaderno pair --force`")


class Client:
    """One device session: a kept-alive HTTPS connection plus the login cookie."""

    def __init__(self, addr: str, client_id: str, key: RSAPrivateKey | None, timeout: float = 60,
                 pinned_cert: bytes | None = None) -> None:
        self.addr = addr.strip("[]")
        self.client_id = client_id
        self.key = key
        self.timeout = timeout
        self.pinned_cert = pinned_cert
        self.cookies: dict[str, str] = {}
        self._conn: PinnedHTTPSConnection | None = None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # self-signed; pinned_cert does the checking
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        self.ctx = ctx

    # --- connection ------------------------------------------------------------
    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _send(self, method: str, path: str, body: bytes | None,
              hdrs: dict[str, str]) -> http.client.HTTPResponse:
        """Send on the kept-alive connection; if that connection had gone stale,
        reconnect and send once more (a fresh connection is never retried)."""
        for _ in range(2):
            conn = self._conn
            reused = conn is not None
            if conn is None:
                conn = self._conn = PinnedHTTPSConnection(
                    self.addr, HTTPS_PORT, context=self.ctx, timeout=self.timeout,
                    pin=self.pinned_cert)
            try:
                conn.request(method, path, body=body, headers=hdrs)
                return conn.getresponse()
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

    def _request(self, method: str, path: str, body: Body = None,
                 headers: dict[str, str] | None = None, ok: Collection[int] = (200, 204),
                 stream_to: BinaryIO | None = None,
                 _reauth: bool = True) -> JsonObject | int | None:
        """One Web API call. A JSON object body is encoded; the answer is decoded
        (None if empty), or with `stream_to`, written there and its size returned.
        The typed entry points are request, request_json and request_stream."""
        hdrs = {"Host": f"digitalpaper.local:{HTTPS_PORT}", "Accept": "application/json"}
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        data: bytes | None
        if isinstance(body, Mapping):
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        else:
            data = body
        hdrs.update(headers or {})
        resp = self._send(method, path, data, hdrs)
        result: JsonObject | int | None
        reauth_needed = False
        try:
            for h in resp.headers.get_all("Set-Cookie") or []:
                name, _, rest = h.partition("=")
                self.cookies[name.strip()] = rest.split(";", 1)[0]
            if resp.status not in ok:
                err_data = resp.read(MAX_ERR_BYTES)
                if (resp.status == 401 and _reauth and self.key is not None
                        and self.cookies and not path.startswith("/auth")):
                    reauth_needed = True
                else:
                    raise self._http_error(method, path, resp.status, err_data)
            elif stream_to is not None:
                total = 0
                while chunk := resp.read(1 << 20):
                    stream_to.write(chunk)
                    total += len(chunk)
                result = total
            else:
                answer = resp.read(MAX_JSON_BYTES + 1)
                if len(answer) > MAX_JSON_BYTES:
                    self.close()
                    raise ProtocolError(f"{method} {path}: response exceeds {MAX_JSON_BYTES} bytes")
                result = self._json(method, path, answer) if answer else None
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
        if reauth_needed:
            self.authenticate()
            return self._request(method, path, body, headers, ok, stream_to, _reauth=False)
        return result

    def request(self, method: str, path: str, body: Body = None,
                headers: dict[str, str] | None = None,
                ok: Collection[int] = (200, 204)) -> JsonObject | None:
        """Call the Web API; the decoded JSON object, or None for an empty answer."""
        result = self._request(method, path, body, headers, ok)
        if isinstance(result, int):
            raise ProtocolError(f"{method} {path}: unexpected answer")
        return result

    def request_json(self, method: str, path: str, body: Body = None,
                     headers: dict[str, str] | None = None,
                     ok: Collection[int] = (200, 204)) -> JsonObject:
        """Like request, for calls whose answer must be a JSON object."""
        result = self.request(method, path, body, headers, ok)
        if result is None:
            raise ProtocolError(f"{method} {path}: the device's answer is empty")
        return result

    def request_stream(self, method: str, path: str, fh: BinaryIO,
                       headers: dict[str, str] | None = None,
                       byte_range: tuple[int, int | None] | None = None) -> int:
        """Write the answer's body to `fh`; returns its size in bytes.
        Optional `byte_range=(start, end)` sends an HTTP Range header."""
        hdrs = dict(headers or {})
        ok: tuple[int, ...] = (200, 204)
        if byte_range is not None:
            start, end = byte_range
            hdrs["Range"] = f"bytes={start}-{'' if end is None else end}"
            ok = (200, 204, 206)
        result = self._request(method, path, None, hdrs, ok, fh)
        if not isinstance(result, int):
            raise ProtocolError(f"{method} {path}: unexpected answer")
        return result

    @staticmethod
    def _http_error(method: str, path: str, status: int, data: bytes) -> QuadernoError:
        code = None
        try:
            err = json.loads(data)
        except ValueError:
            msg = data[:200].decode(errors="replace")
        else:
            if isinstance(err, dict):
                code = err.get("error_code")
                code = str(code) if code is not None else None
                msg = f"{code} {err.get('message')}"
            else:
                msg = str(err)[:200]
        text = f"{method} {path} -> HTTP {status}: {msg}"
        if status == 404:
            return NotFoundError(text, status, code)
        if status == 423:
            return LockedError(text, status, code)
        if status == 507:
            return StorageFullError(text, status, code)
        return RequestError(text, status, code)

    @staticmethod
    def _json(method: str, path: str, data: bytes) -> JsonObject:
        try:
            value = json.loads(data)
        except ValueError as err:
            raise ProtocolError(f"{method} {path}: the device's answer isn't JSON ({err}): "
                                f"{data[:80]!r}") from err
        if not isinstance(value, dict):
            raise ProtocolError(f"{method} {path}: the device's answer isn't a JSON object: "
                                f"{data[:80]!r}")
        return value

    # --- login -----------------------------------------------------------------
    def authenticate(self) -> None:
        if self.key is None:
            raise AuthError("no private key to log in with")
        try:
            nonce = json_str(self.request_json("GET", "/auth/nonce/" + quote(self.client_id)),
                             "nonce")
            sig = crypto.sign(self.key, nonce.encode("latin-1"))
            self.request("PUT", "/auth", {"client_id": self.client_id,
                                          "nonce_signed": base64.b64encode(sig).decode()})
        except (RequestError, NotFoundError) as err:  # 404: the device doesn't know this client
            if err.status in (400, 401, 403, 404):
                raise LoginError(f"the device rejected this client ({err}); it may have been "
                                 "unpaired on the device: run `quaderno pair --force`",
                                 err.status) from err
            raise

    # --- raw document & binary transfer ----------------------------------------
    def download_to(self, doc_id: str, fh: BinaryIO,
                    byte_range: tuple[int, int | None] | None = None) -> int:
        return self.request_stream("GET", f"/documents/{quote(doc_id, safe='')}/file", fh,
                                   headers={"Accept": "*/*"}, byte_range=byte_range)

    def download_annotation_to(self, doc_id: str, page: int, fh: BinaryIO,
                               byte_range: tuple[int, int | None] | None = None) -> int:
        return self.request_stream(
            "GET", f"/documents/{quote(doc_id, safe='')}/annotations/{int(page)}", fh,
            headers={"Accept": "*/*"}, byte_range=byte_range)

    def download_pastlog_to(self, fh: BinaryIO,
                            byte_range: tuple[int, int | None] | None = None) -> int:
        return self.request_stream("GET", "/system/controls/pastlog", fh,
                                   headers={"Accept": "*/*"}, byte_range=byte_range)

    def upload_file(self, local: str, folder_id: str, name: str,
                    ext_id: str | None = None) -> str:
        """Create a document `name` in folder `folder_id` with the contents of
        `local`; returns its id. Uses /documents2 when `ext_id` is given.
        A failed transfer deletes the half-made entry."""
        if ext_id is not None:
            doc_id = json_str(self.request_json("POST", "/documents2", {
                "parent_folder_id": folder_id, "file_name": name, "ext_id": ext_id}),
                "document_id")
        else:
            doc_id = json_str(self.request_json("POST", "/documents", {
                "parent_folder_id": folder_id, "file_name": name, "document_source": ""}),
                "document_id")
        try:
            self._send_file(local, doc_id)
            return doc_id
        except BaseException:
            try:
                self.request("DELETE", f"/documents/{quote(doc_id, safe='')}")
            except (QuadernoError, OSError):
                log.warning("warning: could not remove the incomplete upload %s", name)
            raise

    def replace_file(self, doc_id: str, local: str, *, target_revision: str | None = None,
                     file_hash: str | None = None) -> FileUploadResult:
        """Upload new PDF bytes into an existing document entry in place.
        Optional `target_revision` guards against concurrent edits (40017)."""
        return self._send_file(local, doc_id, target_revision=target_revision, file_hash=file_hash)

    def _send_chunked_multipart(self, path: str, local: str, *, filename: str = "altair.pdf",
                                extra_params: dict[str, str] | None = None) -> FileUploadResult:
        """Send `local` in CHUNK-sized multipart/form-data PUT requests with
        offset_bytes/total_bytes until the device reports completed='yes'."""
        size = os.path.getsize(local)
        with open(local, "rb") as fh:
            offset = 0
            while True:
                chunk = fh.read(CHUNK)
                boundary = uuid.uuid4().hex
                body = (f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                        f"Content-Type: application/octet-stream\r\n\r\n").encode() + chunk + \
                    f"\r\n--{boundary}--\r\n".encode()
                params: dict[str, str] = {"offset_bytes": str(offset), "total_bytes": str(size)}
                if extra_params:
                    params.update(extra_params)
                prev_offset = offset
                r = self.request_json(
                    "PUT", f"{path}?{urlencode(params)}", body,
                    {"Content-Type": f"multipart/form-data; boundary={boundary}"}, ok=(200, 206))
                offset = json_int(r, "current_bytes")
                if offset < 0 or offset > size:
                    raise ProtocolError(f"upload returned out-of-range offset {offset} (size {size})")
                if offset >= size:
                    if r.get("completed") != "yes":
                        raise RequestError("upload finished but device did not mark it complete")
                    return cast(FileUploadResult, r)
                if not chunk or offset <= prev_offset:
                    raise RequestError("upload stalled")
                fh.seek(offset)

    def _send_file(self, local: str, doc_id: str, *, target_revision: str | None = None,
                   file_hash: str | None = None) -> FileUploadResult:
        # The multipart filename is ignored: the document's name was set by the
        # POST in upload_file. A fixed ASCII value keeps arbitrary file names
        # (quotes, non-ASCII) out of a header the device has to parse.
        extra: dict[str, str] = {}
        if target_revision is not None:
            extra["target_revision"] = target_revision
        if file_hash is not None:
            extra["file_hash"] = file_hash
        return self._send_chunked_multipart(
            f"/documents/{quote(doc_id, safe='')}/file", local, extra_params=extra or None)

    def upload_note_template_file(self, template_id: str, local: str) -> FileUploadResult:
        """Upload PDF contents for a created note template."""
        return self._send_chunked_multipart(
            f"/viewer/configs/note_templates/{quote(template_id, safe='')}/file", local)

    def upload_firmware_file(self, local: str) -> FileUploadResult:
        """Upload a firmware update package (FwUpdater.pkg) in chunks."""
        return self._send_chunked_multipart(
            "/system/controls/update_firmware/file", local, filename="FwUpdater.pkg")

    def upload_custom_app(self, local: str, application_id: str, signature: str,
                          encrypted_key_crypto: str, iv: str) -> None:
        """Install a signed & encrypted custom app package (POST /custom_apps/install)."""
        with open(local, "rb") as fh:
            data = fh.read()
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="app.pkg"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + \
            f"\r\n--{boundary}--\r\n".encode()
        qs = urlencode({"application_id": application_id, "signature": signature,
                        "encrypted_key_crypto": encrypted_key_crypto, "iv": iv})
        self.request("POST", f"/custom_apps/install?{qs}", body,
                     {"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def create_folder(self, parent_id: str, name: str, ext_id: str | None = None) -> str:
        if ext_id is not None:
            return json_str(self.request_json("POST", "/folders2", {
                "parent_folder_id": parent_id, "folder_name": name, "ext_id": ext_id}),
                "folder_id")
        return json_str(self.request_json("POST", "/folders", {"parent_folder_id": parent_id,
                                                               "folder_name": name}),
                        "folder_id")
