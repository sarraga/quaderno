"""The Python API used directly (no CLI), against the in-memory fake device."""
import datetime
from pathlib import Path

import pytest
from fakedevice import FakeDevice, MakePdf, expect

import quaderno
from quaderno import (Entry, Event, ExistsError, NotEmptyError, NotFoundError, PutResult,
                      QuadernoError)


@pytest.fixture
def d() -> FakeDevice:
    d = FakeDevice()
    d.put_doc("Books/a.pdf", b"A")
    d.put_doc("Books/Old/b.pdf", b"B")
    d.put_doc("Note/n.pdf", b"N")
    return d


def test_public_surface() -> None:
    assert callable(quaderno.connect) and callable(quaderno.pair) and quaderno.__version__
    assert issubclass(NotFoundError, QuadernoError)


def test_lookup(d: FakeDevice) -> None:
    a = d.resolve("Books/a.pdf")
    assert isinstance(a, Entry) and a.is_document and a.relpath == "Books/a.pdf" and a.size == 1
    assert a.revision and a.mtime and a.parent_id == d.resolve("Books").id
    assert d.try_resolve("nope.pdf") is None
    with pytest.raises(NotFoundError):
        d.resolve("nope.pdf")
    assert [e.name for e in d.entries("Books")] == ["Old", "a.pdf"]
    assert [e.relpath for e in d.walk("Books")] == ["Books/Old", "Books/Old/b.pdf", "Books/a.pdf"]
    found, by_pattern = d.find("*/*.pdf")
    assert by_pattern and [e.relpath for e in found] == ["Books/a.pdf", "Note/n.pdf"]
    rows, by_pattern = d.ls("Books")
    assert not by_pattern and [e.name for e in rows] == ["Old", "a.pdf"]  # folders first
    with pytest.raises(NotFoundError):
        d.find("zz*")


def test_upload_and_put(d: FakeDevice, mkpdf: MakePdf, tmp_path: Path) -> None:
    events: list[Event] = []
    e = d.upload(mkpdf("c.pdf"), "Books", on_event=events.append)
    assert e.path == "Document/Books/c.pdf" and events[-1].kind == "uploaded"
    with pytest.raises(ExistsError):                                     # never overwrites
        d.upload(mkpdf("c.pdf"), "Books")
    res = d.put([mkpdf("c.pdf"), mkpdf("d.pdf"), str(tmp_path / "missing.pdf")], "Books",
                on_event=events.append)
    assert res == PutResult(uploaded=1, skipped=1, missing=1), res
    assert sorted(ev.kind for ev in events[-3:]) == ["skipped", "skipped", "uploaded"]
    assert any(ev.warning and "missing.pdf: no such file" in ev.message for ev in events)
    assert all(ev.message for ev in events)
    mkpdf("tree/x.pdf")
    mkpdf("tree/sub/y.pdf")
    (tmp_path / "tree" / "notes.txt").write_text("t")
    res = d.put(str(tmp_path / "tree"), "Books", recursive=True)
    assert res.uploaded == 2 and res.ignored == 1 and "Document/Books/tree/sub/y.pdf" in d.paths()


def test_get(d: FakeDevice, tmp_path: Path) -> None:
    copy = tmp_path / "a-copy.pdf"
    assert d.get("Books/a.pdf", str(copy)) == 1
    assert copy.read_bytes() == b"A"
    assert int(copy.stat().st_mtime) == d.resolve("Books/a.pdf").mtime
    out = tmp_path / "out"
    assert d.get("Books/Old", str(out), recursive=True) == 1 and (out / "Old" / "b.pdf").exists()
    with pytest.raises(QuadernoError, match="is a folder"):              # folder without recursive
        d.get("Books/Old", str(out))


def test_mkdir_and_move(d: FakeDevice) -> None:
    events: list[Event] = []
    d.put_doc("Books/c.pdf", b"C")
    d.put_doc("Books/d.pdf", b"D")
    d.mkdir("Archive/2026", parents=True, on_event=events.append)
    assert d.resolve("Archive/2026").is_folder
    moved = d.move("Books/*.pdf", "Archive/2026", on_event=events.append)
    assert sorted(old for old, new in moved) == [
        "Document/Books/a.pdf", "Document/Books/c.pdf", "Document/Books/d.pdf"]
    assert all(new.startswith("Document/Archive/2026/") for old, new in moved)
    assert d.move("Archive/2026/a.pdf", "Archive/2026/renamed.pdf") == [
        ("Document/Archive/2026/a.pdf", "Document/Archive/2026/renamed.pdf")]
    assert {"created", "moved"} <= {ev.kind for ev in events}


