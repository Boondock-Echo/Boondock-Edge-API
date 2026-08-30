import json
import os
import re
import logging
import time
from flask import Blueprint, jsonify, request
from serial import Serial, SerialException
import serial.tools.list_ports
from ..services.settings_manager import get_settings_manager

_settings_manager = get_settings_manager()

MIN_SAFE_WAIT_TIME = 0.1

radio_bp = Blueprint('radio', __name__)

# Helper Functions for Radio Operations
def get_port_details():
    """Retrieve detailed metadata for all COM ports."""
    ports = []
    for port in serial.tools.list_ports.comports():
        port_data = {
            "device": port.device,
            "description": port.description,
            "hwid": port.hwid,
            "vid": f"0x{port.vid:04x}" if port.vid else None,
            "pid": f"0x{port.pid:04x}" if port.pid else None,
            "serial_number": port.serial_number,
            "location": port.location,
            "manufacturer": port.manufacturer,
            "product": port.product,
            "interface": port.interface
        }
        ports.append(port_data)
        logging.info(f"Port details: {port_data}")
    return ports

def send_command(scanner, command, wait_time=MIN_SAFE_WAIT_TIME, timeout=None):
    """
    Send a command to the scanner and return the response with optimized timing.
    
    Args:
        scanner: Serial object for the scanner
        command: Command to send (str or bytes)
        wait_time: Time to wait after sending command (default: 0.1s)
        timeout: Optional override for scanner timeout (default: use existing)
    
    Returns:
        str: Decoded response from the scanner
    
    Raises:
        SerialException: If serial communication fails
        UnicodeDecodeError: If response decoding fails
    """
    try:
        # Convert command to bytes if it's a string
        cmd_bytes = command.encode('utf-8') if isinstance(command, str) else command
        
        # Send the command
        scanner.write(cmd_bytes)
        
        # Optionally override timeout for this read
        original_timeout = scanner.timeout
        if timeout is not None:
            scanner.timeout = timeout
        
        # Wait for the scanner to process the command
        time.sleep(wait_time)
        
        # Read response (use read_until to stop at newline, potentially faster than readline)
        response = scanner.read_until(b'\r').decode('utf-8').strip()
        
        # Restore original timeout if we modified it
        if timeout is not None:
            scanner.timeout = original_timeout
        
        logging.debug(f"Command '{command}' sent, response: '{response}', wait_time: {wait_time:.3f}s")
        return response
    
    except SerialException as e:
        logging.error(f"Serial error during command '{command}': {e}")
        raise
    except UnicodeDecodeError as e:
        logging.error(f"Decode error during command '{command}': {e}")
        raise

# Example usage with timing for profiling
def profile_send_command(scanner, command, wait_time=MIN_SAFE_WAIT_TIME):
    """Profile the execution time of send_command."""
    start_time = time.time()
    response = send_command(scanner, command, wait_time)
    elapsed = time.time() - start_time
    logging.info(f"Command: {command}, Wait: {wait_time:.3f}s, Response: {response}, Time: {elapsed:.3f}s")
    return response, elapsed

# Checks what radio is connected o the port
def check_radio_connection(port_info):
    """Test if a port has a BC125AT scanner and return its details."""
    port = port_info["device"]
    try:
        with Serial(port, baudrate=9600, timeout=3) as scanner:
            time.sleep(MIN_SAFE_WAIT_TIME)  # Initial stabilization
            logging.info(f"Trying port {port}")            
            model = send_command(scanner, b'MDL\r')
            if not model.startswith("MDL,BC125AT"):
                logging.info(f"Port {port} is not a BC125AT (response: '{model}')")
                return None

            version = send_command(scanner, b'VER\r')
            
            response = send_command(scanner, b'PRG\r')  # Enter programming mode
            if response != "PRG,OK":
                logging.error(f"Failed to enter program mode: {response}")
                return None

            channel_info = send_command(scanner, b'CIN,500\r')
            parts = channel_info.split(',')
            current_name = parts[2] if len(parts) > 2 else "UNKNOWN"
            logging.info(f"Port {port} - PRG,1 response: '{channel_info}', Current name: '{current_name}'")

            response = send_command(scanner, b'EPG\r')  # Exit programming mode
            if response != "EPG,OK":
                logging.warning(f"Failed to exit program mode: {response}")

            return {
                "port": port,
                "model": model,
                "version": version,
                "current_name": current_name
            }
    except (SerialException, UnicodeDecodeError) as e:
        logging.error(f"Error on {port}: {e}")
        return None
    
