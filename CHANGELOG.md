# Changelog

All notable changes to Ally Center will be documented in this file.

## [Unreleased] - fork (github.com/lex/allycenter)

Verified against a ROG Xbox Ally X (RC73XA, Ryzen AI Z2 Extreme, SteamOS 6.16.12).
See `docs/hardware-survey-rc73xa.md`. Backend only — no frontend changes yet.

### Bug Fixes

- **Settings are now actually applied at startup.** `_main()` only loaded settings and
  never pushed them to hardware, so after a reboot the UI showed your saved values while
  the device ignored them — most visibly, RGB stayed on after being switched off.
- **Fixed charge limit writing to a path that does not exist.** It was looking under
  `asus-nb-wmi`; the real node is `/sys/class/power_supply/BAT0/charge_control_end_threshold`.
  `set_charge_limit` also returned success when it had written nothing.
- **Fixed screen brightness get/set**, which walked `/sys/class/backlight/amdgpu_bl0`
  as if it contained per-device subdirectories. It never could succeed: reads always
  returned a hardcoded 100 and writes always failed.
- **Fixed TDP being written as one value to all power limits.** pl1/pl2/pl3 are a
  staircase (stock 17/21/26); they are now derived proportionally and clamped to the
  firmware's real ranges. The old 5–30W clamp allowed 5W, which is below the minimum
  for all three limits and was silently ignored by firmware.
- **Fixed fan readings coming from the wrong device.** `acpi_fan` also exposes
  `fan1_input` and could win the scan; hwmon is now resolved by name. Both fans
  (`cpu_fan`, `gpu_fan`) are reported.
- Removed a duplicate `set_charge_limit` definition that silently shadowed the first.

### New

- **Re-apply settings on resume from suspend.** The MCU resets the joystick rings across
  a sleep cycle, so RGB was lost on wake even within a single boot.
- **"Apply On Startup" toggle** in the Performance section, which also surfaces when the
  crash guard has disabled itself.
- Live APU package power is now reported from `hwmon/amdgpu/power1_input`; `get_current_tdp`
  previously hardcoded `tdp` to `0`.

### Changed

- **Charge limit is now read-only**, displayed but not settable. SteamOS owns this
  setting and exposes it in Settings > Power; the plugin writing it silently reverted
  the user's choice. Verified: SteamOS's `get-max-charge-level` works on this device.
- Removed the unused `gpu_clock` field from `PERFORMANCE_PROFILES` — no code ever read it.

### Notes on scope

Probing `steamos-manager` showed SteamOS has **no working TDP control** on this device
(`UnknownInterface 'TdpLimit1'`), so power limits remain the plugin's job. SMT and CPU
boost are also kept: the DBus methods exist but SteamOS does not expose them in its UI,
and there is no SMT method at all. Controller remapping/deadzones/rumble are deliberately
not implemented despite `hid_asus_ally` exposing them, because Steam Input already does.
See `docs/steamos-overlap.md`.

- `apply_on_startup` setting (default on) to control whether settings are re-applied at boot.
- Crash sentinel: if a previous startup apply did not complete, the next start skips it
  and disables startup apply rather than reboot-looping into the same fault.
- Startup no longer restores Download mode, so a crash during it cannot leave the device
  dark and power-limited on next boot.
- Charge limit is now read from hardware rather than assumed, so SteamOS's own charge
  limit setting is respected instead of being overwritten at boot.

## [1.1.0] - 2026-01-03

### New Features

- Added RGB speed slider - control how fast animated effects run (Pulse, Spectrum, Wave, Flash)
- Added CPU Settings section with SMT and CPU Boost toggles

### Bug Fixes

- Fixed fan presets - Quiet, Balanced, and Performance now work correctly
- Fixed RGB Battery Level effect to properly show green (full) to red (empty)

### Improvements

- Cleaner popup dialogs for Device Info and About screens
- Added release automation script for developers

### Removed

- Removed Controller section (gyroscope and vibration were not functional)

---

## [1.0.0] - Initial Release

### Features

- **RGB Lighting** - Color picker, brightness control, and animated effects
- **Battery Health** - Monitor battery status and set charge limits
- **Performance Profiles** - Quick TDP presets (Silent, Balanced, Turbo, Max)
- **Fan Control** - Choose between Quiet, Balanced, Performance, or Auto
- **Download Mode** - Turn off screen while downloading games
- **Device Info** - View hardware and system information
- **Persistent Settings** - All settings saved across reboots
