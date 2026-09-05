"""
Device v1 API routes.
Handles device events, audio uploads to S3, log uploads, channel retrieval, and device settings.
"""
import json
import os
import re
import logging
import sqlite3
import subprocess
import io
import time
import pytz
from config import DATA_ROOT
from datetime import datetime, timezone
from flask import Blueprint, after_this_request, jsonify, request, send_file
from flasgger import swag_from
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode
from werkzeug.utils import secure_filename

from app.services.audio_handler import get_audio_handler
from app.utils.crc_utils import check_and_update_duplicate_cache
from app.services.channel_state import (
    set_channel_visual_state,
    get_channel_visual_state,
    get_stored_visual_state,
    get_all_channel_visual_states,
    touch_device_activity,
    ChannelVisualState,
)
from ..utils.logging_setup import error_logger, event_logger
from ..utils.auth import (
    load_tokens,
    is_token_valid,
    is_mac_registered,
    generate_token,
    get_mac_for_token,
    VALID_TOKENS,
)
from ..utils.s3_utils import (
    ensure_bucket_exists,
    is_s3_enabled,
    get_s3_client,
    get_s3_settings,
)
from ..routes.route_utils import (
    RECORDINGS_DIR,
    DB_PATH,
    get_channel_id_from_mac,
    get_mac_from_channel_id,
    create_channel_for_mac,
    get_recording_path,
    channels_lock,
    calculate_wav_duration,
    allowed_file,
)
from ..services.settings_manager import get_settings_manager, normalize_mac_address
from ..services.device_health_monitor import (
    track_device_created,
    track_connection,
    track_event,
    track_error,
    track_file_upload
)
from ..services.cloud_device_events import (
    persist_cloud_device_event_async,
    cloud_event_type_id,
    list_cloud_events_for_mac,
)

_settings_manager = get_settings_manager()

device_bp = Blueprint('device', __name__)

# Directory for device settings storage
DEVICE_SETTINGS_DIR = str(DATA_ROOT / 'device_settings')
os.makedirs(DEVICE_SETTINGS_DIR, exist_ok=True)

# Event code mapping
EVENT_CODE_MAPPING = {
    'I01': 'Starting in normal mode',
    'I02': 'Boondock ready and listening',
    'I03': '',
    'I04': 'Config updated',
    'I05': 'Speaker mute',
    'I06': 'Speaker unmute',
    'I07': 'Recording active',
    'I08': 'Recording inactive',
    'I09': 'PTT activated',
    'I10': 'PTT Released',
    'I11': 'PTT recording active',
    'I12': 'PTT recording inactive',
    'I13': 'Playing System Audio',
    'I14': 'SD Card usage',
    'I15': 'Tx is enabled',
    'I16': 'Tx is disabled',
    'I17': 'Line in minimum db changed',
    'I18': 'Line in gain chagned',
    'I19': 'Play system audio queued',
    'I20': 'Play audio queued',
    'I21': 'Transmit audio file queued',
    'I22': 'Min recording size updated',
    'I23': 'Silence duration udpated',
    'I24': 'Max recording size updated',
    'I25': 'Speaker volume changed to',
    'I26': 'User ID changed to',
    'I27': 'Dock name changed to',
    'I28': 'Playback volume changed',
    'I29': 'OTA update changed to',
    'I30': 'TX volume changed to',
    'I31': 'Start remote recording',
    'I32': 'Stop remote recording',
    'I33': 'Remote Reboot',
    'I34': 'Save Config',
    'I35': 'Factory Reset',
    'I36': 'Set default settings',
    'I37': 'Audio file uploaded',
    'I38': 'Upgrading config file',
    'I39': 'Reboot reason',
    'I40': '',
    'I41': 'Upload response',
    'I42': 'Playback complete',
    'I43': 'Transmit complete',
    'I44': 'Transmit is not allowed',
    'I45': 'Load cdn files',
    'I46': 'Downloading for playback',
    'I47': 'Adding playback queue',
    'I48': 'Upload complete',
    'I49': '',
    'I50': 'Completed downloading audio',
    'I51': 'Recording upload active',
    'I52': 'Recording upload inactive',
    'I53': 'Start remote Mic recording',
    'I54': 'Stop remote Mic recording',
    'I55': 'PTT upload active',
    'I56': 'PTT upload inactive',
    'I57': 'Notify All Settings',
    'I58': 'Downlaod audio cache',
    'I59': 'Cache and Play',
    'I60': '',
    'I61': '',
    'I62': '',
    'I63': 'Audio recording complete',
    'I64': 'Uploading Audio file',
    'I65': 'Audio uploaded successfully',
    'E41': 'Software Reset',
    'E01': 'Unknown error',
    'E02': 'Invalid parameters',
    'E03': 'Duplicate file',
    'E04': 'File too big',
    'E05': 'Empty file',
    'E06': 'File too small',
    'E07': 'Server error when saving file',
    'E08': 'Error moving file',
    'E09': 'Error updating database',
    'E10': 'Error creating directory',
    'BI01': 'Starting Boondock Echo',
    'BI02': 'Online',
    'BI03': 'Firmware update requested',
    'BI04': 'Restarting',
    'BI05': 'Updating firmware',
    'BI06': 'Saving configurations',
    'BI07': 'Recording is enabled',
    'BI08': 'Recording is disabled',
    'BI09': 'You have new messages',
    'BI10': 'No more new messages',
    'BI11': 'Begin message playback',
    'BI12': 'End message playback',
    'BI13': 'Being transmit message',
    'BI14': 'End transmit message',
    'BI15': 'Recording start',
    'BI16': 'Recording end',
    'BI17': 'Mute speaker',
    'BI18': 'Unmute speaker',
    'BI19': '',
    'BI20': 'Registration information updated',
    'BI21': 'Factory reset',
    'BI22': 'Device name updated',
    'BI23': '',
    'BI24': 'PTT is enabled',
    'BI25': 'PTT is disabled',
    'BI26': 'Speaker volume updated',
    'BI27': 'PTT volume updated',
    'BI28': 'Alert volume updated',
    'BI29': 'Timezone updated',
    'BI30': 'Daylight saving updated',
    'BI31': 'License updated',
    'BI32': 'Audio notifications enabled',
    'BI33': 'Audio notifications disabled',
    'BI34': 'Live update enabled',
    'BI35': 'Input gain updated',
    'BI36': 'Min recording updated',
    'BI37': 'Max recording updated',
    'BI38': 'Silence updated',
    'BI39': 'Recorder threshold updated',
    'BI40': 'WiFi SSID and password updated',
    'BI41': 'Static IP setting updated',
    'BI42': 'Static IP updated',
    'BI43': 'Static gateway updated',
    'BI44': 'Primary DNS updated',
    'BI45': 'Secondary DNS updated',
    'BI46': 'SD card',
    'BI47': 'Auto-deleting audio files',
    'BI48': '',
    'BI49': '',
    'BI50': 'Input audio level low',
    'BI51': 'Input audio level high',
    'E800': 'Unknown error',
    'E801': 'No error',
    'E802': 'SD card mount failed',
    'E803': 'No SD card present',
    'E804': 'SD card not formatted',
    'E805': 'Read/Write permission error',
    'E806': 'Unknown SD card error',
    'E807': 'Disk too small',
    'E808': 'Disk too large',
    'E809': 'Invalid file system',
    'E810': 'WiFi connection failed',
    'E811': 'No SSID available',
    'E812': 'WiFi connection lost',
    'E813': 'WiFi disconnected',
    'E814': 'WiFi idle status',
    'E815': 'DNS error',
    'E816': 'SSID missing',
    'E817': 'SSID too long',
    'E818': 'Undefined error E017',
    'E819': 'DNS error when uploading',
    'E820': 'SD card initialization failed',
    'E821': 'HTTP initialization failed',
    'E822': 'HTTP operation failed',
    'E823': 'Not enough space',
    'E824': 'Failed to open file',
    'E825': 'Incomplete file',
    'E826': 'SD card unavailable',
    'E827': 'File open failed',
    'E828': 'File rename failed',
    'E829': 'File read failed',
    'E830': 'File write failed',
    'E831': 'File delete failed',
    'E832': 'File not found',
    'E833': 'Directory creation failed',
    'E834': 'I2S initialization failed',
    'E835': 'Audio playback failed',
    'E836': 'Audio buffer overflow',
    'E837': 'Audio codec initialization failed',
    'E838': 'Server connection failed',
    'E839': 'Audio upload failed',
    'E840': 'Invalid server response',
    'E841': 'MQTT connection failed',
    'E842': 'MQTT subscription failed',
    'E843': 'MQTT publish failed',
    'E844': 'Invalid MQTT message',
    'E845': 'Firmware download failed',
    'E846': 'Firmware verification failed',
    'E847': 'Firmware installation failed',
    'E848': 'OTA begin failed',
    'E849': 'OTA write failed',
    'E850': 'OTA end failed',
    'E851': 'Low heap memory',
    'E852': 'Stack overflow',
    'E853': 'Speaker initialization failed',
    'E854': 'Microphone initialization failed',
    'E855': 'Task creation failed',
    'E856': 'Watchdog timeout',
    'E857': 'System reset',
    'E858': 'Error setting mDNS host',
    'E859': 'Error setting date/time',
    'E860': 'LED initialization failed',
    'E861': 'Keypad initialization failed',
    'E862': 'SD initialization failed',
    'E863': 'SD card full',
    'E864': 'SD write error',
    'E865': 'Configuration initialization error',
    'E866': 'Corrupted configuration',
    'E867': 'I2C initialization failed',
    'E868': 'I2S initialization failed',
    'E869': 'Fatal audio kit error',
    'E870': 'WiFi credentials missing',
    'E871': 'Unknown WiFi error',
    'E872': 'WiFi network unreachable',
    'E873': 'WiFi login failed',
    'E874': 'Poor WiFi signal',
    'E875': 'DNS resolution failed',
    'E876': 'DHCP configuration failed',
    'E877': 'Unknown network error',
    'E878': 'Boondock MQTT initialization failed',
    'E879': 'Boondock server initialization failed',
    'E880': 'Clock initialization failed',
    'E881': 'Main loop initialization failed',
    'E882': 'Audio system check failed',
    'V100': 'Unknown event',
    'V101': 'Starting Boondock',
    'V102': 'Initialization OK',
    'V103': 'Audio uploaded',
    'V104': 'Download Firmware',
    'V105': 'Update Firmware',
    'V106': 'Unused V006',
    'V107': 'Recording active',
    'V108': 'Recording inactive',
    'V109': 'SD card usage',
    'V110': 'User ID changed',
    'V111': 'Dock name changed',
    'V112': 'Remote reboot',
    'V113': 'Unused V013',
    'V114': 'Factory reset',
    'V115': 'Set default settings',
    'V116': 'Unused V016',
    'V117': 'Unused V017',
    'V118': 'Unused V018',
    'V119': 'Online',
    'V120': 'Restarting',
    'V121': 'Updating firmware',
    'V122': 'Saving configurations',
    'V123': 'Begin message playback',
    'V124': 'Begin transmit message',
    'V125': 'Registration info updated',
    'V126': 'License updated',
    'V127': 'Auto deleting audio files',
    'I400': 'Unknown info',
    'I401': 'Speaker mute',
    'I402': 'Speaker unmute',
    'I403': 'PTT activated',
    'I404': 'PTT released',
    'I405': 'PTT recording active',
    'I406': 'PTT recording inactive',
    'I407': 'Playing system audio',
    'I408': 'TX enabled',
    'I409': 'TX disabled',
    'I410': 'Line in minimum dB changed',
    'I411': 'Line in gain changed',
    'I412': 'Play system audio queued',
    'I413': 'Play audio queued',
    'I414': 'Transmit audio queued',
    'I415': 'Min recording size updated',
    'I416': 'Silence duration updated',
    'I417': 'Max recording size updated',
    'I418': 'Speaker volume changed',
    'I419': 'Playback volume changed',
    'I420': 'OTA update changed',
    'I421': 'TX volume changed',
    'I422': 'Start remote recording',
    'I423': 'Stop remote recording',
    'I424': 'Upgrading config file',
    'I425': 'Reboot reason',
    'I426': 'Upload response',
    'I427': 'Playback complete',
    'I428': 'Transmit complete',
    'I429': 'Transmit not allowed',
    'I430': 'Load CDN files',
    'I431': 'Downloading for playback',
    'I432': 'Adding playback queue',
    'I433': 'Upload complete',
    'I434': 'Completed downloading audio',
    'I435': 'Recording upload active',
    'I436': 'Recording upload inactive',
    'I437': 'Start remote mic recording',
    'I438': 'Stop remote mic recording',
    'I439': 'PTT upload active',
    'I440': 'PTT upload inactive',
    'I441': 'Notify all settings',
    'I442': 'Download audio cache',
    'I443': 'Cache and play',
    'I444': 'Audio recording complete',
    'I445': 'Uploading audio file',
    'I446': 'Audio uploaded successfully',
    'I447': 'Unknown error',
    'I448': 'Invalid parameters',
    'I449': 'Duplicate file',
    'I450': 'File too big',
    'I451': 'Empty file',
    'I452': 'File too small',
    'I453': 'Server error saving file',
    'I454': 'Error moving file',
    'I455': 'Error updating database',
    'I456': 'Error creating directory',
    'I457': 'Firmware update requested',
    'I458': 'Recording enabled',
    'I459': 'Recording disabled',
    'I460': 'New messages',
    'I461': 'No more new messages',
    'I462': 'End message playback',
    'I463': 'End transmit message',
    'I464': 'Recording start',
    'I465': 'Recording end',
    'I466': 'Mute speaker',
    'I467': 'Unmute speaker',
    'I468': 'Factory reset',
    'I469': 'Device name updated',
    'I470': 'PTT enabled',
    'I471': 'PTT disabled',
    'I472': 'Speaker volume updated',
    'I473': 'PTT volume updated',
    'I474': 'Alert volume updated',
    'I475': 'Timezone updated',
    'I476': 'Daylight saving updated',
    'I477': 'Audio notifications enabled',
    'I478': 'Audio notifications disabled',
    'I479': 'Live update enabled',
    'I480': 'Input gain updated',
    'I481': 'Min recording updated',
    'I482': 'Max recording updated',
    'I483': 'Silence updated',
    'I484': 'Recorder threshold updated',
    'I485': 'WiFi SSID and password updated',
    'I486': 'Static IP setting updated',
    'I487': 'Static IP updated',
    'I488': 'Static gateway updated',
    'I489': 'Primary DNS updated',
    'I490': 'Secondary DNS updated',
    'I491': 'SD card',
    'I492': 'Input audio level low',
    'I493': 'Input audio level high',
    'I494': 'Online',
}


