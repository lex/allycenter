# Suspend hang investigation — ROG Xbox Ally X (RC73XA)

**Status: root cause NOT established.** This documents what was ruled in and out on
2026-07-25 so the next attempt doesn't repeat the work.

## Symptom

Device is put to sleep; it never wakes. Power LED stays lit, power button does nothing,
only a hard restart recovers it. Intermittent.

## Evidence

Suspend/resume record across all boots retained in the journal:

| Entry | Exit | Result |
|---|---|---|
| 11:40:48 | 15:31:19 | resumed after ~4 h |
| 17:29:23 | — | **hang** |
| 19:45:22 | 19:45:51 | resumed after 29 s |
| 19:55:42 | — | **hang** |
| 21:30:47 | 21:31:49 | resumed after 62 s (rtcwake test) |
| 21:33:56 | 21:34:57 | resumed after 59 s (rtcwake test) |

Roughly 50/50. Both failures end at exactly the same journal line and nothing follows:

```
systemd-sleep[…]: Performing sleep operation 'suspend'...
kernel: PM: suspend entry (s2idle)
```

Sleep config: `mem_sleep = [s2idle]` (no `deep` available), `state = freeze mem disk`.

### Why there are no logs from the failures

The console is suspended during the operation and the journal is not flushed, so
messages emitted after `suspend entry` only reach disk **on resume**. A hang therefore
leaves nothing behind. `/sys/fs/pstore` is empty (no crash remnant), and
`/sys/power/suspend_stats` resets each boot.

## Ruled out

- **`mcu_powersave`.** Was `0` at the time of *both* hangs (the plugin had set it back
  after toggling RGB), so the MCU low-power setting is not the trigger.
- **The plugin's RGB effect threads.** RGB was disabled at both hangs, so no effect
  thread was running and writing to sysfs.
- **Plugin presence generally.** The ~4 h successful suspend also happened with the
  plugin loaded and running.

## Confirmed, but not sufficient

A firmware ACPI bug fires on **every** suspend:

```
ACPI BIOS Error (bug): Could not resolve symbol [\_SB.PCI0.SBRG.EC0.LID], AE_NOT_FOUND
ACPI Error: Aborting method \_SB.PEP._DSM due to previous error (AE_NOT_FOUND)
```

`\_SB.PEP._DSM` is the Power Engine Plugin method used to negotiate s2idle (S0ix)
entry/exit with the SoC. The BIOS references a lid switch (`EC0.LID`) that does not
exist on a handheld, the lookup fails, and the `_DSM` call aborts.

**Important:** this error appears once per *completed* suspend/resume cycle — including
the successful ones — because it is only flushed to the journal on resume. It is
therefore present in both outcomes and **cannot by itself explain the hang**. It is a
genuine firmware defect and a plausible contributor to fragile S0ix entry, not a proven
cause.

BIOS at time of investigation: `RC73XA.316`, dated 2026-02-11.

## Other observations

- USB `1-4` = `N-KEY Device` (the controller) has `power/wakeup = enabled` and requires
  `usb 1-4: reset full-speed USB device number 3 using xhci_hcd` on every resume.
- ACPI wakeup sources enabled: `XHC0`, `XHC3`, `XHC4`, `NHI0` (all S0), `GPP0`, `GPP3` (S4).
- `amd_pmc` debugfs reports `Last S0i3 Status: Unknown/Fail` with all residency counters
  at zero, but this was read on a boot with no suspend attempt, so it is not evidence.

## Could the plugin be causing it?

**Not ruled out, but unsupported by the evidence.** The plugin was loaded and running
during both *successful* suspends, including the ~4 h one, so its mere presence is not
the trigger. `mcu_powersave` — the plugin-controlled setting with a known reputation for
breaking resume on ROG hardware — was `0` at both hangs.

One plausible mechanism remains, and it is worth knowing about even though it was not
active during these failures:

> The RGB effect threads (`pulse`, `spectrum`, `wave`, `flash`, `battery`) write to the
> LED node in a loop, and those writes travel over USB HID to the MCU. A thread caught
> mid-write when the kernel freezes userspace can end up in uninterruptible sleep and
> stall the freeze — which is precisely the step the hangs stop at.

RGB was disabled at both recorded hangs, so no effect thread was running. The hazard
applies to anyone using an animated effect.

Mitigation attempted but **not currently active**: `on_suspend` stops effect threads,
but it is wired to `SteamClient.System.RegisterForOnSuspendRequest`, which was tested on
this build and never fires. There is at present no working pre-suspend hook, so the
hazard stands for animated effects. A `/etc/systemd/system-sleep/` hook would work but
installs outside the plugin directory.

**The clean experiment**, if the hang recurs: disable the plugin entirely in Decky and
use the device normally. If sleep still hangs, the plugin is exonerated outright.

## Suggested next steps (not yet performed)

1. **Reproduce on demand** with an `rtcwake` loop so failures can be triggered rather
   than waited for. Each failure requires a physical hard restart.
2. **Bisect suspects** once reproducible: disable `1-4` USB wakeup, unload `mt7921e`,
   test with the controller detached.
3. `/sys/power/pm_test` phased suspend to isolate which phase hangs — cheaper, but it
   does not exercise real S0i3 entry, which is the suspected failure point.
4. Check for a BIOS newer than `RC73XA.316`; the `EC0.LID` defect is a firmware fix.
5. To capture anything at all from a hang, `no_console_suspend` plus a serial or
   netconsole sink would be required — the journal cannot help.