# Validates data input ranges
def validate_input(channel, frequency, modulation, channel_name):
    """Validate inputs for programming a channel."""
    if not (1 <= channel <= 500):
        raise ValueError("Channel must be between 1 and 500.")
    if not (250000 <= frequency <= 5120000):
        raise ValueError("Frequency must be between 250000 and 5120000 Hz.")
    if modulation not in ["NFM", "AM", "FM"]:
        raise ValueError("Invalid modulation type. Use 'NFM', 'AM', or 'FM'.")
    if not isinstance(channel_name, str) or not (1 <= len(channel_name) <= 16):
        raise ValueError("Channel name must be 1-16 characters.")
    if not all(c.isalnum() or c == '_' for c in channel_name):
        raise ValueError("Channel name must contain only letters, numbers, or underscores.")

def read_channel(scanner, channel):
    """Read current channel configuration from scanner."""
    try:
        command = f"COUT,{channel}\r"
        response = send_command(scanner, command)
        if response.startswith("COUT,OK,"):
            # Split the response and extract relevant fields
            # Assuming response format: COUT,OK,channel,name,freq,mod,ctcss,...
            parts = response.split(',')
            if len(parts) >= 7:
                return {
                    'channel': parts[2],
                    'name': parts[3],
                    'frequency': parts[4],
                    'modulation': parts[5],
                    'ctcss': parts[6]
                }
        logging.debug(f"Unexpected channel read response: {response}")
        return None
    except Exception as e:
        logging.debug(f"Error reading channel {channel}: {e}")
        return None

# Modified read_channel function
def read_channel(port, channel):
    """Read the current settings for a scanner channel."""
    try:
        if not isinstance(channel, int) or channel < 1:
            raise ValueError("Channel must be a positive integer")
        
        with Serial(port, baudrate=9600, timeout=3) as scanner:
            response = send_command(scanner, b'PRG\r')
            if response != "PRG,OK":
                logging.error(f"Failed to enter program mode on {port}: {response}")
                return None

            command = f"CIN,{channel}\r"
            response = send_command(scanner, command)
            logging.info(f"Read Channel {channel} from {port}: {response}")
            
            if not response.startswith("CIN,"):
                logging.error(f"Failed to read channel {channel} on {port}: {response}")
                return None

            parts = response.split(',')
            if len(parts) < 9:
                logging.error(f"Invalid response format from {port} for channel {channel}: {response}")
                return None

            channel_name = parts[2]
            frequency = parts[3]
            modulation = parts[4]
            ctcss = parts[5] if parts[5] else ""
            
            response = send_command(scanner, b'EPG\r')
            if response != "EPG,OK":
                logging.warning(f"Failed to exit program mode on {port}: {response}")

            logging.info(f"Port {port} - Successfully read Channel {channel}")
            return {
                "channel": channel,
                "channel_name": channel_name,
                "frequency": frequency,
                "modulation": modulation
            }
    except ValueError as ve:
        logging.error(f"Validation error on {port}: {ve}")
        return None
    except (SerialException, UnicodeDecodeError) as e:
        logging.error(f"Error on {port}: {e}")
        return None

def write_channel(port, channel, frequency, modulation, channel_name, ctcss=""):
    """Program a frequency and name into a scanner channel if different from current settings."""
    try:
        validate_input(channel, frequency, modulation, channel_name)
        
        # Read current channel settings first
        current_settings = read_channel(port, channel)
        
        # Compare current and new settings
        needs_update = True
        if current_settings:
            needs_update = (
                current_settings['channel_name'] != channel_name or
                current_settings['frequency'] != frequency or
                current_settings['modulation'] != modulation or
                (current_settings.get('ctcss', '') != ctcss if 'ctcss' in current_settings else ctcss != "")
            )

        # Only write if there's a difference or if reading failed
        if not needs_update:
            logging.info(f"Port {port} - Channel {channel} already has matching settings, skipped write")
            return True

        with Serial(port, baudrate=9600, timeout=3) as scanner:
            response = send_command(scanner, b'PRG\r')
            if response != "PRG,OK":
                logging.error(f"Failed to enter program mode on {port}: {response}")
                return False

            command = f"CIN,{channel},{channel_name},{frequency},{modulation},{ctcss},0,0,0\r"
            response = send_command(scanner, command)
            if response != "CIN,OK":
                logging.error(f"Failed to program channel {channel} on {port}: {response}")
                return False

            response = send_command(scanner, b'EPG\r')
            if response != "EPG,OK":
                logging.warning(f"Failed to exit program mode on {port}: {response}")

            logging.info(f"Port {port} - Successfully programmed Channel {channel} with name {channel_name}")
            return True

    except ValueError as ve:
        logging.error(f"Validation error on {port}: {ve}")
        return False
    except (SerialException, UnicodeDecodeError) as e:
        logging.error(f"Error on {port}: {e}")
        return False

    except ValueError as ve:
        logging.error(f"Validation error on {port}: {ve}")
        return False
    except (SerialException, UnicodeDecodeError) as e:
        logging.error(f"Error on {port}: {e}")
        return False

