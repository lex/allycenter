# Hardware reference — ROG Xbox Ally X (RC73XA)

Captured 2026-07-25 from a live device over SSH and verified by writing to each
interface. This is intended to be complete enough to write a controller for this
device from scratch, independent of the current plugin.

## Device

| | |
|---|---|
| Product | `ROG Xbox Ally X RC73XA_RC73XA` |
| BIOS | `RC73XA.316` |
| CPU | AMD Ryzen AI Z2 Extreme |
| Kernel | `6.16.12-drmexec7-valve24.5-1-neptune-616-drm-exec` |
| OS | SteamOS (holo) |

**No custom kernel module is required.** Every control below is exposed by stock
SteamOS drivers and was verified writable as root.

Relevant loaded modules:

| Module | Provides |
|---|---|
| `hid_asus_ally` (driver `asus_rog_ally`) | joystick-ring RGB, via `led_class_multicolor`, on USB HID `0B05:1B4C` |
| `asus_wmi` / `asus_nb_wmi` | PPT power limits, thermal policy, fan hwmon, MCU powersave |
| `asus_armoury` | firmware-attributes interface — **authoritative min/max/default** for power limits |
| `amd_pmf`, `platform_profile` | ACPI platform profile |

## asus_armoury — the firmware-attributes interface

`/sys/class/firmware-attributes/asus-armoury/attributes/` (note: the kernel's own
deprecation message spells it `firmware_attributes` with an underscore; the real path
uses a hyphen).

**This is the complete set on this device — six attributes plus `pending_reboot`:**

| Attribute | `current_value` mode | Type | Values | `display_name` |
|---|---|---|---|---|
| `ppt_pl1_spl` | 644 | integer | min 7, max 35, default 17, increment 1 | Set the CPU slow package limit |
| `ppt_pl2_sppt` | 644 | integer | min 13, max 45, default 21, increment 1 | Set the CPU fast package limit |
| `ppt_pl3_fppt` | 644 | integer | min 19, max 55, default 26, increment 1 | Set the CPU fastest package limit |
| `boot_sound` | 644 | enumeration | `0;1` | Set the boot POST sound |
| `mcu_powersave` | 644 | enumeration | `0;1` | Set MCU powersaving mode |
| `charge_mode` | **444 read-only** | enumeration | `0;1;2` | Show the current mode of charging |
| `pending_reboot` | 444 read-only | — | currently `0` | standard firmware-attributes flag |

Integer attributes expose `current_value`, `default_value`, `min_value`, `max_value`,
`scalar_increment`, `type`. Enumerations expose `current_value`, `possible_values`,
`display_name`, `type`.

Note this is a much smaller set than the `asus_armoury` driver supports generally —
laptop-oriented attributes (GPU MUX, dGPU disable, panel overdrive, dynamic boost) are
absent because the hardware has no such features.

### The legacy path is deprecated

The kernel logs, when `/sys/devices/platform/asus-nb-wmi/*` is written:

```
asus_wmi: Accessing attributes through /sys/bus/platform/asus_wmi is deprecated
and will be removed in a future release.
Please switch over to /sys/class/firmware_attributes.
```

**The plugin currently writes the deprecated path** for `ppt_*`, `boot_sound` and
`mcu_powersave`. It works today and emits this warning, but it is scheduled for removal.
Migrating to `<attr>/current_value` under the firmware-attributes class is the
forward-compatible path, and would also explain the discrepancy below: the two are two
front-ends onto the same firmware, with the armoury one canonical.

`pending_reboot` gives a way to answer the open question of whether armoury writes
persist as firmware settings — write a limit through armoury and see whether the flag
is raised.

## Power limits (PPT)

Two views of the same firmware, which do **not** report the same current state:

- Write path used by the plugin: `/sys/devices/platform/asus-nb-wmi/ppt_*` (mode 644)
- Authoritative metadata: `/sys/class/firmware-attributes/asus-armoury/attributes/<attr>/{current_value,min_value,max_value,default_value}`

