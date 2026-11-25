"""Python API and command-line client for the Fujitsu QUADERNO (Sony DPT) over USB.

    import quaderno
    with quaderno.connect() as dev:          # find the device on USB and log in
        for e in dev.entries("Books"):
            print(e.path, e.size)
        dev.get("Books/x.pdf", "~/Downloads")
        dev.put("paper.pdf", "Books")
        dev.sync("~/QUADERNO")

Pair first with `quaderno pair` (or quaderno.pair(read_pin)). See device.Device
for all operations and errors.py for the exception hierarchy.
"""

from . import models
from .device import Device, PutResult, connect
from .entry import Entry
from .errors import (AuthError, CertificateError, ConnectError, DeviceNotFoundError,
                     DeviceUnreachableError, ExistsError, LockedError, LoginError, NotEmptyError,
                     NotFoundError, NotPairedError, PairingError, ProtocolError, QuadernoError,
                     RequestError, SerialModeError, StorageFullError, WrongPinError)
from .events import Event
from .models import (BatteryStatus, CertificateInfo, CommonValue, ConfigsV1, ConfigsV2, ConfigsV3,
                     CurrentViewing, DeviceInformation, DialogParameters, DocumentAttribute,
                     DocumentProcessStatus, DocumentsViewInfo, FileUploadResult, FirmwareVersion,
                     FolderViewInfo, IndicationInfo, NoteTemplate, PenButtonAssignment,
                     PenButtonsInfo, StorageStatus, TransferWizardInfo, TransferWizardNextScreen,
                     TwoButtonsDialogParameters, UpdateFirmwarePrecheck, ViewerViewInfo,
                     WifiApConfig, WifiApInfo)
from .pairing import PairResult, pair

__version__ = "0.5.0"
__all__ = ["connect", "pair", "Device", "Entry", "Event", "PutResult", "PairResult", "models",
           "BatteryStatus", "CertificateInfo", "CommonValue", "ConfigsV1", "ConfigsV2",
           "ConfigsV3", "CurrentViewing", "DeviceInformation", "DialogParameters",
           "DocumentAttribute", "DocumentProcessStatus", "DocumentsViewInfo", "FileUploadResult",
           "FirmwareVersion", "FolderViewInfo", "IndicationInfo", "NoteTemplate",
           "PenButtonAssignment", "PenButtonsInfo", "StorageStatus", "TransferWizardInfo",
           "TransferWizardNextScreen", "TwoButtonsDialogParameters", "UpdateFirmwarePrecheck",
           "ViewerViewInfo", "WifiApConfig", "WifiApInfo",
           "QuadernoError", "ConnectError", "DeviceNotFoundError", "SerialModeError",
           "DeviceUnreachableError", "AuthError", "NotPairedError", "LoginError",
           "CertificateError", "PairingError", "WrongPinError", "RequestError", "LockedError",
           "StorageFullError", "ProtocolError", "NotFoundError", "ExistsError", "NotEmptyError",
           "__version__"]
