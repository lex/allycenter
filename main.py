"""
Ally Center - Decky Loader Plugin Backend
ROG Ally hardware control and system management

2025 Keith Baker / Pixel Addict Games
Licensed under MIT
"""

import os
import json
import subprocess
import asyncio
import threading
import time
import math
from pathlib import Path

import decky

# Hardware paths - these are specific to the ROG Ally running SteamOS
BATTERY_PATH = "/sys/class/power_supply/BAT0"
BACKLIGHT_PATH = "/sys/class/backlight/amdgpu_bl0"
DMI_PATH = "/sys/class/dmi/id"
ASUS_WMI_PATH = "/sys/devices/platform/asus-nb-wmi"
ALLY_LED_PATH = "/sys/class/leds/ally:rgb:joystick_rings"
FAN_CURVE_PATH = "/sys/devices/platform/asus-nb-wmi/fan_curve_enable"
PWM_PATH = "/sys/devices/platform/asus-nb-wmi/hwmon"
RYZENADJ_PATH = "/usr/bin/ryzenadj"
ALLY_CONTROLLER_PATH = "/sys/devices/platform/asus-nb-wmi"
# The real charge limit lives on the battery, not under asus-nb-wmi
CHARGE_LIMIT_PATH = "/sys/class/power_supply/BAT0/charge_control_end_threshold"
# Authoritative min/max/default for the power limits, published by asus_armoury
ARMOURY_PATH = "/sys/class/firmware-attributes/asus-armoury/attributes"
# ACPI platform profile. Same knob as throttle_thermal_policy and as SteamOS's
# Performance Profile, but addressed by name, which is model-independent.
PLATFORM_PROFILE_PATH = "/sys/firmware/acpi/platform_profile"
PLATFORM_PROFILE_CHOICES_PATH = "/sys/firmware/acpi/platform_profile_choices"
CPUFREQ_BASE = "/sys/devices/system/cpu"
# Kernel LED trigger used for the battery RGB mode, replacing a polling thread.
# Confirmed present in the trigger list on RC73XA.
BATTERY_LED_TRIGGER = "BAT0-charging-orange-full-green"

# Custom fan curves. Two independent 8-point curves, pwm1 = CPU fan, pwm2 = GPU fan.
FAN_CURVE_HWMON_NAME = "asus_custom_fan_curve"
FAN_CURVE_POINTS = 8
# Stock curves read from the device, used for "restore defaults". (temp C, pwm 0-255)
STOCK_FAN_CURVES = {
    "cpu": [(48, 2), (54, 22), (59, 45), (62, 56), (62, 56), (62, 56), (62, 56), (62, 56)],
    "gpu": [(48, 2), (54, 22), (59, 33), (62, 33), (62, 33), (62, 33), (62, 33), (62, 33)],
}
# pwm{n}_enable on the curve device. Verified on RC73XA: with an aggressive curve
# written, enable=2 ignored it entirely (fan stayed at 0 RPM) while enable=1 applied it
# immediately (fan ramped to 5300 RPM at 44 C). So 1 = custom curve, 2 = firmware.
FAN_CURVE_ENABLE_CUSTOM = "1"
FAN_CURVE_ENABLE_AUTO = "2"

# Firmware limits for ppt_pl1_spl. The WMI nodes accept out-of-range values
# without complaint and let the firmware clamp them, so we clamp here instead.
TDP_MIN = 7
TDP_MAX = 35
# Stock staircase is pl1=17, pl2=21, pl3=26; scale the boost limits off pl1.
TDP_PL2_RATIO = 21 / 17
TDP_PL3_RATIO = 26 / 17
TDP_PL2_RANGE = (13, 45)
TDP_PL3_RANGE = (19, 55)

# Preset power profiles. Tuned for the Ryzen AI Z2 Extreme; no value may sit
# below TDP_MIN or the firmware will silently ignore it.
PERFORMANCE_PROFILES = {
    "download": {
        "name": "Download",
        "tdp": 7,
        "fan_curve": "quiet",
        "description": "Minimum power for downloads"
    },
    "silent": {
        "name": "Silent",
        "tdp": 15,
        "fan_curve": "quiet",
        "description": "Low power, minimal fan noise"
    },
    "performance": {
        "name": "Performance", 
        "tdp": 25,
        "fan_curve": "balanced",
        "description": "Balanced performance and thermals"
    },
    "turbo": {
        "name": "Turbo",
        "tdp": 30,
        "fan_curve": "performance",
        "description": "Maximum performance"
    }
}


