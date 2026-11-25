"""TypedDict structs and literal enums for the device's JSON requests and responses.

Every struct in this module describes one JSON body of the QUADERNO Web API. On the
wire, keys are ``snake_case`` and null fields are left out.

Because all structs are ``TypedDict`` subclasses, they are plain ``dict`` objects at
runtime (working seamlessly with ``json.dumps`` and dictionary access) while giving
callers full ``mypy --strict`` checking and IDE field completion.
"""

from typing import Literal, Required, TypedDict

# --- Literal enums ----------------------------------------------------------------

EntryType = Literal["document", "folder"]
DocumentType = Literal["normal", "note"]
SearchTarget = Literal["document_meta", "document_body", "annotation_pattern"]
SearchEntryType = Literal["all", "documents", "notes"]
AnnotationType = Literal["all", "highlights", "handwritings"]
OrderType = Literal[
    "created_date_asc", "created_date_desc",
    "modified_date_asc", "modified_date_desc",
    "entry_name_asc", "entry_name_desc",
    "reading_date_asc", "reading_date_desc",
    "title_asc", "title_desc",
    "author_asc", "author_desc",
    "file_size_asc", "file_size_desc",
]
ViewType = Literal["list", "thumbnail"]
# Note: the device literally spells portrait as "portlait".
Orientation = Literal["portlait", "landscape"]
ViewMode = Literal["single", "double"]
DisplayMode = Literal["single", "two_page_spread", "thumbnail"]
ViewPosition = Literal["left", "right"]

WifiSecurity = Literal["nonsec", "psk", "eap"]
EapMethod = Literal["peap", "tls"]
EapPhase2 = Literal["none", "pap", "mschap", "mschapv2", "gtc"]
WifiApState = Literal["connected", "connecting", "disabled", "none", "saved"]
WifiSleepPolicy = Literal["disabled", "enabled_always", "enabled_if_plugged"]
UseMode = Literal["personal", "shared", "others"]
RegistrationPolicy = Literal["accept_all", "accept_dcss_only", "refuse"]
TimeFormat = Literal["12hour", "24hour"]
ScreenshotFormat = Literal["png", "jpeg"]
YesNo = Literal["yes", "no"]
OnOff = Literal["on", "off"]
OkNg = Literal["ok", "ng"]
SuccessFailure = Literal["success", "failure"]
TrueFalse = Literal["true", "false"]


# --- Common & registration models -------------------------------------------------

class CommonValue(TypedDict):
    """``CommonValueObject`` — single string value wrapper."""
    value: str


class ErrorResult(TypedDict):
    """``ErrorResult`` — error payload returned on HTTP 4xx/5xx responses."""
    error_code: str
    message: str


class NonceResult(TypedDict):
    """``CommonNonceObjectResult`` — Base64 challenge nonce."""
    nonce: str


class RegisterMessage(TypedDict, total=False):
    """``CommonRegisterObject`` — pairing / DCSS handshake message."""
    a: str
    b: str
    c: str
    d: str
    e: str


class DeviceInformation(TypedDict, total=False):
    """``DeviceInformationResult`` — returned by ``GET /register/information``."""
    model_name: str
    serial_number: str
    sku_code: str
    device_color: str


class FileUploadResult(TypedDict, total=False):
    """``CommonFileUploadResult`` — returned by chunked file upload PUT endpoints."""
    completed: str       # "yes" or "no"
    current_bytes: str
    received_bytes: str
    file_revision: str


# --- Documents & folders ----------------------------------------------------------

class EntryDict(TypedDict, total=False):
    """``CommonEntryObjectResult`` / ``CommonEntryObjectResult2``."""
    entry_id: Required[str]
    entry_name: str
    entry_path: str
    entry_type: str
    parent_folder_id: str | None
    document_type: str
    document_source: str
    ext_id: str
    title: str
    author: str
    mime_type: str
    file_size: str
    file_hash: str
    file_revision: str
    current_page: str
    total_page: str
    is_new: str
    created_date: str
    modified_date: str
    reading_date: str


class EntryListResult(TypedDict, total=False):
    """``CommonEntryListObjectResult`` / ``CommonEntryListObjectResult2``."""
    count: int
    entry_list: list[EntryDict]
    entry_list_hash: str