def test_move_refuses_name_clashes_before_moving_anything(d: FakeDevice) -> None:
    d.put_doc("Archive/a.pdf", b"other A")
    d.put_doc("Note/c.pdf", b"C")
    before = d.paths()
    with pytest.raises(ExistsError, match="Document/Archive/a.pdf already exists"):
        d.move(["Note/c.pdf", "Books/a.pdf"], "Archive")       # c.pdf alone would be fine
    assert d.paths() == before
    d.put_doc("Note/Old/b.pdf", b"other B")
    with pytest.raises(ExistsError, match="cannot move both"):  # two sources, one name
        d.move(["Books/Old/b.pdf", "Note/Old/b.pdf"], "Note")
    with pytest.raises(QuadernoError, match="into itself"):     # also checked up front
        d.move(["Note/c.pdf", "Books"], "Books/Old")
    assert d.paths() == sorted(before + ["Document/Note/Old", "Document/Note/Old/b.pdf"])


def test_rm(d: FakeDevice) -> None:
    for n in "xyz":
        d.put_doc(f"Archive/2026/{n}.pdf", n.encode())
    with pytest.raises(NotEmptyError):                                   # not recursive
        d.rm("Books/Old")
    before = d.paths()
    assert d.rm("Archive/2026/*", confirm=lambda plan: False) == [] and d.paths() == before
    seen: list[Entry] = []
    events: list[Event] = []

    def confirm(plan: list[Entry]) -> bool:
        seen.extend(plan)
        return True
    deleted = d.rm("Archive/2026/*", confirm=confirm, on_event=events.append)
    assert len(deleted) == 3 and len(seen) == 3 and not d.entries("Archive/2026")
    assert "deleted" in {ev.kind for ev in events}
    assert d.rm("Books/Old", recursive=True)[0].relpath == "Books/Old"   # literal: no confirm needed
    with pytest.raises(QuadernoError, match="root"):
        d.rm("/", recursive=True)


def test_device_controls(d: FakeDevice) -> None:
    opened = d.open("Note/n*", page=3)
    assert opened.relpath == "Note/n.pdf" and d.opened[-1] == (opened.id, 3)
    when = datetime.datetime(2026, 11, 7, 12, 30, 5, tzinfo=datetime.timezone.utc)
    assert d.set_time(when) == "2026-11-07T12:30:05Z" == d.clock_set
    assert d.timezone() == "UTC"
    info = d.info()
    assert info["firmware_version"] == {"value": "fake"} and "error" in info["battery"]


def test_search(d: FakeDevice) -> None:
    d.put_doc("Books/Rust in Action.pdf", b"R")
    d.put_doc("Note/rusty notes.pdf", b"N")
    hits = d.search("rust")                                    # case-insensitive, across folders
    assert all(isinstance(e, Entry) for e in hits)
    assert sorted(e.relpath for e in hits) == ["Books/Rust in Action.pdf", "Note/rusty notes.pdf"]
    assert d.search("nothing-here") == []
    for i in range(3):
        d.put_doc(f"Books/match{i}.pdf", b"x")                 # 3 hits > page size: paginated
    assert [e.name for e in d.search("match")] == ["match0.pdf", "match1.pdf", "match2.pdf"]
    assert [e.name for e in d.search("match", limit=2)] == ["match0.pdf", "match1.pdf"]


def test_copy(d: FakeDevice) -> None:
    events: list[Event] = []
    copied = d.copy("Books/a.pdf", "Note", on_event=events.append)          # into a folder
    assert copied == [("Document/Books/a.pdf", "Document/Note/a.pdf")]
    assert d.resolve("Note/a.pdf").size == d.resolve("Books/a.pdf").size and d.downloads == 0
    assert events[-1].kind == "copied"
    assert d.copy("Books/a.pdf", "Books/a-copy.pdf")[0][1] == "Document/Books/a-copy.pdf"  # rename
    with expect("already exists", ExistsError):
        d.copy("Books/a.pdf", "Note")                                       # Note/a.pdf exists now
    with expect("only documents"):
        d.copy("Books", "Note")                                             # folders can't be copied
    with expect("must be an existing folder"):
        d.copy(["Books/a.pdf", "Note/n.pdf"], "Note/new")                   # 2 sources, missing dst


