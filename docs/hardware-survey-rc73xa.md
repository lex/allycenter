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
| `asus_armoury` | firmware-attributes interface for power limits. Min/max/default come from a **DMI-matched table in the driver**, not from firmware |
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

### Verified behaviour of armoury writes

- **They take effect immediately.** A 7W `ppt_pl1_spl` written through armoury alone
  held the CPU at 820 MHz / 6.1 W under an 8-thread load — indistinguishable from the
  same limit written through both paths (870 MHz / 6.1 W).
- **They are runtime, not deferred.** `pending_reboot` stayed `0` across writes, so
  these are not firmware settings that survive a reboot. **TDP must still be re-applied
  at boot.**
- **The legacy node's readback does not reflect an armoury write.** After writing `7`
  through armoury, `asus-nb-wmi/ppt_pl1_spl` still read `25`. The two are separate
  readback caches over the same firmware. Never read the legacy node to determine
  current state — read `<attr>/current_value`.
- Switching the plugin to this interface removed the deprecation warning entirely
  (verified: zero warnings in `dmesg` after a full startup apply).

## Power limits (PPT)

Two views of the same firmware, which do **not** report the same current state:

- Write path used by the plugin: `<attr>/current_value` under the firmware-attributes class
- Legacy path (deprecated): `/sys/devices/platform/asus-nb-wmi/ppt_*` (mode 644)

**The ranges are not read from firmware.** `asus-armoury.h` carries a DMI-matched table
of hardcoded limits (`dmi_first_match(power_limits)` in `init_rog_tunables`). The values
exposed in sysfs are that table, not something the firmware reports. This is why the
legacy WMI path accepts out-of-range values — it has no table to check against.

### Limits differ between AC and battery

The driver keeps two sets (`ASUS_ROG_TUNABLE_AC` / `ASUS_ROG_TUNABLE_DC`) and selects
based on whether the charger is connected, so **`min_value`/`max_value`/`default_value`
change when you plug in.** From `asus-armoury.h` for `RC73XA`:

| | AC | Battery (DC) |
|---|---|---|
| `ppt_pl1_spl` | 7–35, no default | 7–35, default 17 |
| `ppt_pl2_sppt` | **14**–45, no default | **13**–45, default 21 |
| `ppt_pl3_fppt` | 19–55, no default | 19–55, default 26 |

Everything recorded in this document was captured **on battery**, so it is the DC set.
Where a default is unset the driver substitutes another value rather than reporting 0.

Verified live: removing the charger changed `ppt_pl2_sppt/min_value` from `14` to `13`
within a few seconds. The ranges really are swapped at runtime.

**`current_value` is a driver-side cache, not a live firmware read**, and there is a
separate cache per power source:

```c
static ssize_t _attr##_current_value_show(...)
{
        struct rog_tunables *tunables = get_current_tunables();
        return sysfs_emit(buf, "%u\n", tunables->_attr);   /* cached, not WMI */
}
```

At probe each set is seeded to `def ?: max`. So after a power-source change,
`current_value` reports whatever was last written *through that interface for that
power source* — which may be the seeded default rather than reality.

**Firmware retains the applied limit across a power-source change.** Verified: with the
plugin stopped, 7W was written while on AC, then the charger was removed with nothing
re-applying. `current_value` then read `25` (the stale DC cache) while a load test
measured **1032 MHz / 7.1 W** — the 7W limit was still genuinely in force.

An earlier version of this document claimed plugging in *discarded* the limits, based on
seeing 35/45/55 after connecting the charger. That was the AC cache's seeded default
being displayed, not a firmware reset. **Never trust `current_value` after a power-source
change; measure under load if it matters.**

Consequences:

- Ranges must be read at the moment of writing, not cached at startup
  (`_armoury_range()` does this).
- Power limits are **re-applied on charger connect/disconnect** — not because firmware
  loses them, but so the driver's per-power-source cache matches what the user
  configured (otherwise the UI reads the seeded default), and so the value is
  re-validated against the new range. The plugin watches
  `/sys/class/power_supply/AC0/online` in its background thread.

### Limits are per model, too

For comparison, from the same table — which is why hardcoding any of this is wrong:

| Board | AC `pl1` max | DC `pl1` max |
|---|---|---|
| `RC71` (original Ally) | 30 | 25 |
| `RC72` (Ally X) | 30 | — |
| `RC73XA` (Xbox Ally X) | 35 | 35 |

