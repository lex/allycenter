# What SteamOS already does — and what it can't

Probed 2026-07-25 on the ROG Xbox Ally X (RC73XA). This decides which features are worth
the plugin carrying, so it doesn't become a second control panel fighting the first.

## steamos-manager

DBus: `com.steampowered.SteamOSManager1` at `/com/steampowered/SteamOSManager1`.
CLI: `steamosctl` (`get-all-properties` for a full dump).

Methods present: `SetTdpLimit`, `SetMaxChargeLevel`, `SetPerformanceProfile`,
`SetCpuBoostState`, `SetCpuScalingGovernor`, `SetCpuScheduler`, `SetFanSpeed`,
`SetGpuPerformanceLevel`, `SetGpuPowerProfile`, `SetManualGpuClock`, `UpdateBios`,
`UpdateDock`, `TrimDevices`, `FormatDevice`.

**A method existing does not mean it works on this device.** Actual results:

| Query | Result |
|---|---|
| `get-tdp-limit` | ❌ `UnknownInterface 'com.steampowered.SteamOSManager1.TdpLimit1'` |
| `get-max-charge-level` | ✅ `80` |
| `get-performance-profile` | ✅ `balanced` |
| `get-available-performance-profiles` | ⚠️ empty list |
| `get-fan-control-state` | ✅ `BIOS` |
| `get-cpu-boost-state` | ✅ `enabled` |
| `get-cpu-scaling-governor` | ✅ `powersave` |
| `get-gpu-performance-level` | ✅ `auto` |
| `get-available-gpu-power-profiles` | ❌ sysfs file missing |

Also seen failing in the journal:

```
steamos-manager: ERROR … member: "SetManualGpuClock" …
  Error setting manual GPU clock: Invalid argument (os error 22)
```

## Conclusions

**SteamOS has no working TDP control on this device** — the `TdpLimit1` interface is
absent entirely. ASUS WMI `ppt_*` is the only mechanism that moves sustained power, and
it was measured to work (605 MHz @ 5W vs 2726 MHz @ 30W). This is the plugin's single
most valuable feature.

| Feature | Verdict |
|---|---|
| TDP / power limits | **Keep** — SteamOS cannot do it here |
| Fan mode (`throttle_thermal_policy`) | **Keep, but never write unprompted** — it is the *same knob* as SteamOS's Performance Profile |
| RGB | **Keep** — SteamOS has nothing |
| Download mode | **Keep** — composite feature, no equivalent |
| SMT toggle | **Keep** — no SMT method exists in steamos-manager at all |
| CPU boost | **Keep** — method exists but is not exposed in the SteamOS UI |
| Charge limit | **Drop** — SteamOS owns it, exposes it in its UI, and ours conflicts |
| Controller remap / deadzones / rumble | **Skip** — Steam Input already covers this |
| GPU clocks | **Drop** — `gpu_clock` in `PERFORMANCE_PROFILES` was never applied by any code |

Note the charge-limit conflict is not hypothetical: applying the plugin's stored
`charge_limit` at startup silently reverted a value SteamOS had set (80 → 100).

## throttle_thermal_policy IS the SteamOS Performance Profile

`PerformanceProfile1` (`steamosctl get/set-performance-profile`) is the ACPI platform
profile, and on this device it is backed by
`/sys/devices/platform/asus-nb-wmi/throttle_thermal_policy` — the same file the plugin
writes for "Fan Mode". Verified by writing each value and reading the profile back, and
by setting each profile and reading the policy back:

| `throttle_thermal_policy` | Platform profile |
|---|---|
| 0 | `balanced` |
| 1 | `performance` |
| 2 | `low-power` |

(Profiles 1 and 2 report `platform_profile = custom` via ACPI while `steamosctl` still
names them correctly.)

