from __future__ import annotations

import http.client
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import hs_offline_launcher as launcher  # noqa: E402


def write_test_pe(path: Path, *, sections: tuple[str, ...] = (".text",), machine: int = 0x8664) -> Path:
    """Create the smallest PE metadata fixture needed by the launcher checks."""
    pe_offset = 0x80
    optional_header_size = 0xF0
    section_table = pe_offset + 24 + optional_header_size
    raw_data = section_table + len(sections) * 40
    data = bytearray(max(1024, raw_data + len(sections) * 16))
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe_offset)
    data[pe_offset : pe_offset + 4] = b"PE\0\0"
    struct.pack_into("<H", data, pe_offset + 4, machine)
    struct.pack_into("<H", data, pe_offset + 6, len(sections))
    struct.pack_into("<H", data, pe_offset + 20, optional_header_size)
    struct.pack_into("<H", data, pe_offset + 22, 0x0002)
    struct.pack_into("<H", data, pe_offset + 24, 0x020B)
    for index, name in enumerate(sections):
        encoded = name.encode("ascii")[:8].ljust(8, b"\0")
        offset = section_table + index * 40
        data[offset : offset + 8] = encoded
        struct.pack_into("<I", data, offset + 8, 16)
        struct.pack_into("<I", data, offset + 16, 16)
        struct.pack_into("<I", data, offset + 20, raw_data + index * 16)
        data[raw_data + index * 16 : raw_data + (index + 1) * 16] = bytes([index + 1]) * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class GameValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.exe = write_test_pe(self.root / "HeroSiege" / "bin" / "Hero_Siege.exe")

    def tearDown(self) -> None:
        launcher.HASH_CACHE.clear()
        self.temp_dir.cleanup()

    def add_runtime(self, directory: Path | None = None) -> Path:
        target = (directory or self.exe.parent) / launcher.STEAM_RUNTIME_NAME
        target.write_bytes(b"steam runtime fixture")
        return target

    def test_small_valid_x64_pe_is_accepted(self) -> None:
        self.add_runtime()

        valid, reason = launcher.validate_game(self.exe)

        self.assertTrue(valid)
        self.assertEqual(reason, "Clean Steam executable")
        details = launcher.game_details(self.exe)
        self.assertEqual(details["build"], "New/unknown Steam build")

    def test_runtime_in_game_root_is_supported(self) -> None:
        self.add_runtime(self.exe.parent.parent)

        valid, _ = launcher.validate_game(self.exe)

        self.assertTrue(valid)
        self.assertEqual(launcher.find_steam_runtime(self.exe).parent, self.exe.parent.parent)

    def test_aurie_section_is_allowed_and_reported(self) -> None:
        write_test_pe(self.exe, sections=(".text", ".aurie"))
        self.add_runtime()

        valid, reason = launcher.validate_game(self.exe)
        details = launcher.game_details(self.exe)

        self.assertTrue(valid)
        self.assertIn("offline use only", reason)
        self.assertTrue(details["modified"])
        self.assertEqual(details["build"], "Modified/custom Hero Siege build")

    def test_missing_runtime_has_actionable_error(self) -> None:
        valid, reason = launcher.validate_game(self.exe)

        self.assertFalse(valid)
        self.assertIn("Verify Hero Siege files in Steam", reason)
        self.assertEqual(launcher.game_details(self.exe)["build"], "Invalid selection")

    def test_non_pe_and_32_bit_files_are_rejected(self) -> None:
        self.add_runtime()
        self.exe.write_bytes(b"not a PE file")
        self.assertFalse(launcher.validate_game(self.exe)[0])

        write_test_pe(self.exe, machine=0x014C)
        valid, reason = launcher.validate_game(self.exe)
        self.assertFalse(valid)
        self.assertIn("not 64-bit", reason)

    def test_pe_without_real_section_data_is_rejected(self) -> None:
        self.add_runtime()
        payload = bytearray(self.exe.read_bytes())
        pe_offset = struct.unpack_from("<I", payload, 0x3C)[0]
        optional_header_size = struct.unpack_from("<H", payload, pe_offset + 20)[0]
        section_table = pe_offset + 24 + optional_header_size
        struct.pack_into("<I", payload, section_table + 16, 0)
        struct.pack_into("<I", payload, section_table + 20, 0)
        self.exe.write_bytes(payload)

        valid, reason = launcher.validate_game(self.exe)

        self.assertFalse(valid)
        self.assertIn("no file data", reason)

    def test_missing_selection_is_not_rendered_as_dot(self) -> None:
        details = launcher.game_details(None)

        self.assertEqual(details["path"], "")
        self.assertEqual(details["build"], "Game not found")


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.library = self.root / "SteamLibrary"
        self.steamapps = self.library / "steamapps"
        self.install_name = "Custom Hero Siege Folder"
        self.exe = write_test_pe(
            self.steamapps / "common" / self.install_name / "bin" / "Hero_Siege.exe"
        )
        (self.exe.parent / launcher.STEAM_RUNTIME_NAME).write_bytes(b"runtime")
        self.steamapps.mkdir(parents=True, exist_ok=True)
        (self.steamapps / f"appmanifest_{launcher.APP_ID}.acf").write_text(
            '"AppState"\n{\n  "appid" "269210"\n  "installdir" "Custom Hero Siege Folder"\n}\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_manifest_install_directory_is_used(self) -> None:
        with mock.patch.object(launcher, "steam_libraries", return_value=[self.library]):
            found = launcher.discover_game_exe()

        self.assertEqual(found, self.exe)

    def test_modern_library_parser_does_not_treat_app_id_as_a_path(self) -> None:
        steam_root = self.root / "Steam"
        extra_library = self.root / "ExtraLibrary"
        vdf = steam_root / "steamapps" / "libraryfolders.vdf"
        vdf.parent.mkdir(parents=True)
        escaped = str(extra_library).replace("\\", "\\\\")
        vdf.write_text(
            f'"libraryfolders"\n{{\n "1"\n {{\n  "path" "{escaped}"\n'
            '  "apps"\n  {\n   "269210" "123456789"\n  }\n }\n}\n',
            encoding="utf-8",
        )

        with mock.patch.object(launcher, "registry_steam_roots", return_value=[steam_root]):
            libraries = launcher.steam_libraries()

        self.assertIn(extra_library, libraries)
        self.assertNotIn(Path("123456789"), libraries)

    def test_stale_saved_path_is_recovered(self) -> None:
        state_dir = self.root / "state"
        config_path = state_dir / "config.json"
        action_log_path = state_dir / "launcher.log"
        state_dir.mkdir()
        config_path.write_text(json.dumps({"game_exe": r"F:\OldLibrary\Hero_Siege.exe"}), encoding="utf-8")

        with (
            mock.patch.object(launcher, "STATE_DIR", state_dir),
            mock.patch.object(launcher, "CONFIG_PATH", config_path),
            mock.patch.object(launcher, "ACTION_LOG_PATH", action_log_path),
            mock.patch.object(launcher, "steam_libraries", return_value=[self.library]),
        ):
            config = launcher.load_config()

        self.assertEqual(config["game_exe"], str(self.exe))
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["game_exe"], str(self.exe))


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.exe = write_test_pe(self.root / "HeroSiege" / "bin" / "Hero_Siege.exe")
        (self.exe.parent / launcher.STEAM_RUNTIME_NAME).write_bytes(b"runtime")

    def tearDown(self) -> None:
        launcher.HASH_CACHE.clear()
        self.temp_dir.cleanup()

    def status(self, *, steam_found: bool = True) -> dict:
        with (
            mock.patch.object(launcher, "load_config", return_value={"game_exe": str(self.exe)}),
            mock.patch.object(launcher, "processes", return_value=[(10, "steam.exe")]),
            mock.patch.object(launcher, "eac_service_status", return_value="stopped"),
            mock.patch.object(launcher, "steam_exe", return_value=Path("steam.exe") if steam_found else None),
        ):
            return launcher.current_status()

    def test_valid_unknown_build_is_ready(self) -> None:
        status = self.status()

        self.assertTrue(status["ready"])
        self.assertEqual(status["blocker"], "")
        self.assertEqual(status["game"]["build"], "New/unknown Steam build")

    def test_invalid_game_exposes_blocker(self) -> None:
        (self.exe.parent / launcher.STEAM_RUNTIME_NAME).unlink()

        status = self.status()

        self.assertFalse(status["ready"])
        self.assertIn("steam_api64.dll is missing", status["blocker"])

    def test_missing_steam_is_not_ready(self) -> None:
        with (
            mock.patch.object(launcher, "load_config", return_value={"game_exe": str(self.exe)}),
            mock.patch.object(launcher, "processes", return_value=[]),
            mock.patch.object(launcher, "eac_service_status", return_value="stopped"),
            mock.patch.object(launcher, "steam_exe", return_value=None),
        ):
            status = launcher.current_status()

        self.assertFalse(status["ready"])
        self.assertIn("Steam was not found", status["blocker"])

    def test_running_game_with_eac_shows_protection_warning(self) -> None:
        with (
            mock.patch.object(launcher, "load_config", return_value={"game_exe": str(self.exe)}),
            mock.patch.object(
                launcher,
                "processes",
                return_value=[(10, "steam.exe"), (20, "Hero_Siege.exe"), (30, "EasyAntiCheat_EOS.exe")],
            ),
            mock.patch.object(launcher, "eac_service_status", return_value="running"),
            mock.patch.object(launcher, "steam_exe", return_value=Path("steam.exe")),
        ):
            status = launcher.current_status()

        self.assertFalse(status["safe"])
        self.assertIn("Hero Siege and EAC are active", status["blocker"])

    def test_unknown_eac_state_fails_closed(self) -> None:
        with (
            mock.patch.object(launcher, "load_config", return_value={"game_exe": str(self.exe)}),
            mock.patch.object(launcher, "processes", return_value=[(10, "steam.exe")]),
            mock.patch.object(launcher, "eac_service_status", return_value="unknown"),
            mock.patch.object(launcher, "steam_exe", return_value=Path("steam.exe")),
        ):
            status = launcher.current_status()

        self.assertFalse(status["safe"])
        self.assertFalse(status["ready"])
        self.assertIn("could not be verified", status["blocker"])

    def test_process_scan_failure_fails_closed(self) -> None:
        with (
            mock.patch.object(launcher, "load_config", return_value={"game_exe": str(self.exe)}),
            mock.patch.object(launcher, "processes", side_effect=OSError("snapshot failed")),
            mock.patch.object(launcher, "eac_service_status", return_value="stopped"),
            mock.patch.object(launcher, "steam_exe", return_value=Path("steam.exe")),
        ):
            status = launcher.current_status()

        self.assertFalse(status["processScanOk"])
        self.assertFalse(status["safe"])
        self.assertFalse(status["ready"])
        self.assertIn("processes could not be verified", status["blocker"])

    def test_ui_uses_backend_readiness_and_visible_blocker(self) -> None:
        self.assertIn("!s.ready", launcher.HTML)
        self.assertIn("s.blocker", launcher.HTML)
        self.assertIn("playing||s.ready?s.game.validation:s.blocker", launcher.HTML)
        self.assertIn('id="validation"', launcher.HTML)

    def test_ui_distinguishes_unknown_eac_from_active(self) -> None:
        self.assertIn("s.eacService==='running'", launcher.HTML)
        self.assertIn("s.eacService==='transitioning'", launcher.HTML)
        self.assertIn("eacPending?'CHECKING':'UNKNOWN'", launcher.HTML)


