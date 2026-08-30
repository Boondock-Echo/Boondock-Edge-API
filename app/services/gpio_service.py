"""
GPIO Service - Direct GPIO control for LED and Relay management.
This service integrates GPIO functionality directly into the Flask application.
"""
import json
import threading
import time
import random
import math
import logging
from config import Config
from enum import Enum
from typing import Dict, Optional
from collections import deque
from datetime import datetime

try:
    from gpiozero import LED, OutputDevice, PWMLED
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    # Create mock classes for development on non-Raspberry Pi systems
    class PWMLED:
        def __init__(self, pin, active_high=True):
            self.pin = pin
            self._value = 0
        def __getattr__(self, name):
            return lambda *args, **kwargs: None
        @property
        def value(self):
            return self._value
        @value.setter
        def value(self, val):
            self._value = val
        def close(self):
            pass
    
    class OutputDevice:
        def __init__(self, pin):
            self.pin = pin
            self._value = False
        def on(self):
            self._value = True
        def off(self):
            self._value = False
        def toggle(self):
            self._value = not self._value
        @property
        def value(self):
            return self._value
        def close(self):
            pass

logger = logging.getLogger(__name__)

# ------------------------------
# Configuration Management
# ------------------------------
# Config file path relative to backend directory
CONFIG_FILE = Config.get_db_dir() / "gpio_config.json"
DEFAULT_CONFIG = {
    "led": {
        "gpio": 13,
        "enabled": True,
        "mode": "source"  # "source" = active_low (False), "sink" = active_high (True)
    },
    "relays": {
        "relay1": {
            "gpio": 19,
            "state": False,
            "normal_state": "off"  # "on" or "off" - default state when initialized
        },
        "relay2": {
            "gpio": 21,
            "state": False,
            "normal_state": "off"
        }
    }
}


class LEDState(str, Enum):
    """LED states"""
    off = "off"                    # LED is off
    on = "on"                      # LED is permanently on
    online = "online"              # LED on for 100ms, off for 5 seconds
    receiving = "receiving"         # LED blinks randomly simulating data receiving
    error = "error"                # LED off 2s, blinks twice 500ms, then error code blinks
    breathing = "breathing"         # LED breathing effect using PWM (1.5s fade in, 1.5s fade out)


