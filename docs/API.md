# QUADERNO Web API Reference (API `2.1`)

Reference for the Web API of the Fujitsu QUADERNO Gen 2 / Gen 3C (firmware `2.2.09.11030FP`), mapped to the Python API (`quaderno.Device`, `quaderno.models`) and CLI (`quaderno`).

## 1. Discovery, Ports & Wire Conventions

- **mDNS / Bonjour**: `_dp_fujitsu._tcp.`, instance prefix `Digital Paper `, port `8080`, hostname `digitalpaper.local`.
- **USB transport (CDC-ACM → CDC-ECM)**: On plug-in the device enumerates as USB VID `0x04C5`, PID `0x1656` (CDC-ACM serial). Writing `01 00 00 01 00 00 00 01 01 04` switches it to PID `0x1657` (CDC-ECM Ethernet), reachable at link-local IPv6 `fe80::...` derived from its serial number (`usb.ipv6_from_serial()`).
  - **macOS**: Serial port `/dev/cu.usbmodem*`, Ethernet interface `enN` (e.g. `en8`), discovered via `ioreg -a -r -c IOUSBHostDevice -l`. No OS setup required.
  - **Linux**: Serial port `/dev/ttyACM*` (needs `uaccess` ACL or `dialout` group), Ethernet interface `ethN`/`usbN` renamed to `quaderno0` by [`quaderno/linux/60-quaderno.rules`](../quaderno/linux/60-quaderno.rules) and configured with IPv6 link-local only by [`quaderno/linux/quaderno.nmconnection`](../quaderno/linux/quaderno.nmconnection) (installed via `quaderno linux-setup | sh`). Discovered via `/sys/bus/usb/devices` and `/sys/class/net/<iface>/carrier`; `usb.discover()` waits through the ~1–2 s sysfs re-enumeration gap after leaving `0x1656`.
- **JSON style**: All wire keys are `snake_case`, and null fields are left out. Every struct in [`quaderno/models.py`](../quaderno/models.py) describes one request or response body.
- **Timestamps**: UTC ISO-8601 `YYYY-MM-DDTHH:MM:SSZ`.
- **Session cookie (`Credentials`)**: Issued by `PUT /auth`. Expires after **30 minutes idle** or **24 hours total**. `quaderno.Client` automatically re-authenticates once on HTTP `401`.
- **Binary transfers**: Downloads support HTTP `Range` (`206 Partial Content`); uploads use chunked `multipart/form-data` PUT requests with `offset_bytes` and `total_bytes` until `{"completed": "yes"}`.

| Port | Protocol | Purpose | Access |
|---:|---|---|---|
| `8080` | HTTP | Pre-auth & registration | Restricted to `/api_version` and `/register/*`. |
| `8443` | HTTPS | Main client API | Self-signed device cert (`digitalpaper.local`); requires `Credentials` cookie except `/auth/*`. |
| `8444` | HTTPS | Factory testmode (`/testmode/*`) | Requires factory testmode credentials. |
| `8445` | HTTPS | Enterprise kitting (`/kitting/*`) | Requires enterprise kitting credentials. |
| `9090` | HTTPS | SetupTool special mode | Only active in setup special mode. |

---

## 2. Endpoint Reference

### Version, Health & Auth

| Method | Path | Request / Response Struct | Python API | CLI |
|---|---|---|---|---|
| `GET` | `/api_version` | `-> CommonValue` (`{"value": "2.1"}`) | `dev.info()["api_version"]` | `quaderno info` |
| `GET` | `/ping` | `-> 204` | `dev.ping()` | `quaderno ping` |
| `GET` | `/auth/nonce/{client_id}` | `-> NonceResult` (TTL 5 min) | `dev.authenticate()` | *(automatic)* |
| `PUT` | `/auth` | `{client_id, nonce_signed} -> 204` + cookie | `dev.authenticate()` | *(automatic)* |

### Registration (HTTP Port `8080`)