| Attribute | WMI node | min | max | default | meaning |
|---|---|---|---|---|---|
| `ppt_pl1_spl` | `ppt_pl1_spl` | 7 | 35 | 17 | sustained |
| `ppt_pl2_sppt` | `ppt_pl2_sppt` | 13 | 45 | 21 | slow boost |
| `ppt_pl3_fppt` | `ppt_fppt` | 19 | 55 | 26 | fast boost |

Also present, no armoury metadata: `ppt_apu_sppt`, `ppt_platform_sppt`.

**Critical gotchas:**

1. **The WMI nodes do not validate.** Writing `120` to `ppt_pl1_spl` is accepted and
   reads back as `120`; the firmware clamps internally. Readback is *not* proof a value
   is legal or in effect. Always clamp in software using the armoury min/max above.
2. **The limits are a staircase**, pl1 < pl2 < pl3 (17/21/26 stock). Writing one number
   to all three is wrong, and any value below ~19 is under `ppt_pl3_fppt`'s minimum.
3. At boot the WMI nodes read `5`, which is below every documented minimum. It is not
   established whether this reflects an applied 5W state or an unpushed driver default —
   see Open questions.
4. **The two interfaces disagree about current state.** After the plugin wrote 25W via
   WMI, `ppt_pl1_spl` read `25` while armoury's `current_value` still read `17`. Armoury
   appears to reflect only what was written *through armoury*. Treat armoury as
   authoritative for `min_value`/`max_value`/`default_value` metadata, and WMI as the
   runtime state. Whether writing through armoury persists across reboot (as a firmware
   setting would) is untested. `pending_reboot` **does** exist on this class (444,
   currently `0`) and is the obvious way to test it.

**Verified effective** (8-thread busy loop, 12 s settle, on battery):

| pl1 | avg CPU freq | battery draw | CPU temp |
|---|---|---|---|
| 5W | 605 MHz | 6.2 W | 47 °C |
| 30W | 2726 MHz | 24.4 W | 77 °C |

## RGB — joystick rings

Path: `/sys/class/leds/ally:rgb:joystick_rings/`

| File | Mode | Notes |
|---|---|---|
| `brightness` | 644 | 0–255, master brightness. `0` = off |
| `max_brightness` | 444 | `255` |
| `multi_index` | 444 | `rgb rgb rgb rgb` — 4 zones |
| `multi_intensity` | 644 | **4 space-separated packed 24-bit ints**, `(r<<16)|(g<<8)|b` |
| `trigger` | 644 | kernel LED triggers, see below |

**Verified format:** writing 4 packed ints succeeds; writing 12 per-channel values
fails with `EINVAL` (`write error: Invalid argument`). The existing plugin encoding is
correct.

`multi_intensity` can be written while `brightness` is 0; it is retained and takes
effect when brightness is raised.

**Useful kernel triggers** (would replace polling loops entirely):
`BAT0-charging`, `BAT0-full`, `BAT0-charging-or-full`, `BAT0-charging-blink-full-solid`,
`BAT0-charging-orange-full-green`, `AC0-online`, `disk-activity`, `cpu0`…`cpu7`, `panic`.

## Fans

Two fans. Beware: `hwmon1` is `acpi_fan` and **also** exposes `fan1_input` — always
resolve hwmon devices by reading `name`, never by index or first match.

`/sys/class/hwmon/hwmon8` → `name = asus` (under `asus-nb-wmi/hwmon/`)

| File | Notes |
|---|---|
| `fan1_input` / `fan1_label` | RPM / `cpu_fan` |
| `fan2_input` / `fan2_label` | RPM / `gpu_fan` |
| `pwm1_enable`, `pwm2_enable` | observed `2` and `0` respectively |

`/sys/class/hwmon/hwmon9` → `name = asus_custom_fan_curve`

8-point curve per fan: `pwm{1,2}_auto_point{1..8}_{pwm,temp}` plus `pwm{1,2}_enable`.
Observed stock CPU curve: `(48 °C, 2)` `(54, 22)` `(59, 45)` `(62, 56)` then flat.
GPU curve: `(48, 2)` `(54, 22)` `(59, 33)` `(62, 33)` then flat.
PWM values are 0–255, temps in °C.