### Why `ppt_apu_sppt` and `ppt_platform_sppt` are missing

Both are in the driver's attribute table, but `asus_fw_attr_add()` only creates a
power tunable when `has_valid_limit()` passes — i.e. the DMI table defines a non-zero
max. `RC73XA` defines none for these two, so the driver deliberately does not expose
them. They still exist on the legacy path, where nothing validates them.

| Attribute | Legacy WMI node | min | max | default | meaning |
|---|---|---|---|---|---|
| `ppt_pl1_spl` | `ppt_pl1_spl` | 7 | 35 | 17 | sustained ("CPU slow package limit") |
| `ppt_pl2_sppt` | `ppt_pl2_sppt` | 13 | 45 | 21 | slow boost ("CPU fast package limit") |
| `ppt_pl3_fppt` | `ppt_fppt` | 19 | 55 | 26 | fast boost ("CPU fastest package limit") |

(Values as read on battery — see below.) Note the name differs between interfaces:
`ppt_pl3_fppt` via firmware-attributes, `ppt_fppt` on the legacy path.

**Critical gotchas:**

1. **The WMI nodes do not validate.** Writing `120` to `ppt_pl1_spl` is accepted and
   reads back as `120`; the firmware clamps internally. Readback is *not* proof a value
   is legal or in effect. Always clamp in software using the armoury min/max above.
2. **The limits are a staircase**, pl1 < pl2 < pl3 (17/21/26 stock). Writing one number
   to all three is wrong, and any value below ~19 is under `ppt_pl3_fppt`'s minimum.
3. At boot the WMI nodes read `5`, which is below every documented minimum. It is not
   established whether this reflects an applied 5W state or an unpushed driver default —
   see Open questions.
4. **The two interfaces disagree about current state.** (Confirmed by test.) After the plugin wrote 25W via
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

### The MCU takes the rings back after a resume

Verified by observation, and it defeats the obvious fixes:

- At wake the rings are correctly off. **A few seconds later the MCU lights them
  green by itself** — the firmware's own default for "nobody is driving these".
  Nothing in sysfs reflects it: `brightness` still reads `0` while the hardware is lit.
- It is **not** the last colour set. With red configured and red last written, green
  still appears. With RGB *enabled* (blue) the colour returns instantly and correctly
  and **no green appears at all** — so this only affects users who keep their lights off.
- Writing `brightness` alone does not reclaim the LEDs. The LED core skips the hardware
  write when the value is unchanged, and even a forced 1→0 nudge was not enough.
- **Any real change to `multi_intensity` does reclaim them**, because it makes the
  driver resend the whole LED state, brightness included. Confirmed: with the rings
  green and `brightness=0`, writing a non-zero intensity turned them off without ever
  touching brightness.
- Because the MCU acts *after* the resume, a single re-apply loses. The plugin
  re-asserts on a schedule (every second for 20s, then tapering to 60s).

When forcing a change, flip the low bit of the colour rather than writing zeros — a
1/255 step is invisible, whereas blanking first makes a lit ring blink.

The proper fix is the MCU's Aura power states (boot/awake/sleep/shutdown) held in its
own NVRAM, which is what Armoury Crate sets. See `steamos-overlap.md`; it needs
asusctl's known-good HID packets rather than guesswork.

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

**`pwm{n}_enable` semantics — verified on hardware:**

| Value | Behaviour |
|---|---|
| `1` | Custom curve active |
| `2` | Firmware/automatic control; written curve points are **ignored** |

Proven by writing an aggressive curve (120 PWM at 30 °C rising to 255) and observing:
at `enable=2` the fan stayed at **0 RPM** despite the curve; setting `enable=1` ramped it
to **5300 RPM at 44 °C**. Writing points alone therefore does nothing — `enable` is the
switch. Returning to `2` restores firmware control, though the fan takes a while to
spin down from a high setting.

**Caution:** at boot the kernel logs
`asus_wmi: fan_curve_get_factory_default (0x00110032) failed: -19` (`-ENODEV`), so the
firmware's factory defaults could not be read. The "stock" curve recorded above is what
the driver reports at rest and restores control correctly, but it is not confirmed to be
the genuine factory curve.

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