**The plugin's original mapping was inverted**: `{"quiet": "1", "balanced": "0",
"performance": "2"}` meant selecting Quiet gave Performance and Performance gave
low-power. Fixed to `{"quiet": "2", "balanced": "0", "performance": "1"}`.

Because it is one shared knob, the plugin must not write it at boot or on resume — doing
so silently reverted a low-power selection made in SteamOS settings. It is now only
written when the user explicitly picks a profile or fan mode in the plugin.

### What SteamOS "low-power" actually does

Measured by snapshotting every power-related node at `balanced` and at `low-power`:
**only `throttle_thermal_policy` changes (0 → 2).** `ppt_pl1/pl2/fppt`, CPU governor,
EPP, CPU boost, `scaling_max_freq`, and GPU performance level are all byte-identical.

It is a firmware thermal/fan policy hint to the EC, **not** a power limit. Combined with
SteamOS having no working TDP control on this device, "low-power" does considerably less
than the name implies — the PPT limits stay wherever the plugin set them.

## Controller configuration API (not used, documented for reference)

`hid_asus_ally` exposes a full gamepad config surface at
`/sys/bus/hid/devices/0003:0B05:1B4C.0003/`:

- `gamepad_mode` (644), `vibration_intensity` (644, `"100 100"`, index `left right`),
  `mcu_version`, `apply_all` (write-only), `reset_btn_mapping` (write-only)
- Per-button dirs `btn_{a,b,x,y,dpad_u/d/l/r,lb,rb,lt,rt,ls,rs,m1,m2,menu,view}` each with
  `remap`, `macro_remap`, `turbo`. Defaults e.g. `btn_a/remap = PAD_A`, `btn_m1/remap = KB_F15`.
- `axis_xy_left` / `axis_xy_right`: `deadzone` (`"0 100"`, index `inner outer`),
  `anti_deadzone`, and a 4-point response curve (`curve_move_pct_1..4`,
  `curve_response_pct_1..4`, defaults 22/50/72/100).
- `axis_z_left` / `axis_z_right`: trigger `deadzone` (`"0 100"`).
- `qam_mode` on device `.0006`.

Deliberately unused — Steam Input already provides this and duplicating it would create
two competing sources of truth.

## Persistent ("permanent") LED off

The kernel driver exposes **only runtime** LED state — there are no Aura power-state
attributes (`boot`/`awake`/`sleep`/`shutdown`) on this driver version, confirmed by
enumerating the full attribute list.

Armoury Crate on Windows achieves permanence by sending an Aura power-state HID feature
report followed by a save command, which the MCU commits to its own NVRAM. Because it
lives in the MCU it applies before the OS loads and survives reboot and shutdown.

On Linux this is implemented by **asusctl / asusd** (Luke D. Jones — the same author as
the `hid_asus_ally` driver). Not installed on this device. The device exposes `hidraw`,
so it is reachable from userspace.

**Do not fuzz this.** Guessed HID feature reports to an MCU that persists them to NVRAM
are not reversible the way a sysfs write is. Port asusctl's known-good packet
definitions instead.

## Other safe, reversible controls available but unused

| Control | Path | Notes |
|---|---|---|
| Live APU package power | `hwmon/amdgpu/power1_input` | µW; real-time TDP readout. `get_current_tdp` currently reports `0` |
| GPU busy % | `amdgpu/gpu_busy_percent` | |
| GPU perf level | `amdgpu/power_dpm_force_performance_level` (644) | `auto`/`low`/`high`/`manual` |
| GPU clock table | `amdgpu/pp_dpm_sclk` | 600 / 604 / 2900 MHz; mclk 400/800/1000 |
| CPU EPP | `cpu0/cpufreq/energy_performance_preference` | `default performance balance_performance balance_power power` |
| CPU max freq | `cpuinfo_max_freq` | 5090910 kHz (~5.09 GHz) |
| NVMe temp | `hwmon/nvme/temp1_input` | ~38 °C |
| USB-C PD | `power_supply/ucsi-source-psy-*` | `usb_type = [C] PD PD_PPS`, voltage/current when charging |
| IMU | `/sys/bus/iio/devices/iio:device0` | `bmi323-imu` (gyro/accel) |
| Thermal zones | `/sys/class/thermal/thermal_zone{0,1,2}` | acpitz |
| Panel | `card0-eDP-1` | 1920x1080; `vrr_capable` empty |
| Custom fan curves | `hwmon/asus_custom_fan_curve` | 8 points per fan, both fans |