| Method | Path | Request / Response Struct | Python API | CLI |
|---|---|---|---|---|
| `GET` | `/register/information` | `-> DeviceInformation` | `dev.device_information()` | `quaderno pair` |
| `GET` | `/register/serial_number` | `-> CommonValue` | `dev.serial_number()` | — |
| `POST` | `/register/pin` | `-> RegisterMessage` | `pairing.register(...)` | `quaderno pair` |
| `POST` | `/register/hash` | `RegisterMessage -> RegisterMessage` | `pairing.register(...)` | `quaderno pair` |
| `POST` | `/register/ca` | `RegisterMessage -> RegisterMessage` | `pairing.register(...)` | `quaderno pair` |
| `POST` | `/register` | `RegisterMessage -> 204` | `pairing.register(...)` | `quaderno pair` |
| `PUT` | `/register/cleanup` | `-> 204` | `pairing.pair(...)` | `quaderno pair` |
| `POST` | `/register/dcss{1..4}` | `RegisterMessage` | Enterprise DCSS pairing | — |

### Documents & Folders (v1 & v2)

| Method | Path | Query / Body | Response | Python API | CLI |
|---|---|---|---|---|---|
| `GET` | `/resolve/entry/{path}` | — | `EntryDict` | `dev.resolve(path)` | *(all path cmds)* |
| `GET` | `/resolve/entry/path/{path}` | — | `EntryDict` | `dev.resolve(path, v2=True)` | — |
| `GET` | `/resolve/entry/ext_id/{ext_id}` | — | `EntryDict` | `dev.resolve_ext_id(ext_id)` | — |
| `GET` | `/documents` | `entry_type`, `search_target`, `annotation_type`, `search_keyword`, `origin_folder_id`, `order_type`, `offset`, `limit`, `fields` | `EntryListResult` | `dev.search(...)` | `quaderno search` |
| `GET` | `/documents2` | Same as `GET /documents` | `EntryListResult` | `dev.search(..., v2=True)` | — |
| `POST` | `/documents` | `{parent_folder_id, file_name, document_source}` | `DocumentIdResult` | `dev.upload_file(...)` | `quaderno put` |
| `POST` | `/documents2` | `{parent_folder_id, file_name, ext_id}` | `DocumentIdResult` | `dev.upload_file(..., ext_id=...)` | — |
| `GET` | `/documents/{id}` | — | `EntryDict` | `dev.get_document(id)` | — |
| `GET` | `/documents2/{id}` | — | `EntryDict` | `dev.get_document(id, v2=True)` | — |
| `PUT` | `/documents/{id}` | `{parent_folder_id, file_name}` | `204` | `dev.move(...)` / `dev.update_document(...)` | `quaderno mv` |
| `PUT` | `/documents2/{id}` | `{parent_folder_id, file_name, ext_id}` | `204` | `dev.update_document(..., ext_id=...)` | — |
| `DELETE` | `/documents/{id}` | optional `{target_revision}` | `204` | `dev.rm(...)` / `dev.delete_document(...)` | `quaderno rm` |
| `GET` | `/documents/{id}/file` | optional `Range` header | `application/pdf` | `dev.get(...)` / `dev.download_to(...)` | `quaderno get` |
| `PUT` | `/documents/{id}/file` | `offset_bytes`, `total_bytes`, `target_revision`, `file_hash` + multipart | `FileUploadResult` | `dev.replace_document(...)` | `quaderno replace` |
| `POST` | `/documents/{id}/copy` | `{parent_folder_id, file_name}` | `DocumentIdResult` | `dev.copy(...)` | `quaderno cp` |
| `GET` | `/documents/{id}/annotations/{page}` | optional `Range` header | `application/octet-stream` | `dev.download_annotation(...)` | `quaderno annotations` |
| `GET` | `/documents/{id}/attribute` | — | `DocumentAttribute` | `dev.get_document_attribute(doc)` | — |
| `PUT` | `/documents/{id}/attribute` | `DocumentAttribute` | `204` | `dev.set_document_attribute(doc, attr)` | — |
| `DELETE` | `/documents/{id}/attribute` | — | `204` | `dev.delete_document_attribute(doc)` | — |
| `POST` | `/folders` | `{parent_folder_id, folder_name}` | `FolderIdResult` | `dev.mkdir(...)` / `dev.create_folder(...)` | `quaderno mkdir` |
| `POST` | `/folders2` | `{parent_folder_id, folder_name, ext_id}` | `FolderIdResult` | `dev.create_folder(..., ext_id=...)` | — |
| `GET` | `/folders/{id}` | — | `EntryDict` | `dev.get_folder(id)` | — |
| `GET` | `/folders2/{id}` | — | `EntryDict` | `dev.get_folder(id, v2=True)` | — |
| `PUT` | `/folders/{id}` | `{parent_folder_id, folder_name}` | `204` | `dev.move(...)` / `dev.update_folder(...)` | `quaderno mv` |
| `PUT` | `/folders2/{id}` | `{parent_folder_id, folder_name, ext_id}` | `204` | `dev.update_folder(..., ext_id=...)` | — |
| `DELETE` | `/folders/{id}` | optional `{force_delete_flag: "true"\|"false"}` | `204` | `dev.rm(...)` / `dev.delete_folder(...)` | `quaderno rm` |
| `GET` | `/folders/{id}/entries` | `order_type`, `offset`, `limit`, `fields` | `EntryListResult` | `dev.entries(...)` / `dev.ls(...)` | `quaderno ls` |
| `GET` | `/folders/{id}/entries2` | `order_type`, `offset`, `limit`, `fields` | `EntryListResult` | `dev.entries(..., v2=True)` | — |