class EacServiceTests(unittest.TestCase):
    def test_numeric_service_state_is_locale_independent(self) -> None:
        with mock.patch.object(
            launcher,
            "windows_service_state",
            return_value=(launcher.SERVICE_RUNNING, 0),
        ) as query:
            status = launcher.eac_service_status()

        self.assertEqual(status, "running")
        query.assert_called_once_with(launcher.EAC_SERVICE_NAME)

    def test_stopped_and_transitioning_states_are_distinguished(self) -> None:
        with mock.patch.object(
            launcher,
            "windows_service_state",
            return_value=(launcher.SERVICE_STOPPED, 0),
        ):
            self.assertEqual(launcher.eac_service_status(), "stopped")
        with mock.patch.object(
            launcher,
            "windows_service_state",
            return_value=(2, 0),
        ):
            self.assertEqual(launcher.eac_service_status(), "transitioning")

    def test_only_missing_service_is_treated_as_inactive(self) -> None:
        with mock.patch.object(
            launcher,
            "windows_service_state",
            return_value=(None, launcher.ERROR_SERVICE_DOES_NOT_EXIST),
        ):
            self.assertEqual(launcher.eac_service_status(), "not-installed")
        with mock.patch.object(
            launcher,
            "windows_service_state",
            return_value=(None, 5),
        ):
            self.assertEqual(launcher.eac_service_status(), "unknown")


