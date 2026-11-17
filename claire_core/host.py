"""OS facts. Windows APIs are imported only inside Windows-specific branches.

Keep the established Mac folder layout unchanged. On Windows, ask the OS for
known folders so redirected/OneDrive/localized folders are not guessed.
"""
import ntpath
import os
import sys
import tempfile
import uuid


FOLDERS = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Movies")
KNOWN_FOLDERS = {
    "Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "Downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "Pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "Music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "Movies": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",  # Windows Videos
}


def windows_known_folder(name):
    """Current-user SHGetKnownFolderPath; no folder creation or shell process.

    https://learn.microsoft.com/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath
    """
    import ctypes
    from ctypes import wintypes
    guid_type = ctypes.c_ubyte * 16
    guid = guid_type.from_buffer_copy(uuid.UUID(KNOWN_FOLDERS[name]).bytes_le)
    shell = ctypes.WinDLL("shell32")
    ole = ctypes.WinDLL("ole32")
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(guid_type), wintypes.DWORD,
                                         wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    pointer = ctypes.c_void_p()
    try:
        result = shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(pointer))
        if result != 0 or not pointer.value:
            raise OSError(f"Windows could not locate {name} (HRESULT {result & 0xffffffff:#x})")
        return ctypes.wstring_at(pointer.value)
    finally:
        if pointer.value:
            ole.CoTaskMemFree(pointer)


def locations():
    home = os.path.expanduser("~")
    result = {"root": os.path.abspath(os.sep), "home": home}
    for name in FOLDERS:
        path = os.path.join(home, name)
        if sys.platform == "win32":
            try:
                path = windows_known_folder(name)
            except OSError:
                # Only use a conventional fallback that actually exists.
                # Never create a fake Desktop beside the redirected one.
                if not os.path.isdir(path):
                    continue
        result[name.casefold()] = path
    if sys.platform == "win32" and "movies" in result:
        result["videos"] = result["movies"]
    return result


def resolve_location(raw, known=None):
    """Resolve OS landmarks without changing an existing explicit path.

    Mac/POSIX keeps the original alias/expanduser semantics. Windows also
    resolves ~/Desktop and missing legacy home/Desktop paths to the OS folder.
    """
    known = locations() if known is None else known
    if raw.casefold() in known:
        return known[raw.casefold()]
    if raw == "~":
        return known["home"]
    if sys.platform != "win32":
        return raw
    relative = raw.replace("\\", "/")
    if relative.startswith("~/"):
        first, _, tail = relative[2:].partition("/")
        target = known.get(first.casefold())
        if target and first.casefold() not in ("root", "home"):
            return ntpath.normpath(ntpath.join(target, tail))
    # Old conversations can retain the guessed literal home/Desktop. Correct
    # that exact landmark only when absent; preserve explicit existing paths.
    if not os.path.lexists(raw):
        for name in FOLDERS:
            legacy = ntpath.join(known["home"], name)
            if ntpath.normcase(ntpath.normpath(raw)) == ntpath.normcase(legacy):
                return known.get(name.casefold(), raw)
    return raw


def is_within(path, root):
    """Canonical containment, including drive roots and Windows case folding."""
    path, root = (os.path.normcase(os.path.realpath(p)) for p in (path, root))
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:  # Different Windows drives.
        return False


def user_file_roots():
    roots = (os.path.realpath(os.path.expanduser("~")), "/private/tmp", "/private/var/folders")
    if sys.platform == "win32":
        roots = roots[:1] + tuple(p for name, p in locations().items() if name not in ("root", "home")) + (tempfile.gettempdir(),)
    return roots


def windows_memory():
    import ctypes
    from ctypes import wintypes

    class Status(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in
            ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]

    status = Status()
    status.length = ctypes.sizeof(status)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(Status)]
    kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    return status.total, status.available
