# HS Offline Launcher

A simple Windows launcher for playing the Steam edition of Hero Siege offline.

## What it does

- finds the Steam installation automatically, including custom library folders;
- recovers automatically when Steam has moved the game since the last launch;
- starts `bin/Hero_Siege.exe` directly with Steam AppID `269210`;
- confirms that the protected launcher and EAC service are inactive;
- never patches the game executable, replaces DLLs, or stops EAC;
- does not require WeMod or Aurie.

A clean Steam executable is recommended. An executable already prepared for
Aurie/ForgePact is identified as a modified build and may also be started, but
only while protection is inactive and only for offline/single-player use.

## Usage

1. Own and install Hero Siege through Steam.
2. Download and open `HS-Offline-Launcher.exe` from the latest release.
3. Confirm that the selected path ends in `HeroSiege\bin\Hero_Siege.exe`.
4. Press **Launch Hero Siege Offline**.
5. Use offline/single-player characters only.

If protection is already active, the launcher refuses to start the game. Use
this application only for offline/single-player characters.

If the launcher reports **Setup Needed**, the exact reason is shown below the
build name. For a missing `steam_api64.dll`, use Steam's **Verify integrity of
game files** action and reopen the launcher.

## Development

```powershell
python .\src\hs_offline_launcher.py
python -m unittest discover -s tests -v
```

To create the Windows release executable from a fresh, pinned packaging
environment (Python 3.13):

```powershell
.\build.ps1
```

The canonical outputs are `dist\HS-Offline-Launcher.exe` and
`dist\SHA256SUMS.txt`. The build uses no UPX compression and embeds the public
product name `HS Offline Launcher`.

This first stage contains no gameplay tools. LootForge, StatForge, ForgePact,
and other offline modules can be integrated behind a separate module boundary.
