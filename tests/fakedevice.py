"""In-memory fake of the QUADERNO Web API for offline tests.

Subclasses quaderno.Device and replaces only `_request`, so every CLI command runs
its real code paths (path resolution, pagination, chunked upload, downloads)."""
import re
import time
import uuid
from collections.abc import Collection, Mapping
from typing import Any, BinaryIO, Protocol, TypeVar, overload
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from quaderno.client import Body
from quaderno.device import Device
from quaderno.errors import NotFoundError, QuadernoError, RequestError
from quaderno.util import JsonObject, norm

FakeEntry = dict[str, Any]  # an entry as the device's API describes it
E = TypeVar("E", bound=BaseException)


@overload
def expect(fragment: str) -> pytest.RaisesExc[QuadernoError]: ...
@overload
def expect(fragment: str, exc: type[E]) -> pytest.RaisesExc[E]: ...


def expect(fragment: str, exc: type[BaseException] = QuadernoError) -> pytest.RaisesExc[Any]:
    """pytest.raises for `exc` (default QuadernoError) whose message contains
    `fragment` literally."""
    return pytest.raises(exc, match=re.escape(fragment))


class MakePdf(Protocol):
    """The mkpdf fixture: writes a small PDF under tmp_path, returns its path."""
    def __call__(self, rel: str, text: str = "x") -> str: ...


def _obj(body: Body) -> JsonObject:
    assert isinstance(body, Mapping), body
    return dict(body)


