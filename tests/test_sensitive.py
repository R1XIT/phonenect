"""Признаки «секретного» копирования (менеджеры паролей) — чистая функция, буфер Windows не трогаем."""
import struct

from phonenect.clipboard_win import is_sensitive

EXCLUDE = "ExcludeClipboardContentFromMonitorProcessing"
VIEWER = "Clipboard Viewer Ignore"
HISTORY = "CanIncludeInClipboardHistory"
CLOUD = "CanUploadToCloudClipboard"


def dword(n: int) -> bytes:
    return struct.pack("<I", n)


def test_empty_and_plain_text_are_not_sensitive():
    assert not is_sensitive({})
    assert not is_sensitive({"Rich Text Format": b"x", "HTML Format": None})


def test_exclude_from_monitor_presence_is_sensitive():
    assert is_sensitive({EXCLUDE: None})
    assert is_sensitive({EXCLUDE: b""})
    assert is_sensitive({EXCLUDE: b"anything"})


def test_viewer_ignore_presence_is_sensitive():
    assert is_sensitive({VIEWER: None})
    assert is_sensitive({VIEWER: b"\x01"})


def test_history_and_cloud_zero_is_sensitive():
    assert is_sensitive({HISTORY: dword(0)})
    assert is_sensitive({CLOUD: dword(0)})


def test_history_and_cloud_one_is_not_sensitive():
    assert not is_sensitive({HISTORY: dword(1)})
    assert not is_sensitive({CLOUD: dword(1)})
    assert not is_sensitive({HISTORY: dword(1), CLOUD: dword(1)})


def test_one_zero_flag_is_enough():
    assert is_sensitive({HISTORY: dword(1), CLOUD: dword(0)})


def test_broken_dword_is_not_sensitive():
    for bad in (None, b"", b"\x00", b"\x00\x00\x00", b"\x00\x00\x00\x00\x00"):
        assert not is_sensitive({HISTORY: bad})
        assert not is_sensitive({CLOUD: bad})
