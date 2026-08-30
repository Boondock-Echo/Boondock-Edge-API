"""
Notification Service

Manages system notifications, warnings, and errors that need to be displayed
to users in the dashboard. Supports temporary, sticky, and stacked notification types.
"""

import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from collections import deque
from enum import Enum

from app.services.system_health_monitor import get_current_metrics
from app.services.device_health_monitor import get_current_health_stats

# Notification types
class NotificationType(Enum):
    SYSTEM_ERROR = "system_error"
    DEVICE_DISCONNECTION = "device_disconnection"
    WARNING = "warning"
    INFO = "info"

# Notification display modes
class NotificationMode(Enum):
    TEMPORARY = "temporary"  # Auto-dismiss after 10 seconds
    STICKY = "sticky"  # Stay until user clears
    STACKED = "stacked"  # Stack multiple, clear one at a time

# In-memory notification storage
_notifications = {}  # id -> notification dict
_notification_queue = deque()  # For stacked notifications
_notification_lock = threading.RLock()

# Track conditions to avoid duplicate notifications
_cpu_high_notification_sent = False
_cpu_high_start_time = None
_disk_high_notification_sent = False
_device_disconnection_notifications = set()  # Set of MAC addresses
_channel_creation_notifications = set()  # Set of channel IDs/MACs

# Notification ID counter
_notification_id_counter = 0


def _generate_notification_id() -> str:
    """Generate a unique notification ID."""
    global _notification_id_counter
    with _notification_lock:
        _notification_id_counter += 1
        return f"notif_{_notification_id_counter}_{int(time.time())}"


def _check_cpu_usage():
    """Check if CPU usage is > 90% for more than 5 minutes."""
    global _cpu_high_notification_sent, _cpu_high_start_time
    
    try:
        metrics = get_current_metrics()
        if not metrics:
            return
        
        cpu_percent = metrics.get('cpu_percent', 0)
        current_time = datetime.now()
        
        if cpu_percent > 90:
            if _cpu_high_start_time is None:
                _cpu_high_start_time = current_time
            elif not _cpu_high_notification_sent:
                # Check if it's been > 5 minutes
                elapsed = (current_time - _cpu_high_start_time).total_seconds()
                if elapsed >= 300:  # 5 minutes
                    notification_id = _generate_notification_id()
                    notification = {
                        'id': notification_id,
                        'type': NotificationType.WARNING.value,
                        'mode': NotificationMode.STICKY.value,
                        'title': 'High CPU Usage',
                        'message': f'CPU usage is {cpu_percent:.1f}% and has been above 90% for over 5 minutes.',
                        'timestamp': current_time.isoformat(),
                        'data': {
                            'cpu_percent': cpu_percent,
                            'duration_seconds': elapsed
                        }
                    }
                    _add_notification(notification)
                    _cpu_high_notification_sent = True
        else:
            # CPU is back to normal, reset tracking
            if cpu_percent <= 90:
                _cpu_high_start_time = None
                _cpu_high_notification_sent = False
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error checking CPU usage: {str(e)}", exc_info=True)


def _check_disk_usage():
    """Check if disk usage is > 95%."""
    global _disk_high_notification_sent
    
    try:
        metrics = get_current_metrics()
        if not metrics:
            return
        
        disk_percent = metrics.get('disk_percent', 0)
        
        if disk_percent > 95 and not _disk_high_notification_sent:
            notification_id = _generate_notification_id()
            notification = {
                'id': notification_id,
                'type': NotificationType.WARNING.value,
                'mode': NotificationMode.STICKY.value,
                'title': 'Disk Space Critical',
                'message': f'Disk usage is {disk_percent:.1f}%. Free up space immediately.',
                'timestamp': datetime.now().isoformat(),
                'data': {
                    'disk_percent': disk_percent
                }
            }
            _add_notification(notification)
            _disk_high_notification_sent = True
        elif disk_percent <= 95:
            # Disk usage is back to normal
            _disk_high_notification_sent = False
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error checking disk usage: {str(e)}", exc_info=True)


def _check_device_disconnections():
    """Check for device disconnections."""
    try:
        stats = get_current_health_stats()
        if not stats:
            return
        
        current_time = datetime.now()
        connection_loss_threshold = 300  # 5 minutes
        
        for device in stats:
            mac_address = device.get('mac_address')
            if not mac_address:
                continue
            
            last_activity_str = device.get('last_activity')
            if not last_activity_str:
                continue
            
            try:
                last_activity = datetime.fromisoformat(last_activity_str.replace('Z', '+00:00'))
                if last_activity.tzinfo:
                    last_activity = last_activity.replace(tzinfo=None)
            except:
                continue
            
            # Check if device was recording but hasn't posted updates
            event_count = device.get('event_count', 0)
            time_since_activity = (current_time - last_activity).total_seconds()
            
            if event_count > 0 and time_since_activity > connection_loss_threshold:
                if mac_address not in _device_disconnection_notifications:
                    notification_id = _generate_notification_id()
                    notification = {
                        'id': notification_id,
                        'type': NotificationType.DEVICE_DISCONNECTION.value,
                        'mode': NotificationMode.STACKED.value,
                        'title': 'Device Disconnected',
                        'message': f'Device {mac_address} has lost connection. Last activity: {last_activity_str}',
                        'timestamp': current_time.isoformat(),
                        'data': {
                            'mac_address': mac_address,
                            'last_activity': last_activity_str,
                            'event_count': event_count
                        }
                    }
                    _add_notification(notification)
                    _device_disconnection_notifications.add(mac_address)
            else:
                # Device is back online
                if mac_address in _device_disconnection_notifications:
                    _device_disconnection_notifications.discard(mac_address)
                    # Remove any existing disconnection notifications for this device
                    _remove_notifications_by_device(mac_address)
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error checking device disconnections: {str(e)}", exc_info=True)