Note `fan_curve_enable` — which the current plugin references — **does not exist** on
this model.

**Caution for the custom fan curve feature:** at boot the kernel logs
`asus_wmi: fan_curve_get_factory_default (0x00110032) failed: -19` (`-ENODEV`). The
curve device still registers and reports plausible points, but the firmware's factory
defaults could not be read. The "stock" curve recorded in this document may therefore be
a driver fallback rather than genuine factory values — relevant to any
restore-to-defaults implementation.

## Thermal policy

`/sys/firmware/acpi/platform_profile` (644), choices `low-power balanced performance`.
**Prefer this** — the names are model-independent.

It is the same knob as `/sys/devices/platform/asus-nb-wmi/throttle_thermal_policy` (644),
verified in both directions on this model:

| policy | platform profile |
|---|---|
| 0 | `balanced` |
| 1 | `performance` |
| 2 | `low-power` |

Writing the policy numerically makes `platform_profile` read `custom` for values 1 and 2,
though `steamosctl` still names them correctly. Writing by name avoids this.

**This is also SteamOS's Performance Profile** (`steamosctl get/set-performance-profile`).
Anything that writes it automatically will silently revert the user's SteamOS choice.
See `steamos-overlap.md`.

## Battery

`/sys/class/power_supply/BAT0/`

| File | Notes |
|---|---|
| `charge_control_end_threshold` | **644, writable** — the real charge limit. Observed `80`, set by SteamOS itself |
| `energy_full` / `energy_full_design` | 78525000 / 80003000 µWh → health ≈ 98.2 % |
| `energy_now`, `power_now`, `voltage_now`, `capacity`, `status`, `cycle_count` | standard |

There is **no** `charge_control_end_threshold` under `asus-nb-wmi`, and no `temp` node
(so battery temperature is unavailable from this path). `cycle_count` reads `0`.

## Display backlight

`/sys/class/backlight/amdgpu_bl0/` — this **is** the device directory; it directly
contains `brightness` (664), `actual_brightness`, `max_brightness` (`62451`), `bl_power`.
It has no per-device subdirectories.

## CPU

| Path | Observed |
|---|---|
| `/sys/devices/system/cpu/smt/control` | `on` |
| `/sys/devices/system/cpu/cpufreq/boost` | `1` |
| `.../cpu0/cpufreq/scaling_driver` | `amd-pstate-epp` |
| `.../cpu0/cpufreq/energy_performance_preference` | `balance_performance` |
| `.../cpu0/cpufreq/scaling_available_governors` | `performance powersave` |

## Misc `asus-nb-wmi` nodes

`mcu_powersave` (644, `1`), `boot_sound` (644, `1`), `charge_mode` (444, `0`),
`nv_temp_target` (644, `75`), `cpufv` (200, write-only).

## Not present on this model

`ryzenadj` is **not installed** — ASUS WMI is the only TDP mechanism.
`fan_curve_enable` does not exist. `asus-nb-wmi/charge_control_end_threshold` does not exist.

## Open questions

- Does the boot-time `ppt_* = 5` readback mean the device is genuinely running at 5W
  after every boot, or is it an unpushed driver default? Distinguishing requires a
  load test immediately after reboot, before anything writes to the nodes.
- `pwm1_enable=2` but `pwm2_enable=0` under hwmon `asus`, while `asus_custom_fan_curve`
  reports `2` for both (both 644). Meaning of the mismatch not established.
- Whether writing power limits through `asus_armoury` persists across reboot the way a
  firmware setting would. If it does, it would remove the need to re-apply TDP at boot.
- Both fans read 0 RPM at idle; not confirmed whether they report real RPM under load.
- Writing `17` to `ppt_pl2_sppt` via WMI briefly read back as `21` through the armoury
  view, suggesting the two interfaces may not be perfectly coherent on every write.