def set_volume(port, volume):
    """Set the Volume"""
    try:
        with Serial(port, baudrate=9600, timeout=3) as scanner:

            command = f"VOL,{volume}\r"
            response = send_command(scanner, command)
            if response != "VOL,OK":
                logging.error(f"Failed to set Volume {volume} on {port}: {response}")
                return False
 

            logging.info(f"Port {port} - Successfully update Volume {port}")
            return True
    except ValueError as ve:
        logging.error(f"Validation error on {port}: {ve}")
        return False
    except (SerialException, UnicodeDecodeError) as e:
        logging.error(f"Error on {port}: {e}")
        return False

def set_squelch(port, squelch):
    """Set the squelch"""
    try:
        with Serial(port, baudrate=9600, timeout=3) as scanner:

            command = f"SQL,{squelch}\r"
            response = send_command(scanner, command)
            if response != "SQL,OK":
                logging.error(f"Failed to set Squelch {squelch} on {port}: {response}")
                return False

            logging.info(f"Port {port} - Successfully updated Squelch {port} ")
            return True
    except ValueError as ve:
        logging.error(f"Validation error on {port}: {ve}")
        return False
    except (SerialException, UnicodeDecodeError) as e:
        logging.error(f"Error on {port}: {e}")
        return False

def get_battery(port, battery):
    """Get the Battery level"""
    try:
        with Serial(port, baudrate=9600, timeout=3) as scanner:

            command = f"BSV,{battery}\r"
            response = send_command(scanner, command)
            if response != "BSV,OK":
                logging.error(f"Failed to set Squelch {battery} on {port}: {response}")
                return False

            logging.info(f"Port {port} - Successfully updated Squelch {port} ")
            return True
    except ValueError as ve:
        logging.error(f"Validation error on {port}: {ve}")
        return False
    except (SerialException, UnicodeDecodeError) as e:
        logging.error(f"Error on {port}: {e}")
        return False

def set_backlight(port, backlight):
    """Set the Backlight"""
    try:
        with Serial(port, baudrate=9600, timeout=3) as scanner:

            command = f"BLT,{backlight}\r"
            response = send_command(scanner, command)
            if response != "BLT,OK":
                logging.error(f"Failed to set Backlight = {backlight} on {port}: {response}")
                return False

            logging.info(f"Port {port} - Successfully updated Backlight {port} ")
            return True
    except ValueError as ve:
        logging.error(f"Validation error on {port}: {ve}")
        return False
    except (SerialException, UnicodeDecodeError) as e:
        logging.error(f"Error on {port}: {e}")
        return False

# Load current inventory of connected scanners
def load_inventory():
    """
    Load existing scanner inventory from database.
    Returns empty dict if no scanners exist.
    """
    try:
        scanners = _settings_manager.get_all_scanners()
        return scanners if scanners else {}
    except Exception as exc:
        logging.error(f"Error loading scanner inventory: {exc}")
        return {}

# Save inventory to database
def save_inventory(inventory):
    """Save scanner inventory to database."""
    try:
        for scanner_id, scanner_data in inventory.items():
            _settings_manager.save_scanner(scanner_id, scanner_data)
        logging.info(f"Saved scanner inventory to database")
    except Exception as exc:
        logging.error(f"Error saving scanner inventory: {exc}")

# Verify that the scanner connected on port is correct one
def verify_scanner(port, expected_name):
    """Verify that the scanner at the given port matches the expected name."""
    port_info = {"device": port}
    scanner_info = check_radio_connection(port_info)
    if scanner_info and scanner_info["current_name"] == expected_name:
        return True
    return False