### Viewer & Note Templates

| Method | Path | Request / Response Struct | Python API | CLI |
|---|---|---|---|---|
| `GET` | `/viewer/status/current_viewing` | `-> CurrentViewing` (`orientation`: `"portlait"\|"landscape"`) | `dev.current_viewing()` | `quaderno viewing` |
| `GET` | `/viewer/status/preset_marks` | `-> PresetMarksResult` | `dev.preset_marks()` | `quaderno marks` |
| `PUT` | `/viewer/controls/open` | `ViewerOpenRequest -> 204` | `dev.open(path)` | `quaderno open` |
| `PUT` | `/viewer/controls/open2` | `ViewerOpen2Request -> 204` (`page` is string) | `dev.open(path, page=N)` | `quaderno open -p N` |
| `PUT` | `/viewer/controls/open2_confirm` | `ViewerOpenConfirmRequest -> 204` | `dev.open_confirm(...)` | — |
| `PUT` | `/viewer/controls/transfer/documents_view` | `DocumentsViewInfo -> 204` | `dev.show_documents_view(...)` | — |
| `PUT` | `/viewer/controls/transfer/folder_view` | `FolderViewInfo -> 204` | `dev.show_folder_view(...)` | — |
| `GET` | `/viewer/configs/note_templates` | `-> NoteTemplateListResult` | `dev.note_templates()` | `quaderno templates ls` |
| `POST` | `/viewer/configs/note_templates` | `{template_name} -> NoteTemplateIdResult` | `dev.upload_note_template(...)` | `quaderno templates add` |
| `GET` | `/viewer/configs/note_templates/{id}` | `-> NoteTemplate` | `dev.get_note_template(id)` | — |
| `PUT` | `/viewer/configs/note_templates/{id}` | `{template_name} -> 204` | `dev.rename_note_template(id, name)` | `quaderno templates rename` |
| `DELETE` | `/viewer/configs/note_templates/{id}` | `-> 204` | `dev.delete_note_template(id)` | `quaderno templates rm` |
| `PUT` | `/viewer/configs/note_templates/{id}/file` | multipart + `offset_bytes`/`total_bytes` `-> FileUploadResult` | `dev.upload_note_template(...)` | `quaderno templates add` |

### System Status, Settings, Wi-Fi, Pen Buttons & Certificates