class GPIOService:
    """GPIO Service for managing LED patterns and relays."""
    
    def __init__(self):
        """Initialize the GPIO service."""
        self.config_file = CONFIG_FILE
        self.config = self._load_config()
        self.led_gpio = self.config["led"]["gpio"]
        self.led_enabled = self.config["led"]["enabled"]
        self.led = None
        self.relays: Dict[str, OutputDevice] = {}
        
        # State Management
        self.current_state = LEDState.off
        self.stop_flag = False
        self.pattern_thread = None
        self.last_command_time = None
        self.TIMEOUT_SECONDS = 60
        self.timeout_monitor_thread = None
        self.error_code_value = 1  # Default error code (1-5)
        self.receiving_start_time: Optional[float] = None
        self.pending_state: Optional[LEDState] = None
        self.MIN_RECEIVING_DURATION = 2.0  # Minimum 2 seconds for Receiving state
        
        # Pattern state tracking
        self.pattern_lock = threading.Lock()
        self.pattern_runner_active = False
        self._pattern_state = {
            'last_state': None,
            'next_action_time': 0.0,
            'led_state': False,
            'online_phase': False,  # True=on, False=off
            'error_phase': 0,  # 0=off 2s, 1=blink twice, 1.5=500ms gap, 2=error code
            'error_flash_count': 0,
            'error_code_count': 0,
            'breathing_start_time': 0.0,
        }
        
        # History Tracking
        self.api_call_history = deque(maxlen=20)
        self.status_change_history = deque(maxlen=100)
        
        # Initialize hardware if GPIO is available
        if GPIO_AVAILABLE:
            self._initialize_hardware()
        else:
            logger.warning("GPIO libraries not available. Running in mock mode.")
    
    def _load_config(self):
        """Load configuration from database, create default if not exists"""
        try:
            from .settings_manager import get_settings_manager
            settings_manager = get_settings_manager()
            config = settings_manager.get_gpio_config()
            if config:
                merged_config = DEFAULT_CONFIG.copy()
                merged_config.update(config)
                if "led" not in merged_config:
                    merged_config["led"] = DEFAULT_CONFIG["led"]
                if "relays" not in merged_config:
                    merged_config["relays"] = DEFAULT_CONFIG["relays"]
                # Ensure LED mode is present and valid
                if "mode" not in merged_config["led"]:
                    merged_config["led"]["mode"] = DEFAULT_CONFIG["led"]["mode"]
                return merged_config
        except Exception as e:
            logger.warning(f"Error loading GPIO config from database: {e}, using defaults")
        
        # Fallback to file if database doesn't have config
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    merged_config = DEFAULT_CONFIG.copy()
                    merged_config.update(config)
                    if "led" not in merged_config:
                        merged_config["led"] = DEFAULT_CONFIG["led"]
                    if "relays" not in merged_config:
                        merged_config["relays"] = DEFAULT_CONFIG["relays"]
                    # Ensure LED mode is present and valid
                    if "mode" not in merged_config["led"]:
                        merged_config["led"]["mode"] = DEFAULT_CONFIG["led"]["mode"]
                    if merged_config["led"]["mode"] not in ["source", "sink"]:
                        merged_config["led"]["mode"] = DEFAULT_CONFIG["led"]["mode"]
                    return merged_config
            except Exception as e:
                logger.error(f"Error loading config: {e}, using defaults")
                return DEFAULT_CONFIG.copy()
        else:
            self._save_config(DEFAULT_CONFIG.copy())
            return DEFAULT_CONFIG.copy()
    
    def _save_config(self, config: dict):
        """Save configuration to database"""
        try:
            from .settings_manager import get_settings_manager
            settings_manager = get_settings_manager()
            settings_manager.save_gpio_config(config)
            # Also save to file for backward compatibility during transition
            try:
                with open(self.config_file, 'w') as f:
                    json.dump(config, f, indent=2)
            except:
                pass  # File save is optional
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            # Fallback to file if database save fails
            try:
                with open(self.config_file, 'w') as f:
                    json.dump(config, f, indent=2)
            except Exception as file_error:
                logger.error(f"Error saving config to file: {file_error}")
    
    def _update_config(self, key_path: list, value):
        """Update a nested config value and save"""
        config = self._load_config()
        current = config
        for key in key_path[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]
        current[key_path[-1]] = value
        self._save_config(config)
        self.config = config
        return config
    
    def _initialize_hardware(self):
        """Initialize LED and relays from config"""
        self._reload_led()
        self._load_relays_from_config()
    
    def _reload_led(self):
        """Reload LED with current GPIO from config"""
        config = self._load_config()
        self.led_gpio = config["led"]["gpio"]
        self.led_enabled = config["led"]["enabled"]
        led_mode = config["led"].get("mode", "source")
        # Inverted logic: "source" = active_low (False), "sink" = active_high (True)
        active_high = True if led_mode == "sink" else False
        
        if self.led:
            try:
                self.led.close()
            except:
                pass
        
        # Use PWMLED for PWM support, honoring source/sink configuration
        if GPIO_AVAILABLE:
            self.led = PWMLED(self.led_gpio, active_high=active_high)
        else:
            self.led = PWMLED(self.led_gpio)
        
        if self.led_enabled:
            self.led.value = 0  # Set brightness to 0
    
    def _load_relays_from_config(self):
        """Load all relays from config and initialize them"""
        config = self._load_config()
        for name, relay_config in config["relays"].items():
            gpio = relay_config["gpio"]
            
            if GPIO_AVAILABLE:
                try:
                    # Try to create the OutputDevice
                    self.relays[name] = OutputDevice(gpio)
                except ValueError as e:
                    # Pin already in use - try to clean it up
                    if "already in use" in str(e).lower():
                        logger.warning(f"GPIO{gpio} ({name}) already in use, attempting cleanup...")
                        try:
                            # Force cleanup by accessing the pin directly
                            import gpiozero
                            if hasattr(gpiozero, 'LED'):
                                # Try to close any existing devices on this pin
                                temp_device = gpiozero.LED(gpio)
                                temp_device.close()
                                time.sleep(0.1)
                            # Try again
                            self.relays[name] = OutputDevice(gpio)
                            logger.info(f"GPIO{gpio} ({name}) successfully recovered after cleanup")
                        except Exception as recovery_error:
                            logger.error(f"Failed to recover GPIO{gpio} ({name}): {recovery_error}")
                            # Continue without this relay
                            self.relays[name] = OutputDevice(gpio)  # Use mock
                    else:
                        raise
            else:
                self.relays[name] = OutputDevice(gpio)
            
            # Use normal_state if state is not explicitly set, otherwise use saved state
            normal_state = relay_config.get("normal_state", "off")
            if "state" in relay_config:
                state = relay_config.get("state", False)
            else:
                state = normal_state == "on"
            
            try:
                if state:
                    self.relays[name].on()
                else:
                    self.relays[name].off()
            except Exception as e:
                logger.warning(f"Failed to set relay {name} state: {e}")
            
            # Ensure normal_state is set in config if missing
            if "normal_state" not in relay_config:
                relay_config["normal_state"] = "off"
                config["relays"][name] = relay_config
                self._save_config(config)
    
    def start(self):
        """Start the GPIO service (initialize hardware and start threads)"""
        if not GPIO_AVAILABLE:
            logger.warning("GPIO service started in mock mode (GPIO libraries not available)")
            return
        
        config = self._load_config()
        self.led_enabled = config["led"]["enabled"]
        
        try:
            # Reload LED with correct mode (source/sink) from config
            self._reload_led()
        except Exception as e:
            logger.warning(f"Failed to initialize LED: {e}")
            self.led = None
            self.led_enabled = False
        
        try:
            # Load relays - this may fail if GPIO pins are already in use
            self._load_relays_from_config()
        except Exception as e:
            logger.warning(f"Failed to load relays: {e}")
            # Continue anyway - relay failures shouldn't prevent app startup
        
        # Startup sequence: Flash LED quickly three times (250ms on, 250ms off), then turn off
        if self.led_enabled and self.led:
            try:
                for _ in range(3):
                    self.led.value = 1.0
                    time.sleep(0.25)
                    self.led.value = 0
                    time.sleep(0.25)
                self.led.value = 0
            except Exception as e:
                logger.warning(f"LED flash sequence failed: {e}")
        
        # Initialize state to off
        self.current_state = LEDState.off
        
        # Start timeout monitor thread
        self.stop_flag = False
        self.timeout_monitor_thread = threading.Thread(target=self._timeout_monitor, daemon=True)
        self.timeout_monitor_thread.start()
        
        # Start pattern runner thread
        self.pattern_runner_active = True
        self.pattern_thread = threading.Thread(target=self._pattern_runner, daemon=True)
        self.pattern_thread.start()
        
        logger.info("GPIO service started successfully")
    
    def stop(self):
        """Stop the GPIO service (cleanup threads and hardware)"""
        self.stop_flag = True
        self.pattern_runner_active = False
        
        if self.pattern_thread and self.pattern_thread.is_alive():
            self.pattern_thread.join(timeout=2)
        if self.timeout_monitor_thread and self.timeout_monitor_thread.is_alive():
            self.timeout_monitor_thread.join(timeout=2)
        
        if self.led_enabled and self.led:
            self.led.value = 0
        
        # Turn off all relays
        for relay in self.relays.values():
            try:
                relay.off()
                relay.close()
            except:
                pass
        
        logger.info("GPIO service stopped")
    
    def _timeout_monitor(self):
        """Monitor for timeout and set state to off if no command received for 60 seconds"""
        while not self.stop_flag:
            if self.last_command_time is not None:
                elapsed = time.time() - self.last_command_time
                if elapsed >= self.TIMEOUT_SECONDS:
                    logger.debug(f"No commands received for {elapsed:.1f} seconds. Setting state to off.")
                    
                    old_state = self.current_state
                    with self.pattern_lock:
                        self.current_state = LEDState.off
                    
                    self._log_status_change(old_state, LEDState.off, "automated")
                    self.last_command_time = None
            
            time.sleep(1)
    
    def _pattern_runner(self):
        """Timer-based pattern runner"""
        while not self.stop_flag:
            with self.pattern_lock:
                current_time = time.time()
                state = self.current_state
                
                # Check if we're in Receiving state and minimum duration has elapsed
                if state == LEDState.receiving and self.receiving_start_time is not None:
                    elapsed = time.time() - self.receiving_start_time
                    if elapsed >= self.MIN_RECEIVING_DURATION and self.pending_state is not None:
                        # Minimum duration elapsed, switch to pending state
                        new_state = self.pending_state
                        self.pending_state = None
                        self.current_state = new_state
                        state = new_state
                        if new_state == LEDState.receiving:
                            self.receiving_start_time = time.time()
                        else:
                            self.receiving_start_time = None
                        self.last_command_time = time.time()
                        self._log_status_change(LEDState.receiving, new_state, "api")
                
                # Reset pattern state if state changed
                if state != self._pattern_state.get('last_state'):
                    self._pattern_state['last_state'] = state
                    self._pattern_state['next_action_time'] = current_time
                    self._pattern_state['led_state'] = False
                    self._pattern_state['online_phase'] = False
                    self._pattern_state['error_phase'] = 0
                    self._pattern_state['error_flash_count'] = 0
                    self._pattern_state['error_code_count'] = 0
                    self._pattern_state['breathing_start_time'] = current_time
                    if self.led_enabled and self.led:
                        self.led.value = 0
                
                if not self.led_enabled or not self.led:
                    time.sleep(0.1)
                    continue
                
                # Execute pattern based on state
                if state == LEDState.off:
                    if self._pattern_state['led_state'] or self.led.value > 0:
                        self.led.value = 0
                        self._pattern_state['led_state'] = False
                    self._pattern_state['next_action_time'] = current_time + 1000.0
                
                elif state == LEDState.on:
                    if not self._pattern_state['led_state'] or self.led.value < 1.0:
                        self.led.value = 1.0
                        self._pattern_state['led_state'] = True
                    self._pattern_state['next_action_time'] = current_time + 1000.0
                
                elif state == LEDState.breathing:
                    BREATHING_CYCLE_TIME = 3.0
                    cycle_start = self._pattern_state['breathing_start_time']
                    elapsed_in_cycle = (current_time - cycle_start) % BREATHING_CYCLE_TIME
                    phase = (elapsed_in_cycle / BREATHING_CYCLE_TIME) * 2 * math.pi
                    brightness = (math.sin(phase) + 1.0) / 2.0
                    brightness = max(0.0, min(1.0, brightness))
                    self.led.value = brightness
                    self._pattern_state['led_state'] = brightness > 0.01
                    self._pattern_state['next_action_time'] = current_time + 0.01
                
                elif state == LEDState.online:
                    if current_time >= self._pattern_state['next_action_time']:
                        if self._pattern_state['online_phase']:
                            self.led.value = 0
                            self._pattern_state['led_state'] = False
                            self._pattern_state['online_phase'] = False
                            self._pattern_state['next_action_time'] = current_time + 5.0
                        else:
                            self.led.value = 1.0
                            self._pattern_state['led_state'] = True
                            self._pattern_state['online_phase'] = True
                            self._pattern_state['next_action_time'] = current_time + 0.1
                
                elif state == LEDState.receiving:
                    if current_time >= self._pattern_state['next_action_time']:
                        if self._pattern_state['led_state']:
                            self.led.value = 0
                            self._pattern_state['led_state'] = False
                            off_time = random.uniform(0.02, 0.1)
                            self._pattern_state['next_action_time'] = current_time + off_time
                        else:
                            self.led.value = 1.0
                            self._pattern_state['led_state'] = True
                            on_time = random.uniform(0.05, 0.15)
                            self._pattern_state['next_action_time'] = current_time + on_time
                
                elif state == LEDState.error:
                    code = max(1, min(5, self.error_code_value))
                    
                    if current_time >= self._pattern_state['next_action_time']:
                        if self._pattern_state['error_phase'] == 0:
                            self.led.value = 0
                            self._pattern_state['led_state'] = False
                            self._pattern_state['error_phase'] = 1
                            self._pattern_state['error_flash_count'] = 0
                            self._pattern_state['next_action_time'] = current_time + 2.0
                        
                        elif self._pattern_state['error_phase'] == 1:
                            if self._pattern_state['error_flash_count'] < 4:
                                if self._pattern_state['led_state']:
                                    self.led.value = 0
                                    self._pattern_state['led_state'] = False
                                    self._pattern_state['error_flash_count'] += 1
                                    self._pattern_state['next_action_time'] = current_time + 0.5
                                else:
                                    self.led.value = 1.0
                                    self._pattern_state['led_state'] = True
                                    self._pattern_state['error_flash_count'] += 1
                                    self._pattern_state['next_action_time'] = current_time + 0.5
                            else:
                                self._pattern_state['error_phase'] = 1.5
                                self.led.value = 0
                                self._pattern_state['led_state'] = False
                                self._pattern_state['next_action_time'] = current_time + 0.5
                        
                        elif self._pattern_state['error_phase'] == 1.5:
                            self._pattern_state['error_phase'] = 2
                            self._pattern_state['error_code_count'] = 0
                            self._pattern_state['next_action_time'] = current_time
                        
                        elif self._pattern_state['error_phase'] == 2:
                            if self._pattern_state['error_code_count'] < code:
                                if self._pattern_state['led_state']:
                                    self.led.value = 0
                                    self._pattern_state['led_state'] = False
                                    self._pattern_state['next_action_time'] = current_time + 0.2
                                else:
                                    self.led.value = 1.0
                                    self._pattern_state['led_state'] = True
                                    self._pattern_state['error_code_count'] += 1
                                    self._pattern_state['next_action_time'] = current_time + 0.3
                            else:
                                self._pattern_state['error_phase'] = 0
                                self._pattern_state['next_action_time'] = current_time
            
            # Small sleep to avoid busy waiting
            sleep_time = max(0.001, min(0.01, self._pattern_state['next_action_time'] - current_time))
            time.sleep(sleep_time)
    
    def set_led_state(self, state: LEDState, error_code: Optional[int] = None):
        """Set LED state"""
        old_state = self.current_state
        
        # Check if we're currently in Receiving state and need to wait for minimum duration
        with self.pattern_lock:
            if self.current_state == LEDState.receiving and self.receiving_start_time is not None:
                elapsed = time.time() - self.receiving_start_time
                if elapsed < self.MIN_RECEIVING_DURATION:
                    self.pending_state = state
                    self.last_command_time = time.time()
                    logger.debug(f"Receiving state active for {elapsed:.2f}s, queueing {state} state")
                    return {"status": "ok", "state": str(state), "queued": True}
        
        # If we're switching TO Receiving state, record the start time
        if state == LEDState.receiving:
            self.receiving_start_time = time.time()
            self.pending_state = None
        else:
            self.receiving_start_time = None
            self.pending_state = None
        
        # Update error code if provided and state is error
        if state == LEDState.error and error_code is not None:
            self.error_code_value = error_code
        
        with self.pattern_lock:
            self.current_state = state
        
        self.last_command_time = time.time()
        
        if old_state != state:
            self._log_status_change(old_state, state, "api")
        
        # Start pattern runner if not already running
        if not self.pattern_runner_active:
            self.pattern_runner_active = True
            self.pattern_thread = threading.Thread(target=self._pattern_runner, daemon=True)
            self.pattern_thread.start()
        
        return {"status": "ok", "state": str(state), "error_code": self.error_code_value if state == LEDState.error else None}
    
    def get_led_state(self):
        """Get current LED state"""
        return {"state": str(self.current_state)}
    
    def _log_status_change(self, old_state: LEDState, new_state: LEDState, source: str = "api"):
        """Log a status change"""
        self.status_change_history.append({
            "timestamp": datetime.now().isoformat(),
            "type": "state_change",
            "source": source,
            "old_state": str(old_state),
            "new_state": str(new_state)
        })
    
    # LED Configuration Methods
    def set_led_gpio(self, gpio: int):
        """Set LED GPIO pin and persist to config"""
        if gpio < 1 or gpio > 40:
            raise ValueError("GPIO pin must be between 1 and 40")
        self._update_config(["led", "gpio"], gpio)
        self._reload_led()
        return {"status": "ok", "gpio": gpio}
    
    def get_led_gpio(self):
        """Get current LED GPIO pin"""
        config = self._load_config()
        return {"gpio": config["led"]["gpio"]}
    
    def set_led_mode(self, mode: str):
        """Set LED mode to 'source' or 'sink' (persistent)"""
        if mode not in ["source", "sink"]:
            raise ValueError("Mode must be 'source' or 'sink'")
        self._update_config(["led", "mode"], mode)
        self._reload_led()
        return {"status": "ok", "mode": mode}
    
    def get_led_mode(self):
        """Get current LED mode ('source' or 'sink')"""
        config = self._load_config()
        return {"mode": config["led"].get("mode", "source")}
    
    def set_led_enabled(self, enabled: bool):
        """Enable or disable LED"""
        self._update_config(["led", "enabled"], enabled)
        self.led_enabled = enabled
        
        if not enabled:
            with self.pattern_lock:
                self.current_state = LEDState.off
            if self.led:
                self.led.value = 0
        
        return {"status": "ok", "enabled": enabled}
    
    def get_led_enabled(self):
        """Get LED enabled status"""
        config = self._load_config()
        return {"enabled": config["led"]["enabled"]}
    
    # Relay Management Methods
    def add_relay(self, name: str, gpio: int, normal_state: str = "off"):
        """Add a new relay"""
        if gpio < 1 or gpio > 40:
            raise ValueError("GPIO pin must be between 1 and 40")
        
        if normal_state not in ["on", "off"]:
            raise ValueError("normal_state must be 'on' or 'off'")
        
        config = self._load_config()
        
        if name in config["relays"]:
            raise ValueError(f"Relay '{name}' already exists")
        
        initial_state = normal_state == "on"
        config["relays"][name] = {
            "gpio": gpio,
            "state": initial_state,
            "normal_state": normal_state
        }
        self._save_config(config)
        
        if GPIO_AVAILABLE:
            self.relays[name] = OutputDevice(gpio)
        else:
            self.relays[name] = OutputDevice(gpio)
        
        if initial_state:
            self.relays[name].on()
        else:
            self.relays[name].off()
        
        return {"status": "ok", "relay": config["relays"][name]}
    
    def remove_relay(self, name: str):
        """Remove a relay"""
        config = self._load_config()
        
        if name not in config["relays"]:
            raise ValueError(f"Relay '{name}' does not exist")
        
        if name in self.relays:
            try:
                self.relays[name].off()
                self.relays[name].close()
            except:
                pass
            del self.relays[name]
        
        del config["relays"][name]
        self._save_config(config)
        
        return {"status": "removed", "name": name}
    
    def get_relay(self, name: str):
        """Get relay information"""
        config = self._load_config()
        
        if name not in config["relays"]:
            raise ValueError(f"Relay '{name}' does not exist")
        
        relay_config = config["relays"][name].copy()
        
        if "normal_state" not in relay_config:
            relay_config["normal_state"] = "off"
        
        if name not in self.relays:
            gpio = relay_config["gpio"]
            if GPIO_AVAILABLE:
                self.relays[name] = OutputDevice(gpio)
            else:
                self.relays[name] = OutputDevice(gpio)
            
            if "state" in relay_config:
                saved_state = relay_config.get("state", False)
            else:
                saved_state = relay_config["normal_state"] == "on"
            
            if saved_state:
                self.relays[name].on()
            else:
                self.relays[name].off()
        
        relay_config["state"] = bool(self.relays[name].value)
        
        return {"relay": relay_config}
    
    def get_all_relays(self):
        """Get all relays"""
        config = self._load_config()
        result = {}
        for name, relay_config in config["relays"].items():
            result[name] = relay_config.copy()
            
            if "normal_state" not in result[name]:
                result[name]["normal_state"] = "off"
            
            if name not in self.relays:
                gpio = relay_config["gpio"]
                if GPIO_AVAILABLE:
                    self.relays[name] = OutputDevice(gpio)
                else:
                    self.relays[name] = OutputDevice(gpio)
                
                if "state" in relay_config:
                    saved_state = relay_config.get("state", False)
                else:
                    saved_state = result[name]["normal_state"] == "on"
                
                if saved_state:
                    self.relays[name].on()
                else:
                    self.relays[name].off()
            
            result[name]["state"] = bool(self.relays[name].value)
        
        return {"relays": result}
    
    def get_relay_normal_state(self, name: str):
        """Get relay normal state ('on' or 'off')"""
        config = self._load_config()
        
        if name not in config["relays"]:
            raise ValueError(f"Relay '{name}' does not exist")
        
        return {"normal_state": config["relays"][name].get("normal_state", "off")}
    
    def set_relay_normal_state(self, name: str, normal_state: str):
        """Set relay normal state ('on' or 'off')"""
        if normal_state not in ["on", "off"]:
            raise ValueError("normal_state must be 'on' or 'off'")
        
        config = self._load_config()
        
        if name not in config["relays"]:
            raise ValueError(f"Relay '{name}' does not exist")
        
        config["relays"][name]["normal_state"] = normal_state
        self._save_config(config)
        
        return {"status": "ok", "name": name, "normal_state": normal_state}
    
    def update_relay_gpio(self, name: str, gpio: int):
        """Update relay GPIO pin"""
        if gpio < 1 or gpio > 40:
            raise ValueError("GPIO pin must be between 1 and 40")
        
        config = self._load_config()
        
        if name not in config["relays"]:
            raise ValueError(f"Relay '{name}' does not exist")
        
        if name in self.relays:
            try:
                self.relays[name].off()
                self.relays[name].close()
            except:
                pass
            del self.relays[name]
        
        config["relays"][name]["gpio"] = gpio
        
        if GPIO_AVAILABLE:
            self.relays[name] = OutputDevice(gpio)
        else:
            self.relays[name] = OutputDevice(gpio)
        
        relay_config = config["relays"][name]
        if "state" in relay_config:
            saved_state = relay_config.get("state", False)
        else:
            normal_state = relay_config.get("normal_state", "off")
            saved_state = normal_state == "on"
        
        if saved_state:
            self.relays[name].on()
        else:
            self.relays[name].off()
        
        self._save_config(config)
        
        return {"status": "ok", "relay": config["relays"][name]}
    
    def control_relay(self, name: str, action: str):
        """Control relay: 'on', 'off', or 'toggle'"""
        config = self._load_config()
        
        if name not in config["relays"]:
            raise ValueError(f"Relay '{name}' does not exist")
        
        if name not in self.relays:
            gpio = config["relays"][name]["gpio"]
            if GPIO_AVAILABLE:
                self.relays[name] = OutputDevice(gpio)
            else:
                self.relays[name] = OutputDevice(gpio)
        
        relay = self.relays[name]
        
        if action == "on":
            relay.on()
            state = True
        elif action == "off":
            relay.off()
            state = False
        elif action == "toggle":
            relay.toggle()
            state = bool(relay.value)
        else:
            raise ValueError("Action must be 'on', 'off', or 'toggle'")
        
        config["relays"][name]["state"] = state
        self._save_config(config)
        
        return {"status": "ok", "name": name, "action": action, "state": state}
    
    def get_history(self):
        """Get API call and status change history"""
        return {
            "api_calls": list(self.api_call_history),
            "status_changes": list(self.status_change_history),
            "summary": {
                "total_api_calls": len(self.api_call_history),
                "total_status_changes": len(self.status_change_history)
            }
        }


# Singleton instance
_gpio_service = None


def get_gpio_service() -> GPIOService:
    """Get the singleton GPIO service instance."""
    global _gpio_service
    if _gpio_service is None:
        _gpio_service = GPIOService()
    return _gpio_service
