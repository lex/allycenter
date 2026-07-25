# Ally Center docs

Reference material for this fork, gathered by probing a live device rather than from
upstream assumptions. All of it was captured on 2026-07-25 from a **ROG Xbox Ally X
(RC73XA)**, Ryzen AI Z2 Extreme, SteamOS 6.16.12.

| Document | What it's for |
|---|---|
| [`hardware-survey-rc73xa.md`](hardware-survey-rc73xa.md) | The sysfs reference: verified paths, power-limit ranges, RGB wire format, fan curve API, LED triggers. Enough to build a controller for this device from scratch. |
| [`hardware-survey-rc73xa.raw.txt`](hardware-survey-rc73xa.raw.txt) | Raw dump behind the survey, so values can be re-checked without re-probing. |
| [`steamos-overlap.md`](steamos-overlap.md) | What SteamOS already does, what it can't do here, and which features the plugin should therefore own. Includes the shared-knob analysis. |
| [`suspend-hang-investigation.md`](suspend-hang-investigation.md) | The sleep/wake failure. Root cause **not** established; records what was ruled in and out. |
| [`feature-candidates.md`](feature-candidates.md) | Read-only survey of what else could be controlled, with a recommended order and an explicit rejected list. |

## Read this before changing hardware-facing code

**Verify against the device, don't infer from the code.** Most bugs found in this fork
were paths that could never have worked on this model — the plugin was written for the
Z1 Extreme ROG Ally and assumed its layout.

**A successful write proves nothing.** The `ppt_*` nodes accept `120` and read it back;
the firmware clamps internally. Use the ranges published by `asus_armoury`
(`/sys/class/firmware-attributes/asus-armoury/attributes/*/{min,max,default}_value`),
and clamp in software.

**Check whether SteamOS owns the knob first.** Two features were found writing over
settings SteamOS manages — the charge limit, and `throttle_thermal_policy`, which *is*
SteamOS's Performance Profile. Anything applied automatically (at boot, on resume)
must not silently revert a user's choice made elsewhere.

## Model-specific caveats

These findings are verified **only** on RC73XA. Known or suspected differences elsewhere:

- **Thermal policy mapping.** This fork uses `{quiet: 2, balanced: 0, performance: 1}`,
  verified in both directions against the ACPI platform profile. Upstream used
  `{quiet: 1, balanced: 0, performance: 2}`, described in a comment as the ROG Ally
  arrangement. If both are accurate for their respective devices, the mapping is
  model-dependent and this fork's value is **wrong for the original Ally**.
  *Suggested fix:* write `/sys/firmware/acpi/platform_profile` by name instead, and read
  `platform_profile_choices` at runtime — no hardcoded numbers to get wrong.
- **Power limit ranges.** 7/35, 13/45, 19/55 are the **battery** values for this board,
  and they come from a DMI-matched table inside the `asus-armoury` driver, not from
  firmware. They differ **per model and per power source** — plugging in changes them.
  Always read `min_value`/`max_value` at the moment of writing. Source of truth:
  `drivers/platform/x86/asus-armoury.h` in the kernel tree.
- **Charge limit location.** On this model it is on the battery
  (`power_supply/BAT0/`), not under `asus-nb-wmi`.
- **`ryzenadj`** is not installed and `fan_curve_enable` does not exist here; both may be
  present on other devices or distros.

## Open questions

Tracked in the individual documents:

- Whether the boot-time `ppt_* = 5` readback means the device genuinely runs at 5W after
  every boot, or is an unpushed driver default (`hardware-survey-rc73xa.md`).
- Root cause of the suspend hang (`suspend-hang-investigation.md`).
- `pwm1_enable=2` / `pwm2_enable=0` mismatch under hwmon `asus`.
- The meaning of `pwm_enable` values on `asus_custom_fan_curve`, and the `pwm2_enable`
  mismatch between it and hwmon `asus` (`feature-candidates.md`).
- Whether writing power limits through `asus_armoury` persists across reboot as a
  firmware setting would.

Resolved since: the resume hook is verified working (via a backend clock-gap watcher —
the Steam client callbacks never fire on this build).
