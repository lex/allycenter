# Feature candidates — read-only survey

Probed 2026-07-25 on the ROG Xbox Ally X (RC73XA), **read-only**: every path below was
inspected and its permissions recorded, but nothing here has been written to or tested.
Treat "writable" as "the mode bits allow it", not as "verified working".

Scope filter applied: safe and reversible only. Anything that persists to firmware NVRAM,
or that duplicates SteamOS or Steam Input, is excluded and listed under *Rejected*.

## Recommended

### 1. Custom fan curves — the biggest win

`/sys/class/hwmon/hwmon9` (`name = asus_custom_fan_curve`), all nodes mode 644:

- `pwm{1,2}_auto_point{1..8}_pwm` — 0–255
- `pwm{1,2}_auto_point{1..8}_temp` — °C
- `pwm{1,2}_enable` — currently `2` for both

Two independent curves, `pwm1` = CPU fan, `pwm2` = GPU fan. Stock values:

| point | CPU temp/pwm | GPU temp/pwm |
|---|---|---|
| 1 | 48 °C / 2 | 48 °C / 2 |
| 2 | 54 / 22 | 54 / 22 |
| 3 | 59 / 45 | 59 / 33 |
| 4–8 | 62 / 56 (flat) | 62 / 33 (flat) |

Nothing else on the device offers this — SteamOS reports `Fan control state: BIOS` and
exposes no curve editing. Genuinely additive.

**Unknown:** the meaning of `pwm_enable` values (presumably `1` = use custom curve,
`2` = automatic/BIOS, but this is a guess). Also unexplained: hwmon `asus` reports
`pwm1_enable=2` / `pwm2_enable=0` while the curve device reports `2` for both.
Resolve before implementing.

**Risk note:** fan curves are the one item on this list where a bad value has thermal
consequences. Any implementation should clamp PWM, enforce monotonically increasing
temperatures, and provide a one-press restore to the stock curve above.

### 2. Live monitoring readouts

All read-only, no risk, and mostly already fetched by the panel:

| Reading | Path |
|---|---|
| APU package power (W) | `hwmon/amdgpu/power1_input` — already wired up |
| GPU busy % | `amdgpu/gpu_busy_percent` |
| GPU / CPU / NVMe temps | `hwmon/amdgpu`, `hwmon/k10temp`, `hwmon/nvme` (~38 °C) |
| Per-fan RPM | `hwmon/asus/fan1_input` (cpu_fan), `fan2_input` (gpu_fan) |
| Battery health | `energy_full` / `energy_full_design` → 98.2 % |
| Charger wattage | `power_supply/ucsi-source-psy-*` — `usb_type = [C] PD PD_PPS`, `voltage_now`/`current_now` (both 0 on battery; needs verifying while charging) |

Backend already reports both fans; the UI currently shows only one.

### 3. Boot POST sound toggle

`asus-armoury/attributes/boot_sound` — `display_name = "Set the boot POST sound"`,
`possible_values = 0;1`, currently `1`. Also at `asus-nb-wmi/boot_sound` (644).

Small, self-contained, and nothing else exposes it. **Caveat:** this is a firmware
attribute, so it likely persists across reboots by design — which is the point, but it
means it is not "reversible" in the same sense as a sysfs runtime write.

### 4. LED battery triggers (replace the polling thread)

`/sys/class/leds/ally:rgb:joystick_rings/trigger` (644) supports, in kernel:
`BAT0-charging`, `BAT0-full`, `BAT0-charging-or-full`,
`BAT0-charging-blink-full-solid`, `BAT0-charging-orange-full-green`, `AC0-online`.

The existing `battery` RGB effect runs a Python thread polling `capacity` every 5 s. A
kernel trigger does the same job with no thread — which also removes one of the threads
implicated as a suspend hazard. Strictly better, if the trigger's colour behaviour is
acceptable.

### 5. CPU energy performance preference (EPP)

`cpu*/cpufreq/energy_performance_preference`, currently `balance_performance`.
Available: `default performance balance_performance balance_power power`.
Driver is `amd-pstate-epp`, `amd_pstate/status = active`, `prefcore = enabled`.

Not exposed in the SteamOS UI. Pairs naturally with the existing CPU section, and is a
meaningful battery-life lever independent of TDP. Must be written to every policy, not
just `cpu0`.

## Possible, lower value

| Candidate | Path | Note |
|---|---|---|
| GPU performance level | `amdgpu/power_dpm_force_performance_level` (644, `auto`) | Accepts `auto`/`low`/`high`/`manual`/`profile_peak`. `pp_power_profile_mode` does **not** exist on this APU, and SteamOS's `SetManualGpuClock` errors here, so `low` is about the only useful setting |
| CPU governor | `scaling_governor` | Only `performance` and `powersave` available; SteamOS already sets it (`powersave`) |
| WiFi power save | `iw dev wlan0 get power_save` → `on` | Already on; toggling off costs battery for latency |
| Screen brightness | `backlight/amdgpu_bl0` | Now fixed in the backend but not surfaced in the UI; Steam already has a brightness slider |
| IMU / gyro readout | `iio:device0` (`bmi323-imu`) | Full accel + gyro raw axes, scales and sampling rates. Only useful as a display curiosity — Steam Input owns gyro |

## Rejected

| Candidate | Why |
|---|---|
| Controller remap, deadzones, response curves, rumble | Steam Input already does all of it. Full API documented in `steamos-overlap.md` |
| Charge limit | SteamOS owns it; ours conflicted. Now read-only |
| Battery bypass charging | **Not available** — `charge_behaviour`, `charge_type` and `charge_control_start_threshold` are all absent on this model |
| Persistent LED-off via MCU NVRAM | Requires HID feature reports to MCU NVRAM. Not reversible like a sysfs write; port asusctl's definitions rather than guess |
| Panel refresh rate / VRR | `modes` lists only `1920x1080`, `vrr_capable` is empty. Nothing to control |
| Panel power saving (ABM) | No `panel_power_savings` node exists |
| Thermal trip points | Read-only (`102 °C` critical / `100 °C` passive). Informational at best |
| `nv_temp_target` (644, `75`), `cpufv` (200) | Undocumented ASUS nodes; `cpufv` is write-only, so its effect cannot be observed before writing. Excluded under "safe only" |

## Suggested order

1. Per-fan RPM + power/GPU-busy readouts — pure read, no risk, immediate value.
2. EPP selector — one write, well-understood, complements existing CPU section.
3. LED battery trigger — removes a polling thread.
4. Custom fan curves — highest value, most care required, needs the `pwm_enable`
   semantics resolved and a stock-restore button.
5. Boot sound toggle — trivial, but understand the persistence implication first.