class FakeDevice(Device):
    def __init__(self) -> None:  # no network: Client.__init__ is not needed
        self.addr = "fake"
        self._conn = None
        self.pinned_cert = None
        self.cookies: dict[str, str] = {}
        self.e: dict[str, FakeEntry] = {
            "root": {"entry_id": "root", "entry_type": "folder", "entry_name": "Document",
                     "entry_path": "Document", "parent_folder_id": None}}
        self.files: dict[str, bytearray] = {}
        self.annotations: dict[tuple[str, int], bytes] = {}
        self.attributes: dict[str, JsonObject] = {}
        self.templates: dict[str, JsonObject] = {}
        self.template_files: dict[str, bytearray] = {}
        self.wifi_aps: dict[tuple[str, str], JsonObject] = {}
        self.ca_certs: list[JsonObject] = []
        self.client_certs: list[JsonObject] = []
        self.indications: dict[str, JsonObject] = {}
        self.pen_buttons_assigning: list[JsonObject] = [{"button_id": "side", "function": "eraser"}]
        self.firmware_buf = bytearray()
        self.actions: list[tuple[str, Any]] = []
        self.deletes: list[str] = []
        self.downloads = 0
        self.opened: list[tuple[str, int | None]] = []
        self.fail_upload = False  # make PUT /documents/{id}/file fail, to test cleanup
        self.clock = 1_700_000_000
        self.clock_set: str | None = None
        self._configs: dict[str, str] = {}

    # --- helpers for tests ---------------------------------------------------
    def _stamp(self, x: FakeEntry) -> None:
        self.clock += 60
        x["modified_date"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock))
        n = int(x.get("file_revision", "r.0.0").rsplit(".", 1)[1]) + 1
        x["file_revision"] = f"{x['entry_id'][:12]}.0.{n}"

    def _by_path(self, path: str) -> FakeEntry | None:
        path = norm(path)
        for x in self.e.values():
            if x["entry_path"] == path:
                return x
        return None

    def _at(self, path: str) -> FakeEntry:
        x = self._by_path(path)
        assert x is not None, path
        return x

    def mkdirs(self, path: str) -> str:
        path = norm(path)
        x = self._by_path(path)
        if x:
            return str(x["entry_id"])
        parent, name = path.rsplit("/", 1)
        return self._new("folder", self.mkdirs(parent), name)

    def put_doc(self, path: str, data: bytes) -> str:
        path = norm(path)
        parent, name = path.rsplit("/", 1)
        i = self._new("document", self.mkdirs(parent), name)
        self.edit_doc(path, data)
        return i

    def edit_doc(self, path: str, data: bytes) -> None:
        x = self._at(path)
        self.files[x["entry_id"]] = bytearray(data)
        x["file_size"] = str(len(data))
        self._stamp(x)

    def dev_move(self, path: str, new_path: str) -> None:
        x = self._at(path)
        parent, name = norm(new_path).rsplit("/", 1)
        x["parent_folder_id"] = self.mkdirs(parent)
        x["entry_name"] = name
        self._repath(x["entry_id"])

    def dev_delete(self, path: str) -> None:
        del self.e[self._at(path)["entry_id"]]

    def paths(self) -> list[str]:
        return sorted(x["entry_path"] for x in self.e.values())

    # --- fake API -------------------------------------------------------------
    def _new(self, kind: str, parent: str, name: str, ext_id: str | None = None) -> str:
        p = self.e[parent]
        path = p["entry_path"] + "/" + name
        if any(x["entry_path"] == path for x in self.e.values()):
            raise RequestError("exists", 400, "40007")
        i = uuid.uuid4().hex
        self.e[i] = {"entry_id": i, "entry_type": kind, "entry_name": name, "entry_path": path,
                     "parent_folder_id": parent}
        if ext_id is not None:
            self.e[i]["ext_id"] = ext_id
        if kind == "document":
            self.e[i]["file_size"] = "0"
            self._stamp(self.e[i])
        return i

    def _kids(self, fid: str) -> list[FakeEntry]:
        return [x for x in self.e.values() if x["parent_folder_id"] == fid]

    def _repath(self, i: str) -> None:
        x = self.e[i]
        x["entry_path"] = self.e[x["parent_folder_id"]]["entry_path"] + "/" + x["entry_name"]
        for c in self._kids(i):
            self._repath(c["entry_id"])

    def _write_stream(self, data: bytes, headers: dict[str, str] | None,
                      stream_to: BinaryIO | None) -> int:
        assert stream_to is not None
        rng = (headers or {}).get("Range")
        if rng and rng.startswith("bytes="):
            s_start, _, s_end = rng[6:].partition("-")
            start = int(s_start) if s_start else 0
            end = int(s_end) + 1 if s_end else len(data)
            data = data[start:end]
        stream_to.write(data)
        return len(data)

    @staticmethod
    def _extract_multipart(body: Body) -> bytes:
        assert isinstance(body, bytes)
        return body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]

    def _request(self, method: str, path: str, body: Body = None,
                 headers: dict[str, str] | None = None, ok: Collection[int] = (200, 204),
                 stream_to: BinaryIO | None = None,
                 _reauth: bool = True) -> JsonObject | int | None:
        u = urlsplit(path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if method == "GET" and p == "/ping":
            return None
        if method == "GET" and p.startswith("/resolve/entry/ext_id/"):
            want_ext = unquote(p[len("/resolve/entry/ext_id/"):])
            for x in self.e.values():
                if x.get("ext_id") == want_ext:
                    return dict(x)
            raise NotFoundError("404")
        if method == "GET" and p.startswith("/resolve/entry/path/"):
            want = unquote(p[len("/resolve/entry/path/"):])
            for x in self.e.values():
                if x["entry_path"] == want:
                    return dict(x)
            raise NotFoundError("404")
        if method == "GET" and p.startswith("/resolve/entry/"):
            want = unquote(p[len("/resolve/entry/"):])
            for x in self.e.values():
                if x["entry_path"] == want:
                    return dict(x)
            raise NotFoundError("404")
        m = re.fullmatch(r"/folders/([^/]+)/entries2?", p)
        if method == "GET" and m:
            kids = sorted(self._kids(m.group(1)), key=lambda x: str(x["entry_name"]))
            off, lim = int(qs.get("offset", 0)), int(qs.get("limit", 100))
            lim = min(lim, 2)  # force pagination
            return {"count": len(kids), "entry_list": [dict(x) for x in kids[off:off + lim]]}
        if method == "GET" and p in ("/documents", "/documents2"):
            kw = qs.get("search_keyword", "").casefold()
            docs = [x for x in self.e.values() if x["entry_type"] == "document"]
            if qs.get("entry_type") == "notes":
                docs = [x for x in docs if x.get("document_type") == "note"]
            origin = qs.get("origin_folder_id")
            if origin and origin in self.e:
                pref = str(self.e[origin]["entry_path"]) + "/"
                docs = [x for x in docs if str(x["entry_path"]).startswith(pref)]
            hits = sorted((x for x in docs if kw in str(x["entry_name"]).casefold()),
                          key=lambda x: str(x["entry_name"]))
            off, lim = int(qs.get("offset", 0)), int(qs.get("limit", 100))
            lim = min(lim, 2)  # force pagination, like /folders/{id}/entries
            fields = qs.get("fields", "").split(",") if qs.get("fields") else None
            page_items = []
            for x in hits[off:off + lim]:
                if fields:
                    d = {k: x[k] for k in fields if k in x}
                    d.setdefault("entry_id", x["entry_id"])
                    page_items.append(d)
                else:
                    page_items.append(dict(x))
            return {"count": len(hits), "entry_list": page_items}
        if method == "PUT" and p in ("/viewer/controls/open", "/viewer/controls/open2",
                                     "/viewer/controls/open2_confirm"):
            b = _obj(body)
            raw_page = b.get("page")
            if raw_page is not None:
                assert isinstance(raw_page, str), f"page must be str, got {raw_page!r}"
            self.opened.append((b["document_id"], int(raw_page) if raw_page is not None else None))
            self.actions.append((p, b))
            return None
        if method == "PUT" and p in ("/viewer/controls/transfer/documents_view",
                                     "/viewer/controls/transfer/folder_view"):
            self.actions.append((p, _obj(body)))
            return None
        if method == "GET" and p == "/viewer/status/current_viewing":
            cur = self.e.get(self.opened[-1][0]) if self.opened else None
            if cur is None:
                return {"views": []}
            doc_id, page = self.opened[-1]
            return {"orientation": "portlait", "view_mode": "single",
                    "views": [{"entry_id": doc_id, "entry_path": cur["entry_path"],
                               "current_page": str(page or 1), "total_page": "10",
                               "title": str(cur["entry_name"]).removesuffix(".pdf")}]}
        if method == "GET" and p == "/viewer/status/preset_marks":
            return {"pattern_list": [{"pattern": "star"}, {"pattern": "asterisk"}]}
        if p == "/viewer/configs/note_templates":
            if method == "GET":
                return {"template_list": list(self.templates.values())}
            if method == "POST":
                b = _obj(body)
                tid = uuid.uuid4().hex
                self.templates[tid] = {
                    "note_template_id": tid, "template_name": b["template_name"],
                    "is_modifiable": "true", "created_date": "2026-01-01T00:00:00Z"}
                return {"note_template_id": tid}
        m = re.fullmatch(r"/viewer/configs/note_templates/([^/]+)/file", p)
        if m and method == "PUT":
            data = self._extract_multipart(body)
            off, total = int(qs["offset_bytes"]), int(qs["total_bytes"])
            buf = self.template_files.setdefault(m.group(1), bytearray())
            assert len(buf) == off
            buf += data
            done = len(buf) >= total
            return {"current_bytes": str(len(buf)), "received_bytes": str(len(data)),
                    "completed": "yes" if done else "no"}
        m = re.fullmatch(r"/viewer/configs/note_templates/([^/]+)", p)
        if m:
            tid = m.group(1)
            if tid not in self.templates:
                raise NotFoundError("404")
            if method == "GET":
                return dict(self.templates[tid])
            if method == "PUT":
                self.templates[tid]["template_name"] = _obj(body)["template_name"]
                return None
            if method == "DELETE":
                del self.templates[tid]
                self.template_files.pop(tid, None)
                return None
        if method == "PUT" and p == "/system/configs/datetime":
            self.clock_set = _obj(body)["value"]
            return None
        if method == "GET" and p == "/system/configs/timezone":
            return {"value": self._configs.get("timezone", "UTC")}
        if p == "/system/configs/pen_buttons_function":
            if method == "GET":
                res: JsonObject = {}
                if qs.get("choices_only") != "true":
                    res["assigning"] = list(self.pen_buttons_assigning)
                if qs.get("assigning_only") != "true":
                    res["choices"] = {"button_id": ["side", "tail"],
                                      "function": ["eraser", "highlight", "none"]}
                return res
            if method == "PUT":
                self.pen_buttons_assigning = list(_obj(body)["assigning"])
                return None
        if p == "/system/configs/wifi_accesspoints" and method == "GET":
            return {"aplist": list(self.wifi_aps.values())}
        m = re.fullmatch(r"/system/configs/wifi_accesspoints/([^/]+)/([^/]+)", p)
        if m:
            key_ap = (unquote(m.group(1)), unquote(m.group(2)))
            if key_ap not in self.wifi_aps:
                raise NotFoundError("404")
            if method == "GET":
                return dict(self.wifi_aps[key_ap])
            if method == "DELETE":
                del self.wifi_aps[key_ap]
                return None
        if p == "/system/configs/certificates/ca":
            if method == "GET":
                return {"cert": list(self.ca_certs)}
            if method == "POST":
                self.ca_certs.append({"name": _obj(body)["name"]})
                return None
        if p == "/system/configs/certificates/client":
            if method == "GET":
                return {"cert": list(self.client_certs)}
            if method == "POST":
                self.client_certs.append({"name": _obj(body)["name"]})
                return None
        if p == "/system/configs/certificates" and method == "DELETE":
            self.ca_certs.clear()
            self.client_certs.clear()
            return None
        _config_defaults = {
            "owner": "tester", "timezone": "UTC", "date_format": "yyyy/M/d",
            "time_format": "24hour", "timeout_to_standby": "60", "initialized_flag": "initialized",
            "mtp": "0", "regulation_voltage": "4200", "use_mode": "personal",
            "annotations_permission": "yes", "additional_client_registration": "accept_all",
            "control_servers_editable": "no", "wifi": "on", "wifi_apmode": "off",
            "wifi_sleep_policy": "enabled_always",
        }
        if method == "GET" and p == "/system/configs":
            v1_keys = ("owner", "timezone", "date_format", "time_format",
                       "timeout_to_standby", "initialized_flag")
            return {k: {"value": self._configs.get(k, _config_defaults[k])} for k in v1_keys}
        if method == "GET" and p == "/system/configs2":
            return {
                "owner": {"value": self._configs.get("owner", _config_defaults["owner"])},
                "timezone": {"value": self._configs.get("timezone", _config_defaults["timezone"])},
                "date_format": {"value": self._configs.get("date_format", _config_defaults["date_format"])},
                "time_format": {"value": self._configs.get("time_format", _config_defaults["time_format"])},
                "timeout_to_sleep": {"value": self._configs.get("timeout_to_standby", _config_defaults["timeout_to_standby"])},
                "initialized_flag": {"value": self._configs.get("initialized_flag", _config_defaults["initialized_flag"])},
                "regulation_voltage": {"value": self._configs.get("regulation_voltage", _config_defaults["regulation_voltage"])},
                "use_mode": {"use_mode": self._configs.get("use_mode", _config_defaults["use_mode"])},
            }
        if method == "GET" and p == "/system/configs3":
            return {
                "owner": {"value": self._configs.get("owner", _config_defaults["owner"])},
                "timezone": {"value": self._configs.get("timezone", _config_defaults["timezone"])},
                "date_format": {"value": self._configs.get("date_format", _config_defaults["date_format"])},
                "time_format": {"value": self._configs.get("time_format", _config_defaults["time_format"])},
                "timeout_to_sleep": {"value": self._configs.get("timeout_to_standby", _config_defaults["timeout_to_standby"])},
                "initialized_flag": {"value": self._configs.get("initialized_flag", _config_defaults["initialized_flag"])},
                "mtp": {"value": self._configs.get("mtp", _config_defaults["mtp"])},
                "regulation_voltage": {"value": self._configs.get("regulation_voltage", _config_defaults["regulation_voltage"])},
                "use_mode": {"use_mode": self._configs.get("use_mode", _config_defaults["use_mode"])},
                "annotations_permission": {"deletable": self._configs.get("annotations_permission", _config_defaults["annotations_permission"])},
                "additional_client_registration": {"value": self._configs.get("additional_client_registration", _config_defaults["additional_client_registration"])},
                "control_servers_editable": {"value": self._configs.get("control_servers_editable", _config_defaults["control_servers_editable"])},
                "pen_buttons_function": {"assigning": list(self.pen_buttons_assigning)},
            }
        m = re.fullmatch(r"/system/configs/([^/]+)", p)
        if m and p != "/system/configs/datetime":
            key = m.group(1)
            wire = {"use_mode": "use_mode", "annotations_permission": "deletable"}.get(key, "value")
            if method == "PUT":
                self._configs[key] = _obj(body)[wire]
                return None
            if method == "GET":
                if key in self._configs or key in _config_defaults:
                    return {wire: self._configs.get(key, _config_defaults.get(key, ""))}
                raise NotFoundError("404")
        if method == "GET" and p in ("/system/controls/screen_shot", "/system/controls/screen_shot2"):
            img = b"\xff\xd8\xffFAKEJPG" if qs.get("format") == "jpeg" else b"\x89PNGFAKEPNG"
            return self._write_stream(img, headers, stream_to)
        if method == "GET" and p == "/system/controls/pastlog":
            return self._write_stream(b"ENCRYPTED-PASTLOG", headers, stream_to)
        if method == "POST" and p == "/system/controls/wifi_accesspoints/scan":
            return {"aplist": [{"ssid": "HomeWiFi", "security": "psk", "frequency_band": "5ghz",
                                "rssi_level": "-55", "state": "none"}]}
        if method == "PUT" and p == "/system/controls/wifi_accesspoints/register":
            b = _obj(body)
            self.wifi_aps[(b["ssid"], b["security"])] = dict(b)
            return None
        if method == "GET" and p == "/system/controls/update_firmware/precheck":
            return {"battery": "ok", "image_file": "ok" if self.firmware_buf else "ng"}
        if method == "PUT" and p == "/system/controls/update_firmware/file":
            data = self._extract_multipart(body)
            off, total = int(qs["offset_bytes"]), int(qs["total_bytes"])
            assert len(self.firmware_buf) == off
            self.firmware_buf += data
            done = len(self.firmware_buf) >= total
            return {"current_bytes": str(len(self.firmware_buf)),
                    "received_bytes": str(len(data)), "completed": "yes" if done else "no"}
        if method == "POST" and p == "/system/controls/indicate":
            iid = uuid.uuid4().hex
            self.indications[iid] = _obj(body)
            return {"value": iid}
        m = re.fullmatch(r"/system/controls/indicate/([^/]+)", p)
        if m:
            iid = m.group(1)
            if method == "PUT":
                self.indications[iid] = _obj(body)
                return None
            if method == "DELETE":
                self.indications.pop(iid, None)
                return None
        if method == "PUT" and p in (
            "/system/controls/sleep", "/system/controls/run", "/system/controls/reboot",
            "/system/controls/shutdown", "/system/controls/reset_configs",
            "/system/controls/clear_documents", "/system/controls/abort_document_process",
            "/system/controls/update_firmware", "/system/controls/transfer/wizard",
            "/system/controls/customize_ux", "/system/controls/customize_setting_ux",
            "/notify/login_result", "/notify/logout_result", "/notify/force_assigned",
        ):
            self.actions.append((p, body if body is not None else qs))
            if p == "/system/controls/clear_documents":
                self.e = {"root": self.e["root"]}
                self.files.clear()
            return None
        if method == "POST" and p == "/custom_apps/install":
            self.actions.append((p, qs))
            return None
        status = {"/api_version": {"value": "2.1"},
                  "/system/status/firmware_version": {"value": "fake"},
                  "/system/status/storage": {"capacity": "1000", "available": "400"},
                  "/system/configs/owner": {"value": "tester"},
                  "/system/status/mac_address": {"value": "00:00:00:00:00:00"},
                  "/system/status/wifi_state": {"ssid": "HomeWiFi", "security": "psk", "state": "connected"},
                  "/system/status/document_process": {"status": "idle", "progress": "0"}}
        if method == "GET" and p in status:
            return status[p]
        if method == "GET" and p == "/system/status/battery":
            raise QuadernoError("GET /system/status/battery -> HTTP 404: no battery", 404)
        if method == "POST" and p in ("/folders", "/folders2"):
            b = _obj(body)
            return {"folder_id": self._new("folder", b["parent_folder_id"], b["folder_name"],
                                           ext_id=b.get("ext_id"))}
        if method == "POST" and p in ("/documents", "/documents2"):
            b = _obj(body)
            if not b["file_name"].lower().endswith(".pdf"):  # what the real device answers
                raise RequestError("POST /documents -> HTTP 400: 40006 The value of a parameter "
                                   "is invalid format. Bad parameter : fileName(unsupported)",
                                   400, "40006")
            return {"document_id": self._new("document", b["parent_folder_id"], b["file_name"],
                                             ext_id=b.get("ext_id"))}
        m = re.fullmatch(r"/documents/([^/]+)/copy", p)
        if m and method == "POST":
            b = _obj(body)
            nid = self._new("document", b["parent_folder_id"], b["file_name"])
            self.files[nid] = bytearray(self.files.get(m.group(1), bytearray()))
            self.e[nid]["file_size"] = str(len(self.files[nid]))
            return {"document_id": nid}
        m = re.fullmatch(r"/documents/([^/]+)/annotations/([0-9]+)", p)
        if m and method == "GET":
            ann_data = self.annotations.get((m.group(1), int(m.group(2))))
            if ann_data is None:
                raise NotFoundError("404")
            return self._write_stream(ann_data, headers, stream_to)
        m = re.fullmatch(r"/documents/([^/]+)/attribute", p)
        if m:
            did = m.group(1)
            if method == "GET":
                return dict(self.attributes.get(did, {"action_entry": [], "action_performer": []}))
            if method == "PUT":
                self.attributes[did] = dict(_obj(body))
                return None
            if method == "DELETE":
                self.attributes.pop(did, None)
                return None
        m = re.fullmatch(r"/documents/([^/]+)/file", p)
        if m and method == "PUT":
            if self.fail_upload:
                raise QuadernoError("PUT -> HTTP 500: simulated failure", 500)
            did = m.group(1)
            x = self.e[did]
            target_rev = qs.get("target_revision")
            if target_rev is not None and target_rev != x.get("file_revision"):
                raise RequestError("revision mismatch", 400, "40017")
            data = self._extract_multipart(body)
            off, total = int(qs["offset_bytes"]), int(qs["total_bytes"])
            if off == 0:
                self.files[did] = bytearray()
            buf = self.files.setdefault(did, bytearray())
            assert len(buf) == off
            buf += data
            x["file_size"] = str(len(buf))
            done = len(buf) >= total
            if done:
                self._stamp(x)
            return {"current_bytes": str(len(buf)), "received_bytes": str(len(data)),
                    "completed": "yes" if done else "no",
                    "file_revision": str(x.get("file_revision", ""))}
        if m and method == "GET":
            self.downloads += 1
            return self._write_stream(bytes(self.files[m.group(1)]), headers, stream_to)
        m = re.fullmatch(r"/(documents2?|folders2?)/([^/]+)", p)
        if m and method == "DELETE":
            i = m.group(2)
            if m.group(1).startswith("folders") and self._kids(i):
                force = isinstance(body, dict) and body.get("force_delete_flag") == "true"
                if not force:
                    raise RequestError("folder not empty", 400, "40018")
                # recursive delete
                stack = [i]
                to_del: list[str] = []
                while stack:
                    cur_id = stack.pop()
                    to_del.append(cur_id)
                    stack.extend(str(c["entry_id"]) for c in self._kids(cur_id))
                for nid in to_del:
                    self.deletes.append(self.e[nid]["entry_path"])
                    del self.e[nid]
                    self.files.pop(nid, None)
                return None
            if m.group(1).startswith("documents") and isinstance(body, dict):
                tr = body.get("target_revision")
                if tr is not None and tr != self.e[i].get("file_revision"):
                    raise RequestError("revision mismatch", 400, "40017")
            self.deletes.append(self.e[i]["entry_path"])
            del self.e[i]
            return None
        if m and method == "PUT":
            b = _obj(body)
            x = self.e[m.group(2)]
            if "parent_folder_id" in b:
                x["parent_folder_id"] = b["parent_folder_id"]
            if "file_name" in b or "folder_name" in b:
                x["entry_name"] = b.get("file_name") or b.get("folder_name")
            if "ext_id" in b:
                x["ext_id"] = b["ext_id"]
            self._repath(x["entry_id"])
            return None
        if m and method == "GET":
            return dict(self.e[m.group(2)])
        raise AssertionError(f"unhandled {method} {path}")
