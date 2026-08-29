#!/usr/bin/env python3
"""HS Offline Launcher.

Starts the clean Steam Hero_Siege.exe directly with Steam's public AppID in
the child environment.  It does not patch game files, stop services, install
DLLs, or load any gameplay tool.  Tool integration deliberately lives outside
this first-stage launcher.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
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
    "0766aa8bfc6eb5679df46f78546644e34fae333adc22474f96903c1d68f251f5": "Season 10 · 2026.08.24",
    "ba72b95ac10785d0ecdcc2b3d1925d6cb3439efaf4cef9de2ea1f67d6cfdd4df": "Season 10 · 2026.08.22 legacy",
}

CREATE_NO_WINDOW = 0x08000000
WEBVIEW_WINDOW = None
SERVER = None
SERVER_THREAD = None
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
    if not cfg.get("game_exe"):
        found = discover_game_exe()
        if found:
            cfg["game_exe"] = str(found)
            save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    ensure_state_dir()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def registry_steam_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        import winreg

        for hive, key_name in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
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
            for raw in re.findall(r'"path"\s+"([^"]+)"', text, flags=re.IGNORECASE):
                libraries.append(Path(raw.replace(r"\\", "\\")))
        except OSError:
            continue
    return unique_paths(libraries)


def discover_game_exe() -> Path | None:
    for library in steam_libraries():
        candidate = library / "steamapps" / "common" / "HeroSiege" / "bin" / "Hero_Siege.exe"
        if candidate.is_file():
            return candidate
    return None


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


def processes() -> list[tuple[int, str]]:
    if os.name != "nt":
        return []
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        return []
    rows: list[tuple[int, str]] = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            rows.append((int(entry.th32ProcessID), entry.szExeFile))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return rows


def matching_processes(*names: str) -> list[tuple[int, str]]:
    wanted = {name.lower() for name in names}
    return [(pid, name) for pid, name in processes() if name.lower() in wanted]


def eac_service_status() -> str:
    try:
        completed = subprocess.run(
            ["sc.exe", "query", "EasyAntiCheat_EOS"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=CREATE_NO_WINDOW,
        )
        match = re.search(r":\s*(\d+)\s+([A-Z_]+)", completed.stdout, flags=re.IGNORECASE)
        if match:
            return "running" if match.group(1) == "4" else "stopped"
        return "not-installed" if completed.returncode else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def eac_processes() -> list[tuple[int, str]]:
    return matching_processes(
        "EasyAntiCheat.exe",
        "EasyAntiCheat_EOS.exe",
        "EasyAntiCheat_EOSSys.exe",
        "start_protected_game.exe",
    )


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


def validate_game(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "Hero_Siege.exe was not found"
    if path.name.lower() != "hero_siege.exe":
        return False, "Select the clean Hero_Siege.exe, not a modded or protected launcher"
    try:
        if path.stat().st_size < 50_000_000 or path.open("rb").read(2) != b"MZ":
            return False, "The selected file is not a valid Hero Siege Windows executable"
        if b".aurie" in path.open("rb").read(4096):
            return False, "The selected executable contains an Aurie patch; select the clean Steam EXE"
    except OSError as exc:
        return False, f"The executable could not be read: {exc}"
    if not (path.parent / "steam_api64.dll").is_file():
        return False, "Steam runtime was not found beside the selected EXE"
    return True, "Clean Steam executable"


def game_details(path: Path) -> dict:
    valid, reason = validate_game(path)
    result = {
        "path": str(path),
        "exists": path.is_file(),
        "valid": valid,
        "validation": reason,
        "size": path.stat().st_size if path.is_file() else 0,
        "hash": "",
        "build": "Not selected",
        "known": False,
    }
    if valid:
        try:
            digest = file_sha256(path)
            result.update(hash=digest, build=KNOWN_BUILDS.get(digest, "New/unknown Steam build"), known=digest in KNOWN_BUILDS)
        except OSError as exc:
            result.update(validation=f"Hash check failed: {exc}")
    return result


def current_status() -> dict:
    cfg = load_config()
    path = Path(cfg.get("game_exe") or "")
    rows = processes()
    names = {name.lower() for _, name in rows}
    game_rows = [(pid, name) for pid, name in rows if name.lower() == "hero_siege.exe"]
    eac_rows = [(pid, name) for pid, name in rows if name.lower() in {
        "easyanticheat.exe", "easyanticheat_eos.exe", "easyanticheat_eossys.exe", "start_protected_game.exe"
    }]
    service = eac_service_status()
    return {
        "appId": APP_ID,
        "game": game_details(path),
        "steamRunning": "steam.exe" in names,
        "steamFound": bool(steam_exe()),
        "gameRunning": bool(game_rows),
        "gamePid": game_rows[0][0] if game_rows else 0,
        "eacService": service,
        "eacProcesses": [{"pid": pid, "name": name} for pid, name in eac_rows],
        "safe": service != "running" and not eac_rows,
        "lastLaunch": dict(LAST_LAUNCH),
    }


def start_steam_if_needed() -> tuple[bool, str]:
    if matching_processes("steam.exe"):
        return True, "Steam is running"
    exe = steam_exe()
    if not exe:
        return False, "Steam was not found"
    try:
        subprocess.Popen([str(exe), "-silent"], cwd=str(exe.parent))
    except OSError as exc:
        return False, f"Steam could not be started: {exc}"
    for _ in range(20):
        time.sleep(0.5)
        if matching_processes("steam.exe"):
            return True, "Steam started"
    return False, "Steam did not become ready"


def launch_game() -> dict:
    cfg = load_config()
    path = Path(cfg.get("game_exe") or "")
    valid, reason = validate_game(path)
    if not valid:
        return {"err": reason}
    if matching_processes("Hero_Siege.exe"):
        return {"err": "Hero Siege is already running"}
    if eac_service_status() == "running" or eac_processes():
        return {"err": "EAC is currently active. Close the protected game/session first; this launcher will not stop or modify EAC."}

    steam_ok, steam_message = start_steam_if_needed()
    if not steam_ok:
        return {"err": steam_message}

    child_env = os.environ.copy()
    child_env["SteamAppId"] = APP_ID
    child_env["SteamGameId"] = APP_ID
    try:
        process = subprocess.Popen([str(path)], cwd=str(path.parent), env=child_env)
    except OSError as exc:
        return {"err": f"Hero Siege could not be launched: {exc}"}

    LAST_LAUNCH.update(pid=process.pid, time=time.strftime("%H:%M:%S"), message="Direct offline launch requested")
    threading.Thread(target=verify_launch, args=(process.pid,), daemon=True).start()
    return {"ok": f"Hero Siege started directly (PID {process.pid}). No game file was changed.", "pid": process.pid}


def verify_launch(pid: int) -> None:
    time.sleep(5)
    running = any(row_pid == pid for row_pid, _ in matching_processes("Hero_Siege.exe"))
    service = eac_service_status()
    protected = eac_processes()
    if running and service != "running" and not protected:
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
    current = Path(load_config().get("game_exe") or "")
    return current.parent if current.parent.is_dir() else Path.home()


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
    path = Path(load_config().get("game_exe") or "")
    target = path.parent if path.parent.is_dir() else discover_game_exe()
    if target and Path(target).is_file():
        target = Path(target).parent
    if not target or not Path(target).is_dir():
        return {"err": "Game folder was not found"}
    try:
        subprocess.Popen(["explorer.exe", str(target)])
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
.badge{border:1px solid #245d50;background:#102721;color:var(--mint);border-radius:30px;padding:10px 15px;font-weight:700;font-size:12px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:28px 0 18px}.stat{background:rgba(15,24,36,.9);border:1px solid var(--line);border-radius:12px;padding:16px}.stat small{display:block;color:var(--muted);margin-bottom:8px}.stat strong{font-size:15px}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;background:var(--amber);box-shadow:0 0 12px currentColor}.good{color:var(--mint)}.bad{color:var(--red)}.warn{color:var(--amber)}
.card{background:linear-gradient(145deg,rgba(18,29,43,.96),rgba(12,20,31,.96));border:1px solid var(--line);border-radius:16px;padding:22px;margin-top:14px;box-shadow:0 18px 50px rgba(0,0,0,.2)}.card h2{font-size:16px;margin:0 0 16px}.pathrow{display:grid;grid-template-columns:1fr auto auto;gap:10px}.path{background:#080d15;border:1px solid #26394c;border-radius:9px;padding:13px 14px;color:#b8c7d6;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:Consolas,monospace;font-size:12px}
button{border:0;border-radius:9px;padding:0 16px;font-weight:700;color:#dce8f2;background:#1b2a3b;cursor:pointer;transition:.18s}button:hover{transform:translateY(-1px);filter:brightness(1.15)}button:disabled{opacity:.45;cursor:not-allowed;transform:none}.launch{width:100%;height:64px;margin-top:16px;background:linear-gradient(100deg,#168fb7,#26c2dc);color:#031018;font-size:17px;box-shadow:0 10px 28px rgba(36,190,222,.18)}
.buildline{margin-top:12px;color:var(--muted);font-size:12px}.buildline strong{color:#b9c9d8}.activity{color:#91a5ba;min-height:74px}.activity strong{color:#dbe9f6}
@media(max-width:760px){.main{padding:24px}.grid{grid-template-columns:1fr}.pathrow{grid-template-columns:1fr 1fr}.path{grid-column:1/-1}}
</style></head>
<body><div class="shell"><main class="main"><div class="top"><div class="brand"><b>HS</b> OFFLINE LAUNCHER</div><div class="badge" id="overall">CHECKING</div></div><div class="title">Play Hero Siege Offline</div><div class="desc">Select the game and press Launch.</div>
<div class="grid" style="grid-template-columns:repeat(3,1fr)"><div class="stat"><small>STEAM</small><strong id="steam"><i class="dot"></i>Checking</strong></div><div class="stat"><small>PROTECTION</small><strong id="eac"><i class="dot"></i>Checking</strong></div><div class="stat"><small>GAME</small><strong id="game"><i class="dot"></i>Checking</strong></div></div>
<section class="card"><h2>Game Location</h2><div class="pathrow"><div class="path" id="path">Detecting game…</div><button onclick="browse()">Browse</button><button onclick="folder()">Folder</button></div><div class="buildline">Build: <strong id="build">—</strong></div><button class="launch" id="launch" onclick="launchGame()">▶ LAUNCH OFFLINE</button></section>
<section class="card activity"><h2>Status</h2><div id="activity"><strong>Ready.</strong></div></section></main></div>
<script>
const $=id=>document.getElementById(id);let busy=false;
async function api(path,method='GET'){const r=await fetch(path,{method});return r.json()}
function state(el,good,text){el.className=good===true?'good':good===false?'bad':'warn';el.innerHTML='<i class="dot"></i>'+text}
async function refresh(){try{const s=await api('/api/status');$('path').textContent=s.game.path||'Not selected';$('build').textContent=s.game.build;state($('steam'),s.steamRunning,s.steamRunning?'Running':(s.steamFound?'Ready':'Not found'));state($('eac'),s.safe,s.safe?'Inactive':'ACTIVE');state($('game'),s.gameRunning,s.gameRunning?'Running':'Closed');const ready=s.game.valid&&s.safe&&!s.gameRunning;$('launch').disabled=busy||!ready;$('overall').textContent=s.gameRunning&&s.safe?'PLAYING OFFLINE':ready?'READY':s.gameRunning?'CHECK':'SETUP NEEDED';$('activity').innerHTML='<strong>'+s.lastLaunch.message+'</strong>'}catch(e){$('activity').textContent='Status error: '+e}}
async function launchGame(){busy=true;$('launch').disabled=true;$('activity').innerHTML='<strong>Launching…</strong> Waiting for the clean game process.';const r=await api('/api/launch','POST');busy=false;$('activity').innerHTML=r.err?'<strong class="bad">'+r.err+'</strong>':'<strong class="good">'+r.ok+'</strong>';refresh()}
async function browse(){if(busy)return;busy=true;$('launch').disabled=true;$('activity').innerHTML='<strong>Opening the game picker…</strong>';try{let r;if(window.pywebview&&window.pywebview.api){r=await window.pywebview.api.select_exe()}else{r=await api('/api/select','POST')}if(r.err){$('activity').innerHTML='<strong class="bad">'+r.err+'</strong>'}else if(r.cancelled){$('activity').innerHTML='<strong>Selection cancelled.</strong>'}else{$('activity').innerHTML='<strong class="good">'+r.ok+'</strong>'}}catch(e){$('activity').innerHTML='<strong class="bad">Browse failed: '+e+'</strong>'}finally{busy=false;await refresh()}}async function folder(){const r=await api('/api/folder','POST');if(r.err)$('activity').innerHTML='<strong class="bad">'+r.err+'</strong>'}
refresh();setInterval(refresh,2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def json_response(self, value: dict, status: int = 200) -> None:
        payload = json.dumps(value).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The WebView can close while its final status refresh is in flight.
            pass

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/api/status":
            self.json_response(current_status())
            return
        if route == "/":
            payload = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/api/launch":
            self.json_response(launch_game())
        elif route == "/api/select":
            self.json_response(select_exe_fallback())
        elif route == "/api/folder":
            self.json_response(open_game_folder())
        else:
            self.send_error(404)


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