# Find a scanner port base don scanner ID
def find_scanner_port_by_id(scanner_id, inventory):
    """
    Find the correct port for a scanner ID, maintaining the scanner's identity.
    
    Args:
        scanner_id (int): Scanner ID number (e.g., 1 for "SCANNER_001")
        inventory (dict): Current scanner inventory
        
    Returns:
        str or None: Port name if found, None otherwise
    """
    expected_name = f"SCANNER_{scanner_id:03d}"
    
    if not inventory:
        logging.error("Scanner inventory is empty")
        return None
    
    # Step 1: Check if the scanner ID exists in inventory
    if expected_name not in inventory:
        logging.error(f"Scanner {expected_name} not found in inventory")
        return None
        
    # Step 2: Check if the scanner is marked as connected in inventory
    scanner_data = inventory[expected_name]
    port = scanner_data.get("port")
    
    if not port:
        logging.error(f"Scanner {expected_name} has no port information in inventory")
        return None
        
    # Step 3: Verify the scanner at this port matches the expected name
    if scanner_data["status"] == "connected":
        if verify_scanner(port, expected_name):
            return port
        else:
            logging.warning(f"Scanner at {port} does not match expected name {expected_name}")
    
    # Step 4: If not found or mismatched, scan all ports to find the scanner
    all_ports = get_port_details()
    for port_info in all_ports:
        port = port_info["device"]
        if verify_scanner(port, expected_name):
            # Update inventory with new port if found
            logging.info(f"Found scanner {expected_name} on new port {port}")
            inventory[expected_name] = {
                **scanner_data,  # Keep existing data
                "port": port,    # Update port
                "status": "connected"
            }
            save_inventory(inventory)
            return port
    
    logging.error(f"Scanner {expected_name} not found on any port")
    return None

def _is_esp32_port(port_info):
    """Check if a port is an ESP32 recorder (should be skipped for scanner detection)."""
    # Vendor/Product IDs and descriptor keywords commonly exposed by CP210x bridges used on ESP32 boards
    ESP32_VENDOR_IDS = {0x10C4}  # 0x10C4 = Silicon Labs
    ESP32_PRODUCT_IDS = {0xEA60, 0xEA63, 0xEA70}  # CP210x product IDs
    ESP32_KEYWORDS = (
        'cp210',
        'silicon labs',
        'usb to uart',
        'esp32'
    )
    
    # Check VID/PID if available (port_info from get_port_details() has hex strings like "0x10c4")
    vid = port_info.get('vid')
    pid = port_info.get('pid')
    if vid and pid:
        try:
            # Convert hex string to int (e.g., "0x10c4" -> 4292)
            vid_int = int(vid, 16) if isinstance(vid, str) and vid.startswith('0x') else (int(vid) if isinstance(vid, (int, str)) else None)
            pid_int = int(pid, 16) if isinstance(pid, str) and pid.startswith('0x') else (int(pid) if isinstance(pid, (int, str)) else None)
            if vid_int and pid_int and vid_int in ESP32_VENDOR_IDS and pid_int in ESP32_PRODUCT_IDS:
                return True
        except (ValueError, TypeError):
            pass
    
    # Check descriptor keywords
    descriptor_parts = filter(None, [
        port_info.get('description', ''),
        port_info.get('manufacturer', ''),
        port_info.get('product', ''),
        port_info.get('device', '')
    ])
    descriptor = ' '.join(descriptor_parts).lower()
    
    return any(keyword in descriptor for keyword in ESP32_KEYWORDS)