def test_viewing(d: FakeDevice) -> None:
    assert d.current_viewing()["views"] == []                  # nothing open yet
    e = d.open("Books/a.pdf", page=4)
    view = d.current_viewing()["views"][0]
    assert view["entry_path"] == e.path and view["current_page"] == "4"


def test_config(d: FakeDevice) -> None:
    cfg = d.configs()
    assert cfg["timezone"] == "UTC" and cfg["timeout_to_standby"] == "60"
    assert cfg["use_mode"] == "personal" and cfg["annotations_permission"] == "yes"
    assert cfg["mtp"] == "0"
    d.set_config("timeout_to_standby", "300")
    assert d.get_config("timeout_to_standby") == "300" and d.configs()["timeout_to_standby"] == "300"
    d.set_config("use_mode", "shared")
    assert d.get_config("use_mode") == "shared"
    d.set_config("annotations_permission", "no")
    assert d.get_config("annotations_permission") == "no"
    assert "timeout_to_sleep" in d.configs_v2() and "pen_buttons_function" in d.configs_v3()
    with expect("can't be set"):
        d.set_config("firmware_version", "9")


def test_v2_documents_folders_and_in_place_replace(d: FakeDevice, mkpdf: MakePdf,
                                                   tmp_path: Path) -> None:
    fid = d.create_folder(d.resolve("Books").id, "Synced", ext_id="ext-dir-1")
    assert d.resolve_ext_id("ext-dir-1").id == fid
    assert d.resolve("Books/Synced", v2=True).ext_id == "ext-dir-1"
    doc_id = d.upload_file(mkpdf("v2.pdf", "v1-body"), fid, "v2.pdf", ext_id="ext-doc-1")
    e = d.get_document(doc_id, v2=True)
    assert e.ext_id == "ext-doc-1" and e.name == "v2.pdf"
    assert [x.id for x in d.entries("Books/Synced", v2=True)] == [doc_id]
    d.update_document(doc_id, ext_id="ext-doc-2")
    assert d.resolve_ext_id("ext-doc-2").id == doc_id
    d.update_folder(fid, ext_id="ext-dir-2")
    assert d.get_folder(fid, v2=True).ext_id == "ext-dir-2"
    old_rev = e.revision
    res = d.replace_document(e, mkpdf("v2new.pdf", "v2-body"), target_revision=old_rev)
    assert res["completed"] == "yes" and res["file_revision"] != old_rev
    with pytest.raises(quaderno.RequestError) as mismatch:
        d.replace_document(e, mkpdf("v2bad.pdf", "bad"), target_revision=old_rev)
    assert mismatch.value.code == "40017"
    out = tmp_path / "partial.bin"
    with open(out, "wb") as fh:
        n = d.download_to(doc_id, fh, byte_range=(0, 3))
    assert n == 4 and out.read_bytes() == b"%PDF"
    d.delete_document(doc_id, target_revision=str(res["file_revision"]))
    assert d.try_resolve("Books/Synced/v2.pdf") is None
    d.put_doc("Books/Synced/inner.pdf", b"X")
    with pytest.raises(quaderno.RequestError) as ne:
        d.delete_folder(fid, force=False)
    assert ne.value.code == "40018"
    d.delete_folder(fid, force=True)
    assert d.try_resolve("Books/Synced") is None


def test_annotations_attributes_and_search_options(d: FakeDevice, tmp_path: Path) -> None:
    a = d.resolve("Books/a.pdf")
    d.annotations[(a.id, 1)] = b"\x01\x02\x03\x04"
    ann_file = tmp_path / "p1.bin"
    assert d.download_annotation(a, 1, str(ann_file)) == 4
    assert ann_file.read_bytes() == b"\x01\x02\x03\x04"
    attr: quaderno.DocumentAttribute = {
        "action_entry": [{"index": "1", "by_user": {"caption": "Submit"}, "performers": ["p1"]}],
        "action_performer": [{"index": "p1", "request": {"url": "https://example.invalid"}}],
    }
    d.set_document_attribute(a, attr)
    assert d.get_document_attribute(a) == attr
    d.delete_document_attribute(a)
    assert d.get_document_attribute(a).get("action_entry") == []
    hits = d.search(".pdf", origin_folder="Books", fields=["entry_id", "entry_name"], v2=True)
    assert sorted(h.name for h in hits) == ["a.pdf", "b.pdf"]