| Method | Path | Request / Response Struct | Python API | CLI |
|---|---|---|---|---|
| `GET` | `/system/status/battery` | `-> BatteryStatus` | `dev.info()["battery"]` | `quaderno info` |
| `GET` | `/system/status/storage` | `-> StorageStatus` | `dev.info()["storage"]` | `quaderno info` |
| `GET` | `/system/status/firmware_version` | `-> FirmwareVersion` | `dev.info()["firmware_version"]` | `quaderno info` |
| `GET` | `/system/status/mac_address` | `-> CommonValue` | `dev.info()["mac_address"]` | `quaderno info` |
| `GET` | `/system/status/wifi_state` | `-> WifiApInfo` | `dev.wifi_state()` | `quaderno wifi status` |
| `GET` | `/system/status/document_process` | `-> DocumentProcessStatus` | `dev.document_process()` | — |
| `GET` | `/system/configs` | `-> ConfigsV1` | `dev.configs()` (fallback) | `quaderno config` |
| `GET` | `/system/configs2` | `-> ConfigsV2` | `dev.configs_v2()` | — |
| `GET` | `/system/configs3` | `-> ConfigsV3` | `dev.configs()` / `dev.configs_v3()` | `quaderno config [--all]` |
| `GET`/`PUT` | `/system/configs/{key}` | `CommonValue` (`owner`, `timezone`, `date_format`, `time_format`, `timeout_to_standby`, `mtp`, `regulation_voltage`, `additional_client_registration`, `control_servers_editable`, `initialized_flag`, `wifi`, `wifi_apmode`, `wifi_sleep_policy`) | `dev.get_config(k)` / `dev.set_config(k, v)` | `quaderno config KEY [VAL]` |
| `PUT` | `/system/configs/datetime` | `CommonValue` (ISO-8601 UTC) | `dev.set_time(...)` | `quaderno time` |
| `GET`/`PUT` | `/system/configs/use_mode` | `UseModeConfig` (`personal`\|`shared`\|`others`) | `dev.get_config("use_mode")` / `set_config` | `quaderno config use_mode [V]` |
| `GET`/`PUT` | `/system/configs/annotations_permission` | `AnnotationsPermissionConfig` (`yes`\|`no`) | `dev.get_config("annotations_permission")` / `set_config` | `quaderno config annotations_permission [V]` |
| `GET`/`PUT` | `/system/configs/pen_buttons_function` | `PenButtonsInfo` / `PenButtonsConfig` | `dev.pen_buttons(...)` / `dev.set_pen_buttons(...)` | `quaderno pen-buttons {ls,set}` |
| `POST` | `/system/controls/wifi_accesspoints/scan` | `-> WifiApScanList` | `dev.wifi_scan()` | `quaderno wifi scan` |
| `PUT` | `/system/controls/wifi_accesspoints/register` | `WifiApConfig -> 204` | `dev.add_wifi_accesspoint(...)` | `quaderno wifi add` |
| `GET` | `/system/configs/wifi_accesspoints` | `-> WifiApConfigList` | `dev.wifi_accesspoints()` | `quaderno wifi ls` |
| `GET`/`DELETE` | `/system/configs/wifi_accesspoints/{ssid}/{security}` | `-> WifiApConfig` / `204` | `dev.get_wifi_accesspoint(...)` / `delete_wifi_accesspoint(...)` | `quaderno wifi rm` |
| `GET`/`POST` | `/system/configs/certificates/ca` | `CertificateList` / `CaCertificateUpload` | `dev.ca_certificates()` / `install_ca_certificate(...)` | `quaderno certs {ls,add-ca}` |
| `GET`/`POST` | `/system/configs/certificates/client` | `CertificateList` / `ClientCertificateUpload` | `dev.client_certificates()` / `install_client_certificate(...)` | `quaderno certs {ls,add-client}` |
| `DELETE` | `/system/configs/certificates` | `-> 204` | `dev.clear_certificates()` | `quaderno certs clear --yes` |

### System Controls, Firmware Update, Notifications & Custom Apps