#Initialize the scanners and ports
def init_scanners():
    """
    Check all COM ports and update the connection status of scanners in inventory.
    Scanner inventory is stored in the database (not JSON files).
    Maintains scanner identities across port changes without reprogramming IDs.
    Skips ESP32 recorder ports to avoid decode errors.
    
    Returns:
        dict: Summary of connected and disconnected scanners
    """
    # Load existing inventory or create empty one if it doesn't exist
    inventory = load_inventory()
    
    # Get all available COM ports in the system
    all_ports = get_port_details()
    port_to_scanner_map = {}  # Maps ports to detected scanner info
    
    # First pass: Detect all scanners and their IDs
    for port_info in all_ports:
        port = port_info["device"]
        
        # Skip ESP32 recorder ports - they're not scanners and will cause decode errors
        if _is_esp32_port(port_info):
            logging.debug(f"Skipping ESP32 recorder port {port} during scanner detection")
            continue
        
        logging.info(f"Checking port {port}")

        scanner_info = check_radio_connection(port_info)  # Already updated to use send_command
        
        if scanner_info:
            current_name = scanner_info["current_name"]
            if re.match(r"SCANNER_\d{3}", current_name):
                # This is a valid scanner ID
                port_to_scanner_map[port] = {
                    "unique_name": current_name,
                    "model": scanner_info["model"],
                    "version": scanner_info["version"]
                }
                logging.info(f"Detected scanner {current_name} on port {port}")
            else:
                # Not a valid ID, but a scanner is present
                port_to_scanner_map[port] = {
                    "unique_name": None,  # No valid ID
                    "current_name": current_name,  # Store the current name for reference
                    "model": scanner_info["model"],
                    "version": scanner_info["version"]
                }
                logging.info(f"Detected scanner with invalid name '{current_name}' on port {port}")
    
    # Second pass: Update the inventory
    updated_inventory = {}
    id_to_port_map = {}  # Maps scanner IDs to detected ports
    
    # Map detected scanner IDs to their ports
    for port, info in port_to_scanner_map.items():
        if info["unique_name"]:  # Only for scanners with valid IDs
            id_to_port_map[info["unique_name"]] = port
    
    # Process each scanner in inventory
    for unique_name, data in inventory.items():
        if unique_name in id_to_port_map:
            # Scanner was found with its ID
            new_port = id_to_port_map[unique_name]
            updated_inventory[unique_name] = {
                **data,
                "port": new_port,
                "status": "connected",
                "model": port_to_scanner_map[new_port]["model"],
                "version": port_to_scanner_map[new_port]["version"]
            }
            # Remove from id_to_port_map to track which have been processed
            del id_to_port_map[unique_name]
        else:
            # Scanner with this ID was not found
            # Check if it might be on a new port without its ID
            old_port = data.get("port")
            
            if old_port in port_to_scanner_map and port_to_scanner_map[old_port]["unique_name"] is None:
                # A scanner without ID was found on the same port
                logging.warning(f"Scanner {unique_name} lost its ID on port {old_port}")
                updated_inventory[unique_name] = {
                    **data,
                    "status": "connected",
                    "needs_reprogramming": True  # Flag for future reprogramming
                }
            else:
                # Scanner is truly disconnected
                updated_inventory[unique_name] = {
                    **data,
                    "status": "disconnected"
                }
    
    # Add any new scanners found with valid IDs not in inventory
    for unique_name, port in id_to_port_map.items():
        if unique_name not in updated_inventory:
            updated_inventory[unique_name] = {
                "port": port,
                "model": port_to_scanner_map[port]["model"],
                "version": port_to_scanner_map[port]["version"],
                "status": "connected"
            }
            logging.info(f"Added new scanner {unique_name} on port {port} to inventory")
    
    # If inventory is empty and we found scanners without IDs, assign new IDs
    if not inventory and any(info["unique_name"] is None for info in port_to_scanner_map.values()):
        logging.info("No existing inventory and found scanners without IDs. Auto-assigning new IDs.")
        for port, info in port_to_scanner_map.items():
            if info["unique_name"] is None:
                # Find next available ID
                existing_ids = set(updated_inventory.keys())
                for i in range(1, 1000):
                    new_id = f"SCANNER_{i:03d}"
                    if new_id not in existing_ids:
                        # Program the scanner with the new ID
                        success = write_channel(port, 500, 1625500, "NFM", new_id)  # Already updated to use send_command
                        if success:
                            updated_inventory[new_id] = {
                                "port": port,
                                "model": info["model"],
                                "version": info["version"],
                                "status": "connected"
                            }
                            logging.info(f"Auto-assigned new ID {new_id} to scanner at {port}")
                        else:
                            logging.error(f"Failed to auto-assign ID {new_id} to scanner at {port}")
                        break
    
    # Save the updated inventory
    save_inventory(updated_inventory)
    
    # Generate summary
    connected_scanners = [scanner_id for scanner_id, data in updated_inventory.items() 
                         if data["status"] == "connected"]
    disconnected_scanners = [scanner_id for scanner_id, data in updated_inventory.items() 
                            if data["status"] == "disconnected"]
    
    summary = {
        "total_scanners": len(updated_inventory),
        "connected_scanners": len(connected_scanners),
        "disconnected_scanners": len(disconnected_scanners),
        "connected_scanner_ids": connected_scanners,
        "disconnected_scanner_ids": disconnected_scanners
    }
    
    # Log any scanners that need ID reprogramming
    needs_reprogramming = [unique_name for unique_name, data in updated_inventory.items() 
                         if data.get("needs_reprogramming", False)]
    if needs_reprogramming:
        logging.warning(f"Scanners that need ID reprogramming: {', '.join(needs_reprogramming)}")
        summary["needs_reprogramming"] = needs_reprogramming
        
    logging.info(f"Inventory update complete: {summary['connected_scanners']} connected, {summary['disconnected_scanners']} disconnected")
    
    # Print summary to console
    print("\n=== SCANNER INVENTORY SUMMARY ===")
    print(f"Scanners Connected: {summary['connected_scanners']}")
    print(f"Scanners Disconnected: {summary['disconnected_scanners']}")
    if needs_reprogramming:
        print(f"Scanners Needing ID Reprogramming: {len(needs_reprogramming)}")
    print("===============================\n")
    
    return summary

# Initialize scanner inventory on application startup
def initialize_scanner_inventory():
    """Initialize scanner inventory when the application starts."""
    try:
        summary = init_scanners()
        logging.info(f"Scanner inventory initialized on startup: {summary}")
        return summary
    except Exception as e:
        logging.error(f"Error initializing scanner inventory on startup: {str(e)}")
        return None