def _mac_hex_only(mac_str):
    if not mac_str:
        return ""
    return re.sub(r"[^0-9A-Fa-f]", "", str(mac_str).strip()).upper()


def _normalize_cloud_mac_address(mac_str):
    """Return AA:BB:CC:DD:EE:FF or None if invalid."""
    hx = _mac_hex_only(mac_str)
    if len(hx) != 12:
        return None
    return ":".join(hx[i : i + 2] for i in range(0, 12, 2))


def _handle_cloud_style_device_event(data):
    """Cloud API: JSON body mac_address, event_type, optional event_data (DEVICE_API.md)."""
    mac_colon = _normalize_cloud_mac_address(data.get("mac_address") or "")
    if not mac_colon:
        return jsonify({"error": "Missing required fields"}), 400

    event_type = (data.get("event_type") or "").strip()
    if not event_type:
        return jsonify({"error": "Missing required fields"}), 400

    event_type_id = cloud_event_type_id(event_type)
    event_data = data.get("event_data")
    if event_data is not None and not isinstance(event_data, dict):
        event_data = {"value": event_data}

    load_tokens()
    auth_header = request.headers.get("Authorization")
    token = (
        auth_header.split("Bearer ")[1]
        if auth_header and auth_header.startswith("Bearer ")
        else None
    )
    warning = None
    new_token = None
    expires_at = None

    if not is_mac_registered(mac_colon):
        new_token, expires_at = generate_token(mac_colon)
        warning = "MAC address registered"

    token_mac = get_mac_for_token(token) if token else None
    token_hex = _mac_hex_only(token_mac) if token_mac else ""
    token_valid = bool(
        token_hex
        and token_hex == _mac_hex_only(mac_colon)
        and is_token_valid(token)
    )
    if not token_valid:
        if not new_token:
            new_token, expires_at = generate_token(mac_colon)
        warning = "Invalid token" if not warning else warning + "; Invalid token"

    mac_key = normalize_mac_address(mac_colon)
    if len(mac_key) != 12:
        return jsonify({"error": "Invalid MAC address"}), 400

    persist_cloud_device_event_async(
        mac_key, event_type_id, event_type, event_data
    )

    touch_device_activity(mac_key)
    if get_channel_id_from_mac(mac_key, refresh=False) is None:
        new_id = create_channel_for_mac(mac_key)
        if new_id is not None:
            track_device_created(mac_key)

    et = event_type.strip().lower()
    if et == "online":
        track_connection(mac_key)
        track_event(mac_key)
        set_channel_visual_state(mac_key, "online")
    elif et == "ping":
        track_connection(mac_key)
        track_event(mac_key)
        if get_stored_visual_state(mac_key) != ChannelVisualState.RECORDING:
            set_channel_visual_state(mac_key, "online")
    elif et == "record_begin":
        track_event(mac_key)
        set_channel_visual_state(mac_key, ChannelVisualState.RECORDING)
    elif et == "record_end":
        track_event(mac_key)
        set_channel_visual_state(mac_key, ChannelVisualState.IDLE)
    elif et == "warning":
        track_event(mac_key)
        set_channel_visual_state(mac_key, ChannelVisualState.WARNING)
    elif et in ("error", "fatal_error"):
        track_event(mac_key)
        track_error(mac_key)
        set_channel_visual_state(mac_key, ChannelVisualState.ERROR)
    else:
        track_event(mac_key)

    ts = datetime.now(timezone.utc).isoformat()
    body = {"message": "Event received", "timestamp": ts}
    if warning:
        body["warning"] = warning
    if new_token:
        body["new_token"] = new_token
        body["expires_at"] = expires_at
    return jsonify(body), 200