| Method | Path | Request / Response | Python API | CLI |
|---|---|---|---|---|
| `GET` | `/system/controls/screen_shot` | `-> image/png` | `dev.screenshot(path)` | `quaderno screenshot` |
| `GET` | `/system/controls/screen_shot2` | `?format=png\|jpeg -> image/*` | `dev.screenshot(path, fmt="jpeg")` | `quaderno screenshot --format jpeg` |
| `PUT` | `/system/controls/{sleep,run,reboot,shutdown}` | `-> 204` | `dev.sleep()` / `wake()` / `reboot()` / `shutdown()` | `quaderno power {sleep,wake,reboot,shutdown}` |
| `PUT` | `/system/controls/reset_configs` | `-> 204` | `dev.reset_configs()` | — |
| `PUT` | `/system/controls/clear_documents` | `-> 204` | `dev.clear_documents()` | — |
| `PUT` | `/system/controls/abort_document_process` | `?content_id=... -> 204` | `dev.abort_document_process(...)` | — |
| `GET` | `/system/controls/pastlog` | optional `Range` `-> application/octet-stream` | `dev.download_pastlog(path)` | `quaderno pastlog` |
| `POST` | `/system/controls/indicate` | `IndicationInfo -> CommonValue` | `dev.indicate(...)` | — |
| `PUT`/`DELETE` | `/system/controls/indicate/{id}` | `IndicationInfo -> 204` / `204` | `dev.update_indication(...)` / `dismiss_indication(...)` | — |
| `PUT` | `/system/controls/transfer/wizard` | `TransferWizardInfo -> 204` | `dev.transfer_wizard(...)` | — |
| `PUT` | `/system/controls/customize_ux` | JSON `-> 204` | `dev.customize_ux(...)` | — |
| `PUT` | `/system/controls/customize_setting_ux` | JSON `-> 204` | `dev.customize_setting_ux(...)` | — |
| `GET` | `/system/controls/update_firmware/precheck` | `-> UpdateFirmwarePrecheck` | `dev.firmware_precheck()` | `quaderno firmware precheck` |
| `PUT` | `/system/controls/update_firmware/file` | multipart + `offset_bytes`/`total_bytes` `-> FileUploadResult` | `dev.upload_firmware(pkg)` | `quaderno firmware upload` |
| `PUT` | `/system/controls/update_firmware` | `-> 204` | `dev.start_firmware_update()` | `quaderno firmware update --yes` |
| `PUT` | `/notify/{login_result,logout_result,force_assigned}` | `CommonValue -> 204` | `dev.notify_login_result(...)` / `logout_result(...)` / `force_assigned(...)` | — |
| `POST` | `/custom_apps/install` | `?application_id&signature&encrypted_key_crypto&iv` + multipart `-> 204` | `dev.install_custom_app(...)` | — |

### Abolished Endpoints (`410 Gone`)

These routes exist but always return HTTP `410` (`error_code: "41000"`):
- `GET/PUT /system/configs/current_pen_set`, `GET /system/configs/pen_sets`, `GET/PUT /system/configs/pen_grip_style`
- `GET/PUT /system/configs/wifi_frequency_band`
- `GET /system/status/wps_state`, `PUT /system/controls/wps_start/{button,pin}`, `PUT /system/controls/wps_cancel`

---

## 3. Device Error Codes

Errors return `ErrorResult` (`{"error_code": "40007", "message": "..."}`). `quaderno.Client` raises `NotFoundError` (`404`), `LockedError` (`423`), `StorageFullError` (`507`), or `RequestError` (all other 4xx/5xx) with `.status` and `.code`.

| HTTP | Code | Meaning |
|---:|---|---|
| 400 | `40000`–`40006` | Invalid JSON (`40000`), missing required param (`40001`), non-numeric (`40002`), bad Base64 (`40003`), bad ISO-8601 (`40004`), undefined enum value (`40005`), invalid format (`40006`) |
| 400 | `40007`–`40018` | Name conflict (`40007`), resource limit (`40008`), non-updatable (`40009`), file missing (`40010`), partial upload mismatch (`40011`), nonexistent (`40012`), unsupported/unopenable/uneditable/unsized file (`40013`–`40016`), revision mismatch (`40017`), folder not empty (`40018`) |
| 401 | `40100` | Authentication required (expired/missing `Credentials` cookie; retried automatically once by `Client`) |
| 403 | `40300`–`40308` | Permission denied (`40300`), bad pairing params / wrong PIN (`40301`), low battery (`40302`), insufficient resources (`40303`), Wi-Fi off (`40304`), app closed (`40305`), UUID mismatch (`40306`), invalid state (`40307`), password protected (`40308`) |
| 404 | `40400`–`40402` | Path not found (`40400`), resource not found (`40401`), temporarily unavailable (`40402`) |
| 405 / 408 / 410 | `40500` / `40800` / `41000` | Method not allowed / Request timeout / Abolished |
| 411 / 413 / 415 | `41100` / `41300` / `41500` | Content-Length required / Payload too large / Unsupported Media Type |
| 423 | `42300` | Resource locked (e.g., document currently open in viewer) → `LockedError` |
| 500 / 501 / 503 | `50000`, `50095`–`50099`, `50100`, `50300`–`50301` | Internal error / upload failed / not implemented / unavailable or collision |
| 507 | `50700`–`50701` | Insufficient device storage → `StorageFullError` |