def test_viewer_templates_and_marks(d: FakeDevice, mkpdf: MakePdf) -> None:
    assert d.preset_marks() == ["star", "asterisk"]
    d.open_confirm("Books/a.pdf", "Open?", "Proceed?", "Yes", "No", page=2)
    assert d.opened[-1] == (d.resolve("Books/a.pdf").id, 2)
    assert d.current_viewing()["orientation"] == "portlait"
    d.show_documents_view("thumbnail", "modified_date_desc")
    d.show_folder_view("Books", "list", "entry_name_asc")
    tid = d.upload_note_template(mkpdf("grid.pdf", "grid"), name="Grid")
    assert d.get_note_template(tid)["template_name"] == "Grid"
    assert len(d.note_templates()) == 1
    d.rename_note_template(tid, "Dot Grid")
    assert d.get_note_template(tid)["template_name"] == "Dot Grid"
    d.delete_note_template(tid)
    assert d.note_templates() == []


def test_wifi_certs_pen_power_firmware_and_misc(d: FakeDevice, tmp_path: Path) -> None:
    d.ping()
    assert d.wifi_state()["state"] == "connected"
    assert d.wifi_scan()[0]["ssid"] == "HomeWiFi"
    d.add_wifi_accesspoint(config=quaderno.WifiApConfig(
        ssid="Office", security="psk", passwd="secret", proxy="false"))
    assert d.get_wifi_accesspoint("Office", "psk")["ssid"] == "Office"
    assert len(d.wifi_accesspoints()) == 1
    d.delete_wifi_accesspoint("Office", "psk")
    assert d.wifi_accesspoints() == []
    d.install_ca_certificate("CorpCA", "ca.pem", b"PEM")
    d.install_client_certificate("Me", "me.p12", b"P12", password="pw")
    assert d.ca_certificates() == [{"name": "CorpCA"}]
    assert d.client_certificates() == [{"name": "Me"}]
    d.clear_certificates()
    assert d.ca_certificates() == [] and d.client_certificates() == []
    assert "choices" in d.pen_buttons()
    d.set_pen_buttons([{"button_id": "side", "function": "highlight"}])
    assert d.pen_buttons(assigning_only=True)["assigning"][0]["function"] == "highlight"
    jpg = tmp_path / "shot.jpg"
    png = tmp_path / "shot.png"
    d.screenshot(str(jpg))
    d.screenshot(str(png))
    assert jpg.read_bytes().startswith(b"\xff\xd8\xff")
    assert png.read_bytes().startswith(b"\x89PNG")
    logf = tmp_path / "pastlog.bin"
    assert d.download_pastlog(str(logf)) == len(b"ENCRYPTED-PASTLOG")
    assert d.document_process()["status"] == "idle"
    d.abort_document_process("c1")
    ind = d.indicate({"title": "Hi", "message": "Syncing", "button_caption": "OK"}, show_animation=True)
    assert ind is not None and isinstance(ind["value"], str)
    d.update_indication(ind["value"], {"title": "Hi", "message": "Done", "button_caption": "OK"})
    d.dismiss_indication(ind["value"])
    d.transfer_wizard(["w1"], next_screen={"documents_view": {"view_type": "list"}})
    d.customize_ux({"home": []})
    d.customize_setting_ux({"settings": []})
    d.notify_login_result(True)
    d.notify_logout_result(False)
    d.notify_force_assigned("1")
    pkg = tmp_path / "FwUpdater.pkg"
    pkg.write_bytes(b"DPUP" + b"\x00" * 32)
    assert d.firmware_precheck()["image_file"] == "ng"
    assert d.upload_firmware(str(pkg))["completed"] == "yes"
    assert d.firmware_precheck()["image_file"] == "ok"
    d.start_firmware_update()
    d.install_custom_app(str(pkg), "MyApp", "sig", "key", "iv")
    d.sleep()
    d.wake()
    d.reboot()
    d.shutdown()
    d.reset_configs()
    d.clear_documents()
    assert d.paths() == ["Document"]