# API Routes
@radio_bp.route('/init', methods=['POST'])
def initialize_radio_inventory():
    """Initialize scanner inventory without reprogramming scanners."""
    try:
        summary = init_scanners()
        return jsonify({"message": "Scanner inventory initialized", "summary": summary}), 200
    except Exception as e:
        logging.error(f"Error initializing inventory: {str(e)}")
        return jsonify({"error": str(e)}), 500

@radio_bp.route('/clear', methods=['POST'])
def clear_scanners():
    """Clear inventory and reset all scanner memories (Channel 1 only)."""
    try:
        all_ports = get_port_details()
        for port_info in all_ports:
            port = port_info["device"]
            scanner_info = check_radio_connection(port_info)
            if scanner_info:
                success = write_channel(port, 500, 1625500, "NFM", "CLEARED")
                if not success:
                    logging.error(f"Failed to clear scanner on {port}")

        # Clear the inventory from database (delete all scanners)
        scanners = _settings_manager.get_all_scanners()
        for scanner_id in scanners.keys():
            _settings_manager.delete_scanner(scanner_id)
        logging.info("Scanner inventory cleared")

        return jsonify({"message": "Scanners and inventory cleared successfully"}), 200
    except Exception as e:
        logging.error(f"Error clearing scanners: {str(e)}")
        return jsonify({"error": str(e)}), 500

@radio_bp.route('/list', methods=['GET'])
def list_scanners():
    """Return a JSON list of all scanners in the inventory."""
    try:
        inventory = load_inventory()
        return jsonify({"scanners": inventory}), 200
    except Exception as e:
        logging.error(f"Error listing scanners: {str(e)}")
        return jsonify({"error": str(e)}), 500

@radio_bp.route('/<int:scanner_id>/channel/<int:channel>', methods=['POST'])
def add_channel_to_radio(scanner_id, channel):
    """Add a new channel to  overwriting any existing channel."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Channel data is required"}), 400

        required_fields = ["frequency", "modulation", "channel_name"]
        if not all(field in data for field in required_fields):
            return jsonify({"error": "Missing required fields: frequency, modulation, channel_name"}), 400

        frequency = data["frequency"]
        modulation = data["modulation"]
        channel_name = data["channel_name"]
        ctcss = data.get("ctcss", "")

        inventory = load_inventory()
        port = find_scanner_port_by_id(scanner_id, inventory)
        if not port:
            return jsonify({"error": f"Scanner SCANNER_{scanner_id:03d} not found"}), 404

        # Program the channel (overwrites existing if present)
        success = write_channel(port, channel, frequency, modulation, channel_name, ctcss)
        if success:
            return jsonify({"message": f"Channel {channel} added/overwritten on SCANNER_{scanner_id:03d}"}), 200
        return jsonify({"error": "Failed to program channel"}), 500
    except Exception as e:
        logging.error(f"Error adding channel to scanner {scanner_id}: {str(e)}")
        return jsonify({"error": str(e)}), 500
 
@radio_bp.route('/<int:scanner_id>/channel/<int:channel>', methods=['GET'])
def get_channel_info(scanner_id, channel):
    """Read channel info from the radio with timing for each step."""
    try:

        inventory = load_inventory()
        port = find_scanner_port_by_id(scanner_id, inventory)
        if not port:
            return jsonify({"error": f"Scanner SCANNER_{scanner_id:03d} not found"}), 404

        channel_info = read_channel(port, channel)
        if channel_info is None:
            return jsonify({"error": f"Failed to read channel {channel} from scanner {scanner_id}"}), 500
        return jsonify(channel_info), 200
    except Exception as e:
        end_total = time.time()
        logging.error(f"Error reading scanner {scanner_id}: {str(e)}")
        return jsonify({"error": str(e)}), 500

@radio_bp.route('/<int:scanner_id>/volume', methods=['POST'])
def set_scanner_volume(scanner_id):
    """Add a new channel to  overwriting any existing channel."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Channel data is required"}), 400

        required_fields = ["volume"]
        if not all(field in data for field in required_fields):
            return jsonify({"error": "Missing required fields: volume"}), 400

        volume = data["volume"]

        inventory = load_inventory()
        port = find_scanner_port_by_id(scanner_id, inventory)
        if not port:
            return jsonify({"error": f"Scanner SCANNER_{scanner_id:03d} not found"}), 404

        # Program the channel (overwrites existing if present)
        success = set_volume(port=port, volume=volume)

        if success:
            return jsonify({"message": f"Volume updated to {volume}"}), 200
        return jsonify({"error": "Failed to Set Volume"}), 500
    except Exception as e:
        logging.error(f"Error Settinv Volume {scanner_id}: {str(e)}")
        return jsonify({"error": str(e)}), 500