@device_bp.route('/event', methods=['POST'])
@swag_from({
    'tags': ['Events'],
    'summary': 'Handle device event (legacy endpoint)',
    'description': 'Legacy endpoint for device events. Use /api/v1/events instead.',
    'parameters': [
        {
            'name': 'mac',
            'in': 'query',
            'type': 'string',
            'required': True,
            'description': 'MAC address'
        },
        {
            'name': 'body',
            'in': 'body',
            'required': False,
            'schema': {
                'type': 'object',
                'description': 'Optional event data',
                'example': {
                    'tag': 'john'
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Event processed successfully'},
        '201': {'description': 'New channel created'},
        '400': {'description': 'Bad request'},
        '500': {'description': 'Server error'}
    }
})
def handle_event_legacy():
    """Legacy endpoint for device events. Same as POST /api/v1/events?mac=…"""
    return _handle_legacy_events_post()


@device_bp.route('/v1/events', methods=['POST'])
@swag_from({
    'tags': ['Events'],
    'summary': 'Handle device event (legacy ?mac= or cloud JSON body)',
    'description': (
        'Legacy Edge: POST with query mac= and optional JSON for channel updates. '
        'Cloud-style: POST application/json with mac_address, event_type, optional event_data.'
    ),
    'parameters': [
        {
            'name': 'mac',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'MAC address (legacy Edge firmware)',
        },
        {
            'name': 'body',
            'in': 'body',
            'required': False,
            'schema': {
                'oneOf': [
                    {
                        'type': 'object',
                        'description': 'Legacy optional channel fields',
                        'example': {'tag': 'john'},
                    },
                    {
                        'type': 'object',
                        'required': ['mac_address', 'event_type'],
                        'properties': {
                            'mac_address': {'type': 'string'},
                            'event_type': {'type': 'string'},
                            'event_data': {'type': 'object'},
                        },
                    },
                ],
            },
        },
    ],
    'responses': {
        '200': {'description': 'OK — channel or cloud event'},
        '201': {'description': 'New channel created (legacy)'},
        '400': {'description': 'Bad request'},
        '500': {'description': 'Server error'},
    },
})
def post_v1_events():
    """Dispatch legacy Edge (?mac=) vs cloud-style JSON lifecycle events."""
    if request.args.get("mac"):
        return _handle_legacy_events_post()
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        ma = (data.get("mac_address") or "").strip()
        et = (data.get("event_type") or "").strip()
        if ma and et:
            try:
                return _handle_cloud_style_device_event(data)
            except Exception:
                logging.exception("cloud-style device event")
                return (
                    jsonify(
                        {"error": "An error occurred processing your request"}
                    ),
                    500,
                )
        if len(data) > 0:
            return jsonify({"error": "Missing required fields"}), 400
    if request.data:
        return jsonify({"error": "Missing required fields"}), 400
    return (
        jsonify({"error": "MAC address is required in query parameters"}),
        400,
    )


def _handle_legacy_events_post():
    """Handle device events and manage channel configuration (Edge ?mac=)."""
    try:
        # Extract MAC address from query parameters
        mac = request.args.get('mac')
        if not mac:
          # error_logger.error("MAC address is missing in query parameters.")
            return jsonify({'error': 'MAC address is required in query parameters'}), 400

        mac = mac.upper()

        # Parse JSON body (optional)
        data = request.get_json(silent=True)

        # Check if channel exists by querying database directly
        existing_channel_id = get_channel_id_from_mac(mac, refresh=False)
        existing_channel = None
        if existing_channel_id:
            existing_channel = _settings_manager.get_channel(existing_channel_id)
        
        # Default channel attributes
        default_channel = {
            "id": None,
            "mac": mac,
            "name": None,
            "status": "enabled",
            "model": "tiny.en",
            "src_language": "english",
            "threshold": "50",
            "silence": "1000",
            "min_rec": "1000",
            "max_rec": "30000",
            "audio_gain": "3",
            "color": "#000000",
            "background_color": "#ffffff",
            "team_color": "#b54f4f",
            "target_language": "english",
            "driver": "driver name",
            "person": "person name",
            "tag": "tag",
            "car": "car name",
            "audio_stream_enabled": False,
            "speaker_enabled": False,
            "speaker_volume": 50,
            "state": "resume"
        }

        # If no JSON body is provided (ping request)
        if data is None:
            # Track connection
            track_connection(mac)
            
            if existing_channel:
                event_logger.info(f"Ping received for MAC: {mac}. Returning latest channel data.")
                response_data = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": "Channel exists",
                    "channel": {
                        "state": "stop" if existing_channel.get("status") == "disabled" else existing_channel.get("state", "resume"),
                        "threshold": existing_channel.get("threshold"),
                        "silence": existing_channel.get("silence"),
                        "min_rec": existing_channel.get("min_rec"),
                        "max_rec": existing_channel.get("max_rec"),
                        "audio_gain": existing_channel.get("audio_gain"),
                        "stream": 1 if existing_channel.get("audio_stream_enabled", False) else 0,
                        "speaker": existing_channel.get("speaker_enabled", False),
                        "volume": existing_channel.get("speaker_volume", 50),
                    }
                }
                if existing_channel.get("audio_stream_enabled") and existing_channel.get("audio_stream_port"):
                    response_data["channel"]["port"] = existing_channel.get("audio_stream_port")
                return jsonify(response_data), 200
            else:
                # Create a new channel if none exists - call OUTSIDE the lock to avoid deadlock
                new_channel_id = create_channel_for_mac(mac)
                if new_channel_id is None:
                    error_logger.error(f"Failed to create channel for MAC: {mac} in event handler")
                    return jsonify({"error": "Failed to create channel"}), 500
                
                # Track device creation
                track_device_created(mac)
                
                # Get the newly created channel from database
                existing_channel = _settings_manager.get_channel(new_channel_id)
                if not existing_channel:
                    error_logger.error(f"Channel {new_channel_id} was created but not found in database")
                    return jsonify({"error": "Channel creation failed"}), 500
                
                event_logger.info(f"Ping received for MAC: {mac}. No channel found, created new channel: {new_channel_id}")
                response_data = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": "New channel created due to ping request",
                    "channel": {
                        "state": existing_channel.get("state", "resume"),
                        "threshold": existing_channel.get("threshold", "50"),
                        "silence": existing_channel.get("silence", "1000"),
                        "min_rec": existing_channel.get("min_rec", "1000"),
                        "max_rec": existing_channel.get("max_rec", "30000"),
                        "audio_gain": existing_channel.get("audio_gain", "3"),
                        "stream": 1 if existing_channel.get("audio_stream_enabled", False) else 0,
                        "speaker": existing_channel.get("speaker_enabled", False),
                        "volume": existing_channel.get("speaker_volume", 50),
                    }
                }
                if existing_channel.get("audio_stream_enabled") and existing_channel.get("audio_stream_port"):
                    response_data["channel"]["port"] = existing_channel.get("audio_stream_port")
                return jsonify(response_data), 201

        # If JSON body is provided and channel exists, update only if changes are needed
        if existing_channel:
                # Track connection and event
                track_connection(mac)
                track_event(mac)
                
                # Check for visual state event_code first (before persisting to channels.json)
                event_code = data.get('event_code')
                if event_code:
                    # Track errors if event code starts with 'E'
                    if event_code.startswith('E'):
                        track_error(mac)
                    
                    # Map event codes to visual states
                    if event_code in ['I07', 'I11', 'I51', 'I55', 'I63', 'I64', 'I65']:
                        # Recording/upload related events = recording state
                        set_channel_visual_state(mac, ChannelVisualState.RECORDING)
                    elif event_code in ['I08', 'I12', 'I52', 'I56']:
                        # Idle/inactive events = idle state
                        set_channel_visual_state(mac, ChannelVisualState.IDLE)
                    elif event_code.startswith('E'):
                        # Error events (E01-E10, etc) = error state
                        set_channel_visual_state(mac, ChannelVisualState.ERROR)
                    elif event_code in ['I05', 'I06', 'I15', 'I16']:
                        # Warning-type events = warning state
                        set_channel_visual_state(mac, ChannelVisualState.WARNING)
                    
                    event_logger.debug(f"Set visual state for MAC {mac} based on event_code {event_code}")
                
                # Check for changes
                changes_needed = False
                updated_fields = {}
                for key, value in data.items():
                    if key in existing_channel and existing_channel[key] != value:
                        changes_needed = True
                        updated_fields[key] = value

                if changes_needed:
                    # Update existing channel with provided data
                    existing_channel.update(updated_fields)
                    # Save updates using SettingsManager
                    _settings_manager.save_channel(existing_channel)

                    event_logger.info(f"Updated channel for MAC: {mac}. Changes: {updated_fields}")
                else:
                    event_logger.debug(f"No changes needed for MAC: {mac}. Current data matches: {data}")

                response_data = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": "Channel processed successfully" if changes_needed else "No update needed",
                    "channel": {
                        "state": "stop" if existing_channel.get("status") == "disabled" else existing_channel.get("state", "resume"),
                        "threshold": existing_channel.get("threshold"),
                        "silence": existing_channel.get("silence"),
                        "min_rec": existing_channel.get("min_rec"),
                        "max_rec": existing_channel.get("max_rec"),
                        "audio_gain": existing_channel.get("audio_gain"),
                        "stream": 1 if existing_channel.get("audio_stream_enabled", False) else 0,
                        "speaker": existing_channel.get("speaker_enabled", False),
                        "volume": existing_channel.get("speaker_volume", 50),
                    }
                }
                if existing_channel.get("audio_stream_enabled") and existing_channel.get("audio_stream_port"):
                    response_data["channel"]["port"] = existing_channel.get("audio_stream_port")
                return jsonify(response_data), 200
        else:
            # If MAC is new and JSON body is provided, create a new channel using the standard function
            new_channel_id = create_channel_for_mac(mac)
        if new_channel_id is None:
            error_logger.error(f"Failed to create channel for MAC: {mac} in event handler")
            return jsonify({"error": "Failed to create channel"}), 500
        
        # Track device creation, connection, and event
        track_device_created(mac)
        track_connection(mac)
        track_event(mac)
        
        # Check for error events
        event_code = data.get('event_code') if data else None
        if event_code and event_code.startswith('E'):
            track_error(mac)
        
        # Get the newly created channel from database
        existing_channel = _settings_manager.get_channel(new_channel_id)
        if not existing_channel:
            error_logger.error(f"Channel {new_channel_id} was created but not found in database")
            return jsonify({"error": "Channel creation failed"}), 500
        
        # Update the channel with any additional data from the request
        if data:
            existing_channel.update(data)
            _settings_manager.save_channel(existing_channel)
        
        event_logger.info(f"New channel added for MAC: {mac}. Channel ID: {new_channel_id}")
        response_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": "New channel added successfully",
            "channel": {
                "state": existing_channel.get("state", "resume"),
                "threshold": existing_channel.get("threshold", "50"),
                "silence": existing_channel.get("silence", "1000"),
                "min_rec": existing_channel.get("min_rec", "1000"),
                "max_rec": existing_channel.get("max_rec", "30000"),
                "audio_gain": existing_channel.get("audio_gain", "3"),
                "stream": 1 if existing_channel.get("audio_stream_enabled", False) else 0,
                "speaker": existing_channel.get("speaker_enabled", False),
                "volume": existing_channel.get("speaker_volume", 50),
            }
        }
        if existing_channel.get("audio_stream_enabled") and existing_channel.get("audio_stream_port"):
            response_data["channel"]["port"] = existing_channel.get("audio_stream_port")
        return jsonify(response_data), 201

    except Exception as e:
        error_logger.error(f"Error in handle_event: {str(e)}")
        # Track error if we have a MAC address
        mac = request.args.get('mac')
        if mac:
            track_error(mac.upper())
        return jsonify({"error": "Internal server error"}), 500


