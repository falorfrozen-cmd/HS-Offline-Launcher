#!/usr/bin/env python3
"""HS Offline Launcher.

Starts the selected Steam Hero_Siege.exe directly with Steam's public AppID in
the child environment.  It does not patch game files, stop services, or install
DLLs. Existing offline mod-loader builds are identified and may be launched,
but the launcher itself never modifies them.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


APP_ID = "269210"
APP_NAME = "HS Offline Launcher"
PORTS = (8861, 8862, 8863, 8961)
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "HSOfflineLauncher"
CONFIG_PATH = STATE_DIR / "config.json"
ACTION_LOG_PATH = STATE_DIR / "launcher.log"

KNOWN_BUILDS = {
    "438bf4848688c5be52ac15f26f02b46da620d90587c28e766a9cea190f3a7de4": "Season 10 · 2026.08.30",
    "0766aa8bfc6eb5679df46f78546644e34fae333adc22474f96903c1d68f251f5": "Season 10 · 2026.08.24",
    "ba72b95ac10785d0ecdcc2b3d1925d6cb3439efaf4cef9de2ea1f67d6cfdd4df": "Season 10 · 2026.08.22 legacy",
}

STEAM_RUNTIME_NAME = "steam_api64.dll"
PE_MACHINE_AMD64 = 0x8664
MAX_PE_OFFSET = 16 * 1024 * 1024
INACTIVE_EAC_STATES = frozenset({"stopped", "not-installed"})
EAC_SERVICE_NAME = "EasyAntiCheat_EOS"
EAC_PROCESS_NAMES = frozenset({
    "easyanticheat.exe",
    "easyanticheat_eos.exe",
    "easyanticheat_eossys.exe",
    "start_protected_game.exe",
})

WINDOWS_DIR = Path(os.environ.get("SystemRoot", r"C:\Windows"))
EXPLORER_EXE = WINDOWS_DIR / "explorer.exe"
API_HEADER = "X-HS-Launcher-Token"
API_TOKEN = secrets.token_urlsafe(32)

SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SC_STATUS_PROCESS_INFO = 0
SERVICE_STOPPED = 1
SERVICE_RUNNING = 4
ERROR_CALL_NOT_IMPLEMENTED = 120
ERROR_SERVICE_DOES_NOT_EXIST = 1060
WEBVIEW_WINDOW = None
SERVER = None
SERVER_THREAD = None
LAUNCH_LOCK = threading.Lock()
LAST_LAUNCH = {"pid": 0, "time": "", "message": "Ready"}
HASH_CACHE: dict[tuple[str, int, int], str] = {}


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def log_action(message: str) -> None:
    """Keep a small local diagnostic for UI actions reported by users."""
    try:
        ensure_state_dir()
        if ACTION_LOG_PATH.exists() and ACTION_LOG_PATH.stat().st_size > 128_000:
            ACTION_LOG_PATH.write_text("", encoding="utf-8")
        with ACTION_LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except OSError:
        pass


def load_config() -> dict:
    ensure_state_dir()
    cfg = {"game_exe": ""}
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                cfg.update(saved)
        except (OSError, ValueError, TypeError):
            pass
    configured = configured_game_path(cfg)
    if configured is None or not configured.is_file():
        found = discover_game_exe()
        if found:
            cfg["game_exe"] = str(found)
            save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    ensure_state_dir()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def configured_game_path(cfg: dict) -> Path | None:
    raw = cfg.get("game_exe")
    if not isinstance(raw, (str, os.PathLike)):
        return None
    value = os.path.expandvars(os.path.expanduser(str(raw).strip().strip('"')))
    return Path(value) if value else None


def registry_steam_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        import winreg

        for hive, key_name in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        ):
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    for value_name in ("SteamPath", "InstallPath"):
                        try:
                            value, _ = winreg.QueryValueEx(key, value_name)
                            roots.append(Path(value))
                        except OSError:
                            continue
            except OSError:
                continue
    except ImportError:
        pass
    roots.append(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam")
    return unique_paths(roots)


def unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def steam_libraries() -> list[Path]:
    libraries: list[Path] = []
    for root in registry_steam_roots():
        libraries.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
            modern = re.findall(r'"path"\s+"([^"]+)"', text, flags=re.IGNORECASE)
            # Numeric key/value entries are an old libraryfolders.vdf format.
            # Do not parse them in the modern format, where app IDs are also
            # numeric key/value pairs and are not library paths.
            legacy = [] if modern else re.findall(
                r'^\s*"\d+"\s+"([^"]+)"', text, flags=re.IGNORECASE | re.MULTILINE
            )
            for raw in modern + legacy:
                libraries.append(Path(raw.replace(r"\\", "\\")))
        except OSError:
            continue
    return unique_paths(libraries)


def discover_game_exe() -> Path | None:
    candidates: list[Path] = []
    for library in steam_libraries():
        steamapps = library / "steamapps"
        manifest = steamapps / f"appmanifest_{APP_ID}.acf"
        try:
            text = manifest.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r'"installdir"\s+"([^"]+)"', text, flags=re.IGNORECASE)
            if match:
                install_dir = match.group(1).replace(r"\\", "\\")
                candidates.append(steamapps / "common" / install_dir / "bin" / "Hero_Siege.exe")
        except OSError:
            pass
        candidates.append(steamapps / "common" / "HeroSiege" / "bin" / "Hero_Siege.exe")

    existing = [candidate for candidate in unique_paths(candidates) if candidate.is_file()]
    if not existing:
        return None
    return next((candidate for candidate in existing if find_steam_runtime(candidate)), existing[0])


def steam_exe() -> Path | None:
    for root in registry_steam_roots():
        candidate = root / "steam.exe"
        if candidate.is_file():
            return candidate
    return None


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", wintypes.DWORD),
        ("dwCurrentState", wintypes.DWORD),
        ("dwControlsAccepted", wintypes.DWORD),
        ("dwWin32ExitCode", wintypes.DWORD),
        ("dwServiceSpecificExitCode", wintypes.DWORD),
        ("dwCheckPoint", wintypes.DWORD),
        ("dwWaitHint", wintypes.DWORD),
        ("dwProcessId", wintypes.DWORD),
        ("dwServiceFlags", wintypes.DWORD),
    ]


def processes() -> list[tuple[int, str]]:
    if os.name != "nt":
        return []
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = wintypes.HANDLE(-1).value
    if snapshot in (None, invalid_handle):
        raise OSError("Windows process snapshot could not be created")
    rows: list[tuple[int, str]] = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        if not ok:
            raise OSError("Windows process snapshot could not be read")
        while ok:
            rows.append((int(entry.th32ProcessID), entry.szExeFile))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return rows


def matching_processes(*names: str) -> list[tuple[int, str]]:
    wanted = {name.lower() for name in names}
    return [(pid, name) for pid, name in processes() if name.lower() in wanted]


def windows_service_state(service_name: str) -> tuple[int | None, int]:
    """Return a locale-independent SCM state and the Win32 error code."""
    if os.name != "nt":
        return None, ERROR_CALL_NOT_IMPLEMENTED

    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        advapi32.OpenSCManagerW.restype = wintypes.HANDLE
        advapi32.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
        advapi32.OpenServiceW.restype = wintypes.HANDLE
        advapi32.QueryServiceStatusEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPBYTE,
            wintypes.DWORD,
            wintypes.LPDWORD,
        ]
        advapi32.QueryServiceStatusEx.restype = wintypes.BOOL
        advapi32.CloseServiceHandle.argtypes = [wintypes.HANDLE]
        advapi32.CloseServiceHandle.restype = wintypes.BOOL
    except (AttributeError, OSError):
        return None, ERROR_CALL_NOT_IMPLEMENTED

    manager = advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not manager:
        return None, ctypes.get_last_error()
    try:
        service = advapi32.OpenServiceW(manager, service_name, SERVICE_QUERY_STATUS)
        if not service:
            return None, ctypes.get_last_error()
        try:
            status = SERVICE_STATUS_PROCESS()
            bytes_needed = wintypes.DWORD()
            buffer = ctypes.cast(ctypes.byref(status), wintypes.LPBYTE)
            if not advapi32.QueryServiceStatusEx(
                service,
                SC_STATUS_PROCESS_INFO,
                buffer,
                ctypes.sizeof(status),
                ctypes.byref(bytes_needed),
            ):
                return None, ctypes.get_last_error()
            return int(status.dwCurrentState), 0
        finally:
            advapi32.CloseServiceHandle(service)
    finally:
        advapi32.CloseServiceHandle(manager)


def eac_service_status() -> str:
    state, error = windows_service_state(EAC_SERVICE_NAME)
    if state == SERVICE_STOPPED:
        return "stopped"
    if state == SERVICE_RUNNING:
        return "running"
    if state is not None:
        return "transitioning"
    if error == ERROR_SERVICE_DOES_NOT_EXIST:
        return "not-installed"
    return "unknown"


def eac_is_inactive(status: str) -> bool:
    return status in INACTIVE_EAC_STATES


def eac_processes() -> list[tuple[int, str]]:
    return [(pid, name) for pid, name in processes() if name.lower() in EAC_PROCESS_NAMES]


def file_sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path).lower(), stat.st_size, stat.st_mtime_ns)
    if key in HASH_CACHE:
        return HASH_CACHE[key]
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    value = digest.hexdigest()
    HASH_CACHE.clear()
    HASH_CACHE[key] = value
    return value


def pe_section_names(path: Path) -> set[str]:
    """Read enough PE metadata to identify a real 64-bit Windows executable."""
    size = path.stat().st_size
    with path.open("rb") as stream:
        dos_header = stream.read(64)
        if len(dos_header) != 64 or dos_header[:2] != b"MZ":
            raise ValueError("missing DOS header")
        pe_offset = int.from_bytes(dos_header[0x3C:0x40], "little")
        if pe_offset < 64 or pe_offset > MAX_PE_OFFSET or pe_offset + 24 > size:
            raise ValueError("invalid PE header offset")
        stream.seek(pe_offset)
        pe_header = stream.read(24)
        if len(pe_header) != 24 or pe_header[:4] != b"PE\0\0":
            raise ValueError("missing PE signature")
        machine = int.from_bytes(pe_header[4:6], "little")
        if machine != PE_MACHINE_AMD64:
            raise ValueError("the executable is not 64-bit")
        section_count = int.from_bytes(pe_header[6:8], "little")
        optional_header_size = int.from_bytes(pe_header[20:22], "little")
        characteristics = int.from_bytes(pe_header[22:24], "little")
        if not 1 <= section_count <= 96:
            raise ValueError("invalid PE section count")
        if characteristics & 0x0002 == 0:
            raise ValueError("PE image is not executable")
        if optional_header_size < 2:
            raise ValueError("missing PE optional header")
        stream.seek(pe_offset + 24)
        if int.from_bytes(stream.read(2), "little") != 0x020B:
            raise ValueError("the executable is not PE32+")
        section_table = pe_offset + 24 + optional_header_size
        if section_table + section_count * 40 > size:
            raise ValueError("truncated PE section table")
        stream.seek(section_table)
        result: set[str] = set()
        has_file_data = False
        for _ in range(section_count):
            section = stream.read(40)
            name = section[:8].split(b"\0", 1)[0].decode("ascii", errors="ignore").lower()
            if name:
                result.add(name)
            raw_size = int.from_bytes(section[16:20], "little")
            raw_offset = int.from_bytes(section[20:24], "little")
            if raw_size and raw_offset and raw_offset + raw_size <= size:
                has_file_data = True
        if not has_file_data:
            raise ValueError("PE sections contain no file data")
        return result


def find_steam_runtime(path: Path) -> Path | None:
    """Support both current bin-local and older game-root Steam layouts."""
    for directory in unique_paths([path.parent, path.parent.parent]):
        candidate = directory / STEAM_RUNTIME_NAME
        if candidate.is_file():
            return candidate
    return None


def validate_game(path: Path | None) -> tuple[bool, str]:
    if path is None or not path.is_file():
        return False, "Hero_Siege.exe was not found"
    if path.name.lower() != "hero_siege.exe":
        return False, "Select the clean Hero_Siege.exe, not a modded or protected launcher"
    try:
        sections = pe_section_names(path)
    except ValueError as exc:
        return False, f"The selected file is not a valid Hero Siege Windows executable ({exc})"
    except OSError as exc:
        return False, f"The executable could not be read: {exc}"
    if not find_steam_runtime(path):
        return False, "steam_api64.dll is missing. Verify Hero Siege files in Steam, then try again."
    if ".aurie" in sections:
        return True, "Compatible Aurie/ForgePact build — offline use only"
    return True, "Clean Steam executable"


def game_details(path: Path | None) -> dict:
    valid, reason = validate_game(path)
    exists = bool(path and path.is_file())
    modified = False
    if valid and path:
        try:
            modified = ".aurie" in pe_section_names(path)
        except (OSError, ValueError):
            pass
    result = {
        "path": str(path) if path else "",
        "exists": exists,
        "valid": valid,
        "validation": reason,
        "size": path.stat().st_size if exists and path else 0,
        "hash": "",
        "build": "Checking…" if valid else ("Game not found" if not exists else "Invalid selection"),
        "known": False,
        "modified": modified,
    }
    if valid and path:
        try:
            digest = file_sha256(path)
            if modified:
                label = "Modified/custom Hero Siege build"
            else:
                label = KNOWN_BUILDS.get(digest, "New/unknown Steam build")
            result.update(hash=digest, build=label, known=digest in KNOWN_BUILDS)
        except OSError as exc:
            result.update(build="Hash unavailable", validation=f"Executable is valid; hash check failed: {exc}")
    return result


def current_status() -> dict:
    cfg = load_config()
    path = configured_game_path(cfg)
    try:
        rows = processes()
        process_scan_ok = True
    except OSError:
        rows = []
        process_scan_ok = False
    names = {name.lower() for _, name in rows}
    game_rows = [(pid, name) for pid, name in rows if name.lower() == "hero_siege.exe"]
    eac_rows = [(pid, name) for pid, name in rows if name.lower() in EAC_PROCESS_NAMES]
    service = eac_service_status()
    game = game_details(path)
    steam_running = "steam.exe" in names
    steam_found = bool(steam_exe())
    safe = process_scan_ok and eac_is_inactive(service) and not eac_rows
    game_running = bool(game_rows)
    ready = game["valid"] and safe and not game_running and (steam_running or steam_found)
    if game_running and not safe:
        blocker = "Warning: Hero Siege and EAC are active. Close the protected game/session."
    elif game_running:
        blocker = "Hero Siege is already running"
    elif not game["valid"]:
        blocker = game["validation"]
    elif not process_scan_ok:
        blocker = "Running processes could not be verified. Reopen the launcher and try again."
    elif service == "unknown":
        blocker = "EAC status could not be verified. Reopen the launcher or check the Easy Anti-Cheat service."
    elif service == "transitioning":
        blocker = "EAC is changing state. Wait a moment, then try again."
    elif not safe:
        blocker = "EAC is active. Close the protected game/session before launching offline."
    elif not steam_running and not steam_found:
        blocker = "Steam was not found. Install or open Steam, then try again."
    else:
        blocker = ""
    return {
        "appId": APP_ID,
        "game": game,
        "steamRunning": steam_running,
        "steamFound": steam_found,
        "gameRunning": game_running,
        "gamePid": game_rows[0][0] if game_rows else 0,
        "eacService": service,
        "eacProcesses": [{"pid": pid, "name": name} for pid, name in eac_rows],
        "processScanOk": process_scan_ok,
        "safe": safe,
        "ready": ready,
        "blocker": blocker,
        "lastLaunch": dict(LAST_LAUNCH),
    }


def start_steam_if_needed() -> tuple[bool, str]:
    try:
        if matching_processes("steam.exe"):
            return True, "Steam is running"
    except OSError:
        return False, "Running processes could not be verified"
    exe = steam_exe()
    if not exe:
        return False, "Steam was not found"
    try:
        subprocess.Popen([str(exe), "-silent"], cwd=str(exe.parent))
    except OSError as exc:
        return False, f"Steam could not be started: {exc}"
    for _ in range(20):
        time.sleep(0.5)
        try:
            if matching_processes("steam.exe"):
                return True, "Steam started"
        except OSError:
            return False, "Running processes could not be verified"
    return False, "Steam did not become ready"


def launch_game() -> dict:
    if not LAUNCH_LOCK.acquire(blocking=False):
        return {"err": "A launch request is already in progress"}
    try:
        return launch_game_locked()
    finally:
        LAUNCH_LOCK.release()


def launch_safety_blocker() -> str:
    try:
        rows = processes()
    except OSError:
        return "Running processes could not be verified, so launch was blocked"
    if any(name.lower() == "hero_siege.exe" for _, name in rows):
        return "Hero Siege is already running"

    service = eac_service_status()
    active_eac = [(pid, name) for pid, name in rows if name.lower() in EAC_PROCESS_NAMES]
    if eac_is_inactive(service) and not active_eac:
        return ""
    if service == "unknown":
        return "EAC status could not be verified, so launch was blocked"
    if service == "transitioning":
        return "EAC is changing state. Wait a moment, then try again"
    return (
        "EAC is currently active. Close the protected game/session first; "
        "this launcher will not stop or modify EAC."
    )


def launch_game_locked() -> dict:
    cfg = load_config()
    path = configured_game_path(cfg)
    valid, reason = validate_game(path)
    if not valid:
        return {"err": reason}
    assert path is not None
    blocker = launch_safety_blocker()
    if blocker:
        return {"err": blocker}

    steam_ok, steam_message = start_steam_if_needed()
    if not steam_ok:
        return {"err": steam_message}

    # Steam startup can take several seconds. Protection or another game
    # instance may appear during that wait, so fail closed again immediately
    # before creating the game process.
    blocker = launch_safety_blocker()
    if blocker:
        return {"err": blocker}

    child_env = os.environ.copy()
    child_env["SteamAppId"] = APP_ID
    child_env["SteamGameId"] = APP_ID
    runtime = find_steam_runtime(path)
    if runtime and runtime.parent != path.parent:
        child_env["PATH"] = str(runtime.parent) + os.pathsep + child_env.get("PATH", "")
    try:
        process = subprocess.Popen([str(path)], cwd=str(path.parent), env=child_env)
    except OSError as exc:
        return {"err": f"Hero Siege could not be launched: {exc}"}

    LAST_LAUNCH.update(pid=process.pid, time=time.strftime("%H:%M:%S"), message="Direct offline launch requested")
    threading.Thread(target=verify_launch, args=(process.pid,), daemon=True).start()
    return {"ok": f"Hero Siege started directly (PID {process.pid}). No game file was changed.", "pid": process.pid}


def verify_launch(pid: int) -> None:
    time.sleep(5)
    try:
        rows = processes()
    except OSError:
        LAST_LAUNCH["message"] = "Warning: running processes could not be verified"
        return
    running = any(row_pid == pid and name.lower() == "hero_siege.exe" for row_pid, name in rows)
    service = eac_service_status()
    protected = [(row_pid, name) for row_pid, name in rows if name.lower() in EAC_PROCESS_NAMES]
    if running and eac_is_inactive(service) and not protected:
        LAST_LAUNCH["message"] = "Verified: game running, EAC inactive"
    elif not running:
        LAST_LAUNCH["message"] = "The game exited during startup"
    else:
        LAST_LAUNCH["message"] = "Warning: an EAC component became active"


def store_selected_exe(selected: str) -> dict:
    if not selected:
        return {"cancelled": True}

    path = Path(selected)
    valid, reason = validate_game(path)
    if not valid:
        return {"err": reason}

    cfg = load_config()
    cfg["game_exe"] = str(path)
    save_config(cfg)
    return {"ok": "Hero_Siege.exe selected", "path": str(path)}


def initial_game_directory() -> Path:
    current = configured_game_path(load_config())
    return current.parent if current and current.parent.is_dir() else Path.home()


def select_exe_fallback() -> dict:
    """Open the Win32 picker when the UI is running in a normal browser."""
    try:
        return store_selected_exe(win_file_dialog(str(initial_game_directory())))
    except OSError as exc:
        return {"err": str(exc)}


class LauncherApi:
    """Native pywebview operations that must be owned by the app window."""

    def select_exe(self) -> dict:
        log_action("Browse clicked")
        try:
            selected = win_file_dialog(
                str(initial_game_directory()),
                owner_hwnd=current_window_handle(),
            )
        except Exception as exc:
            log_action(f"Browse failed: {type(exc).__name__}: {exc}")
            return {"err": f"The file picker could not be opened: {exc}"}
        result = store_selected_exe(selected)
        log_action("Browse cancelled" if result.get("cancelled") else f"Browse result: {result}")
        return result


def current_window_handle() -> int:
    window = WEBVIEW_WINDOW
    native = getattr(window, "native", None) if window is not None else None
    if native is None and window is not None:
        browser_view = getattr(getattr(window, "gui", None), "BrowserView", None)
        instances = getattr(browser_view, "instances", {})
        native = instances.get(window.uid)
    handle = getattr(native, "Handle", None)
    if handle is None:
        return 0
    try:
        return int(handle.ToInt64())
    except AttributeError:
        return int(handle)


def win_file_dialog(initial_dir: str, owner_hwnd: int = 0) -> str:
    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p), ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    buffer = ctypes.create_unicode_buffer(32768)
    dialog = OPENFILENAMEW()
    dialog.lStructSize = ctypes.sizeof(dialog)
    dialog.hwndOwner = owner_hwnd
    dialog.lpstrFilter = "Hero_Siege.exe\0Hero_Siege.exe\0Executable files (*.exe)\0*.exe\0\0"
    dialog.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
    dialog.nMaxFile = len(buffer)
    dialog.lpstrInitialDir = initial_dir
    dialog.lpstrTitle = "Select the clean Steam Hero_Siege.exe"
    dialog.Flags = 0x00001000 | 0x00000800 | 0x00080000
    dialog.lpstrDefExt = "exe"
    comdlg32 = ctypes.windll.comdlg32
    if comdlg32.GetOpenFileNameW(ctypes.byref(dialog)):
        return buffer.value
    error_code = int(comdlg32.CommDlgExtendedError())
    if error_code:
        raise OSError(f"The Windows file picker failed (error 0x{error_code:04X})")
    return ""


def open_game_folder() -> dict:
    path = configured_game_path(load_config())
    if path is None or not validate_game(path)[0]:
        path = discover_game_exe()
    if path is None or not validate_game(path)[0]:
        return {"err": "Game folder was not found"}
    if str(path).startswith((r"\\", "//")):
        return {"err": "Network game folders are not supported"}
    try:
        target = path.resolve(strict=True).parent
    except OSError as exc:
        return {"err": f"Game folder could not be resolved: {exc}"}
    try:
        subprocess.Popen([str(EXPLORER_EXE), str(target)])
    except OSError as exc:
        return {"err": f"Game folder could not be opened: {exc}"}
    return {"ok": "Game folder opened"}


HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HS Offline Launcher</title>
<style>
:root{--bg:#070b12;--panel:#0e1622;--panel2:#121d2b;--line:#26374a;--text:#edf4fb;--muted:#8494a8;--cyan:#35c4ee;--mint:#55e0b2;--amber:#f4bd5e;--red:#ff6576}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 82% 0,#142438 0,transparent 35%),linear-gradient(145deg,#070b12,#0a111b 60%,#070b12);color:var(--text);font:14px/1.45 "Segoe UI",sans-serif;min-height:100vh;overflow:hidden}
.shell{height:100vh}.main{max-width:940px;margin:0 auto;padding:34px 38px;overflow:auto}.top{display:flex;align-items:center;justify-content:space-between}.brand{font-size:22px;font-weight:800;letter-spacing:.3px}.brand b{color:var(--cyan)}.title{font-size:29px;font-weight:800;margin:24px 0 3px}.desc{color:var(--muted)}
.badge{border:1px solid #5d5124;background:#272310;color:var(--amber);border-radius:30px;padding:10px 15px;font-weight:700;font-size:12px}.badge.good{border-color:#245d50;background:#102721}.badge.bad{border-color:#71313b;background:#29151a}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:28px 0 18px}.stat{background:rgba(15,24,36,.9);border:1px solid var(--line);border-radius:12px;padding:16px}.stat small{display:block;color:var(--muted);margin-bottom:8px}.stat strong{font-size:15px}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;background:var(--amber);box-shadow:0 0 12px currentColor}.good{color:var(--mint)}.bad{color:var(--red)}.warn{color:var(--amber)}
.card{background:linear-gradient(145deg,rgba(18,29,43,.96),rgba(12,20,31,.96));border:1px solid var(--line);border-radius:16px;padding:22px;margin-top:14px;box-shadow:0 18px 50px rgba(0,0,0,.2)}.card h2{font-size:16px;margin:0 0 16px}.pathrow{display:grid;grid-template-columns:1fr auto auto;gap:10px}.path{background:#080d15;border:1px solid #26394c;border-radius:9px;padding:13px 14px;color:#b8c7d6;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:Consolas,monospace;font-size:12px}
button{border:0;border-radius:9px;padding:0 16px;font-weight:700;color:#dce8f2;background:#1b2a3b;cursor:pointer;transition:.18s}button:hover{transform:translateY(-1px);filter:brightness(1.15)}button:disabled{opacity:.45;cursor:not-allowed;transform:none}.launch{width:100%;height:64px;margin-top:16px;background:linear-gradient(100deg,#168fb7,#26c2dc);color:#031018;font-size:17px;box-shadow:0 10px 28px rgba(36,190,222,.18)}
.buildline{margin-top:12px;color:var(--muted);font-size:12px}.buildline strong{color:#b9c9d8}.validation{margin-top:7px;min-height:20px;font-size:12px}.activity{color:#91a5ba;min-height:74px}.activity strong{color:#dbe9f6}
@media(max-width:760px){.main{padding:24px}.grid{grid-template-columns:1fr}.pathrow{grid-template-columns:1fr 1fr}.path{grid-column:1/-1}}
</style></head>
<body><div class="shell"><main class="main"><div class="top"><div class="brand"><b>HS</b> OFFLINE LAUNCHER</div><div class="badge" id="overall">CHECKING</div></div><div class="title">Play Hero Siege Offline</div><div class="desc">Select the game and press Launch.</div>
<div class="grid" style="grid-template-columns:repeat(3,1fr)"><div class="stat"><small>STEAM</small><strong id="steam"><i class="dot"></i>Checking</strong></div><div class="stat"><small>PROTECTION</small><strong id="eac"><i class="dot"></i>Checking</strong></div><div class="stat"><small>GAME</small><strong id="game"><i class="dot"></i>Checking</strong></div></div>
<section class="card"><h2>Game Location</h2><div class="pathrow"><div class="path" id="path">Detecting game…</div><button onclick="browse()">Browse</button><button onclick="folder()">Folder</button></div><div class="buildline">Build: <strong id="build">—</strong></div><div class="validation warn" id="validation">Checking the selected executable…</div><button class="launch" id="launch" onclick="launchGame()">▶ LAUNCH OFFLINE</button></section>
<section class="card activity"><h2>Status</h2><div id="activity"><strong>Ready.</strong></div></section></main></div>
<script>
const $=id=>document.getElementById(id);const API_TOKEN='__HS_API_TOKEN__';let busy=false;let activityPinnedUntil=0;
async function api(path,method='GET'){const headers={'X-HS-Launcher-Token':API_TOKEN};if(method!=='GET')headers['Content-Type']='application/json';const r=await fetch(path,{method,headers});const body=await r.json();if(!r.ok)throw new Error(body.err||('HTTP '+r.status));return body}
function state(el,good,text){el.className=good===true?'good':good===false?'bad':'warn';el.innerHTML='<i class="dot"></i>'+text}
function activity(text,kind='',holdMs=0){const strong=document.createElement('strong');strong.textContent=text;strong.className=kind;$('activity').replaceChildren(strong);if(holdMs)activityPinnedUntil=Date.now()+holdMs}
async function refresh(){try{const s=await api('/api/status');$('path').textContent=s.game.path||'Not selected';$('build').textContent=s.game.build;state($('steam'),s.steamRunning,s.steamRunning?'Running':(s.steamFound?'Ready':'Not found'));const eacActive=s.eacService==='running'||s.eacProcesses.length>0;const eacPending=s.eacService==='transitioning';state($('eac'),s.safe?true:eacActive?false:null,s.safe?'Inactive':eacActive?'ACTIVE':eacPending?'CHECKING':'UNKNOWN');state($('game'),s.gameRunning,s.gameRunning?'Running':'Closed');$('launch').disabled=busy||!s.ready;const playing=s.gameRunning&&s.safe;$('overall').textContent=playing?'PLAYING OFFLINE':s.ready?'READY':s.gameRunning?'CHECK':'SETUP NEEDED';$('overall').className='badge '+(playing||s.ready?'good':s.gameRunning?'warn':'bad');$('validation').textContent=playing||s.ready?s.game.validation:s.blocker;$('validation').className='validation '+(playing||s.ready?(s.game.modified?'warn':'good'):'bad');if(!busy&&Date.now()>=activityPinnedUntil){const last=s.lastLaunch.message;const fallback=playing?'Game is running offline; EAC is inactive.':s.ready?'Ready to launch offline.':s.blocker;activity(last&&last!=='Ready'?last:fallback,playing||s.ready?'good':s.gameRunning?'warn':'bad')}}catch(e){activity('Status error: '+e,'bad',6000)}}
async function launchGame(){busy=true;$('launch').disabled=true;activity('Launching… Waiting for the game process.');try{const r=await api('/api/launch','POST');activity(r.err||r.ok,r.err?'bad':'good',6000)}catch(e){activity('Launch failed: '+e,'bad',6000)}finally{busy=false;await refresh()}}
async function browse(){if(busy)return;busy=true;$('launch').disabled=true;activity('Opening the game picker…');try{let r;if(window.pywebview&&window.pywebview.api){r=await window.pywebview.api.select_exe()}else{r=await api('/api/select','POST')}if(r.err){activity(r.err,'bad',6000)}else if(r.cancelled){activity('Selection cancelled.','',3000)}else{activity(r.ok,'good',3000)}}catch(e){activity('Browse failed: '+e,'bad',6000)}finally{busy=false;await refresh()}}
async function folder(){try{const r=await api('/api/folder','POST');if(r.err)activity(r.err,'bad',6000)}catch(e){activity('Folder failed: '+e,'bad',6000)}}
refresh();setInterval(refresh,2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def expected_host(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def request_host_valid(self) -> bool:
        return self.headers.get("Host", "").strip().lower() == self.expected_host()

    def request_origin_valid(self) -> bool:
        origin = self.headers.get("Origin", "").strip().lower()
        return not origin or origin == f"http://{self.expected_host()}"

    def api_authorized(self) -> bool:
        supplied = self.headers.get(API_HEADER, "")
        return (
            self.request_host_valid()
            and self.request_origin_valid()
            and hmac.compare_digest(supplied, API_TOKEN)
        )

    def reject_request(self) -> None:
        self.json_response({"err": "Forbidden"}, 403)

    def json_response(self, value: dict, status: int = 200) -> None:
        payload = json.dumps(value).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The WebView can close while its final status refresh is in flight.
            pass

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/api/status":
            if not self.api_authorized():
                self.reject_request()
                return
            self.json_response(current_status())
            return
        if route == "/":
            if not self.request_host_valid():
                self.reject_request()
                return
            payload = HTML.replace("__HS_API_TOKEN__", API_TOKEN).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def do_POST(self):
        if not self.api_authorized():
            self.reject_request()
            return
        route = urlparse(self.path).path
        if route == "/api/launch":
            self.json_response(launch_game())
        elif route == "/api/select":
            self.json_response(select_exe_fallback())
        elif route == "/api/folder":
            self.json_response(open_game_folder())
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.reject_request()


class LauncherServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False


def start_server() -> tuple[LauncherServer, int, threading.Thread]:
    for port in PORTS:
        try:
            server = LauncherServer(("127.0.0.1", port), Handler)
            thread = threading.Thread(target=server.serve_forever, name="launcher-http", daemon=True)
            thread.start()
            return server, port, thread
        except OSError:
            continue
    raise RuntimeError("No local launcher port is available")


def stop_server() -> None:
    global SERVER, SERVER_THREAD
    server = SERVER
    thread = SERVER_THREAD
    SERVER = None
    SERVER_THREAD = None
    if server is None:
        return
    try:
        server.shutdown()
    except OSError:
        pass
    finally:
        server.server_close()
    if thread and thread is not threading.current_thread():
        thread.join(timeout=2)


def main() -> int:
    global SERVER, SERVER_THREAD, WEBVIEW_WINDOW
    if os.name != "nt":
        print("HS Offline Launcher currently supports Windows only.", file=sys.stderr)
        return 1
    ensure_state_dir()
    load_config()
    SERVER, port, SERVER_THREAD = start_server()
    url = f"http://127.0.0.1:{port}/"
    exit_code = 0
    try:
        try:
            import webview
        except ImportError:
            webbrowser.open(url)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        else:
            try:
                WEBVIEW_WINDOW = webview.create_window(
                    APP_NAME,
                    url,
                    js_api=LauncherApi(),
                    width=960,
                    height=670,
                    min_size=(760, 600),
                    background_color="#070b12",
                )
                webview.start(debug=False)
            except Exception as exc:
                # Do not reopen the app in a browser after its native window closes.
                # Keep a small diagnostic for reports from machines with a broken WebView runtime.
                ensure_state_dir()
                (STATE_DIR / "launcher-error.log").write_text(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{type(exc).__name__}: {exc}\n",
                    encoding="utf-8",
                )
                exit_code = 1
            finally:
                WEBVIEW_WINDOW = None
    finally:
        stop_server()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