def _add_notification(notification: Dict[str, Any]):
    """Add a notification to the system."""
    with _notification_lock:
        notification_id = notification['id']
        _notifications[notification_id] = notification
        
        # Handle different modes
        mode = notification.get('mode', NotificationMode.STICKY.value)
        if mode == NotificationMode.STACKED.value:
            _notification_queue.append(notification_id)
        elif mode == NotificationMode.TEMPORARY.value:
            # Schedule auto-removal after 10 seconds
            threading.Timer(10.0, lambda: remove_notification(notification_id)).start()


def _remove_notifications_by_device(mac_address: str):
    """Remove all notifications for a specific device."""
    with _notification_lock:
        to_remove = []
        for notif_id, notif in _notifications.items():
            if notif.get('data', {}).get('mac_address') == mac_address:
                to_remove.append(notif_id)
        
        for notif_id in to_remove:
            remove_notification(notif_id)


def create_channel_creation_notification(mac_address: str, channel_name: Optional[str] = None):
    """Create a notification when a new channel is created."""
    if mac_address in _channel_creation_notifications:
        return  # Already notified
    
    notification_id = _generate_notification_id()
    notification = {
        'id': notification_id,
        'type': NotificationType.INFO.value,
        'mode': NotificationMode.TEMPORARY.value,
        'title': 'New Channel Created',
        'message': f'Auto-created channel for MAC address {mac_address}' + (f' ({channel_name})' if channel_name else ''),
        'timestamp': datetime.now().isoformat(),
        'data': {
            'mac_address': mac_address,
            'channel_name': channel_name
        }
    }
    _add_notification(notification)
    _channel_creation_notifications.add(mac_address)


def get_active_notifications() -> List[Dict[str, Any]]:
    """Get all active notifications."""
    with _notification_lock:
        notifications = list(_notifications.values())
        
        # For stacked mode, only return the first one in queue
        stacked_ids = set(_notification_queue)
        if stacked_ids:
            # Return only the first stacked notification
            for queue_id in _notification_queue:
                if queue_id in _notifications:
                    # Return all stacked notifications in order, but mark which is visible
                    result = []
                    for qid in _notification_queue:
                        if qid in _notifications:
                            notif = _notifications[qid].copy()
                            notif['is_visible'] = (qid == queue_id)
                            result.append(notif)
                    # Also include non-stacked notifications
                    for notif in notifications:
                        if notif['id'] not in stacked_ids:
                            result.append(notif)
                    return result
        
        return notifications


def remove_notification(notification_id: str) -> bool:
    """Remove a notification by ID."""
    with _notification_lock:
        if notification_id in _notifications:
            notification = _notifications[notification_id]
            mode = notification.get('mode', NotificationMode.STICKY.value)
            
            # Remove from notifications
            del _notifications[notification_id]
            
            # Remove from queue if stacked
            if mode == NotificationMode.STACKED.value and notification_id in _notification_queue:
                _notification_queue.remove(notification_id)
            
            return True
        return False


def clear_all_notifications():
    """Clear all notifications."""
    with _notification_lock:
        _notifications.clear()
        _notification_queue.clear()


# Background monitoring thread
_monitoring_thread = None
_monitoring_interval = 30  # Check every 30 seconds
_monitoring_running = False


def _monitoring_loop():
    """Background loop to check for conditions."""
    global _monitoring_running
    while _monitoring_running:
        try:
            _check_cpu_usage()
            _check_disk_usage()
            _check_device_disconnections()
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error in notification monitoring loop: {str(e)}", exc_info=True)
        
        time.sleep(_monitoring_interval)


def start_monitoring():
    """Start the background monitoring thread."""
    global _monitoring_thread, _monitoring_running
    
    if _monitoring_running:
        return
    
    _monitoring_running = True
    _monitoring_thread = threading.Thread(target=_monitoring_loop, daemon=True)
    _monitoring_thread.start()


def stop_monitoring():
    """Stop the background monitoring thread."""
    global _monitoring_running
    _monitoring_running = False


# Auto-start monitoring on module import
start_monitoring()