class DocumentIdResult(TypedDict):
    """``CommonDocumentIdObject`` / ``CommonDocumentIdObject2``."""
    document_id: str


class FolderIdResult(TypedDict):
    """``CommonFolderIdObject`` / ``CommonFolderIdObject2``."""
    folder_id: str


# --- Dialogs, indications & document action attributes ----------------------------

class DialogParameters(TypedDict, total=False):
    """``DialogParametersObject`` — single-button on-screen dialog."""
    title: str
    message: str
    button_caption: str


class TwoButtonsDialogParameters(TypedDict, total=False):
    """``TwoButtonsDialogParametersObject`` — confirmation dialog."""
    title: str
    message: str
    positive_button_caption: str
    negative_button_caption: str


class IndicationInfo(TypedDict, total=False):
    """``IndicationInfoObject2`` — body for ``POST/PUT /system/controls/indicate``."""
    dialog_params: DialogParameters
    show_animation: str  # "true" or "false"


class ActionEntryByUser(TypedDict, total=False):
    """``ActionEntryByUserObject2``."""
    caption: str
    confirm_dialog_params: TwoButtonsDialogParameters


class ActionEntry(TypedDict, total=False):
    """``ActionEntryObject2``."""
    index: str
    by_user: ActionEntryByUser
    performers: list[str]


class ActionPerformerIndicate(TypedDict, total=False):
    """``ActionPerformerIndicateObject2``."""
    indication_id: str
    dialog_params: DialogParameters
    show_animation: str


class ActionPerformerRequest(TypedDict, total=False):
    """``ActionPerformerRequestObject2``."""
    url: str
    restrict: str


class ActionPerformerResult(TypedDict, total=False):
    """``ActionPerformerResultObject2``."""
    success_dialog_params: DialogParameters
    failure_dialog_params: DialogParameters


class ActionPerformer(TypedDict, total=False):
    """``ActionPerformerObject2``."""
    index: str
    indicate: ActionPerformerIndicate
    request: ActionPerformerRequest
    result: ActionPerformerResult


class DocumentAttribute(TypedDict, total=False):
    """``DocumentAttributeObject2`` — body for ``/documents/{id}/attribute``."""
    action_entry: list[ActionEntry]
    action_performer: list[ActionPerformer]


# --- Viewer & note templates ------------------------------------------------------

class ViewerOpenRequest(TypedDict):
    """``ViewerControlsOpenPutRequest`` — body for ``PUT /viewer/controls/open``."""
    document_id: str


class ViewerOpen2Request(TypedDict, total=False):
    """``ViewerControlsOpenPutRequest2`` — body for ``PUT /viewer/controls/open2``."""
    document_id: Required[str]
    page: str


class ViewerOpenConfirmRequest(TypedDict, total=False):
    """``ViewerControlsOpenPutRequest2Confirm`` — ``PUT /viewer/controls/open2_confirm``."""
    document_id: Required[str]
    confirm_dialog_params: Required[TwoButtonsDialogParameters]
    page: str


class DocumentsViewInfo(TypedDict, total=False):
    """``DocumentsViewInfoObject`` — ``PUT /viewer/controls/transfer/documents_view``."""
    view_type: str   # "list" or "thumbnail"
    order_type: str


class FolderViewInfo(TypedDict, total=False):
    """``FolderViewInfoObject`` — ``PUT /viewer/controls/transfer/folder_view``."""
    folder_id: Required[str]
    view_type: str   # "list" or "thumbnail"
    order_type: str


class ViewerViewInfo(TypedDict, total=False):
    """``ViewerStatusViewInfo2`` — one open pane in ``CurrentViewing``."""
    entry_id: str
    entry_path: str
    ext_id: str
    title: str
    document_source: str
    display_mode: str            # "single", "two_page_spread", "thumbnail"
    position: str                # "left", "right" (in double view)
    current_page: str
    total_page: str
    thumbnail_current_page: str


class CurrentViewing(TypedDict, total=False):
    """``ViewerStatusCurrentViewingGetResult2`` — ``GET /viewer/status/current_viewing``."""
    orientation: str             # "portlait" or "landscape"
    view_mode: str               # "single" or "double"
    views: list[ViewerViewInfo]