class LaunchSafetyTests(unittest.TestCase):
    def test_second_launch_request_is_rejected_while_one_is_in_progress(self) -> None:
        acquired = launcher.LAUNCH_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            result = launcher.launch_game()
        finally:
            launcher.LAUNCH_LOCK.release()

        self.assertIn("already in progress", result["err"])

    def test_process_scan_failure_blocks_launch_before_side_effects(self) -> None:
        with (
            mock.patch.object(launcher, "load_config", return_value={"game_exe": "Hero_Siege.exe"}),
            mock.patch.object(launcher, "validate_game", return_value=(True, "valid")),
            mock.patch.object(launcher, "processes", side_effect=OSError("snapshot failed")),
            mock.patch.object(launcher, "start_steam_if_needed") as start_steam,
            mock.patch.object(launcher.subprocess, "Popen") as popen,
        ):
            result = launcher.launch_game_locked()

        self.assertIn("launch was blocked", result["err"])
        start_steam.assert_not_called()
        popen.assert_not_called()

    def test_eac_is_rechecked_after_waiting_for_steam(self) -> None:
        with (
            mock.patch.object(launcher, "load_config", return_value={"game_exe": "Hero_Siege.exe"}),
            mock.patch.object(launcher, "validate_game", return_value=(True, "valid")),
            mock.patch.object(
                launcher,
                "processes",
                side_effect=[[], [(42, "EasyAntiCheat_EOS.exe")]],
            ),
            mock.patch.object(launcher, "eac_service_status", return_value="stopped"),
            mock.patch.object(launcher, "start_steam_if_needed", return_value=(True, "Steam started")),
            mock.patch.object(launcher.subprocess, "Popen") as popen,
        ):
            result = launcher.launch_game_locked()

        self.assertIn("EAC is currently active", result["err"])
        popen.assert_not_called()


class FolderOpeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.exe = write_test_pe(self.root / "HeroSiege" / "bin" / "Hero_Siege.exe")
        (self.exe.parent / launcher.STEAM_RUNTIME_NAME).write_bytes(b"runtime")

    def tearDown(self) -> None:
        launcher.HASH_CACHE.clear()
        self.temp_dir.cleanup()

    def test_only_validated_game_directory_is_opened(self) -> None:
        with (
            mock.patch.object(launcher, "load_config", return_value={"game_exe": str(self.exe)}),
            mock.patch.object(launcher.subprocess, "Popen") as popen,
        ):
            result = launcher.open_game_folder()

        self.assertEqual(result, {"ok": "Game folder opened"})
        popen.assert_called_once_with([str(launcher.EXPLORER_EXE), str(self.exe.resolve().parent)])

    def test_invalid_config_is_not_forwarded_to_explorer(self) -> None:
        with (
            mock.patch.object(launcher, "load_config", return_value={"game_exe": str(self.root)}),
            mock.patch.object(launcher, "discover_game_exe", return_value=None),
            mock.patch.object(launcher.subprocess, "Popen") as popen,
        ):
            result = launcher.open_game_folder()

        self.assertEqual(result, {"err": "Game folder was not found"})
        popen.assert_not_called()


class LocalServerSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = launcher.LauncherServer(("127.0.0.1", 0), launcher.Handler)
        self.thread = launcher.threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_root_injects_session_token(self) -> None:
        status, body = self.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertIn(launcher.API_TOKEN.encode("ascii"), body)
        self.assertNotIn(b"__HS_API_TOKEN__", body)

    def test_api_requires_token(self) -> None:
        with mock.patch.object(launcher, "current_status", return_value={"ready": True}):
            denied, _ = self.request("GET", "/api/status")
            allowed, body = self.request(
                "GET",
                "/api/status",
                {launcher.API_HEADER: launcher.API_TOKEN},
            )

        self.assertEqual(denied, 403)
        self.assertEqual(allowed, 200)
        self.assertEqual(json.loads(body), {"ready": True})

    def test_bad_host_is_rejected(self) -> None:
        status, _ = self.request("GET", "/", {"Host": "attacker.invalid"})

        self.assertEqual(status, 403)

    def test_foreign_origin_and_preflight_are_rejected(self) -> None:
        headers = {
            launcher.API_HEADER: launcher.API_TOKEN,
            "Origin": "https://example.invalid",
        }
        with mock.patch.object(launcher, "launch_game") as launch:
            post_status, _ = self.request("POST", "/api/launch", headers)
            options_status, _ = self.request("OPTIONS", "/api/launch", headers)

        self.assertEqual(post_status, 403)
        self.assertEqual(options_status, 403)
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