@radio_bp.route('/<int:scanner_id>/squelch', methods=['POST'])
def set_squelch_level(scanner_id):
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Channel data is required"}), 400

        required_fields = ["squelch"]
        if not all(field in data for field in required_fields):
            return jsonify({"error": "Missing required fields: squelch"}), 400

        squelch = data["squelch"]

        inventory = load_inventory()
        port = find_scanner_port_by_id(scanner_id, inventory)
        if not port:
            return jsonify({"error": f"Scanner SCANNER_{scanner_id:03d} not found"}), 404

        # Program the channel (overwrites existing if present)
        success = set_squelch(port=port, squelch=squelch)

        if success:
            return jsonify({"message": f"squelch updated to {squelch}"}), 200
        return jsonify({"error": "Failed to Set squelch to {squelch}"}), 500
    except Exception as e:
        logging.error(f"Error setting squelch {scanner_id}: {str(e)}")
        return jsonify({"error": str(e)}), 500

@radio_bp.route('/<int:scanner_id>/park', methods=['POST'])
def park_scanner(scanner_id):
    """Save the Channel 500 settings to Channel 1"""
    try:
        inventory = load_inventory()
        port = find_scanner_port_by_id(scanner_id, inventory)
        if not port:
            return jsonify({"error": f"Failed:: Scanner SCANNER_{scanner_id:03d} not found or disconnected"}), 404

        # Read settings from channel 500
        settings = read_channel(port, 500)
        if not settings:
            logging.error(f"Failed to read channel 500 settings for SCANNER_{scanner_id:03d}")
            return jsonify({"error": "Failed:: Unable to read channel 500 settings"}), 500

        # Extract settings and write to channel 1
        success = write_channel(
            port=port,
            channel=1,
            frequency=settings['frequency'],
            modulation=settings['modulation'],
            channel_name=settings['channel_name'],
            ctcss=settings.get('ctcss', '')  # Use get() to handle case where ctcss might not be present
        )

        if not success:
            logging.error(f"Failed:: channel 500 settings to channel 1 on SCANNER_{scanner_id:03d}")
            return jsonify({"error": "Failed:: {scanner_id} channel 500 settings to channel 1" }), 500

        # Set volume to 0 after successful write
        with Serial(port, baudrate=9600, timeout=3) as scanner:
            response = send_command(scanner, b'VOL,0\r')
            if response != "VOL,OK":  # Assuming VOL command returns VOL,OK on success
                logging.warning(f"Failed:: to set volume to 0 on {port}: {response}")

        logging.info(f"Successfully parked channel 500 settings to channel 1 on SCANNER_{scanner_id:03d}")
        return jsonify({"message": f"Sucess:: Radio {scanner_id} set to Parked with {settings['channel_name']}"}), 200

    except Exception as e:
        logging.error(f"Failed::Error parking scanner {scanner_id}: {str(e)}")
        return jsonify({"error": str(e)}), 500
  
######################## TEST #######################################################

@radio_bp.route('/<int:scanner_id>/set_backlight', methods=['POST'])
def set_backlight(scanner_id):
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Channel data is required"}), 400

        required_fields = ["backlight"]
        if not all(field in data for field in required_fields):
            return jsonify({"error": "Missing required fields: backlight"}), 400

        backlight = data["backlight"]

        inventory = load_inventory()
        port = find_scanner_port_by_id(scanner_id, inventory)
        if not port:
            return jsonify({"error": f"Scanner SCANNER_{scanner_id:03d} not found"}), 404

        # Program the channel (overwrites existing if present)
        success = set_squelch(port=port, backlight=backlight)

        if success:
            return jsonify({"message": f"backlight updated to {backlight}"}), 200
        return jsonify({"error": "Failed to Set backlight to {backlight}"}), 500
    except Exception as e:
        logging.error(f"Error setting backlight {scanner_id}: {str(e)}")
        return jsonify({"error": str(e)}), 500


