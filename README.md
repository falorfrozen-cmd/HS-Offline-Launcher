# HS Offline Launcher

A simple Windows launcher for playing the Steam edition of Hero Siege offline.

## What it does

- finds the Steam installation automatically;
- starts `bin/Hero_Siege.exe` directly with Steam AppID `269210`;
- confirms that the protected launcher and EAC service are inactive;
- never patches the game executable, replaces DLLs, or stops EAC;
- does not require WeMod or Aurie.

## Usage

1. Own and install Hero Siege through Steam.
2. Download and open `HS Offline Launcher.exe` from the latest release.
3. Confirm that the selected path ends in `HeroSiege\bin\Hero_Siege.exe`.
4. Press **Launch Hero Siege Offline**.
5. Use offline/single-player characters only.

If protection is already active, the launcher refuses to start the game. Use
this application only for offline/single-player characters.

## Development

```powershell
python .\src\hs_offline_launcher.py
```

This first stage contains no gameplay tools. LootForge, StatForge, ForgePact,
and other offline modules can be integrated behind a separate module boundary.