@device_bp.route('/v1/events', methods=['GET'])
@swag_from({
    'tags': ['Events'],
    'summary': 'Get event code mapping',
    'description': 'Returns a mapping of all event codes to their descriptions',
    'responses': {
        '200': {
            'description': 'Event code mapping retrieved successfully',
            'schema': {
                'type': 'object',
                'additionalProperties': {
                    'type': 'string'
                },
                'example': {
                    'I01': 'Starting in normal mode',
                    'I02': 'Boondock ready and listening',
                    'E01': 'Unknown error',
                    'BI01': 'Starting Boondock Echo',
                    'BI02': 'Online'
                }
            }
        }
    }
})
def get_event_codes():
    """Get event code mapping for Boondock devices."""
    # Filter out empty descriptions for cleaner response
    filtered_mapping = {code: desc for code, desc in EVENT_CODE_MAPPING.items() if desc}
    return jsonify({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_codes": filtered_mapping
    }), 200


@device_bp.route('/v1/channel-visual-states', methods=['GET'])
@swag_from({
    'tags': ['Events'],
    'summary': 'Get channel visual states',
    'description': 'Returns in-memory visual states for all channels (recording, idle, error, warning)',
    'responses': {
        '200': {
            'description': 'Visual states retrieved successfully',
            'schema': {
                'type': 'object',
                'additionalProperties': {
                    'type': 'object',
                    'properties': {
                        'state': {
                            'type': 'string',
                            'enum': ['idle', 'recording', 'error', 'warning']
                        },
                        'timestamp': {'type': 'string'}
                    }
                },
                'example': {
                    'B8D61A5AC6C0': {
                        'state': 'recording',
                        'timestamp': '2025-12-26T14:30:45.123456'
                    }
                }
            }
        }
    }
})
def get_channel_visual_states():
    """Get visual states for all channels in memory."""
    try:
        states = get_all_channel_visual_states()
        return jsonify(states), 200
    except Exception as e:
        error_logger.error(f"Error fetching channel visual states: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500


@device_bp.route('/v1/channel-visual-states/<mac>', methods=['GET'])
@swag_from({
    'tags': ['Events'],
    'summary': 'Get visual state for a specific channel',
    'parameters': [
        {
            'name': 'mac',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'MAC address of the channel'
        }
    ],
    'responses': {
        '200': {'description': 'Visual state retrieved successfully'},
        '404': {'description': 'Channel has no visual state'}
    }
})
def get_channel_visual_state_by_mac(mac):
    """Get visual state for a specific channel by MAC address."""
    try:
        state = get_channel_visual_state(mac)
        if state is None:
            return jsonify({"state": None, "message": "No visual state set for this channel"}), 200
        
        return jsonify({"mac": normalize_mac_address(mac), "state": state}), 200
    except Exception as e:
        error_logger.error(f"Error fetching visual state for {mac}: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500


def _device_logs_root(mac_key: str) -> str:
    return DATA_ROOT / "logs" / mac_key.lower()


@device_bp.route('/v1/devices/<mac>/logs/files', methods=['GET'])
def device_logs_list_files(mac):
    """List uploaded device log files under logs/<mac>/."""
    mac_key = normalize_mac_address(mac)
    if len(mac_key) != 12:
        return jsonify({"error": "Invalid MAC address"}), 400
    root = _device_logs_root(mac_key)
    if not os.path.isdir(root):
        return jsonify({"files": [], "mac": mac_key}), 200
    files = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            low = fn.lower()
            if not low.endswith((".log", ".txt", ".json")):
                continue
            full = os.path.join(dirpath, fn)
            try:
                rel = os.path.relpath(full, root).replace("\\", "/")
            except ValueError:
                continue
            if ".." in rel:
                continue
            try:
                st = os.stat(full)
                files.append(
                    {
                        "path": rel,
                        "size": st.st_size,
                        "modified": datetime.fromtimestamp(
                            st.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    }
                )
            except OSError:
                continue
    files.sort(key=lambda x: x["path"], reverse=True)
    return jsonify({"files": files, "mac": mac_key}), 200


@device_bp.route('/v1/devices/<mac>/logs/content', methods=['GET'])
def device_logs_content(mac):
    """Return log file text; use ?path= relative path or ?date=YYYY-MM-DD."""
    mac_key = normalize_mac_address(mac)
    if len(mac_key) != 12:
        return jsonify({"error": "Invalid MAC address"}), 400
    root = _device_logs_root(mac_key)
    abs_path = None
    rel_path = request.args.get("path", "").strip().replace("\\", "/")
    date_str = request.args.get("date", "").strip()

    if rel_path:
        if ".." in rel_path or rel_path.startswith("/"):
            return jsonify({"error": "Invalid path"}), 400
        cand = os.path.abspath(os.path.join(root, rel_path))
        if not cand.startswith(root + os.sep) and cand != root:
            return jsonify({"error": "Invalid path"}), 400
        if os.path.isfile(cand):
            abs_path = cand
    elif date_str and re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        y, m, _ = date_str.split("-")
        for ext in (".log", ".txt", ".json"):
            cand = os.path.join(root, y, m, f"{date_str}{ext}")
            if os.path.isfile(cand):
                abs_path = os.path.abspath(cand)
                break
    else:
        return jsonify({"error": "Provide path= or date=YYYY-MM-DD"}), 400

    if not abs_path or not os.path.isfile(abs_path):
        return jsonify({"error": "File not found"}), 404

    max_bytes = 512 * 1024
    try:
        sz = os.path.getsize(abs_path)
        with open(abs_path, "rb") as f:
            if sz > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
                raw = f.read()
                truncated = True
            else:
                raw = f.read()
                truncated = False
        text = raw.decode("utf-8", errors="replace")
        return jsonify(
            {
                "content": text,
                "truncated": truncated,
                "path": os.path.relpath(abs_path, root).replace("\\", "/"),
                "mac": mac_key,
            }
        ), 200
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@device_bp.route('/v1/devices/<mac>/events', methods=['GET'])
def device_cloud_events_list(mac):
    """Recent cloud_device_events for this MAC."""
    mac_key = normalize_mac_address(mac)
    if len(mac_key) != 12:
        return jsonify({"error": "Invalid MAC address"}), 400
    try:
        limit = int(request.args.get("limit", 100))
    except ValueError:
        limit = 100
    types_param = request.args.get("types", "").strip()
    type_list = (
        [x.strip().lower() for x in types_param.split(",") if x.strip()]
        if types_param
        else None
    )
    events = list_cloud_events_for_mac(mac_key, limit=limit, event_types=type_list)
    return jsonify({"events": events, "mac": mac_key}), 200


@device_bp.route('/v1/audio/s3', methods=['POST'])
@device_bp.route('/v2/audio/s3', methods=['POST'])
@swag_from({
    'tags': ['Audio'],
    'summary': 'Upload an audio file to S3 (v1: default WAV; v2: default MP3)',
    'security': [{'BearerAuth': []}],
    'consumes': ['multipart/form-data'],
    'parameters': [
        {
            'name': 'Authorization',
            'in': 'header',
            'type': 'string',
            'required': False,
            'description': 'Bearer token for authentication (e.g., Bearer your-token)'
        },
        {
            'name': 'mac_address',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'Device MAC address'
        },
        {
            'name': 'audio_file',
            'in': 'formData',
            'type': 'file',
            'required': True,
            'description': 'WAV file to upload'
        },
        {
            'name': 'convert_to_mp3',
            'in': 'formData',
            'type': 'boolean',
            'required': False,
            'description': 'Convert WAV → MP3 before upload',
            'default': False
        },
        {
            'name': 'tags',
            'in': 'formData',
            'type': 'string',
            'required': False,
            'description': 'JSON containing "recorder", "dock", "user" objects that will be stored as S3 tags',
            'example': '{"recorder":{"id":"rec-01"},"dock":{"id":"dock-A"},"user":{"name":"Alice"}}'
        },
        {
            'name': 'timestamp',
            'in': 'formData',
            'type': 'string',
            'format': 'date-time',
            'required': False,
            'description': 'ISO-8601 time the clip was recorded'
        }
    ],
    'responses': {
        '200': {
            'description': 'Audio uploaded successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'},
                    'timestamp': {'type': 'string', 'format': 'date-time'},
                    'warning': {'type': 'string'},
                    'new_token': {'type': 'string'},
                    'expires_at': {'type': 'string', 'format': 'date-time'}
                },
                'required': ['message', 'timestamp']
            }
        },
        '400': {'description': 'Bad request'},
        '500': {'description': 'Server error during upload or bucket creation'}
    }
})
def upload_audio_s3():
    """Upload audio files from Boondock devices to iDrive storage"""
    # Registering a response callback here ensures early validation returns are
    # timed as well as the complete upload path for both routes handled by this
    # function.
    is_v2_audio = request.path.rstrip("/").endswith("/api/v2/audio/s3")
    request_started_at = time.perf_counter()
    step_started_at = request_started_at
    previous_log_duration_ms = 0.0
    request_size = request.content_length

    @after_this_request
    def log_audio_performance(response):
        duration_ms = (time.perf_counter() - request_started_at) * 1000
        try:
            filename = request.files["audio_file"].filename
        except Exception:
            filename = 'Error'
            pass

        logging.info(
            "audio_s3_performance file=%s status_code=%s "
            "duration_ms=%.2f request_bytes=%s response_bytes=%s",
            filename,
            response.status_code,
            duration_ms,
            request_size if request_size is not None else "unknown",
            response.content_length
            if response.content_length is not None
            else "unknown",
        )
        return response

    def log_audio_step(step):
        """Log both the current step and cumulative request duration."""
        try:
            filename = request.files["audio_file"].filename
        except Exception:
            filename = 'Error'
            pass

        nonlocal previous_log_duration_ms, step_started_at
        now = time.perf_counter()
        logging.info(
            "audio_s3_step_performance file=%s step=%s "
            "step_duration_ms=%.2f total_duration_ms=%.2f "
            "previous_log_duration_ms=%.2f",
            filename,
            step,
            (now - step_started_at) * 1000,
            (now - request_started_at) * 1000,
            previous_log_duration_ms,
        )
        # Start the next step after the log record has been emitted. Logging can
        # block on the configured handler (for example journald), and charging
        # that delay to the next application step produces misleading timings.
        log_completed_at = time.perf_counter()
        previous_log_duration_ms = (log_completed_at - now) * 1000
        step_started_at = log_completed_at

    def log_audio_request():
        """Log request metadata without exposing credentials or file contents."""
        redacted_headers = {"authorization", "cookie", "proxy-authorization"}
        headers = {
            key: "[REDACTED]" if key.lower() in redacted_headers else value
            for key, value in request.headers.items()
        }
        sensitive_form_fields = {"access_token", "api_key", "password", "secret", "token"}
        form = {
            key: ["[REDACTED]"] if key.lower() in sensitive_form_fields else values
            for key, values in request.form.to_dict(flat=False).items()
        }
        files = {
            key: [
                {
                    "filename": uploaded_file.filename,
                    "content_type": uploaded_file.content_type,
                    "content_length": uploaded_file.content_length,
                }
                for uploaded_file in uploaded_files
            ]
            for key, uploaded_files in request.files.lists()
        }
        request_details = {
            "method": request.method,
            "path": request.path,
            "query": request.args.to_dict(flat=False),
            "headers": headers,
            "content_type": request.content_type,
            "content_length": request.content_length,
            "form": form,
            "files": files,
            "remote_addr": request.remote_addr,
        }
        logging.info(
            "audio_s3_request request=%s",
            json.dumps(request_details, sort_keys=True, default=str),
        )

    load_tokens()
    log_audio_step("initialization")

    # 1. ---- Parse auth header --------------------------------------------------
    auth_header = request.headers.get("Authorization")
    token = None
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split("Bearer ")[1]
    elif auth_header:
        logging.warning("Authorization header present but missing 'Bearer ' prefix")
    log_audio_step("auth_header")

    # 2. ---- Validate form data -------------------------------------------------
    # Wrap form data access in try-except to handle connection errors gracefully
    try:
        if "mac_address" not in request.form or "audio_file" not in request.files:
            logging.warning("Missing mac_address or audio_file in request")
            log_audio_request()
            log_audio_step("form_parsing")
            return (
                jsonify({"error": "Missing required fields (mac_address and audio_file)"}),
                400,
            )
        
        mac_address = request.form["mac_address"]
        audio_file = request.files["audio_file"]
        log_audio_request()
        log_audio_step("form_parsing")
    except (OSError, ConnectionResetError, ConnectionError) as e:
        # Handle cases where client disconnects before full request is received
        error_msg = str(e)
        if "unexpected end of file" in error_msg.lower() or "connection reset" in error_msg.lower():
            logging.warning(f"Client disconnected during upload: {error_msg}")
            log_audio_step("form_parsing")
            return (
                jsonify({"error": "Upload interrupted - connection closed by client"}),
                499,  # 499 Client Closed Request (non-standard but appropriate)
            )
        else:
            logging.error(f"Connection error during form data parsing: {error_msg}")
            log_audio_step("form_parsing")
            return (
                jsonify({"error": "Connection error during upload"}),
                400,
            )
    except Exception as e:
        # Catch any other unexpected errors during form parsing
        logging.error(f"Unexpected error during form data parsing: {str(e)}")
        log_audio_step("form_parsing")
        return (
            jsonify({"error": "Failed to parse request data"}),
            400,
        )

    # v2 path defaults to MP3 when convert_to_mp3 omitted (cloud API); v1 defaults to WAV
    if "convert_to_mp3" in request.form:
        convert_to_mp3 = request.form.get("convert_to_mp3", "false").lower() in [
            "true",
            "1",
            "yes",
        ]
    else:
        convert_to_mp3 = bool(is_v2_audio)

    tags_param = request.form.get("tags")
    timestamp_str = request.form.get("timestamp", "").strip()
    
    # ── Tags (unchanged) --------------------------------------------------------
    tags_metadata = None
    tagging_header = None

    if tags_param:
        try:
            tags_dict = json.loads(tags_param)
            if not isinstance(tags_dict, dict):
                raise ValueError("tags must be a JSON object")

            tags_metadata = json.dumps(tags_dict)

            tag_groups = {}
            for group in ["recorder", "dock", "user"]:
                if group in tags_dict and isinstance(tags_dict[group], dict):
                    tag_groups[group] = json.dumps(tags_dict[group])
            if tag_groups:
                tagging_header = urlencode(tag_groups)
        except (json.JSONDecodeError, ValueError):
            logging.warning("Invalid tags JSON")
            log_audio_step("upload_metadata")
            return jsonify({"error": "Invalid tags JSON"}), 400

    # ── Timestamp (unchanged) ---------------------------------------------------
    if timestamp_str:
        try:
            utc_now = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            if utc_now.tzinfo is None:
                utc_now = pytz.UTC.localize(utc_now)
        except Exception:
            logging.warning("Invalid timestamp format: %s", timestamp_str)
            log_audio_step("upload_metadata")
            return jsonify({"error": "Invalid timestamp format; use ISO 8601"}), 400
    else:
        utc_now = datetime.now(pytz.UTC)
    log_audio_step("upload_metadata")

    # 3. ---- Token-vs-MAC validation (unchanged) --------------------------------
    warning = None
    new_token = None
    expires_at = None

    if not is_mac_registered(mac_address):
        new_token, expires_at = generate_token(mac_address)
        warning = "MAC address registered"

    token_valid = (
        token
        and token in VALID_TOKENS
        and is_token_valid(token)
        and VALID_TOKENS[token]["mac_address"] == mac_address
    )
    logging.debug("token_valid=%s", token_valid)

    if not token_valid:
        if not new_token:
            new_token, expires_at = generate_token(mac_address)
        warning = "Invalid token" if not warning else warning + "; Invalid token"

    logging.debug(
        "Token validation result for %s: %s. New token issued: %s",
        mac_address,
        token_valid,
        bool(new_token),
    )
    log_audio_step("token_validation")

    if audio_file.filename == "":
        logging.warning("Empty filename in upload")
        log_audio_step("file_validation")
        return jsonify({"error": "No file selected"}), 400

    uploaded_filename = secure_filename(audio_file.filename)
    if not uploaded_filename:
        logging.warning("Invalid filename in upload")
        log_audio_step("file_validation")
        return jsonify({"error": "Invalid filename"}), 400
    if not allowed_file(uploaded_filename, {"wav"}):
        logging.warning("Unsupported audio file extension: %s", uploaded_filename)
        log_audio_step("file_validation")
        return jsonify({"error": "Unsupported audio file type; expected a WAV file"}), 400
    log_audio_step("file_validation")

    # Get channel_id from MAC address for local storage and database
    channel_id = get_channel_id_from_mac(mac_address.upper(), refresh=True)
    if channel_id is None:
        logging.info(f"Channel ID not found for MAC address: {mac_address}, creating new channel")
        # Create a new channel for this MAC address
        channel_id = create_channel_for_mac(mac_address)
        if channel_id is None:
            logging.warning(f"Failed to create channel for MAC address: {mac_address}, continuing without local save")
        else:
            logging.info(f"Successfully created channel {channel_id} for MAC address: {mac_address}")
    log_audio_step("channel_lookup")

    logging.info("Received file for Channel ID: %s for MAC: %s with timestamp: %s", channel_id, mac_address, utc_now.isoformat())

    # 4. ---- Build S3 key path --------------------------------------------------
    # Get bucket name from settings (may be blank when S3 is intentionally disabled)
    s3_settings = get_s3_settings()
    bucket = s3_settings.get('bucket_name', '')

    # Use MAC address as folder prefix in the bucket. The final object name is
    # assigned after local collision handling so local and S3 names stay aligned.
    mac_folder = mac_address.lower()
    ext = "mp3" if convert_to_mp3 else "wav"
    filename = uploaded_filename

    # Prepare for local save and S3 upload
    local_file_saved = False
    relative_path = None
    absolute_path = None
    recording_id = None
    is_duplicate = False
    crc_value = None
    log_audio_step("s3_configuration")

    # 5. ---- Save locally and upload to S3 --------------------------------------
    try:
        # Save file locally if channel_id is available
        if channel_id is not None:
            try:
                # Read bytes once for filename/size duplicate check
                audio_file.seek(0)
                file_bytes = audio_file.read()
                audio_file.seek(0)
                crc_name = (audio_file.filename or "").strip() or "upload.wav"
                crc_result = check_and_update_duplicate_cache(file_bytes, channel_id, crc_name)
                is_duplicate = bool(crc_result.get("is_duplicate", False))
                crc_value = crc_result.get("crc")
                log_audio_step("duplicate_check")
                if is_duplicate:
                    logging.info(
                        "Duplicate file detected (S3 device upload, within 30 min window) for channel %s: "
                        "filesize=%s, previous=%s",
                        channel_id,
                        len(file_bytes),
                        crc_result.get("previous_timestamp"),
                    )
                # Get MAC address for file path structure
                mac_address_for_path = mac_address.lower() if mac_address else None
                if not mac_address_for_path:
                    # Try to get MAC from channel_id
                    mac_address_for_path = get_mac_from_channel_id(channel_id)
                
                if not mac_address_for_path:
                    logging.warning("MAC address not found for channel_id %s, using fallback path", channel_id)
                    # Fallback to old structure if MAC not found
                    absolute_path = DATA_ROOT / 'recordings' / f'channel_{channel_id}' / f"audio_{utc_now.strftime('%Y%m%d_%H%M%S')}.wav"
                else:
                    # Preserve the upload name; get_recording_path adds a
                    # microsecond suffix if that name already exists.
                    absolute_path = get_recording_path(
                        mac_address_for_path, utc_now, uploaded_filename
                    )
                
                # Create directory for the file if it doesn't exist
                directory_path = absolute_path.parent
                try:
                    directory_path.mkdir(parents=True, exist_ok=True)
                    if not directory_path.exists():
                        raise OSError(f"Failed to create directory: {directory_path}")
                    logging.debug("Directory ensured: %s", directory_path)
                except OSError as e:
                    error_logger.error("Failed to create directory %s: %s", directory_path, str(e))
                    # Continue with S3 upload even if local save fails
                log_audio_step("local_path_setup")
                
                # Save the file locally (always save as WAV for local storage)
                if convert_to_mp3:
                    # If converting to MP3, we need to save the original WAV first
                    # The MP3 conversion happens later for S3
                    audio_file.seek(0)  # Reset file pointer
                    audio_file.save(absolute_path)
                else:
                    audio_file.seek(0)  # Reset file pointer
                    audio_file.save(absolute_path)
                
                local_file_saved = True
                logging.info("File saved locally: %s", absolute_path)
                log_audio_step("local_file_save")
                
                # Calculate file size
                file_size = absolute_path.stat().st_size
                
                # Calculate duration from actual WAV file (more accurate than file size estimation)
                duration = None
                try:
                    import wave
                    with wave.open(str(absolute_path), 'rb') as wav_f:
                        frames = wav_f.getnframes()
                        rate = wav_f.getframerate()
                        duration = frames / float(rate)
                except Exception as e:
                    # Fallback to file size estimation if WAV file can't be read
                    logging.warning("Could not read WAV file for duration calculation, using file size estimation: %s", e)
                    duration = calculate_wav_duration(file_size)
                log_audio_step("audio_duration")
                
                # Create recording entry in database
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()

                db_timestamp = datetime.now(pytz.UTC).strftime('%Y%m%d_%H%M%S')
                relative_path = absolute_path.relative_to(DATA_ROOT).as_posix()
                cursor.execute('''
                    INSERT INTO recordings (channel_id, filename, timestamp, transcription, status, is_duplicate, crc, filesize, duration)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (channel_id, relative_path, db_timestamp, 'No transcription available', 'queued', 1 if is_duplicate else 0, crc_value, file_size, duration))
                
                recording_id = cursor.lastrowid
                conn.commit()
                conn.close()
                logging.info("Database entry created: recording_id=%s", recording_id)
                log_audio_step("database_insert")
                
                # Track file upload - file is saved and database entry created
                track_file_upload(mac_address.upper())
                
            except Exception as local_error:
                error_logger.error(f"Failed to save file locally: {str(local_error)}")
                # Continue with S3 upload even if local save fails
                local_file_saved = False
        log_audio_step("local_persistence")

         # Queue for transcription if file was saved locally (do this BEFORE S3 upload check)
        if local_file_saved and channel_id is not None and relative_path:
            try:
                audio_handler = get_audio_handler()
                success, result = audio_handler.queue_upload_for_processing(
                    relative_path, channel_id, is_duplicate=is_duplicate
                )
                if success:
                    logging.debug("File queued for transcription: %s", relative_path)
                else:
                    logging.warning("Failed to queue file for transcription: %s", result)
            except Exception as queue_error:
                error_logger.error(f"Failed to queue file for transcription: {str(queue_error)}")
                # Don't fail the request if transcription queueing fails
        log_audio_step("transcription_queue")

        # Upload to S3 (only if enabled in settings)
        s3_filename = f"{absolute_path.stem}.{ext}"
        s3_key = f"{mac_folder}/{utc_now:%Y/%m/%d}/{s3_filename}"
        logging.debug("bucket=%s, s3_key=%s", bucket, s3_key)

        if not is_s3_enabled():
            log_audio_step("s3_enabled_check")
            logging.debug("S3 upload is disabled in settings, skipping S3 upload")
            # Use same success message format as when S3 is enabled, so device recognizes it as success
            response = {
                "message": "Audio uploaded successfully",
                "timestamp": utc_now.isoformat(),
            }
            if warning:
                response["warning"] = warning
            if new_token:
                response["new_token"] = new_token
                response["expires_at"] = expires_at
            if local_file_saved and recording_id:
                response["recording_id"] = recording_id
                response["channel_id"] = channel_id
                response["local_file"] = relative_path
                response["local_path"] = relative_path  # Device also checks for this key
                response["is_duplicate"] = is_duplicate
                if crc_value is not None:
                    response["crc"] = crc_value
            log_audio_step("response_build")
            return jsonify(response), 200
        log_audio_step("s3_enabled_check")
        
        # Ensure bucket exists (only once, using the configured bucket name)
        # Wrap S3 operations in try-except to prevent errors from reaching the device
        s3_upload_success = False
        try:
            ensure_bucket_exists(bucket)
        except Exception as s3_bucket_error:
            logging.error(f"S3 bucket check/creation failed: {str(s3_bucket_error)}")
            # Continue without S3 upload, but don't fail the request
        log_audio_step("s3_bucket_check")

        # ── If conversion requested, transcode on-the-fly using ffmpeg ─────────
        if convert_to_mp3:
            # Use the locally saved file if available, otherwise save to temp
            if local_file_saved and absolute_path and os.path.exists(absolute_path):
                wav_source = absolute_path
                temp_wav_created = False
            else:
                # Save to temp file if not already saved locally
                tmp_wav = NamedTemporaryFile(suffix=".wav", delete=False)
                audio_file.seek(0)  # Reset file pointer
                audio_file.save(tmp_wav.name)
                tmp_wav.close()
                wav_source = tmp_wav.name
                temp_wav_created = True
            
            tmp_mp3 = wav_source.replace(".wav", ".mp3")
            try:
                ffmpeg_cmd = [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    wav_source,
                    "-codec:a", "libmp3lame",
                    "-qscale:a", "0",           # highest quality VBR
                    "-ar", "8000",              # preserve original sample rate
                    "-ac", "1",                 # preserve mono channel
                    tmp_mp3,
                ]
                subprocess.run(ffmpeg_cmd, check=True)
                with open(tmp_mp3, "rb") as mp3_file:
                    upload_source = mp3_file.read()
            finally:
                # Only remove temp file if we created it (not the local saved file)
                if temp_wav_created and os.path.exists(wav_source):
                    os.remove(wav_source)
                if os.path.exists(tmp_mp3):
                    os.remove(tmp_mp3)
            upload_source = io.BytesIO(upload_source)
        else:
            # If file was already saved locally, read from there; otherwise use the file object
            if local_file_saved and absolute_path and os.path.exists(absolute_path):
                upload_source = open(absolute_path, 'rb')
            else:
                audio_file.seek(0)  # Reset file pointer
                upload_source = audio_file
        log_audio_step("audio_conversion")

        extra_args = {}
        if tagging_header:
            extra_args["Tagging"] = tagging_header
        # Optionally set ContentType so the browser knows what it is
        extra_args.setdefault(
            "ContentType", "audio/mpeg" if convert_to_mp3 else "audio/wav"
        )

        # Upload to S3 (catch errors to prevent device from receiving S3-related errors)
        try:
            client = get_s3_client()
            if not client:
                logging.warning("S3 client not available - credentials not configured")
            else:
                client.upload_fileobj(upload_source, bucket, s3_key, ExtraArgs=extra_args)
                s3_upload_success = True
                logging.info("Audio uploaded to S3 - Bucket: %s, S3_Key: %s", bucket, s3_key)
        except Exception as s3_upload_error:
            # Log the error but don't fail the request - device should always get success response
            error_logger.error(f"S3 upload failed (will retry in backup): {str(s3_upload_error)}")
            logging.warning(f"S3 upload temporarily failed for {s3_key}, but request succeeded. Error: {str(s3_upload_error)}")
        finally:
            # Close file handle if we opened a local file
            if not convert_to_mp3 and local_file_saved and absolute_path and os.path.exists(absolute_path):
                if hasattr(upload_source, 'close'):
                    upload_source.close()
        log_audio_step("s3_upload")

        response = {
            "message": "Audio uploaded successfully",
            "timestamp": utc_now.isoformat(),
        }
        if warning:
            response["warning"] = warning
        if new_token:
            response["new_token"] = new_token
            response["expires_at"] = expires_at
        if local_file_saved and recording_id:
            response["recording_id"] = recording_id
            response["channel_id"] = channel_id
            response["local_file"] = relative_path
            response["local_path"] = relative_path  # Device also checks for this key
            response["is_duplicate"] = is_duplicate
            if crc_value is not None:
                response["crc"] = crc_value

        log_audio_step("response_build")
        return jsonify(response), 200

    except Exception as exc:
        log_audio_step("error_handling")
        logging.exception("Failed during S3 upload flow: %s", exc)


@device_bp.route('/V1/upload/logs', methods=['POST'])
@device_bp.route('/v1/upload/logs', methods=['POST'])
@swag_from({
    'tags': ['Logs'],
    'summary': 'Upload log files from ESP32 devices',
    'security': [{'BearerAuth': []}],
    'consumes': ['multipart/form-data'],
    'parameters': [
        {
            'name': 'Authorization',
            'in': 'header',
            'type': 'string',
            'required': False,
            'description': 'Bearer token for authentication (e.g., Bearer your-token)'
        },
        {
            'name': 'mac_address',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'Device MAC address'
        },
        {
            'name': 'filename',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'Original filename from device (e.g., 2025-11-16-LOG)'
        },
        {
            'name': 'file',
            'in': 'formData',
            'type': 'file',
            'required': True,
            'description': 'Log file to upload (.txt, .log, or .json)'
        }
    ],
    'responses': {
        '200': {
            'description': 'Log file uploaded successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'},
                    'timestamp': {'type': 'string', 'format': 'date-time'},
                    'file_path': {'type': 'string'},
                    'warning': {'type': 'string'},
                    'new_token': {'type': 'string'},
                    'expires_at': {'type': 'string', 'format': 'date-time'}
                },
                'required': ['message', 'timestamp', 'file_path']
            }
        },
        '400': {'description': 'Bad request - Missing required fields or invalid file type'},
        '500': {'description': 'Internal server error'}
    }
})
def upload_logs():
    """Upload log files from ESP32 devices. Files are stored under /logs/<devicemac>/YYYY/MM/YYYY-MM-DD.log or .txt"""
    load_tokens()  # Ensure latest tokens
    
    # Parse auth header
    auth_header = request.headers.get('Authorization')
    token = auth_header.split('Bearer ')[1] if auth_header and auth_header.startswith('Bearer ') else None
    
    # Validate form data - wrap in try-except to handle connection errors gracefully
    try:
        if 'mac_address' not in request.form or 'filename' not in request.form:
            return jsonify({'error': 'Missing required fields (mac_address and filename)'}), 400
        
        if 'file' not in request.files:
            return jsonify({'error': 'No file part in the request'}), 400
        
        mac_address = request.form['mac_address']
        original_filename = request.form['filename']
        file = request.files['file']
    except (OSError, ConnectionResetError, ConnectionError) as e:
        # Handle cases where client disconnects before full request is received
        error_msg = str(e)
        if "unexpected end of file" in error_msg.lower() or "connection reset" in error_msg.lower():
            logging.warning(f"Client disconnected during log upload: {error_msg}")
            return (
                jsonify({"error": "Upload interrupted - connection closed by client"}),
                499,  # 499 Client Closed Request (non-standard but appropriate)
            )
        else:
            logging.error(f"Connection error during form data parsing: {error_msg}")
            return (
                jsonify({"error": "Connection error during upload"}),
                400,
            )
    except Exception as e:
        # Catch any other unexpected errors during form parsing
        logging.error(f"Unexpected error during form data parsing: {str(e)}")
        return (
            jsonify({"error": "Failed to parse request data"}),
            400,
        )
    
    if file.filename == '':
        return jsonify({'error': 'No file selected for uploading'}), 400
    
    # Validate file extension (cloud API also allows .json)
    allowed_extensions = {'.txt', '.log', '.json'}
    file_ext = os.path.splitext(file.filename)[1].lower()
    if file_ext not in allowed_extensions:
        return jsonify({
            'error': f'Invalid file type. Only .txt, .log, and .json files are allowed. Received: {file_ext}'
        }), 400
    
    # Parse date from filename (format: 2025-11-16-LOG)
    # Extract date part (YYYY-MM-DD) from filename
    try:
        # Try to extract date from filename like "2025-11-16-LOG" or "2025-11-16-LOG.txt"
        date_match = re.match(r'(\d{4}-\d{2}-\d{2})', original_filename)
        if not date_match:
            # If no date in filename, use current UTC time
            utc_now = datetime.now(timezone.utc)
            warning = 'Could not parse date from filename, using current UTC time'
        else:
            date_str = date_match.group(1)
            # Parse the date and create a UTC datetime for that date at midnight
            parsed_date = datetime.strptime(date_str, '%Y-%m-%d')
            # Create timezone-aware datetime in UTC
            utc_now = parsed_date.replace(tzinfo=timezone.utc)
            warning = None
    except ValueError as e:
        # If date parsing fails, use current UTC time
        utc_now = datetime.now(timezone.utc)
        warning = f'Error parsing date from filename: {str(e)}, using current UTC time'
    
    # Normalize MAC address to lowercase
    mac = mac_address.lower() if mac_address else None
    if not mac:
        return jsonify({'error': 'Invalid MAC address'}), 400
    
    # Generate file path: logs/<MAC>/YYYY/MM/YYYY-MM-DD.log or .txt
    year = utc_now.strftime('%Y')
    month = utc_now.strftime('%m')
    date_str = utc_now.strftime('%Y-%m-%d')
    
    # Use the file extension from the uploaded file
    final_filename = f'{date_str}{file_ext}'
    
    # Build relative path: logs/<MAC>/YYYY/MM/YYYY-MM-DD.log or .txt
    relative_path = os.path.join('logs', mac, year, month, final_filename)
    
    # Build absolute path
    absolute_path = DATA_ROOT / relative_path
    
    # Create directory for the file if it doesn't exist
    directory_path = os.path.dirname(absolute_path)
    try:
        os.makedirs(directory_path, exist_ok=True)
    except OSError as e:
        error_logger.error(f"Error creating directory {directory_path}: {str(e)}")
        return jsonify({'error': f'Failed to create directory: {str(e)}'}), 500
    
    # Save the file
    try:
        file.save(absolute_path)
        event_logger.info(f"Log file uploaded: MAC={mac}, File={final_filename}, Path={relative_path}")
    except Exception as e:
        error_logger.error(f"Error saving log file: {str(e)}")
        return jsonify({'error': f'Failed to save file: {str(e)}'}), 500
    
    # Handle token validation (similar to events endpoint)
    warning_msg = warning
    new_token = None
    expires_at = None
    
    if not is_mac_registered(mac_address):
        new_token, expires_at = generate_token(mac_address)
        warning_msg = 'MAC address registered' if not warning_msg else warning_msg + '; MAC address registered'
    
    token_mac = get_mac_for_token(token) if token else None
    token_valid = token_mac == mac_address and is_token_valid(token) if token_mac else False
    if not token_valid:
        if not new_token:
            new_token, expires_at = generate_token(mac_address)
        warning_msg = 'Invalid token' if not warning_msg else warning_msg + '; Invalid token'
    
    timestamp = datetime.now(timezone.utc).isoformat()
    response = {
        'message': 'Log file uploaded successfully',
        'timestamp': timestamp,
        'file_path': relative_path
    }
    
    if warning_msg:
        response['warning'] = warning_msg
    if new_token:
        response['new_token'] = new_token
        response['expires_at'] = expires_at
    
    return jsonify(response), 200

@device_bp.route('/v1/settings', methods=['POST'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Save device settings for Boondock devices',
    'security': [{'BearerAuth': []}],
    'consumes': ['multipart/form-data'],
    'parameters': [
        {
            'name': 'Authorization',
            'in': 'header',
            'type': 'string',
            'required': False,
            'description': 'Bearer token for authentication (e.g., Bearer your-token)'
        },
        {
            'name': 'mac_address',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'Device MAC address'
        },
        {
            'name': 'settings',
            'in': 'formData',
            'type': 'string',
            'required': True,
            'description': 'JSON string containing device settings'
        }
    ],
    'responses': {
        '200': {
            'description': 'Settings saved successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'},
                    'timestamp': {'type': 'string', 'format': 'date-time'},
                    'mac_address': {'type': 'string'},
                    'warning': {'type': 'string'},
                    'new_token': {'type': 'string'},
                    'expires_at': {'type': 'string', 'format': 'date-time'}
                },
                'required': ['message', 'timestamp', 'mac_address']
            }
        },
        '400': {'description': 'Bad request - Missing required fields or invalid JSON'},
        '500': {'description': 'Server error - Failed to save settings'}
    }
})
def save_device_settings():
    """Save device settings for Boondock devices."""
    load_tokens()
    auth_header = request.headers.get('Authorization')
    token = auth_header.split('Bearer ')[1] if auth_header and auth_header.startswith('Bearer ') else None

    if 'mac_address' not in request.form or 'settings' not in request.form:
        return jsonify({'error': 'Missing required fields (mac_address and settings)'}), 400

    mac_address = request.form['mac_address']
    settings_str = request.form['settings']

    warning = None
    new_token = None
    expires_at = None

    if not is_mac_registered(mac_address):
        new_token, expires_at = generate_token(mac_address)
        warning = 'MAC address registered'

    token_mac = get_mac_for_token(token) if token else None
    token_valid = token_mac == mac_address and is_token_valid(token) if token_mac else False
    if not token_valid:
        if not new_token:
            new_token, expires_at = generate_token(mac_address)
        warning = 'Invalid token' if not warning else warning + '; Invalid token'

    try:
        settings = json.loads(settings_str)
    except json.JSONDecodeError:
        return jsonify({'error': 'Invalid JSON format for settings'}), 400

    filepath = os.path.join(DEVICE_SETTINGS_DIR, f"{mac_address}.json")
    try:
        with open(filepath, 'w') as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        return jsonify({'error': f'Failed to save settings: {str(e)}'}), 500

    utc_now = datetime.now(pytz.UTC)
    log_message = f"Settings saved - Device: {mac_address}, Filepath: {filepath}"
    logging.info(log_message)

    response = {
        'message': 'Settings saved successfully',
        'timestamp': utc_now.isoformat(),
        'mac_address': mac_address
    }
    if warning:
        response['warning'] = warning
    if new_token:
        response['new_token'] = new_token
        response['expires_at'] = expires_at

    return jsonify(response), 200


@device_bp.route('/v1/settings/<mac_address>', methods=['GET'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Retrieve device settings for Boondock devices',
    'security': [{'BearerAuth': []}],
    'parameters': [
        {
            'name': 'Authorization',
            'in': 'header',
            'type': 'string',
            'required': False,
            'description': 'Bearer token for authentication (e.g., Bearer your-token)'
        },
        {
            'name': 'mac_address',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Device MAC address'
        }
    ],
    'responses': {
        '200': {
            'description': 'Settings retrieved successfully',
            'schema': {
                'type': 'object',
                'additionalProperties': True,
                'properties': {
                    'warning': {'type': 'string'},
                    'new_token': {'type': 'string'},
                    'expires_at': {'type': 'string', 'format': 'date-time'}
                }
            }
        },
        '404': {'description': 'Settings not found for this device'},
        '500': {'description': 'Server error - Failed to read settings'}
    }
})
def get_device_settings(mac_address):
    """Retrieve device settings for Boondock devices."""
    load_tokens()
    auth_header = request.headers.get('Authorization')
    token = auth_header.split('Bearer ')[1] if auth_header and auth_header.startswith('Bearer ') else None

    warning = None
    new_token = None

    if not is_mac_registered(mac_address):
        new_token, expires_at = generate_token(mac_address)
        warning = 'MAC address registered'

    token_mac = get_mac_for_token(token) if token else None
    token_valid = token_mac == mac_address and is_token_valid(token) if token_mac else False
    if not token_valid:
        if not new_token:
            new_token, expires_at = generate_token(mac_address)
        warning = 'Invalid token' if not warning else warning + '; Invalid token'

    filepath = os.path.join(DEVICE_SETTINGS_DIR, f"{mac_address}.json")
    if not os.path.exists(filepath):
        return jsonify({'error': 'Settings not found for this device'}), 404

    try:
        with open(filepath, 'r') as f:
            settings = json.load(f)
    except Exception as e:
        return jsonify({'error': f'Failed to read settings: {str(e)}'}), 500

    log_message = f"Settings retrieved - Device: {mac_address}, Filepath: {filepath}"
    logging.info(log_message)

    if warning or new_token:
        settings = dict(settings)
        if warning:
            settings['warning'] = warning
        if new_token:
            settings['new_token'] = new_token
            settings['expires_at'] = expires_at

    return jsonify(settings), 200


@device_bp.route('/v1/firmware/check', methods=['GET'])
@swag_from({
    "tags": ["Firmware"],
    "summary": "Check for device firmware upgrade (cloud-style API)",
    "parameters": [
        {
            "name": "current_version",
            "in": "query",
            "type": "string",
            "required": True,
            "description": "Installed firmware version e.g. 1.0.0",
        }
    ],
    "responses": {
        "200": {"description": "Upgrade status"},
        "400": {"description": "Missing current_version"},
    },
})
def firmware_check():
    cv = request.args.get("current_version")
    if not cv or not str(cv).strip():
        return jsonify({"error": "Missing current_version parameter"}), 400
    cv = str(cv).strip()
    try:
        from ..services.firmware_device_service import find_upgrade_for_device

        base = request.url_root.rstrip("/")
        up = find_upgrade_for_device(cv)
        if not up:
            return (
                jsonify(
                    {
                        "upgrade_available": False,
                        "message": "Device is up to date",
                        "download_link": None,
                        "target_version": None,
                    }
                ),
                200,
            )
        fid, ver, desc = up
        link = f"{base}/api/v1/firmware/download/{fid}/firmware.bin"
        return (
            jsonify(
                {
                    "upgrade_available": True,
                    "message": f"Upgrade available to version {ver}",
                    "download_link": link,
                    "description": desc,
                    "target_version": ver,
                }
            ),
            200,
        )
    except Exception:
        logging.exception("firmware check")
        return (
            jsonify({"error": "Unexpected error while checking firmware"}),
            500,
        )


@device_bp.route('/v1/firmware/download/<firmware_id>/<filename>', methods=['GET'])
def firmware_download(firmware_id, filename):
    """Serve OTA binaries from managed firmware storage."""
    if filename not in ("firmware.bin", "bootloader.bin", "partitions.bin"):
        return jsonify({"error": "Not found"}), 404
    try:
        from ..services.firmware_device_service import get_firmware_file_path

        path = get_firmware_file_path(firmware_id, filename)
        if not path:
            return jsonify({"error": "Not found"}), 404
        return send_file(
            path,
            as_attachment=True,
            download_name=filename,
            mimetype="application/octet-stream",
        )
    except Exception:
        logging.exception("firmware download")
        return jsonify({"error": "Unexpected error"}), 500