@radio_bp.route('/reassign', methods=['POST'])
def reassign_scanner_id():

    """
    Reassign a scanner from one ID to another.
    
    Expected JSON payload:
    {
        "old_id": 8,  # The current scanner ID number (e.g., 8 for "SCANNER_008")
        "new_id": 7   # The desired new ID (e.g., 7 for "SCANNER_007")
    }
    """
    try:
        data = request.get_json()
        if not data or "old_id" not in data or "new_id" not in data:
            return jsonify({"error": "Both old_id and new_id parameters are required"}), 400
            
        old_id = int(data["old_id"])
        new_id = int(data["new_id"])
        
        old_name = f"SCANNER_{old_id:03d}"
        new_name = f"SCANNER_{new_id:03d}"
        
        # Load inventory
        inventory = load_inventory()
        
        # Check if old scanner exists in inventory
        if old_name not in inventory:
            return jsonify({"error": f"Scanner {old_name} not found in inventory"}), 404
            
        # Check if new ID already exists
        if new_name in inventory:
            return jsonify({"error": f"ID {new_name} already exists in inventory"}), 409
            
        # Find the scanner's port
        port = find_scanner_port_by_id(old_id, inventory)
        if not port:
            return jsonify({"error": f"Scanner {old_name} not found or not connected"}), 404
            
        # Reprogram Channel 1 with the new ID
        success = write_channel(port, 500, 1625500, "NFM", new_name)
        if not success:
            return jsonify({"error": f"Failed to program new ID {new_name} to scanner"}), 500
            
        # Update inventory
        scanner_data = inventory[old_name]
        inventory[new_name] = {
            **scanner_data,
            "port": port,
            "status": "connected"
        }
        
        # Remove old entry
        del inventory[old_name]
        
        # Save updated inventory
        save_inventory(inventory)
        
        return jsonify({
            "message": f"Successfully reassigned scanner from {old_name} to {new_name}",
            "old_id": old_name,
            "new_id": new_name,
            "port": port
        }), 200
    except Exception as e:
        logging.error(f"Error reassigning scanner ID: {str(e)}")
        return jsonify({"error": str(e)}), 500
    
@radio_bp.route('/restore_ids', methods=['POST'])
def restore_scanner_ids():
    """Restore IDs to scanners that have lost them according to inventory."""
    try:
        inventory = load_inventory()
        restored = []
        failed = []
        
        for unique_name, data in inventory.items():
            if data.get("needs_reprogramming", False) and data.get("status") == "connected":
                port = data.get("port")
                if port:
                    # First check if this port already has a valid scanner ID
                    port_info = {"device": port}
                    scanner_info = check_radio_connection(port_info)
                    
                    if scanner_info and re.match(r"SCANNER_\d{3}", scanner_info["current_name"]):
                        # Port already has a scanner with valid ID - don't overwrite
                        logging.warning(f"Port {port} already has scanner with ID {scanner_info['current_name']}. Skipping reprogramming of {unique_name}.")
                        failed.append({"id": unique_name, "reason": f"Port already has scanner with ID {scanner_info['current_name']}"})
                        continue
                        
                    # Safe to reprogram
                    success = write_channel(port, 500, 1625500, "NFM", unique_name)
                    if success:
                        # Remove the needs_reprogramming flag
                        inventory[unique_name]["needs_reprogramming"] = False
                        restored.append(unique_name)
                        logging.info(f"Restored ID {unique_name} to scanner at port {port}")
                    else:
                        failed.append({"id": unique_name, "reason": "Failed to program"})
                        logging.error(f"Failed to restore ID {unique_name} to scanner at port {port}")
        
        save_inventory(inventory)
        
        return jsonify({
            "message": f"Restored IDs to {len(restored)} scanners, {len(failed)} failed/skipped",
            "restored": restored,
            "failed": failed
        }), 200
    except Exception as e:
        logging.error(f"Error restoring scanner IDs: {str(e)}")
        return jsonify({"error": str(e)}), 500


#  new api of edit sacnner details 
# New Edit Scanner API
@radio_bp.route('/<scanner_id>', methods=['PUT'])
def edit_scanner(scanner_id):
    """
    Edit an existing scanner in the inventory.
    Expects a JSON body with updated scanner details.
    """
    try:
        # Load the current inventory
        inventory = load_inventory()

        # Check if the scanner exists
        if scanner_id not in inventory:
            return jsonify({"error": f"Scanner {scanner_id} not found"}), 404

        # Get the updated data from the request body
        updated_data = request.get_json()
        if not updated_data:
            return jsonify({"error": "No data provided in request body"}), 400

        # Define allowed fields to update
        allowed_fields = {"id", "channel", "port", "model", "version", "status" , "volume", "squelch"}
        current_scanner = inventory[scanner_id]

        # Update only the fields provided in the request that are allowed
        for key, value in updated_data.items():
            if key in allowed_fields:
                current_scanner[key] = value
            else:
                logging.warning(f"Field {key} is not allowed for update")

        # Save the updated inventory back to the file
        inventory[scanner_id] = current_scanner
        save_inventory(inventory)

        return jsonify({
            "message": f"Scanner {scanner_id} updated successfully",
            "scanner": inventory[scanner_id]
        }), 200

    except Exception as e:
        logging.error(f"Error updating scanner {scanner_id}: {str(e)}")
        return jsonify({"error": str(e)}), 500