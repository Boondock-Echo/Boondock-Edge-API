"""
LED Status Service - Manages LED status indicators based on application lifecycle.
All LED operations are handled directly through the integrated GPIO service.
Uses the state-based API: states (off, on, online, receiving, error, breathing).
"""
import logging
import threading
import time
from typing import Optional
from app.services.gpio_service import get_gpio_service, LEDState

logger = logging.getLogger(__name__)

# Retry configuration
RETRY_INTERVAL = 5  # seconds between retries
MAX_RETRIES = 12  # Maximum retries (1 minute total)


class LEDStatusService:
    """Service to manage LED status indicators."""
    
    def __init__(self):
        self.is_connected = False
        self.is_ready = False
        self.active_thread = None
        self.stop_active_thread = False
        self.inactivity_monitor_thread = None
        self.stop_inactivity_monitor = False
        self.lock = threading.Lock()
        self.previous_state = 'online'  # Track previous state to return to after receiving
    
    def _set_state(self, state: LEDState, retry: bool = False) -> bool:
        """
        Set LED state. Never blocks - fails gracefully.
        
        Args:
            state: LEDState enum value
            retry: Not used (kept for API compatibility, but retries are non-blocking)
        
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            gpio_service = get_gpio_service()
            result = gpio_service.set_led_state(state)
            
            if result.get('status') == 'ok':
                self.is_connected = True
                logger.info(f"LED state set to '{state}' successfully")
                return True
            else:
                self.is_connected = False
                logger.debug(f"Failed to set LED state '{state}'. Application continues normally.")
                return False
        except Exception as e:
            logger.debug(f"Exception setting LED state '{state}': {e}. Application continues normally.")
            self.is_connected = False
            return False
    
    def _set_state_with_params(self, state: LEDState, params: dict = None, retry: bool = False) -> bool:
        """
        Set LED state with optional parameters. Never blocks - fails gracefully.
        
        Args:
            state: LEDState enum value
            params: Optional parameters (e.g., {'error_code': 1})
            retry: Not used (kept for API compatibility, but retries are non-blocking)
        
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            gpio_service = get_gpio_service()
            error_code = params.get('error_code') if params else None
            result = gpio_service.set_led_state(state, error_code=error_code)
            
            if result.get('status') == 'ok':
                self.is_connected = True
                logger.debug(f"LED state set to '{state}' successfully")
                return True
            elif result.get('status') == 'error':
                self.is_connected = True  # Service is available, just LED is disabled
                logger.debug(f"LED state '{state}' not set: {result.get('message', 'LED processing is disabled')}")
                return False
            else:
                self.is_connected = False
                logger.debug(f"Failed to set LED state '{state}'. Application continues normally.")
                return False
        except Exception as e:
            logger.debug(f"Exception setting LED state '{state}': {e}. Application continues normally.")
            self.is_connected = False
            return False
    
    def _retry_busy_in_background(self):
        """
        Background thread to retry setting busy state without blocking the application.
        """
        logger.info("Starting background retry for LED busy state...")
        for attempt in range(MAX_RETRIES):
            if self._set_state_with_params(LEDState.breathing, retry=False):
                logger.info("LED busy state set successfully (background retry)")
                return
            
            if attempt < MAX_RETRIES - 1:
                logger.debug(f"Retrying LED busy state in background (attempt {attempt + 1}/{MAX_RETRIES})...")
                time.sleep(RETRY_INTERVAL)
        
        logger.warning(f"Failed to set LED busy state after {MAX_RETRIES} background attempts. Application will continue without LED indicators.")
    
    def set_busy(self, retry_on_failure: bool = True) -> bool:
        """
        Set LED to busy state (breathing pattern) when app is starting.
        If retry_on_failure is True, retries in background thread without blocking.
        
        Args:
            retry_on_failure: Whether to retry on failure (in background thread)
        
        Returns:
            bool: True if successful immediately, False otherwise (retries continue in background)
        """
        logger.info("Setting LED to busy state (breathing pattern)...")
        
        # Try once immediately
        if self._set_state_with_params(LEDState.breathing, retry=False):
            return True
        
        # If failed and retry requested, start background retry thread (non-blocking)
        if retry_on_failure:
            retry_thread = threading.Thread(target=self._retry_busy_in_background, daemon=True)
            retry_thread.start()
            logger.info("LED busy state retry started in background thread (non-blocking)")
            # Return False immediately so application doesn't block
            return False
        
        return False
    
    def set_online(self) -> bool:
        """
        Set LED to online state (1 second on, 1 second off) when app is ready and connected to internet.
        
        Returns:
            bool: True if successful
        """
        logger.info("Setting LED to online state...")
        success = self._set_state(LEDState.online, retry=False)
        if success:
            self.is_ready = True
        return success
    
    def set_receiving(self) -> bool:
        """
        Set LED to receiving state (random flashing pattern) when receiving audio upload.
        
        Returns:
            bool: True if successful
        """
        logger.debug("Setting LED to receiving state (receiving audio upload)...")
        # Save current state before switching to receiving
        with self.lock:
            try:
                gpio_service = get_gpio_service()
                data = gpio_service.get_led_state()
                current_state = data.get('state')
                if current_state and current_state != 'receiving':
                    self.previous_state = current_state
            except Exception as e:
                logger.debug(f"Failed to get current LED state: {e}")
        return self._set_state_with_params(LEDState.receiving, retry=False)
    
    def stop_receiving(self) -> bool:
        """
        Stop receiving state and return to previous state (typically online).
        
        Returns:
            bool: True if successful
        """
        logger.debug("Stopping LED receiving state, returning to previous state...")
        # Return to previous state (default to 'online' if not set)
        state_str = self.previous_state if self.previous_state else 'online'
        try:
            state_to_restore = LEDState(state_str)
            return self._set_state_with_params(state_to_restore, retry=False)
        except ValueError:
            # If invalid state, default to online
            return self._set_state_with_params(LEDState.online, retry=False)
    
    def set_error(self, error_code: int = 1) -> bool:
        """
        Set LED to error state for critical errors.
        
        Args:
            error_code: Error code (1-5)
        
        Returns:
            bool: True if successful
        """
        logger.warning(f"Setting LED to error state (error code: {error_code})...")
        return self._set_state_with_params(LEDState.error, {'error_code': error_code}, retry=False)
    
    def set_off(self) -> bool:
        """
        Set LED to off state.
        
        Returns:
            bool: True if successful
        """
        logger.info("Setting LED to off state...")
        return self._set_state(LEDState.off, retry=False)
    
    # Legacy method names for backward compatibility
    def send_starting(self, retry_on_failure: bool = True) -> bool:
        """Legacy: Alias for set_busy"""
        return self.set_busy(retry_on_failure)
    
    def send_startup(self, retry_on_failure: bool = True) -> bool:
        """Legacy: Alias for set_busy"""
        return self.set_busy(retry_on_failure)
    
    def send_normal(self) -> bool:
        """Legacy: Alias for set_online"""
        return self.set_online()
    
    def send_ready(self) -> bool:
        """Legacy: Alias for set_online"""
        return self.set_online()
    
    def send_receiving_audio(self) -> bool:
        """Legacy: Alias for set_receiving"""
        return self.set_receiving()
    
    def send_processing(self) -> bool:
        """Legacy: Alias for set_receiving"""
        return self.set_receiving()
    
    def send_error(self) -> bool:
        """Legacy: Alias for set_error"""
        return self.set_error()
    
    def send_shutdown(self) -> bool:
        """Legacy: Alias for set_off"""
        return self.set_off()
    
    def start_active_heartbeat(self):
        """
        No-op: Online state is persistent, no heartbeat needed.
        """
        logger.info("LED status: App is active (online state is persistent)")
    
    def stop_active_heartbeat(self):
        """
        No-op: Online state is persistent.
        """
        logger.info("LED active heartbeat stopped (no-op)")
    
    def check_connection(self) -> bool:
        """
        Check if GPIO service is available.
        
        Returns:
            bool: True if connected, False otherwise
        """
        try:
            gpio_service = get_gpio_service()
            data = gpio_service.get_led_state()
            self.is_connected = True
            return True
        except Exception as e:
            logger.debug(f"GPIO service check failed: {e}")
            self.is_connected = False
            return False
    
    def _inactivity_monitor(self):
        """
        Background thread that monitors for inactivity and ensures online state.
        When application is running but there is no activity, switch to online state.
        """
        INACTIVITY_CHECK_INTERVAL = 30  # Check every 30 seconds
        logger.info("LED inactivity monitor started")
        
        while not self.stop_inactivity_monitor:
            try:
                # Get current state
                gpio_service = get_gpio_service()
                data = gpio_service.get_led_state()
                
                if data:
                    current_state = data.get('state')
                    
                    # If state is not online (and not receiving), set it to online
                    # This ensures that when there's no activity, we're in online state
                    if current_state and current_state not in ['online', 'receiving']:
                        logger.debug(f"No activity detected, ensuring online state (current state: {current_state})")
                        self._set_state(LEDState.online, retry=False)
                
            except Exception as e:
                logger.debug(f"Error in inactivity monitor: {e}")
            
            # Wait before next check
            time.sleep(INACTIVITY_CHECK_INTERVAL)
        
        logger.info("LED inactivity monitor stopped")
    
    def start_inactivity_monitor(self):
        """
        Start the inactivity monitor thread that ensures online state when there's no activity.
        """
        if self.inactivity_monitor_thread is None or not self.inactivity_monitor_thread.is_alive():
            self.stop_inactivity_monitor = False
            self.inactivity_monitor_thread = threading.Thread(target=self._inactivity_monitor, daemon=True)
            self.inactivity_monitor_thread.start()
            logger.info("LED inactivity monitor thread started")
    
    def stop_inactivity_monitor_thread(self):
        """
        Stop the inactivity monitor thread.
        """
        self.stop_inactivity_monitor = True
        if self.inactivity_monitor_thread and self.inactivity_monitor_thread.is_alive():
            self.inactivity_monitor_thread.join(timeout=5)
        logger.info("LED inactivity monitor thread stopped")


# Singleton instance
_led_status_service = None


def get_led_status_service() -> LEDStatusService:
    """Get the singleton LED status service instance."""
    global _led_status_service
    if _led_status_service is None:
        _led_status_service = LEDStatusService()
    return _led_status_service

