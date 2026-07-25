# Ally Center

A comprehensive Decky Loader plugin for the **ASUS ROG Ally** running SteamOS.

> This is a fork of [PixelAddictUnlocked/allycenter](https://github.com/PixelAddictUnlocked/allycenter),
> maintained at [lex/allycenter](https://github.com/lex/allycenter). It adds fixes for the
> ROG Xbox Ally X (RC73XA) and settings that actually persist across reboots.
> Please report issues with this fork here, not to the original author.

## ⚠️ Disclaimer — read this first

**This software is provided "as is", with no warranty of any kind, and nobody is
liable for anything it does to your hardware.**

This plugin writes directly to kernel interfaces that control **power limits, thermal
policy, fan behaviour, charging thresholds and display backlight** on your device. Used
carelessly, software that touches these controls can cause overheating, unexpected
shutdowns, reduced hardware lifespan, data loss, or permanent damage. It may also void
your warranty.

By installing or using it you accept that:

- You use it **entirely at your own risk**.
- Neither the maintainer of this fork, nor the original author, nor any contributor is
  liable for any damage, loss or injury arising from its use — see the
  [LICENSE](LICENSE) for the full disclaimer of warranty and liability.
- It is **not affiliated with, endorsed by, or supported by** ASUS, Valve, Microsoft, or
  any of their subsidiaries. "ROG", "ROG Ally", "Xbox", "Steam" and "SteamOS" are
  trademarks of their respective owners.
- Hardware support varies by model and firmware. A control that works on one device may
  do nothing — or something different — on yours.

If you are not comfortable with that, do not install it.

![AllyCenter Screenshot](images/1.png)
![AllyCenter Screenshot](images/2.png)
![AllyCenter Screenshot](images/3.png)
![AllyCenter Screenshot](images/4.png)
![AllyCenter Screenshot](images/5.png)

## Features

### Download Mode

Turn off the display for background downloads to save battery. When enabled:

- Screen brightness set to 0
- Automatically switches to the minimum power profile
- RGB lighting disabled
- MCU powersave enabled (stops charging LED blink)
- Open the Quick Access Menu to exit

### Performance

- **Use External TDP** - Disable Ally Center's TDP management to use SimpleDeckyTDP or other plugins
- **Performance Presets** - Quick switch between Download (7W), Silent (15W), Performance (25W), and Turbo (30W) modes
- **TDP Override** - Manually set TDP from 7W to 35W with fine-grained control
- **Fan Mode** - Choose between Auto, Quiet, Balanced, and Performance fan profiles
- **Live Monitoring** - View current CPU and GPU temperatures in real-time

TDP is applied as the firmware's three-stage limit (sustained / slow boost / fast boost)
rather than a single number, and is clamped to the range the firmware actually accepts —
the kernel interface silently accepts out-of-range values without applying them.

### CPU Settings

- **SMT (Hyper-Threading)** - Toggle on/off for better single-thread performance in some games
- **CPU Boost** - Disable to reduce heat and power consumption

### Battery

- **Charge Level** - Current battery percentage and charging status
- **Battery Health** - Monitor battery health percentage
- **Detailed Stats** - View cycle count, voltage, design capacity, and current capacity
- **Charge Limit** - Set maximum charge level (60-100%) to extend battery lifespan.
  Shows the value the hardware actually holds, which SteamOS's own charge-limit setting
  may have changed. Not overwritten on startup.

### RGB Lighting

- **Color Selection** - Full color spectrum slider with preset colors (ROG Red, Cyan, Purple, Green, Orange, Pink, White, Blue)
- **Brightness Control** - Adjust LED brightness from 0-100%
- **Effects** - Static, Pulse, Spectrum, Wave, Flash, Battery Level, or Off
- **Speed Control** - Adjust animation speed for animated effects

### Device Info

View detailed system information:

- CPU model
- GPU model
- Memory total
- BIOS version
- Kernel version

## Requirements

- ASUS ROG Ally, ROG Ally X, or ROG Xbox Ally X
- SteamOS (or compatible distro like Bazzite, ChimeraOS)
- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) installed

## Installation

### Quick Install (Recommended)

**Important:** Run this directly on your ROG Ally or via SSH.

**On your ROG Ally:**

1. Switch to Desktop Mode
2. Open Konsole (terminal)
3. Run:

```bash
curl -L https://github.com/lex/allycenter/raw/main/install.sh | sh
```

**Via SSH:**

```bash
ssh deck@<your-ally-ip>
curl -L https://github.com/lex/allycenter/raw/main/install.sh | sh
```

The installer will download the latest release, install it, and restart Decky Loader automatically.

### Manual Install

1. Download the latest release from the [Releases](https://github.com/lex/allycenter/releases) page
2. Extract to `~/homebrew/plugins/Ally Center/`
3. Restart Decky Loader or reboot

## Usage

1. Press the **...** button on your ROG Ally to open the Quick Access Menu
2. Navigate to the **Decky** plugin icon (plug icon)
3. Select **Ally Center** from the plugin list
4. Use the toggles, sliders, and buttons to control your device

## Hardware Support

Only the ROG Xbox Ally X column has been verified against real hardware by this fork;
see [`docs/hardware-survey-rc73xa.md`](docs/hardware-survey-rc73xa.md) for the full
sysfs reference it was tested against. The other columns are inherited from upstream
and are unverified here.

| Feature             | ROG Ally | ROG Ally X | ROG Xbox Ally X (RC73XA) |
| ------------------- | -------- | ---------- | ------------------------ |
| Download Mode       | ✅       | ✅         | ✅                        |
| Performance Presets | ✅       | ✅         | ✅ verified               |
| TDP Override        | ✅       | ✅         | ✅ verified               |
| Fan Control         | ✅       | ✅         | ✅ verified               |
| CPU Settings        | ✅       | ✅         | ✅                        |
| Battery Health      | ✅       | ✅         | ✅ (no temperature)       |
| Charge Limit        | ✅       | ✅         | ✅ verified               |
| RGB Lighting        | ✅       | ✅         | ✅ verified               |
| Device Info         | ✅       | ✅         | ✅                        |

## Settings

Your preferences are saved to disk and **re-applied to the hardware on startup**, so
they survive a reboot rather than only appearing to. Settings are stored in:

```
~/homebrew/settings/Ally Center/settings.json
```

### Startup safety

Re-applying settings at boot is controlled by `apply_on_startup` (default `true`).

A sentinel file is written next to the settings before anything touches the hardware
and removed once the apply completes. If the plugin starts and finds that sentinel
still present, it concludes the previous attempt did not finish, **skips the apply and
sets `apply_on_startup` to `false`** rather than looping into the same fault on every
boot. Re-enable it once you know what went wrong.

To recover manually if the plugin is misbehaving at boot, edit the settings file
directly and set:

```json
{ "apply_on_startup": false }
```

## License

MIT License - see [LICENSE](LICENSE) for details. This fork adds modifications under the
same license; the original copyright is retained as MIT requires.

**No warranty and no liability** — see the [disclaimer](#️-disclaimer--read-this-first) above.

## Credits

- [Keith Baker / Pixel Addict Games](https://github.com/PixelAddictUnlocked/allycenter) - original Ally Center
- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) - Plugin framework
- [HueSync](https://github.com/honjow/HueSync) - RGB inspiration
- [ASUS Linux](https://asus-linux.org) - Hardware documentation

## Support

- [GitHub Issues](https://github.com/lex/allycenter/issues) — for this fork

For the original plugin, use the upstream project's own channels.