class PresetMark(TypedDict):
    """``CommonPresetMarkObjectResult``."""
    pattern: str


class PresetMarksResult(TypedDict):
    """``ViewerStatusPresetMarksGetResult`` — ``GET /viewer/status/preset_marks``."""
    pattern_list: list[PresetMark]


class NoteTemplate(TypedDict, total=False):
    """``CommonTemplateObjectResult`` — one note template entry."""
    note_template_id: Required[str]
    template_name: str
    is_modifiable: str           # "true" or "false"
    created_date: str


class NoteTemplateListResult(TypedDict):
    """``ViewerConfigsNoteTemplateGetResult`` — ``GET /viewer/configs/note_templates``."""
    template_list: list[NoteTemplate]


class NoteTemplateIdResult(TypedDict):
    """``ViewerConfigsNoteTemplatePostResult`` — ``POST /viewer/configs/note_templates``."""
    note_template_id: str


# --- System configs, pen buttons, Wi-Fi & certificates ----------------------------

class UseModeConfig(TypedDict):
    """``ConfigsUseModeObject`` — ``/system/configs/use_mode``."""
    use_mode: str                # "personal", "shared", "others"


class AnnotationsPermissionConfig(TypedDict):
    """``AnnotationsPermissionObject`` — ``/system/configs/annotations_permission``."""
    deletable: str               # "yes" or "no"


class PenButtonAssignment(TypedDict):
    """``ButtonfunctionInfoObject`` — one pen button -> function binding."""
    button_id: str
    function: str


class PenButtonChoices(TypedDict, total=False):
    """``ButtonfunctionChoicesObject`` — valid button IDs and functions."""
    button_id: list[str]
    function: list[str]


class PenButtonsConfig(TypedDict):
    """``ButtonfunctionInfoListObject`` — ``PUT /system/configs/pen_buttons_function``."""
    assigning: list[PenButtonAssignment]


class PenButtonsInfo(TypedDict, total=False):
    """``ButtonfunctionInfoListGetObject`` — ``GET /system/configs/pen_buttons_function``."""
    assigning: list[PenButtonAssignment]
    choices: PenButtonChoices


class ConfigsV1(TypedDict, total=False):
    """``ConfigsListResult`` — ``GET /system/configs``."""
    owner: CommonValue
    timezone: CommonValue
    date_format: CommonValue
    time_format: CommonValue
    timeout_to_standby: CommonValue
    initialized_flag: CommonValue


class ConfigsV2(TypedDict, total=False):
    """``ConfigsListResult2`` — ``GET /system/configs2``."""
    owner: CommonValue
    timezone: CommonValue
    date_format: CommonValue
    time_format: CommonValue
    timeout_to_sleep: CommonValue
    initialized_flag: CommonValue
    regulation_voltage: CommonValue
    use_mode: UseModeConfig


class ConfigsV3(TypedDict, total=False):
    """``ConfigsListResult3`` — ``GET /system/configs3``."""
    owner: CommonValue
    timezone: CommonValue
    date_format: CommonValue
    time_format: CommonValue
    timeout_to_sleep: CommonValue
    initialized_flag: CommonValue
    mtp: CommonValue
    regulation_voltage: CommonValue
    use_mode: UseModeConfig
    annotations_permission: AnnotationsPermissionConfig
    additional_client_registration: CommonValue
    control_servers_editable: CommonValue
    pen_buttons_function: PenButtonsConfig


class WifiApConfig(TypedDict, total=False):
    """``ApConfigureObject`` — saved or newly registered Wi-Fi AP configuration."""
    ssid: Required[str]
    security: Required[str]      # "nonsec", "psk", "eap"
    passwd: str
    dhcp: str                    # "true" or "false"
    static_address: str
    network_mask: str
    gateway: str
    dns1: str
    dns2: str
    proxy: str                   # "true" or "false"
    proxy_host: str
    proxy_port: str
    eap: str                     # "peap" or "tls"
    eap_phase2: str              # "none", "pap", "mschap", "mschapv2", "gtc"
    eap_id: str
    eap_anid: str
    eap_cacert: str
    eap_cert: str


