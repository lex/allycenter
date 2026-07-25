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
    
    async def _main(self):
        """Main entry point for the plugin"""
        self.settings_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
        await self.load_settings()
        await self._apply_on_startup()
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
                        await self.set_performance_profile(profile)
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

        if self.settings.get("tdp_override") and not self.settings.get("use_external_tdp"):
            await step("tdp", self.set_tdp(self.settings.get("custom_tdp", 17)))
            await step("fan_mode", self.set_fan_mode(self.settings.get("fan_mode", "auto")))
        elif not self.settings.get("use_external_tdp"):
            await step("profile", self.set_performance_profile(profile))
        else:
            decky.logger.info("External TDP management enabled, skipping TDP restore")
            await step("fan_mode", self.set_fan_mode(self.settings.get("fan_mode", "auto")))

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

        await self.save_settings()
        decky.logger.info(f"Applied settings at startup: {results}")
        return results

    async def _unload(self):
        """Cleanup when plugin is unloaded"""
        # Stop any running effect
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
            "available": os.path.exists(ALLY_LED_PATH)
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
            mcu_path = os.path.join(ASUS_WMI_PATH, "mcu_powersave")
            if os.path.exists(mcu_path):
                value = "1" if enabled else "0"
                with open(mcu_path, 'w') as f:
                    f.write(value)
                decky.logger.info(f"MCU powersave {'enabled' if enabled else 'disabled'}")
                return True
            else:
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

    def _effect_battery(self):
        """RGB color based on battery level - green (full) to red (empty)"""
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
                if os.path.exists(brightness_path):
                    with open(brightness_path, 'w') as f:
                        f.write("0")
                decky.logger.info("RGB disabled")
                return True

            effect = self.settings.get("rgb_effect", "static")

            if effect == "off":
                self._stop_effect()
                if os.path.exists(brightness_path):
                    with open(brightness_path, 'w') as f:
                        f.write("0")
                return True

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

    async def set_performance_profile(self, profile_id: str) -> bool:
        try:
            if profile_id not in PERFORMANCE_PROFILES:
                decky.logger.error(f"Unknown profile: {profile_id}")
                return False
            
            profile = PERFORMANCE_PROFILES[profile_id]
            tdp = profile["tdp"]
            fan_curve = profile.get("fan_curve", "balanced")
            
            await self.set_tdp(tdp)
            await self.set_fan_mode(fan_curve)
            
            self.settings["current_profile"] = profile_id
            self.settings["tdp_override"] = False
            await self.save_settings()
            
            decky.logger.info(f"Applied profile: {profile['name']} ({tdp}W, fan={fan_curve})")
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
            "mode": self.settings.get("fan_mode", "auto"),
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

    async def set_fan_mode(self, mode: str) -> bool:
        try:
            self.settings["fan_mode"] = mode
            await self.save_settings()
            
            # ROG Ally thermal policy values: 0=balanced, 1=silent/quiet, 2=turbo/performance
            # Note: Values 1 and 2 are swapped compared to other ASUS laptops
            mode_map = {"quiet": "1", "balanced": "0", "performance": "2", "auto": "0"}
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
        return {
            "tdp": self.settings.get("custom_tdp", 15),
            "min": 5,
            "max": 30,
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
            tdp = max(TDP_MIN, min(TDP_MAX, tdp))
            self.settings["custom_tdp"] = tdp
            await self.save_settings()

            # pl1/pl2/pl3 are a staircase (stock 17/21/26), not one number written
            # three times. The sysfs nodes accept anything and let the firmware clamp
            # silently, so the ranges are enforced here.
            pl2 = max(TDP_PL2_RANGE[0], min(TDP_PL2_RANGE[1], round(tdp * TDP_PL2_RATIO)))
            pl3 = max(TDP_PL3_RANGE[0], min(TDP_PL3_RANGE[1], round(tdp * TDP_PL3_RATIO)))

            ppt_values = {
                "ppt_pl1_spl": tdp,
                "ppt_pl2_sppt": pl2,
                "ppt_fppt": pl3,
                "ppt_apu_sppt": tdp,
                "ppt_platform_sppt": tdp,
            }

            written = []
            failed = []
            for name, value in ppt_values.items():
                ppt_path = os.path.join(ASUS_WMI_PATH, name)
                if not os.path.exists(ppt_path):
                    continue
                try:
                    with open(ppt_path, 'w') as f:
                        f.write(str(value))
                    written.append(f"{name}={value}")
                except PermissionError:
                    decky.logger.warning(f"Permission denied writing to {ppt_path}")
                    failed.append(name)
                except OSError as e:
                    decky.logger.warning(f"Rejected write {value} to {ppt_path}: {e}")
                    failed.append(name)

            # pl1 is the limit that actually governs sustained power; without it
            # the others are meaningless, so don't claim success.
            if any(w.startswith("ppt_pl1_spl=") for w in written):
                decky.logger.info(f"Set TDP {tdp}W via ASUS WMI ({', '.join(written)})")
                if failed:
                    decky.logger.warning(f"Some PPT writes failed: {failed}")
                return True
            
            if os.path.exists(RYZENADJ_PATH):
                tdp_mw = tdp * 1000
                subprocess.run(
                    [RYZENADJ_PATH, f"--stapm-limit={tdp_mw}", f"--fast-limit={tdp_mw}", f"--slow-limit={tdp_mw}"],
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
        """Get current SMT and CPU boost settings"""
        smt_path = "/sys/devices/system/cpu/smt/control"
        boost_path = "/sys/devices/system/cpu/cpufreq/boost"
        
        result = {
            "smt_enabled": True,
            "smt_available": os.path.exists(smt_path),
            "boost_enabled": True,
            "boost_available": os.path.exists(boost_path)
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
        except Exception as e:
            decky.logger.error(f"Failed to read CPU settings: {e}")
        
        return result

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