class Plugin:
    settings_path: str = None
    settings: dict = {}
    screen_off: bool = False
    effect_thread: threading.Thread = None
    effect_running: bool = False
    resume_watcher: threading.Thread = None
    resume_watcher_running: bool = False
    loop = None

    # How often to check for a suspend gap, and the smallest gap treated as a
    # real suspend rather than scheduling jitter.
    RESUME_POLL_SECONDS = 5
    RESUME_MIN_SUSPEND_SECONDS = 10
    
    async def _main(self):
        """Main entry point for the plugin"""
        self.settings_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
        self.loop = asyncio.get_running_loop()
        await self.load_settings()
        await self._apply_on_startup()
        self._start_resume_watcher()
        decky.logger.info("Ally Center initialized")

    def _sentinel_path(self) -> str:
        return os.path.join(os.path.dirname(self.settings_path), "apply_in_progress")

    async def _apply_on_startup(self):
        """Apply persisted settings at boot, guarded by a crash sentinel.

        A sentinel file is written before touching any hardware and removed once the
        apply completes. If it is still present at startup the previous attempt did
        not finish - most likely it hung or took the session down - so we skip and
        disable startup apply entirely rather than reboot-looping into the same fault.
        """
        if not self.settings.get("apply_on_startup", True):
            decky.logger.info("Startup apply disabled by setting, skipping")
            return

        sentinel = self._sentinel_path()
        if os.path.exists(sentinel):
            decky.logger.error(
                "Previous startup apply did not complete - disabling startup apply. "
                "Re-enable it from the Ally Center panel once the cause is understood."
            )
            self.settings["apply_on_startup"] = False
            self.settings["startup_apply_failed"] = True
            try:
                os.remove(sentinel)
            except OSError:
                pass
            await self.save_settings()
            return

        try:
            os.makedirs(os.path.dirname(sentinel), exist_ok=True)
            with open(sentinel, 'w') as f:
                f.write(str(time.time()))
        except Exception as e:
            decky.logger.warning(f"Could not write startup sentinel, applying anyway: {e}")

        try:
            await self.apply_all_settings()
        finally:
            try:
                if os.path.exists(sentinel):
                    os.remove(sentinel)
            except OSError as e:
                decky.logger.warning(f"Could not clear startup sentinel: {e}")

    def _start_resume_watcher(self):
        """Detect resume from suspend without depending on Steam or DBus.

        CLOCK_MONOTONIC does not advance while the system is suspended, but
        CLOCK_BOOTTIME does. A gap between the two deltas is therefore a suspend
        that just ended, and its size is how long we were asleep.

        The frontend's SteamClient resume callback was tried first and never fired
        on this Steam build, so this is the mechanism that actually works.
        """
        if self.resume_watcher and self.resume_watcher.is_alive():
            return

        def watch():
            last_mono = time.monotonic()
            last_boot = time.clock_gettime(time.CLOCK_BOOTTIME)
            while self.resume_watcher_running:
                time.sleep(self.RESUME_POLL_SECONDS)
                mono = time.monotonic()
                boot = time.clock_gettime(time.CLOCK_BOOTTIME)
                slept = (boot - last_boot) - (mono - last_mono)
                last_mono, last_boot = mono, boot

                if slept >= self.RESUME_MIN_SUSPEND_SECONDS:
                    decky.logger.info(f"Detected resume after {slept:.0f}s suspended")
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self.on_resume(), self.loop
                        ).result(timeout=30)
                    except Exception as e:
                        decky.logger.error(f"Resume re-apply failed: {e}")

        self.resume_watcher_running = True
        self.resume_watcher = threading.Thread(target=watch, daemon=True)
        self.resume_watcher.start()
        decky.logger.info("Resume watcher started")

    async def on_suspend(self) -> bool:
        """Stop RGB animation threads before the system suspends.

        The effect threads write to the LED node in a loop, and those writes go over
        USB HID to the MCU. A thread caught mid-write when the kernel freezes userspace
        can sit in uninterruptible sleep and stall the freeze. Stopping cleanly first
        removes that hazard. This is precautionary - it is not established that it has
        ever caused a hang. See docs/suspend-hang-investigation.md.
        """
        try:
            self._stop_effect()
            decky.logger.info("Suspending, stopped RGB effect threads")
            return True
        except Exception as e:
            decky.logger.error(f"Failed to stop effects before suspend: {e}")
            return False

    async def on_resume(self) -> bool:
        """Re-apply hardware state after waking from suspend.

        The MCU resets the joystick rings across a suspend/resume cycle, so RGB set
        before sleep is gone on wake. Power limits are re-applied too since firmware
        may restore its own defaults. Called from the frontend's resume handler.
        """
        try:
            decky.logger.info("Resumed from suspend, re-applying settings")
            await self._apply_rgb()

            if not self.settings.get("use_external_tdp"):
                if self.settings.get("tdp_override"):
                    await self.set_tdp(self.settings.get("custom_tdp", 17))
                else:
                    profile = self.settings.get("current_profile", "performance")
                    if profile != "download":
                        await self.set_performance_profile(profile, apply_fan=False)
            return True
        except Exception as e:
            decky.logger.error(f"Failed to re-apply after resume: {e}")
            return False

    async def get_startup_apply(self) -> dict:
        return {
            "enabled": self.settings.get("apply_on_startup", True),
            "last_attempt_failed": self.settings.get("startup_apply_failed", False),
        }

    async def set_startup_apply(self, enabled: bool) -> bool:
        self.settings["apply_on_startup"] = enabled
        # Clearing the flag acknowledges the previous failure
        self.settings["startup_apply_failed"] = False
        await self.save_settings()
        decky.logger.info(f"Startup apply {'enabled' if enabled else 'disabled'}")
        return True

    async def apply_all_settings(self) -> dict:
        """Push every persisted setting to the hardware.

        Nothing here did this before, which is why settings survived a reboot in the
        UI but not on the device. Each step is isolated so one unsupported control
        cannot stop the rest from being applied.
        """
        results = {}

        async def step(name, coro):
            try:
                results[name] = bool(await coro)
            except Exception as e:
                decky.logger.error(f"Startup apply failed for {name}: {e}")
                results[name] = False

        # Don't restore Download mode on boot - if the plugin died while it was
        # active the device would come back crippled and dark with no obvious cause.
        profile = self.settings.get("current_profile", "performance")
        if profile == "download":
            profile = self.settings.get("saved_profile", "performance")
            self.settings["current_profile"] = profile

        # Fan mode is deliberately NOT applied here. throttle_thermal_policy is the
        # same knob as SteamOS's Performance Profile, so writing it at boot silently
        # reverted a low-power/performance selection made in SteamOS settings.
        if self.settings.get("tdp_override") and not self.settings.get("use_external_tdp"):
            await step("tdp", self.set_tdp(self.settings.get("custom_tdp", 17)))
        elif not self.settings.get("use_external_tdp"):
            await step("profile", self.set_performance_profile(profile, apply_fan=False))
        else:
            decky.logger.info("External TDP management enabled, skipping TDP restore")

        await step("rgb", self._apply_rgb())

        # Deliberately NOT re-applying the charge limit. It persists in hardware on
        # its own, and SteamOS has its own charge-limit setting - pushing our stored
        # value at boot silently reverts whatever the user set outside this plugin.
        # Adopt the hardware value instead so the UI reflects reality.
        try:
            hw = await self.get_charge_limit()
            if hw.get("available"):
                self.settings["charge_limit"] = hw["limit"]
        except Exception as e:
            decky.logger.warning(f"Could not sync charge limit from hardware: {e}")

        if "smt_enabled" in self.settings:
            await step("smt", self.set_smt_enabled(self.settings["smt_enabled"]))
        if "cpu_boost_enabled" in self.settings:
            await step("cpu_boost", self.set_cpu_boost_enabled(self.settings["cpu_boost_enabled"]))
        if self.settings.get("epp"):
            await step("epp", self.set_epp(self.settings["epp"]))

        # Fan curves are runtime state and are lost on reboot. Boot sound is a
        # firmware attribute and persists on its own, so it is not re-applied.
        for fan, points in (self.settings.get("fan_curves") or {}).items():
            if points:
                await step(f"fan_curve_{fan}", self.set_fan_curve(fan, points))

        await self.save_settings()
        decky.logger.info(f"Applied settings at startup: {results}")
        return results

    async def _unload(self):
        """Cleanup when plugin is unloaded"""
        # Stop any running effect
        self.resume_watcher_running = False
        self._stop_effect()
        # Restore screen if it was off
        if self.screen_off:
            await self.set_screen_state(True)
        decky.logger.info("Ally Center unloaded")

    async def _migration(self):
        """Handle plugin migrations"""
        pass

    async def load_settings(self):
        try:
            if os.path.exists(self.settings_path):
                with open(self.settings_path, 'r') as f:
                    self.settings = json.load(f)
            else:
                self.settings = {
                    "current_profile": "performance",
                    "rgb_enabled": True,
                    "rgb_color": "#FF0000",
                    "rgb_brightness": 100,
                    "rgb_effect": "static",
                    "charge_limit": 100,
                    "apply_on_startup": True
                }
                await self.save_settings()
        except Exception as e:
            decky.logger.error(f"Failed to load settings: {e}")
            self.settings = {}
        return self.settings

    async def save_settings(self):
        try:
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            with open(self.settings_path, 'w') as f:
                json.dump(self.settings, f, indent=2)
        except Exception as e:
            decky.logger.error(f"Failed to save settings: {e}")

    async def get_settings(self) -> dict:
        return self.settings

    async def update_setting(self, key: str, value) -> bool:
        self.settings[key] = value
        await self.save_settings()
        return True

    async def get_device_info(self) -> dict:
        info = {
            "model": "Unknown",
            "bios_version": "Unknown",
            "serial": "Unknown",
            "cpu": "Unknown",
            "gpu": "Unknown",
            "kernel": "Unknown",
            "memory_total": "Unknown"
        }
        
        try:
            # Read DMI info
            dmi_files = {
                "model": "product_name",
                "bios_version": "bios_version",
                "serial": "product_serial"
            }
            
            for key, filename in dmi_files.items():
                filepath = os.path.join(DMI_PATH, filename)
                if os.path.exists(filepath):
                    with open(filepath, 'r') as f:
                        info[key] = f.read().strip()
            
            # Get CPU info
            if os.path.exists("/proc/cpuinfo"):
                with open("/proc/cpuinfo", 'r') as f:
                    for line in f:
                        if line.startswith("model name"):
                            info["cpu"] = line.split(":")[1].strip()
                            break
            
            # Get kernel version
            result = subprocess.run(["uname", "-r"], capture_output=True, text=True)
            if result.returncode == 0:
                info["kernel"] = result.stdout.strip()
            
            # Get memory info
            if os.path.exists("/proc/meminfo"):
                with open("/proc/meminfo", 'r') as f:
                    for line in f:
                        if line.startswith("MemTotal"):
                            mem_kb = int(line.split()[1])
                            info["memory_total"] = f"{mem_kb // 1024 // 1024} GB"
                            break
            
            # GPU info (AMD APU)
            info["gpu"] = "AMD Radeon 780M" if "Z1" in info.get("cpu", "") else "AMD Radeon Graphics"
            
        except Exception as e:
            decky.logger.error(f"Failed to get device info: {e}")
        
        return info

    async def get_battery_info(self) -> dict:
        battery = {
            "present": False,
            "status": "Unknown",
            "capacity": 0,
            "health": 100,
            "cycle_count": 0,
            "voltage": 0,
            "current": 0,
            "temperature": 0,
            "design_capacity": 0,
            "full_capacity": 0,
            "charge_limit": self._read_charge_limit(),
            "time_to_empty": "Unknown",
            "time_to_full": "Unknown"
        }
        
        try:
            if not os.path.exists(BATTERY_PATH):
                return battery
            
            battery["present"] = True
            
            # Read battery files
            battery_files = {
                "status": "status",
                "capacity": "capacity",
                "cycle_count": "cycle_count",
                "voltage_now": "voltage_now",
                "current_now": "current_now",
                "energy_full_design": "energy_full_design",
                "energy_full": "energy_full"
            }
            
            for key, filename in battery_files.items():
                filepath = os.path.join(BATTERY_PATH, filename)
                if os.path.exists(filepath):
                    with open(filepath, 'r') as f:
                        value = f.read().strip()
                        if key == "status":
                            battery["status"] = value
                        elif key == "capacity":
                            battery["capacity"] = int(value)
                        elif key == "cycle_count":
                            battery["cycle_count"] = int(value)
                        elif key == "voltage_now":
                            battery["voltage"] = int(value) / 1000000  # Convert to V
                        elif key == "current_now":
                            battery["current"] = int(value) / 1000000  # Convert to A
                        elif key == "energy_full_design":
                            battery["design_capacity"] = int(value) / 1000000  # Convert to Wh
                        elif key == "energy_full":
                            battery["full_capacity"] = int(value) / 1000000  # Convert to Wh
            
            # Calculate health percentage
            if battery["design_capacity"] > 0:
                battery["health"] = round((battery["full_capacity"] / battery["design_capacity"]) * 100, 1)
            
            # Try to get temperature from ACPI
            temp_path = os.path.join(BATTERY_PATH, "temp")
            if os.path.exists(temp_path):
                with open(temp_path, 'r') as f:
                    battery["temperature"] = int(f.read().strip()) / 10  # Convert to Celsius
            
        except Exception as e:
            decky.logger.error(f"Failed to get battery info: {e}")
        
        return battery

    async def get_rgb_state(self) -> dict:
        return {
            "enabled": self.settings.get("rgb_enabled", True),
            "color": self.settings.get("rgb_color", "#FF0000"),
            "brightness": self.settings.get("rgb_brightness", 100),
            "effect": self.settings.get("rgb_effect", "static"),
            "speed": self.settings.get("rgb_speed", 50),
            "available": os.path.exists(ALLY_LED_PATH),
            "trigger": self._current_led_trigger()
        }

    async def set_rgb_color(self, color: str) -> bool:
        try:
            self.settings["rgb_color"] = color
            await self.save_settings()
            await self._apply_rgb()
            return True
        except Exception as e:
            decky.logger.error(f"Failed to set RGB color: {e}")
            return False

    async def set_rgb_brightness(self, brightness: int) -> bool:
        try:
            brightness = max(0, min(100, brightness))
            self.settings["rgb_brightness"] = brightness
            await self.save_settings()
            await self._apply_rgb()
            return True
        except Exception as e:
            decky.logger.error(f"Failed to set RGB brightness: {e}")
            return False

    async def set_rgb_speed(self, speed: int) -> bool:
        try:
            speed = max(10, min(100, speed))
            self.settings["rgb_speed"] = speed
            await self.save_settings()
            # Restart effect if one is running to apply new speed
            effect = self.settings.get("rgb_effect", "static")
            if effect not in ["static", "off"]:
                await self._apply_rgb()
            decky.logger.info(f"Set RGB speed to {speed}%")
            return True
        except Exception as e:
            decky.logger.error(f"Failed to set RGB speed: {e}")
            return False

    async def set_rgb_effect(self, effect: str) -> bool:
        try:
            self.settings["rgb_effect"] = effect
            self.settings["rgb_enabled"] = effect != "off"
            await self.save_settings()
            await self._apply_rgb()
            return True
        except Exception as e:
            decky.logger.error(f"Failed to set RGB effect: {e}")
            return False

    async def set_rgb_enabled(self, enabled: bool) -> bool:
        try:
            self.settings["rgb_enabled"] = enabled
            await self.save_settings()
            await self._apply_rgb()
            # When RGB is disabled, enable MCU powersave to stop charging LED blink
            await self._set_mcu_powersave(not enabled)
            return True
        except Exception as e:
            decky.logger.error(f"Failed to toggle RGB: {e}")
            return False

    async def _set_mcu_powersave(self, enabled: bool) -> bool:
        """Enable/disable MCU powersave mode to control charging LED blink during sleep"""
        try:
            value = "1" if enabled else "0"
            via = self._write_platform_attr("mcu_powersave", "mcu_powersave", value)
            if via:
                decky.logger.info(f"MCU powersave {'enabled' if enabled else 'disabled'} ({via})")
                return True
            decky.logger.warning("MCU powersave not available")
            return False
        except PermissionError:
            decky.logger.warning("Permission denied setting MCU powersave")
            return False
        except Exception as e:
            decky.logger.error(f"Failed to set MCU powersave: {e}")
            return False

    def _stop_effect(self):
        self.effect_running = False
        if self.effect_thread and self.effect_thread.is_alive():
            self.effect_thread.join(timeout=1.0)
        self.effect_thread = None

    def _set_led_color(self, r: int, g: int, b: int, brightness: int = 255):
        try:
            brightness_path = os.path.join(ALLY_LED_PATH, "brightness")
            multi_intensity_path = os.path.join(ALLY_LED_PATH, "multi_intensity")
            
            color_int = (r << 16) | (g << 8) | b
            
            if os.path.exists(multi_intensity_path):
                color_str = f"{color_int} {color_int} {color_int} {color_int}"
                with open(multi_intensity_path, 'w') as f:
                    f.write(color_str)
            
            if os.path.exists(brightness_path):
                with open(brightness_path, 'w') as f:
                    f.write(str(brightness))
        except Exception as e:
            pass  # Silently fail during animations

    def _set_led_zones(self, colors: list, brightness: int = 255):
        try:
            brightness_path = os.path.join(ALLY_LED_PATH, "brightness")
            multi_intensity_path = os.path.join(ALLY_LED_PATH, "multi_intensity")
            
            color_ints = []
            for r, g, b in colors:
                color_ints.append((r << 16) | (g << 8) | b)
            
            if os.path.exists(multi_intensity_path):
                color_str = " ".join(str(c) for c in color_ints)
                with open(multi_intensity_path, 'w') as f:
                    f.write(color_str)
            
            if os.path.exists(brightness_path):
                with open(brightness_path, 'w') as f:
                    f.write(str(brightness))
        except Exception as e:
            pass

    def _get_effect_delay(self) -> float:
        """Calculate delay based on speed setting (10-100). Higher speed = shorter delay."""
        speed = self.settings.get("rgb_speed", 50)
        # Map speed 10-100 to delay 0.15-0.01 seconds (inverted)
        return 0.15 - (speed - 10) * (0.14 / 90)

    def _effect_pulse(self):
        color = self.settings.get("rgb_color", "#FF0000").lstrip('#')
        r = int(color[0:2], 16)
        g = int(color[2:4], 16)
        b = int(color[4:6], 16)
        base_brightness = int(self.settings.get("rgb_brightness", 100) * 255 / 100)
        
        phase = 0.0
        while self.effect_running:
            delay = self._get_effect_delay()
            # Sine wave for smooth breathing (0 to 1)
            factor = (math.sin(phase) + 1) / 2
            brightness = int(base_brightness * (0.1 + 0.9 * factor))
            self._set_led_color(r, g, b, brightness)
            phase += 0.1
            time.sleep(delay)

    def _effect_spectrum(self):
        base_brightness = int(self.settings.get("rgb_brightness", 100) * 255 / 100)
        
        hue = 0
        while self.effect_running:
            delay = self._get_effect_delay()
            # HSV to RGB conversion
            h = hue / 360.0
            i = int(h * 6)
            f = h * 6 - i
            q = 1 - f
            t = f
            
            if i % 6 == 0: r, g, b = 1, t, 0
            elif i % 6 == 1: r, g, b = q, 1, 0
            elif i % 6 == 2: r, g, b = 0, 1, t
            elif i % 6 == 3: r, g, b = 0, q, 1
            elif i % 6 == 4: r, g, b = t, 0, 1
            else: r, g, b = 1, 0, q
            
            self._set_led_color(int(r * 255), int(g * 255), int(b * 255), base_brightness)
            hue = (hue + 2) % 360
            time.sleep(delay)

    def _effect_wave(self):
        base_brightness = int(self.settings.get("rgb_brightness", 100) * 255 / 100)
        
        offset = 0
        while self.effect_running:
            delay = self._get_effect_delay()
            colors = []
            for zone in range(4):
                hue = ((offset + zone * 90) % 360) / 360.0
                i = int(hue * 6)
                f = hue * 6 - i
                q = 1 - f
                t = f
                
                if i % 6 == 0: r, g, b = 1, t, 0
                elif i % 6 == 1: r, g, b = q, 1, 0
                elif i % 6 == 2: r, g, b = 0, 1, t
                elif i % 6 == 3: r, g, b = 0, q, 1
                elif i % 6 == 4: r, g, b = t, 0, 1
                else: r, g, b = 1, 0, q
                
                colors.append((int(r * 255), int(g * 255), int(b * 255)))
            
            self._set_led_zones(colors, base_brightness)
            offset = (offset + 3) % 360
            time.sleep(delay)

    def _effect_flash(self):
        color = self.settings.get("rgb_color", "#FF0000").lstrip('#')
        r = int(color[0:2], 16)
        g = int(color[2:4], 16)
        b = int(color[4:6], 16)
        base_brightness = int(self.settings.get("rgb_brightness", 100) * 255 / 100)
        
        on = True
        while self.effect_running:
            # Flash uses longer delay (3x normal) since it's on/off
            delay = self._get_effect_delay() * 3
            if on:
                self._set_led_color(r, g, b, base_brightness)
            else:
                self._set_led_color(0, 0, 0, 0)
            on = not on
            time.sleep(delay)

    def _set_led_trigger(self, trigger: str) -> bool:
        """Point the LED at a kernel trigger, or "none" to drive it ourselves.

        The kernel can drive battery state directly, which is what the old battery
        effect emulated with a polling thread.
        """
        try:
            path = os.path.join(ALLY_LED_PATH, "trigger")
            if not os.path.exists(path):
                return False
            with open(path, 'w') as f:
                f.write(trigger)
            return True
        except Exception as e:
            decky.logger.warning(f"Could not set LED trigger '{trigger}': {e}")
            return False

    def _current_led_trigger(self) -> str:
        """The trigger file lists all options with the active one in [brackets]."""
        try:
            with open(os.path.join(ALLY_LED_PATH, "trigger"), 'r') as f:
                for token in f.read().split():
                    if token.startswith('[') and token.endswith(']'):
                        return token[1:-1]
        except Exception:
            pass
        return "none"

    def _effect_battery(self):
        """RGB color based on battery level - green (full) to red (empty).

        Retained as a fallback for devices whose LED lacks the BAT0 kernel triggers;
        _apply_rgb prefers the trigger, which needs no thread.
        """
        base_brightness = int(self.settings.get("rgb_brightness", 100) * 255 / 100)
        
        while self.effect_running:
            try:
                # Read battery capacity
                capacity = 50  # Default
                capacity_path = os.path.join(BATTERY_PATH, "capacity")
                if os.path.exists(capacity_path):
                    with open(capacity_path, 'r') as f:
                        capacity = int(f.read().strip())
                
                # Calculate color: green (100%) -> yellow (50%) -> red (0%)
                if capacity >= 50:
                    # Green to Yellow (100% -> 50%)
                    ratio = (capacity - 50) / 50.0
                    r = int(255 * (1 - ratio))
                    g = 255
                    b = 0
                else:
                    # Yellow to Red (50% -> 0%)
                    ratio = capacity / 50.0
                    r = 255
                    g = int(255 * ratio)
                    b = 0
                
                self._set_led_color(r, g, b, base_brightness)
                time.sleep(5)  # Update every 5 seconds
                
            except Exception as e:
                time.sleep(5)

    def _start_effect(self, effect: str):
        self._stop_effect()
        
        if effect == "static" or effect == "off":
            return  # No animation needed
        
        effect_map = {
            "pulse": self._effect_pulse,
            "spectrum": self._effect_spectrum,
            "wave": self._effect_wave,
            "flash": self._effect_flash,
            "battery": self._effect_battery,
        }
        
        effect_func = effect_map.get(effect)
        if effect_func:
            self.effect_running = True
            self.effect_thread = threading.Thread(target=effect_func, daemon=True)
            self.effect_thread.start()
            decky.logger.info(f"Started effect: {effect}")

    async def _apply_rgb(self) -> bool:
        try:
            if not os.path.exists(ALLY_LED_PATH):
                decky.logger.warning("Ally LED path not found")
                return False

            brightness_path = os.path.join(ALLY_LED_PATH, "brightness")

            if not self.settings.get("rgb_enabled", True):
                # Turn off RGB
                self._stop_effect()
                self._set_led_trigger("none")
                if os.path.exists(brightness_path):
                    with open(brightness_path, 'w') as f:
                        f.write("0")
                decky.logger.info("RGB disabled")
                return True

            effect = self.settings.get("rgb_effect", "static")

            if effect == "off":
                self._stop_effect()
                self._set_led_trigger("none")
                if os.path.exists(brightness_path):
                    with open(brightness_path, 'w') as f:
                        f.write("0")
                return True

            # Battery state can be driven by the kernel directly, with no polling
            # thread. Falls through to the Python effect if the trigger is missing.
            if effect == "battery":
                self._stop_effect()
                if self._set_led_trigger(BATTERY_LED_TRIGGER):
                    brightness = self.settings.get("rgb_brightness", 100)
                    if os.path.exists(brightness_path):
                        with open(brightness_path, 'w') as f:
                            f.write(str(int(brightness * 255 / 100)))
                    decky.logger.info(f"RGB battery mode via kernel trigger "
                                      f"'{BATTERY_LED_TRIGGER}'")
                    return True
                decky.logger.info("Battery LED trigger unavailable, using polling effect")

            # Any self-driven mode needs the kernel to stop owning the LED
            self._set_led_trigger("none")

            if effect == "static":
                # Static color - no animation
                self._stop_effect()
                color = self.settings.get("rgb_color", "#FF0000").lstrip('#')
                brightness = self.settings.get("rgb_brightness", 100)
                
                r = int(color[0:2], 16)
                g = int(color[2:4], 16)
                b = int(color[4:6], 16)
                hw_brightness = int(brightness * 255 / 100)
                
                self._set_led_color(r, g, b, hw_brightness)
                decky.logger.info(f"Set static RGB: #{color} @ {brightness}%")
            else:
                # Start animated effect
                self._start_effect(effect)

            return True

        except Exception as e:
            decky.logger.error(f"Failed to apply RGB settings: {e}")
            return False

    def _command_exists(self, cmd: str) -> bool:
        return subprocess.run(
            ["which", cmd], 
            capture_output=True
        ).returncode == 0

    async def get_performance_profiles(self) -> dict:
        return {
            "profiles": PERFORMANCE_PROFILES,
            "current": self.settings.get("current_profile", "performance")
        }

    async def set_performance_profile(self, profile_id: str, apply_fan: bool = True) -> bool:
        try:
            if profile_id not in PERFORMANCE_PROFILES:
                decky.logger.error(f"Unknown profile: {profile_id}")
                return False

            profile = PERFORMANCE_PROFILES[profile_id]
            tdp = profile["tdp"]
            fan_curve = profile.get("fan_curve", "balanced")

            await self.set_tdp(tdp)
            # throttle_thermal_policy is the same knob as SteamOS's Performance
            # Profile (low-power/balanced/performance). Writing it unprompted - at
            # boot or on resume - silently reverts the user's SteamOS selection, so
            # it is only written when the user picks a profile here.
            if apply_fan:
                await self.set_fan_mode(fan_curve)

            self.settings["current_profile"] = profile_id
            self.settings["tdp_override"] = False
            await self.save_settings()
            
            fan_note = f"fan={fan_curve}" if apply_fan else "fan unchanged (SteamOS owns it)"
            decky.logger.info(f"Applied profile: {profile['name']} ({tdp}W, {fan_note})")
            return True
            
        except Exception as e:
            decky.logger.error(f"Failed to set performance profile: {e}")
            return False

    async def get_current_tdp(self) -> dict:
        result = {
            "tdp": 0,
            "cpu_temp": 0,
            "gpu_temp": 0
        }
        
        try:
            # Try to read from hwmon
            hwmon_base = "/sys/class/hwmon"
            if os.path.exists(hwmon_base):
                for hwmon in os.listdir(hwmon_base):
                    hwmon_path = os.path.join(hwmon_base, hwmon)
                    name_path = os.path.join(hwmon_path, "name")
                    
                    if os.path.exists(name_path):
                        with open(name_path, 'r') as f:
                            name = f.read().strip()
                        
                        # AMD CPU/APU temps
                        if name in ["k10temp", "zenpower"]:
                            temp_path = os.path.join(hwmon_path, "temp1_input")
                            if os.path.exists(temp_path):
                                with open(temp_path, 'r') as f:
                                    result["cpu_temp"] = int(f.read().strip()) / 1000
                        
                        # AMD GPU temps
                        if name == "amdgpu":
                            temp_path = os.path.join(hwmon_path, "temp1_input")
                            if os.path.exists(temp_path):
                                with open(temp_path, 'r') as f:
                                    result["gpu_temp"] = int(f.read().strip()) / 1000

                            # GPU clock
                            freq_path = os.path.join(hwmon_path, "freq1_input")
                            if os.path.exists(freq_path):
                                with open(freq_path, 'r') as f:
                                    result["gpu_clock"] = int(f.read().strip()) / 1000000  # MHz

                            # Live APU package power. This is the actual draw, as
                            # opposed to the configured limit - "tdp" was hardcoded 0.
                            power_path = os.path.join(hwmon_path, "power1_input")
                            if os.path.exists(power_path):
                                with open(power_path, 'r') as f:
                                    result["tdp"] = round(int(f.read().strip()) / 1000000, 1)  # W
            
        except Exception as e:
            decky.logger.error(f"Failed to get TDP info: {e}")
        
        return result

    async def get_screen_state(self) -> dict:
        return {
            "screen_off": self.screen_off,
            "brightness": await self._get_brightness()
        }

    async def _get_brightness(self) -> int:
        # BACKLIGHT_PATH is the device directory itself and holds brightness
        # directly - it has no per-device subdirectories to walk.
        try:
            brightness_path = os.path.join(BACKLIGHT_PATH, "brightness")
            max_path = os.path.join(BACKLIGHT_PATH, "max_brightness")

            if os.path.exists(brightness_path) and os.path.exists(max_path):
                with open(brightness_path, 'r') as f:
                    current = int(f.read().strip())
                with open(max_path, 'r') as f:
                    maximum = int(f.read().strip())

                if maximum > 0:
                    return int((current / maximum) * 100)
        except Exception as e:
            decky.logger.error(f"Failed to get brightness: {e}")

        return 100

    async def set_screen_state(self, on: bool) -> bool:
        try:
            brightness_file = os.path.join(BACKLIGHT_PATH, "brightness")
            max_file = os.path.join(BACKLIGHT_PATH, "max_brightness")
            
            if not os.path.exists(brightness_file):
                decky.logger.error(f"Backlight device not found at {brightness_file}")
                return False
            
            if on:
                # Restore brightness to saved value
                with open(max_file, 'r') as f:
                    max_brightness = int(f.read().strip())
                restore_value = self.settings.get("saved_brightness", max_brightness // 2)
                with open(brightness_file, 'w') as f:
                    f.write(str(restore_value))
                decky.logger.info(f"Screen restored to brightness {restore_value}")
                
                # Restore previous performance profile
                saved_profile = self.settings.get("saved_profile", "performance")
                await self.set_performance_profile(saved_profile)
                
                # Disable MCU powersave when exiting download mode (restore normal LED behavior)
                await self._set_mcu_powersave(False)
                
                self.screen_off = False
            else:
                # Save current brightness before turning off
                with open(brightness_file, 'r') as f:
                    current = int(f.read().strip())
                if current > 100:  # Only save if brightness is meaningful
                    self.settings["saved_brightness"] = current
                self.settings["saved_profile"] = self.settings.get("current_profile", "performance")
                await self.save_settings()
                decky.logger.info(f"Saved brightness: {current}, profile: {self.settings['saved_profile']}")
                
                # Set brightness to minimum
                with open(brightness_file, 'w') as f:
                    f.write("0")
                decky.logger.info("Screen brightness set to 0")
                
                # Set to download/5W profile
                await self.set_performance_profile("download")
                
                # Enable MCU powersave to disable charging LED blink during download mode
                await self._set_mcu_powersave(True)
                
                self.screen_off = True
            
            return True
            
        except Exception as e:
            decky.logger.error(f"Failed to set screen state: {e}")
            return False

    async def toggle_screen(self) -> bool:
        return await self.set_screen_state(self.screen_off)

    def _find_throttle_thermal_policy(self) -> str:
        """Find the throttle_thermal_policy sysfs path"""
        # Check direct path first
        direct_path = os.path.join(ASUS_WMI_PATH, "throttle_thermal_policy")
        if os.path.exists(direct_path):
            return direct_path
        
        # Check under hwmon
        hwmon_path = os.path.join(ASUS_WMI_PATH, "hwmon")
        if os.path.exists(hwmon_path):
            for hwmon in os.listdir(hwmon_path):
                policy_path = os.path.join(hwmon_path, hwmon, "throttle_thermal_policy")
                if os.path.exists(policy_path):
                    return policy_path
        
        # Check /sys/class/hwmon for asus-nb-wmi device
        hwmon_base = "/sys/class/hwmon"
        if os.path.exists(hwmon_base):
            for hwmon in os.listdir(hwmon_base):
                hwmon_dir = os.path.join(hwmon_base, hwmon)
                name_path = os.path.join(hwmon_dir, "name")
                if os.path.exists(name_path):
                    try:
                        with open(name_path, 'r') as f:
                            if "asus" in f.read().strip().lower():
                                policy_path = os.path.join(hwmon_dir, "throttle_thermal_policy")
                                if os.path.exists(policy_path):
                                    return policy_path
                    except:
                        pass
        
        return ""

    def _current_fan_mode(self) -> str:
        """Report the mode the hardware is actually in. SteamOS can change the
        platform profile behind our back, so stored settings are not the truth."""
        profile = self._get_platform_profile()
        for mode, name in self.PLATFORM_PROFILE_NAMES.items():
            # "auto" also maps to balanced; prefer the explicit "balanced" label
            if name == profile and mode != "auto":
                return mode
        return self.settings.get("fan_mode", "auto")

    def _find_hwmon_by_name(self, wanted: str) -> str:
        """Resolve a hwmon device by its name. Never trust hwmon index order -
        acpi_fan also exposes fan1_input and will shadow the ASUS device."""
        hwmon_base = "/sys/class/hwmon"
        if not os.path.exists(hwmon_base):
            return ""
        for hwmon in sorted(os.listdir(hwmon_base)):
            hwmon_path = os.path.join(hwmon_base, hwmon)
            name_path = os.path.join(hwmon_path, "name")
            try:
                with open(name_path, 'r') as f:
                    if f.read().strip() == wanted:
                        return hwmon_path
            except Exception:
                continue
        return ""

    async def get_fan_info(self) -> dict:
        result = {
            "mode": self._current_fan_mode(),
            "speed": 0,
            "cpu_fan": 0,
            "gpu_fan": 0,
            "available": False,
            "policy_path": "",
            "current_policy": -1
        }

        try:
            # Find throttle_thermal_policy path
            policy_path = self._find_throttle_thermal_policy()
            if policy_path:
                result["available"] = True
                result["policy_path"] = policy_path
                try:
                    with open(policy_path, 'r') as f:
                        result["current_policy"] = int(f.read().strip())
                except:
                    pass
            
            # Read both fans from the ASUS hwmon device specifically
            asus_hwmon = self._find_hwmon_by_name("asus")
            if asus_hwmon:
                for key, node in (("cpu_fan", "fan1_input"), ("gpu_fan", "fan2_input")):
                    fan_path = os.path.join(asus_hwmon, node)
                    if os.path.exists(fan_path):
                        try:
                            with open(fan_path, 'r') as f:
                                result[key] = int(f.read().strip())
                        except Exception:
                            pass
                # Keep "speed" for backwards compatibility with the existing UI
                result["speed"] = result["cpu_fan"]
        except Exception as e:
            decky.logger.error(f"Failed to get fan info: {e}")
        
        return result

    # Plugin fan modes mapped onto ACPI platform profile names. "auto" has no
    # distinct profile, so it means balanced.
    PLATFORM_PROFILE_NAMES = {
        "quiet": "low-power",
        "balanced": "balanced",
        "performance": "performance",
        "auto": "balanced",
    }

    def _read_platform_profile_choices(self) -> list:
        try:
            with open(PLATFORM_PROFILE_CHOICES_PATH, 'r') as f:
                return f.read().split()
        except Exception:
            return []

    def _set_platform_profile(self, mode: str) -> bool:
        """Write the ACPI platform profile by name. Returns False if unavailable so
        the caller can fall back to the numeric thermal policy."""
        wanted = self.PLATFORM_PROFILE_NAMES.get(mode)
        if not wanted or not os.path.exists(PLATFORM_PROFILE_PATH):
            return False

        choices = self._read_platform_profile_choices()
        if wanted not in choices:
            decky.logger.warning(
                f"Platform profile '{wanted}' not offered by this device "
                f"(choices: {choices or 'none'}), falling back to thermal policy"
            )
            return False

        try:
            with open(PLATFORM_PROFILE_PATH, 'w') as f:
                f.write(wanted)
            decky.logger.info(f"Set fan mode: {mode} (platform_profile={wanted})")
            return True
        except Exception as e:
            decky.logger.warning(f"Could not write platform profile: {e}")
            return False

    def _get_platform_profile(self) -> str:
        try:
            with open(PLATFORM_PROFILE_PATH, 'r') as f:
                return f.read().strip()
        except Exception:
            return ""

    async def set_fan_mode(self, mode: str) -> bool:
        try:
            self.settings["fan_mode"] = mode
            await self.save_settings()

            # Preferred path: write the ACPI platform profile by name. This is the
            # same underlying knob as throttle_thermal_policy and as SteamOS's
            # Performance Profile, but the names are model-independent - the numeric
            # policy mapping is not, and getting it wrong silently inverts the modes.
            if self._set_platform_profile(mode):
                return True

            # Fallback for devices without an ACPI platform profile. This numeric
            # mapping was verified on RC73XA (0=balanced, 1=performance, 2=low-power)
            # and may not hold on other models - hence the preference above.
            mode_map = {"quiet": "2", "balanced": "0", "performance": "1", "auto": "0"}
            policy_value = mode_map.get(mode, "0")

            # Find and write to throttle_thermal_policy
            policy_path = self._find_throttle_thermal_policy()
            if policy_path:
                try:
                    with open(policy_path, 'w') as f:
                        f.write(policy_value)
                    decky.logger.info(f"Set fan mode: {mode} (policy={policy_value}) via {policy_path}")
                    return True
                except PermissionError:
                    decky.logger.warning(f"Permission denied writing to {policy_path}")
                    # Try with subprocess as fallback
                    try:
                        result = subprocess.run(
                            ["tee", policy_path],
                            input=policy_value,
                            capture_output=True,
                            text=True
                        )
                        if result.returncode == 0:
                            decky.logger.info(f"Set fan mode via tee: {mode} (policy={policy_value})")
                            return True
                    except Exception as e:
                        decky.logger.error(f"tee fallback failed: {e}")
                    return False
                except Exception as e:
                    decky.logger.error(f"Failed to write to {policy_path}: {e}")
                    return False
            
            decky.logger.warning("Fan control not available - throttle_thermal_policy not found")
            decky.logger.info(f"Checked paths: {ASUS_WMI_PATH}/throttle_thermal_policy and hwmon subdirs")
            return False
        except Exception as e:
            decky.logger.error(f"Failed to set fan mode: {e}")
            return False

    # ---- Custom fan curves -------------------------------------------------
    # Verified on RC73XA: writing points has no effect until pwm{n}_enable is set to
    # 1, and setting it back to 2 returns control to the firmware. A bad curve still
    # has thermal consequences, hence the clamping and the restore-defaults call.

    _FAN_KEYS = {"cpu": 1, "gpu": 2}

    async def get_fan_curve(self) -> dict:
        result = {"available": False, "cpu": [], "gpu": [], "cpu_custom": False,
                  "gpu_custom": False, "stock": {k: [list(p) for p in v]
                                                 for k, v in STOCK_FAN_CURVES.items()}}
        try:
            hwmon = self._find_hwmon_by_name(FAN_CURVE_HWMON_NAME)
            if not hwmon:
                return result
            result["available"] = True

            for name, idx in self._FAN_KEYS.items():
                points = []
                for point in range(1, FAN_CURVE_POINTS + 1):
                    temp_path = os.path.join(hwmon, f"pwm{idx}_auto_point{point}_temp")
                    pwm_path = os.path.join(hwmon, f"pwm{idx}_auto_point{point}_pwm")
                    if not (os.path.exists(temp_path) and os.path.exists(pwm_path)):
                        break
                    try:
                        with open(temp_path, 'r') as f:
                            temp = int(f.read().strip())
                        with open(pwm_path, 'r') as f:
                            pwm = int(f.read().strip())
                        points.append([temp, pwm])
                    except Exception:
                        break
                result[name] = points

                enable_path = os.path.join(hwmon, f"pwm{idx}_enable")
                if os.path.exists(enable_path):
                    try:
                        with open(enable_path, 'r') as f:
                            result[f"{name}_custom"] = f.read().strip() == FAN_CURVE_ENABLE_CUSTOM
                    except Exception:
                        pass
        except Exception as e:
            decky.logger.error(f"Failed to read fan curve: {e}")
        return result

    def _sanitise_curve(self, points: list) -> list:
        """Clamp PWM to 0-255 and temps to 20-100C, and force both to be
        non-decreasing. A curve that dips could park the fan low at high temp."""
        clean = []
        last_temp, last_pwm = 0, 0
        for entry in points[:FAN_CURVE_POINTS]:
            try:
                temp, pwm = int(entry[0]), int(entry[1])
            except (TypeError, ValueError, IndexError):
                continue
            temp = max(20, min(100, temp))
            pwm = max(0, min(255, pwm))
            temp = max(temp, last_temp)
            pwm = max(pwm, last_pwm)
            clean.append((temp, pwm))
            last_temp, last_pwm = temp, pwm
        return clean

    async def set_fan_curve(self, fan: str, points: list) -> bool:
        """Write an 8-point curve. `fan` is "cpu" or "gpu"."""
        try:
            idx = self._FAN_KEYS.get(fan)
            if idx is None:
                decky.logger.error(f"Unknown fan '{fan}'")
                return False

            hwmon = self._find_hwmon_by_name(FAN_CURVE_HWMON_NAME)
            if not hwmon:
                decky.logger.warning("Custom fan curve not available")
                return False

            clean = self._sanitise_curve(points)
            if len(clean) < FAN_CURVE_POINTS:
                # Pad by repeating the last point, which is how the stock curves end
                if not clean:
                    decky.logger.error("Refusing to write an empty fan curve")
                    return False
                clean += [clean[-1]] * (FAN_CURVE_POINTS - len(clean))

            for point, (temp, pwm) in enumerate(clean, start=1):
                with open(os.path.join(hwmon, f"pwm{idx}_auto_point{point}_temp"), 'w') as f:
                    f.write(str(temp))
                with open(os.path.join(hwmon, f"pwm{idx}_auto_point{point}_pwm"), 'w') as f:
                    f.write(str(pwm))

            enable_path = os.path.join(hwmon, f"pwm{idx}_enable")
            if os.path.exists(enable_path):
                with open(enable_path, 'w') as f:
                    f.write(FAN_CURVE_ENABLE_CUSTOM)

            self.settings.setdefault("fan_curves", {})[fan] = [list(p) for p in clean]
            await self.save_settings()
            decky.logger.info(f"Set {fan} fan curve: {clean}")
            return True
        except Exception as e:
            decky.logger.error(f"Failed to set {fan} fan curve: {e}")
            return False

    async def reset_fan_curve(self, fan: str = "") -> bool:
        """Restore the stock curve(s) and hand control back to the firmware."""
        try:
            hwmon = self._find_hwmon_by_name(FAN_CURVE_HWMON_NAME)
            if not hwmon:
                return False

            targets = [fan] if fan in self._FAN_KEYS else list(self._FAN_KEYS)
            for name in targets:
                idx = self._FAN_KEYS[name]
                for point, (temp, pwm) in enumerate(STOCK_FAN_CURVES[name], start=1):
                    with open(os.path.join(hwmon, f"pwm{idx}_auto_point{point}_temp"), 'w') as f:
                        f.write(str(temp))
                    with open(os.path.join(hwmon, f"pwm{idx}_auto_point{point}_pwm"), 'w') as f:
                        f.write(str(pwm))
                enable_path = os.path.join(hwmon, f"pwm{idx}_enable")
                if os.path.exists(enable_path):
                    with open(enable_path, 'w') as f:
                        f.write(FAN_CURVE_ENABLE_AUTO)
                self.settings.get("fan_curves", {}).pop(name, None)

            await self.save_settings()
            decky.logger.info(f"Restored stock fan curve(s): {targets}")
            return True
        except Exception as e:
            decky.logger.error(f"Failed to reset fan curve: {e}")
            return False

    async def get_fan_diagnostics(self) -> dict:
        """Get diagnostic info about fan control paths for debugging"""
        result = {
            "asus_wmi_exists": os.path.exists(ASUS_WMI_PATH),
            "throttle_policy_path": "",
            "throttle_policy_value": -1,
            "fan_boost_mode_path": "",
            "fan_boost_mode_value": -1,
            "fan_curve_enable_path": "",
            "available_files": []
        }
        
        try:
            # Check direct throttle_thermal_policy
            policy_path = os.path.join(ASUS_WMI_PATH, "throttle_thermal_policy")
            if os.path.exists(policy_path):
                result["throttle_policy_path"] = policy_path
                try:
                    with open(policy_path, 'r') as f:
                        result["throttle_policy_value"] = int(f.read().strip())
                except:
                    pass
            
            # Check fan_boost_mode (alternative on some models)
            boost_path = os.path.join(ASUS_WMI_PATH, "fan_boost_mode")
            if os.path.exists(boost_path):
                result["fan_boost_mode_path"] = boost_path
                try:
                    with open(boost_path, 'r') as f:
                        result["fan_boost_mode_value"] = int(f.read().strip())
                except:
                    pass
            
            # Check fan_curve_enable
            curve_path = os.path.join(ASUS_WMI_PATH, "fan_curve_enable")
            if os.path.exists(curve_path):
                result["fan_curve_enable_path"] = curve_path
            
            # List all files in asus-nb-wmi
            if os.path.exists(ASUS_WMI_PATH):
                result["available_files"] = os.listdir(ASUS_WMI_PATH)
            
            decky.logger.info(f"Fan diagnostics: {result}")
        except Exception as e:
            decky.logger.error(f"Fan diagnostics error: {e}")
        
        return result

    # ---- firmware-attributes (asus_armoury) --------------------------------
    # The legacy /sys/devices/platform/asus-nb-wmi/* path still works but the kernel
    # logs a deprecation warning on every write and says it will be removed. These
    # helpers prefer the firmware-attributes class and fall back to the old path.

    def _armoury_path(self, name: str, leaf: str = "current_value") -> str:
        return os.path.join(ARMOURY_PATH, name, leaf)

    def _read_armoury(self, name: str, leaf: str = "current_value"):
        path = self._armoury_path(name, leaf)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r') as f:
                return f.read().strip()
        except Exception:
            return None

    def _write_armoury(self, name: str, value) -> bool:
        path = self._armoury_path(name)
        if not os.path.exists(path):
            return False
        try:
            with open(path, 'w') as f:
                f.write(str(value))
            return True
        except Exception as e:
            decky.logger.warning(f"Could not write armoury attribute {name}: {e}")
            return False

    def _armoury_range(self, name: str, fallback: tuple) -> tuple:
        """Read (min, max) from the driver rather than hardcoding per model.

        These come from a DMI-matched table in asus-armoury, and the driver keeps
        separate AC and battery sets - so the range genuinely changes when the
        charger is plugged in. Read it at write time; never cache it at startup.
        """
        lo = self._read_armoury(name, "min_value")
        hi = self._read_armoury(name, "max_value")
        try:
            if lo is not None and hi is not None:
                return int(lo), int(hi)
        except ValueError:
            pass
        return fallback

    def _write_platform_attr(self, armoury_name: str, legacy_name: str, value) -> str:
        """Write via firmware-attributes if possible, else the deprecated WMI node.
        Returns which path was used, or "" on failure.

        Verified: an armoury write takes effect immediately (a 7W limit set this way
        held the CPU at 820 MHz under load) and does not raise pending_reboot, so
        these are runtime settings rather than deferred firmware ones. Note the legacy
        node's readback does NOT reflect an armoury write - the two are separate
        readback caches over the same firmware, so never read the legacy node to
        determine current state.
        """
        if self._write_armoury(armoury_name, value):
            return "armoury"

        legacy = os.path.join(ASUS_WMI_PATH, legacy_name)
        if os.path.exists(legacy):
            try:
                with open(legacy, 'w') as f:
                    f.write(str(value))
                return "legacy"
            except Exception as e:
                decky.logger.warning(f"Could not write {legacy}: {e}")
        return ""

    async def get_pending_reboot(self) -> dict:
        """Whether firmware reports a change needing a reboot to take effect."""
        value = None
        path = os.path.join(ARMOURY_PATH, "pending_reboot")
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    value = f.read().strip()
            except Exception as e:
                decky.logger.warning(f"Could not read pending_reboot: {e}")
        return {"available": value is not None, "pending": value == "1"}

    async def set_tdp_override(self, enabled: bool) -> bool:
        try:
            self.settings["tdp_override"] = enabled
            await self.save_settings()
            decky.logger.info(f"TDP override {'enabled' if enabled else 'disabled'}")
            return True
        except Exception as e:
            decky.logger.error(f"Failed to set TDP override: {e}")
            return False

    async def get_tdp_settings(self) -> dict:
        # Range comes from firmware where available; the old hardcoded 5-30 let the
        # UI offer values below the firmware minimum, which were silently ignored.
        tdp_min, tdp_max = self._armoury_range("ppt_pl1_spl", (TDP_MIN, TDP_MAX))
        return {
            "tdp": self.settings.get("custom_tdp", 15),
            "min": tdp_min,
            "max": tdp_max,
            "tdp_override": self.settings.get("tdp_override", False),
            "use_external_tdp": self.settings.get("use_external_tdp", False),
            "available": os.path.exists(RYZENADJ_PATH) or os.path.exists("/sys/devices/platform/asus-nb-wmi")
        }

    async def set_use_external_tdp(self, enabled: bool) -> bool:
        """Enable/disable external TDP management (e.g., SimpleDeckyTDP)"""
        try:
            self.settings["use_external_tdp"] = enabled
            await self.save_settings()
            decky.logger.info(f"External TDP management {'enabled' if enabled else 'disabled'}")
            return True
        except Exception as e:
            decky.logger.error(f"Failed to set external TDP mode: {e}")
            return False

    async def set_tdp(self, tdp: int) -> bool:
        try:
            # Ranges come from firmware where available, so they are correct per
            # model rather than hardcoded from one device.
            pl1_lo, pl1_hi = self._armoury_range("ppt_pl1_spl", (TDP_MIN, TDP_MAX))
            pl2_lo, pl2_hi = self._armoury_range("ppt_pl2_sppt", TDP_PL2_RANGE)
            pl3_lo, pl3_hi = self._armoury_range("ppt_pl3_fppt", TDP_PL3_RANGE)

            tdp = max(pl1_lo, min(pl1_hi, tdp))
            self.settings["custom_tdp"] = tdp
            await self.save_settings()

            # pl1/pl2/pl3 are a staircase (stock 17/21/26), not one number written
            # three times. The nodes accept anything and let firmware clamp silently,
            # so the ranges are enforced here.
            pl2 = max(pl2_lo, min(pl2_hi, round(tdp * TDP_PL2_RATIO)))
            pl3 = max(pl3_lo, min(pl3_hi, round(tdp * TDP_PL3_RATIO)))

            # (armoury name, legacy WMI name, value) - note the fast limit is called
            # ppt_pl3_fppt through firmware-attributes but ppt_fppt on the old path.
            limits = [
                ("ppt_pl1_spl", "ppt_pl1_spl", tdp),
                ("ppt_pl2_sppt", "ppt_pl2_sppt", pl2),
                ("ppt_pl3_fppt", "ppt_fppt", pl3),
            ]

            written = []
            paths_used = set()
            for armoury_name, legacy_name, value in limits:
                via = self._write_platform_attr(armoury_name, legacy_name, value)
                if via:
                    written.append(f"{armoury_name}={value}")
                    paths_used.add(via)
                else:
                    decky.logger.warning(f"Could not set {armoury_name}")

            # These two exist only on the legacy path and have no firmware-attributes
            # equivalent. Write them only if we had to use the legacy path anyway,
            # so the normal path does not trigger the deprecation warning.
            if "legacy" in paths_used:
                for extra in ("ppt_apu_sppt", "ppt_platform_sppt"):
                    extra_path = os.path.join(ASUS_WMI_PATH, extra)
                    if os.path.exists(extra_path):
                        try:
                            with open(extra_path, 'w') as f:
                                f.write(str(tdp))
                            written.append(f"{extra}={tdp}")
                        except Exception:
                            pass

            # pl1 governs sustained power; without it the rest is meaningless.
            if any(w.startswith("ppt_pl1_spl=") for w in written):
                via = "+".join(sorted(paths_used))
                decky.logger.info(f"Set TDP {tdp}W via {via} ({', '.join(written)})")
                return True

            if os.path.exists(RYZENADJ_PATH):
                tdp_mw = tdp * 1000
                subprocess.run(
                    [RYZENADJ_PATH, f"--stapm-limit={tdp_mw}", f"--fast-limit={tdp_mw}",
                     f"--slow-limit={tdp_mw}"],
                    capture_output=True
                )
                decky.logger.info(f"Set TDP to {tdp}W via ryzenadj")
                return True

            decky.logger.warning("No TDP control method available")
            return False
        except Exception as e:
            decky.logger.error(f"Failed to set TDP: {e}")
            return False

    def _read_charge_limit(self) -> int:
        """Read the charge limit from hardware. SteamOS owns this setting, so the
        hardware is the source of truth, not anything we stored."""
        path = self._find_charge_limit_path()
        if path:
            try:
                with open(path, 'r') as f:
                    return int(f.read().strip())
            except Exception as e:
                decky.logger.warning(f"Could not read charge limit: {e}")
        return self.settings.get("charge_limit", 100)

    def _find_charge_limit_path(self) -> str:
        """Locate the charge threshold. It lives on the battery, not asus-nb-wmi."""
        if os.path.exists(CHARGE_LIMIT_PATH):
            return CHARGE_LIMIT_PATH
        # Fall back to scanning power supplies in case BAT0 is named differently
        ps_base = "/sys/class/power_supply"
        if os.path.exists(ps_base):
            for supply in sorted(os.listdir(ps_base)):
                candidate = os.path.join(ps_base, supply, "charge_control_end_threshold")
                if os.path.exists(candidate):
                    return candidate
        return ""

    async def get_charge_limit(self) -> dict:
        path = self._find_charge_limit_path()
        limit = self.settings.get("charge_limit", 100)

        # Report what the hardware actually holds - SteamOS has its own charge
        # limit setting and may have changed it behind our back.
        if path:
            try:
                with open(path, 'r') as f:
                    limit = int(f.read().strip())
            except Exception as e:
                decky.logger.warning(f"Could not read charge limit: {e}")

        return {"limit": limit, "available": bool(path)}



    async def set_brightness(self, brightness: int) -> bool:
        """Set screen brightness (0-100)"""
        try:
            brightness = max(0, min(100, brightness))

            brightness_path = os.path.join(BACKLIGHT_PATH, "brightness")
            max_path = os.path.join(BACKLIGHT_PATH, "max_brightness")

            if os.path.exists(brightness_path) and os.path.exists(max_path):
                with open(max_path, 'r') as f:
                    maximum = int(f.read().strip())

                hw_brightness = int((brightness / 100) * maximum)

                with open(brightness_path, 'w') as f:
                    f.write(str(hw_brightness))

                decky.logger.info(f"Set brightness to {brightness}% ({hw_brightness}/{maximum})")
                return True

            decky.logger.warning(f"Backlight device not found at {BACKLIGHT_PATH}")
            return False

        except Exception as e:
            decky.logger.error(f"Failed to set brightness: {e}")
            return False

    async def get_cpu_settings(self) -> dict:
        """Get current SMT, CPU boost and energy performance preference"""
        smt_path = "/sys/devices/system/cpu/smt/control"
        boost_path = "/sys/devices/system/cpu/cpufreq/boost"
        epp_path = os.path.join(CPUFREQ_BASE, "cpu0/cpufreq/energy_performance_preference")
        epp_avail_path = os.path.join(
            CPUFREQ_BASE, "cpu0/cpufreq/energy_performance_available_preferences"
        )

        result = {
            "smt_enabled": True,
            "smt_available": os.path.exists(smt_path),
            "boost_enabled": True,
            "boost_available": os.path.exists(boost_path),
            "epp": "",
            "epp_available": os.path.exists(epp_path),
            "epp_options": []
        }

        try:
            if os.path.exists(smt_path):
                with open(smt_path, 'r') as f:
                    smt_state = f.read().strip()
                result["smt_enabled"] = smt_state == "on"

            if os.path.exists(boost_path):
                with open(boost_path, 'r') as f:
                    boost_state = f.read().strip()
                result["boost_enabled"] = boost_state == "1"

            if os.path.exists(epp_path):
                with open(epp_path, 'r') as f:
                    result["epp"] = f.read().strip()
            if os.path.exists(epp_avail_path):
                with open(epp_avail_path, 'r') as f:
                    result["epp_options"] = f.read().split()
        except Exception as e:
            decky.logger.error(f"Failed to read CPU settings: {e}")

        return result

    async def set_epp(self, preference: str) -> bool:
        """Set the energy performance preference on every CPU.

        amd-pstate-epp exposes this per-policy, so writing only cpu0 would leave the
        other cores on the old preference.
        """
        try:
            options = (await self.get_cpu_settings()).get("epp_options", [])
            if options and preference not in options:
                decky.logger.error(f"Unsupported EPP '{preference}', options: {options}")
                return False

            written = 0
            for cpu in sorted(os.listdir(CPUFREQ_BASE)):
                if not cpu.startswith("cpu") or not cpu[3:].isdigit():
                    continue
                path = os.path.join(CPUFREQ_BASE, cpu, "cpufreq", "energy_performance_preference")
                if not os.path.exists(path):
                    continue
                try:
                    with open(path, 'w') as f:
                        f.write(preference)
                    written += 1
                except Exception as e:
                    decky.logger.warning(f"Could not set EPP on {cpu}: {e}")

            if written:
                self.settings["epp"] = preference
                await self.save_settings()
                decky.logger.info(f"Set EPP to {preference} on {written} CPUs")
                return True

            decky.logger.warning("EPP control not available")
            return False
        except Exception as e:
            decky.logger.error(f"Failed to set EPP: {e}")
            return False

    async def get_boot_sound(self) -> dict:
        value = self._read_armoury("boot_sound")
        if value is None:
            path = os.path.join(ASUS_WMI_PATH, "boot_sound")
            if os.path.exists(path):
                try:
                    with open(path, 'r') as f:
                        value = f.read().strip()
                except Exception as e:
                    decky.logger.error(f"Failed to read boot sound: {e}")
        return {"enabled": value == "1", "available": value is not None}

    async def set_boot_sound(self, enabled: bool) -> bool:
        """Toggle the POST beep. Note this is a firmware attribute, so unlike the
        other controls here it persists across reboots by design."""
        try:
            via = self._write_platform_attr("boot_sound", "boot_sound", "1" if enabled else "0")
            if not via:
                decky.logger.warning("Boot sound control not available")
                return False

            self.settings["boot_sound"] = enabled
            await self.save_settings()
            decky.logger.info(f"Boot sound {'enabled' if enabled else 'disabled'}")
            return True
        except Exception as e:
            decky.logger.error(f"Failed to set boot sound: {e}")
            return False

    async def get_monitoring(self) -> dict:
        """Live sensor readout for the panel."""
        result = {
            "cpu_temp": 0.0, "gpu_temp": 0.0, "nvme_temp": 0.0,
            "apu_power": 0.0, "gpu_busy": 0, "gpu_clock": 0,
            "cpu_fan": 0, "gpu_fan": 0,
            "charger_watts": 0.0, "on_ac": False,
        }

        def read_int(path):
            try:
                with open(path, 'r') as f:
                    return int(f.read().strip())
            except Exception:
                return None

        try:
            amdgpu = self._find_hwmon_by_name("amdgpu")
            if amdgpu:
                v = read_int(os.path.join(amdgpu, "temp1_input"))
                if v is not None:
                    result["gpu_temp"] = round(v / 1000, 1)
                v = read_int(os.path.join(amdgpu, "power1_input"))
                if v is not None:
                    result["apu_power"] = round(v / 1000000, 1)
                v = read_int(os.path.join(amdgpu, "freq1_input"))
                if v is not None:
                    result["gpu_clock"] = int(v / 1000000)

            k10 = self._find_hwmon_by_name("k10temp")
            if k10:
                v = read_int(os.path.join(k10, "temp1_input"))
                if v is not None:
                    result["cpu_temp"] = round(v / 1000, 1)

            nvme = self._find_hwmon_by_name("nvme")
            if nvme:
                v = read_int(os.path.join(nvme, "temp1_input"))
                if v is not None:
                    result["nvme_temp"] = round(v / 1000, 1)

            asus = self._find_hwmon_by_name("asus")
            if asus:
                for key, node in (("cpu_fan", "fan1_input"), ("gpu_fan", "fan2_input")):
                    v = read_int(os.path.join(asus, node))
                    if v is not None:
                        result[key] = v

            gpu_dev = self._find_amdgpu_device()
            if gpu_dev:
                v = read_int(os.path.join(gpu_dev, "gpu_busy_percent"))
                if v is not None:
                    result["gpu_busy"] = v

            ac_online = read_int("/sys/class/power_supply/AC0/online")
            result["on_ac"] = bool(ac_online)

            # Charger wattage from whichever USB-C PD source is live
            ps_base = "/sys/class/power_supply"
            if os.path.exists(ps_base):
                for supply in os.listdir(ps_base):
                    if not supply.startswith("ucsi-source-psy"):
                        continue
                    base = os.path.join(ps_base, supply)
                    if read_int(os.path.join(base, "online")) != 1:
                        continue
                    uv = read_int(os.path.join(base, "voltage_now")) or 0
                    ua = read_int(os.path.join(base, "current_now")) or 0
                    if uv and ua:
                        result["charger_watts"] = round((uv / 1000000) * (ua / 1000000), 1)
                        break
        except Exception as e:
            decky.logger.error(f"Failed to read monitoring data: {e}")

        return result

    def _find_amdgpu_device(self) -> str:
        """Resolve the amdgpu PCI device directory (not a connector directory)."""
        base = "/sys/bus/pci/drivers/amdgpu"
        if not os.path.exists(base):
            return ""
        for entry in sorted(os.listdir(base)):
            path = os.path.join(base, entry)
            if os.path.isdir(path) and os.path.exists(os.path.join(path, "gpu_busy_percent")):
                return path
        return ""

    async def set_smt_enabled(self, enabled: bool) -> bool:
        """Enable or disable Simultaneous Multi-Threading (SMT)"""
        try:
            smt_path = "/sys/devices/system/cpu/smt/control"
            
            if not os.path.exists(smt_path):
                decky.logger.warning("SMT control not available")
                return False
            
            value = "on" if enabled else "off"
            with open(smt_path, 'w') as f:
                f.write(value)
            
            self.settings["smt_enabled"] = enabled
            await self.save_settings()
            
            decky.logger.info(f"SMT {'enabled' if enabled else 'disabled'}")
            return True
            
        except PermissionError:
            decky.logger.error("Permission denied setting SMT - requires root")
            return False
        except Exception as e:
            decky.logger.error(f"Failed to set SMT: {e}")
            return False

    async def set_cpu_boost_enabled(self, enabled: bool) -> bool:
        """Enable or disable CPU boost"""
        try:
            boost_path = "/sys/devices/system/cpu/cpufreq/boost"
            
            if not os.path.exists(boost_path):
                decky.logger.warning("CPU boost control not available")
                return False
            
            value = "1" if enabled else "0"
            with open(boost_path, 'w') as f:
                f.write(value)
            
            self.settings["cpu_boost_enabled"] = enabled
            await self.save_settings()
            
            decky.logger.info(f"CPU boost {'enabled' if enabled else 'disabled'}")
            return True
            
        except PermissionError:
            decky.logger.error("Permission denied setting CPU boost - requires root")
            return False
        except Exception as e:
            decky.logger.error(f"Failed to set CPU boost: {e}")
            return False