class WifiApConfigList(TypedDict):
    """``ApConfigureListObject`` — ``GET /system/configs/wifi_accesspoints``."""
    aplist: list[WifiApConfig]


class WifiApInfo(TypedDict, total=False):
    """``CommonApObject`` — active Wi-Fi state or scanned AP entry."""
    ssid: str
    security: str
    frequency_band: str          # "2.4ghz" or "5ghz"
    rssi_level: str
    state: str                   # "connected", "connecting", "disabled", "none", "saved"


class WifiApScanList(TypedDict):
    """``CommonApListObject`` — ``POST /system/controls/wifi_accesspoints/scan``."""
    aplist: list[WifiApInfo]


class CertificateInfo(TypedDict):
    """``CommonCertificatesObject`` — installed certificate entry."""
    name: str


class CertificateList(TypedDict):
    """``CommonCertificatesListObject`` — ``GET /system/configs/certificates/{ca,client}``."""
    cert: list[CertificateInfo]


class CaCertificateUpload(TypedDict):
    """``CaCertificatesFileObject`` — ``POST /system/configs/certificates/ca``."""
    name: str
    filename: str
    file: str                    # Base64-encoded certificate bytes


class ClientCertificateUpload(TypedDict):
    """``ClientCertificatesFileObject`` — ``POST /system/configs/certificates/client``."""
    name: str
    filename: str
    file: str                    # Base64-encoded PKCS#12 bytes
    password: str


# --- System status, controls & firmware update ------------------------------------

class BatteryStatus(TypedDict, total=False):
    """``StatusBatteryResult2`` — ``GET /system/status/battery``."""
    level: str
    status: str
    health: str
    plugged: str
    pen: str
    icon_type: str


class StorageStatus(TypedDict, total=False):
    """``StatusStorageResult`` — ``GET /system/status/storage``."""
    capacity: str
    available: str


class FirmwareVersion(TypedDict, total=False):
    """``StatusFirmwareVersionResult`` — ``GET /system/status/firmware_version``."""
    model_name: str
    value: str


class DocumentProcessStatus(TypedDict, total=False):
    """``DocumentProcessStatusObject`` — ``GET /system/status/document_process``."""
    content_id: str
    content_type: str
    progress: str
    status: str


class UpdateFirmwarePrecheck(TypedDict, total=False):
    """``UpdateFirmwarePrecheckResult`` — ``GET /system/controls/update_firmware/precheck``."""
    battery: str                 # "ok" or "ng"
    image_file: str              # "ok" or "ng"


class TransferWizardNextScreen(TypedDict, total=False):
    """``TransferWizardNextScreenInfoObject`` — destination after transfer wizard."""
    document_open: ViewerOpen2Request
    documents_view: DocumentsViewInfo
    folder_view: FolderViewInfo


class TransferWizardInfo(TypedDict, total=False):
    """``TransferWizardInfoObject`` — ``PUT /system/controls/transfer/wizard``."""
    wizard_id_list: Required[list[str]]
    next_screen: TransferWizardNextScreen


# --- Kitting / enterprise models (port 8445) --------------------------------------

class HomeMenuAppInfo(TypedDict):
    """``HomeMenuAppInfoObject``."""
    application_id: str
    visibility: str


class HomeMenuAppUpdate(TypedDict, total=False):
    """``HomeMenuAppUpdateObject`` — ``PUT /kitting/home_menu/apps_visibility``."""
    application_id: str
    visibility: str
    multiple: list[HomeMenuAppInfo]


class TransferTimeoutInfo(TypedDict, total=False):
    """``TransferTimeoutInfoObject`` — ``/kitting/transfer_timeout``."""
    socket_timeout: str
    transfer_loop_timeout: str


class CustomAppSecurityInfo(TypedDict):
    """``CustomAppInstallerSecurityObject`` — ``PUT /kitting/custom_app/security_info``."""
    key_crypto_master: str
    key_sig_pub: str


class KittingDcssInfo(TypedDict, total=False):
    """``KittingDCSSInfo`` — ``GET /kitting/register/code``."""
    host: str
    port: str


class KittingRegisterCodeRequest(TypedDict, total=False):
    """``KittingRegisterCodeRequest`` — ``PUT /kitting/register/code``."""
    host: str
    port: str
    register_code: str
