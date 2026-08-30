"""
GPIO routes for LED and Relay control via integrated GPIO service.

This module provides Flask routes that directly call the integrated GPIO service.
The GPIO service is now part of the main Flask application, eliminating the need
for a separate service process.

Flow: Web Interface → Flask Backend (this module) → GPIO Service (integrated)
"""
import logging
from flask import Blueprint, jsonify, request
from flasgger import swag_from
from app.services.gpio_service import get_gpio_service, LEDState

gpio_bp = Blueprint('gpio', __name__)
logger = logging.getLogger(__name__)


def _call_gpio_service(func_name, *args, **kwargs):
    """
    Call a GPIO service method and handle errors.
    
    Args:
        func_name: Name of the GPIO service method to call
        *args: Positional arguments to pass to the method
        **kwargs: Keyword arguments to pass to the method
    
    Returns:
        tuple: (response_data, status_code)
    """
    try:
        gpio_service = get_gpio_service()
        method = getattr(gpio_service, func_name)
        result = method(*args, **kwargs)
        return result, 200
    except ValueError as e:
        logger.error(f"GPIO service error: {e}")
        return {'error': str(e)}, 400
    except Exception as e:
        logger.error(f"Unexpected error calling GPIO service: {e}")
        return {'error': str(e)}, 500


# ------------------------------
# LED Pattern Control (Legacy - for backward compatibility)
# ------------------------------

@gpio_bp.route('/gpio/pattern', methods=['GET'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Get current LED pattern (legacy)',
    'responses': {
        '200': {'description': 'Current LED pattern'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def get_pattern():
    """Get current LED pattern (legacy endpoint - uses state API)."""
    # Map to state endpoint for backward compatibility
    data, status = _call_gpio_service('get_led_state')
    if status == 200 and data:
        # Convert state to pattern format for backward compatibility
        state = data.get('state', 'off')
        pattern_map = {
            'off': 'off',
            'on': 'on',
            'online': 'slow',
            'receiving': 'fast',
            'error': 'pulse',
            'breathing': 'pulse'
        }
        pattern = pattern_map.get(state, 'off')
        return jsonify({'pattern': pattern, 'state': state}), 200
    return jsonify(data), status


@gpio_bp.route('/gpio/pattern/<pattern>', methods=['POST'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Set LED pattern (legacy)',
    'parameters': [
        {
            'name': 'pattern',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Pattern name (legacy support)'
        }
    ],
    'responses': {
        '200': {'description': 'Pattern set successfully'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def set_pattern(pattern):
    """Set LED pattern (legacy endpoint - maps to state API)."""
    # Map legacy patterns to states
    pattern_to_state = {
        'off': 'off',
        'on': 'on',
        'startup': 'breathing',
        'fast': 'receiving',
        'medium': 'online',
        'slow': 'online',
        'pulse': 'breathing',
        'two': 'receiving',
        'three': 'receiving'
    }
    
    state_str = pattern_to_state.get(pattern.lower(), 'off')
    try:
        state = LEDState(state_str)
        data, status = _call_gpio_service('set_led_state', state)
        return jsonify(data), status
    except ValueError:
        return jsonify({'error': f'Invalid pattern: {pattern}'}), 400


@gpio_bp.route('/gpio/stop', methods=['POST'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Stop LED activity (legacy)',
    'responses': {
        '200': {'description': 'LED stopped'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def stop_led():
    """Stop LED activity (legacy endpoint - sets state to off)."""
    data, status = _call_gpio_service('set_led_state', LEDState.off)
    return jsonify(data), status


# ------------------------------
# LED State Control
# ------------------------------

@gpio_bp.route('/gpio/state', methods=['GET'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Get current LED state',
    'responses': {
        '200': {'description': 'Current LED state'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def get_state():
    """Get current LED state."""
    data, status = _call_gpio_service('get_led_state')
    return jsonify(data), status


@gpio_bp.route('/gpio/state/<state>', methods=['POST'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Set LED state',
    'parameters': [
        {
            'name': 'state',
            'in': 'path',
            'type': 'string',
            'enum': ['off', 'on', 'online', 'receiving', 'error'],
            'required': True,
            'description': 'LED state: off, on, online, receiving, or error'
        },
        {
            'name': 'error_code',
            'in': 'query',
            'type': 'integer',
            'minimum': 1,
            'maximum': 5,
            'required': False,
            'description': 'Error code (1-5) for error state'
        }
    ],
    'responses': {
        '200': {'description': 'State set successfully'},
        '400': {'description': 'Invalid state'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def set_state(state):
    """Set LED state."""
    # Validate state
    valid_states = ['off', 'on', 'online', 'receiving', 'error', 'breathing']
    if state not in valid_states:
        return jsonify({'error': f'Invalid state. Must be one of: {", ".join(valid_states)}'}), 400
    
    # Get error_code from query params if provided
    error_code = request.args.get('error_code', type=int)
    
    try:
        led_state = LEDState(state)
        data, status = _call_gpio_service('set_led_state', led_state, error_code=error_code)
        return jsonify(data), status
    except ValueError:
        return jsonify({'error': f'Invalid state: {state}'}), 400


# ------------------------------
# LED Configuration
# ------------------------------

@gpio_bp.route('/gpio/led/gpio', methods=['GET'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Get LED GPIO pin',
    'responses': {
        '200': {'description': 'LED GPIO pin'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def get_led_gpio():
    """Get LED GPIO pin."""
    data, status = _call_gpio_service('get_led_gpio')
    return jsonify(data), status


@gpio_bp.route('/gpio/led/gpio/<int:gpio>', methods=['POST'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Set LED GPIO pin',
    'parameters': [
        {
            'name': 'gpio',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'GPIO pin number (1-40)'
        }
    ],
    'responses': {
        '200': {'description': 'GPIO pin set successfully'},
        '400': {'description': 'Invalid GPIO pin'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def set_led_gpio(gpio):
    """Set LED GPIO pin."""
    data, status = _call_gpio_service('set_led_gpio', gpio)
    return jsonify(data), status


@gpio_bp.route('/gpio/led/enabled', methods=['GET'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Get LED enabled status',
    'responses': {
        '200': {'description': 'LED enabled status'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def get_led_enabled():
    """Get LED enabled status."""
    data, status = _call_gpio_service('get_led_enabled')
    return jsonify(data), status


@gpio_bp.route('/gpio/led/enabled/<enabled>', methods=['POST'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Enable or disable LED',
    'parameters': [
        {
            'name': 'enabled',
            'in': 'path',
            'type': 'string',
            'enum': ['true', 'false'],
            'required': True
        }
    ],
    'responses': {
        '200': {'description': 'LED enabled status updated'},
        '400': {'description': 'Invalid enabled value'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def set_led_enabled(enabled):
    """Enable or disable LED."""
    # Convert string to boolean for API
    enabled_bool = enabled.lower() == 'true'
    data, status = _call_gpio_service('set_led_enabled', enabled_bool)
    return jsonify(data), status


# ------------------------------
# LED Mode Configuration
# ------------------------------

@gpio_bp.route('/gpio/led/mode', methods=['GET'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Get LED mode (source or sink)',
    'responses': {
        '200': {'description': 'LED mode'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def get_led_mode():
    """Get LED mode (source or sink)."""
    data, status = _call_gpio_service('get_led_mode')
    return jsonify(data), status


@gpio_bp.route('/gpio/led/mode/<mode>', methods=['POST'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Set LED mode',
    'parameters': [
        {
            'name': 'mode',
            'in': 'path',
            'type': 'string',
            'enum': ['source', 'sink'],
            'required': True,
            'description': 'LED mode: source (active high) or sink (active low)'
        }
    ],
    'responses': {
        '200': {'description': 'LED mode updated'},
        '400': {'description': 'Invalid mode'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def set_led_mode(mode):
    """Set LED mode to source or sink."""
    if mode not in ['source', 'sink']:
        return jsonify({'error': "Mode must be 'source' or 'sink'"}), 400
    data, status = _call_gpio_service('set_led_mode', mode)
    return jsonify(data), status


# ------------------------------
# Relay Management
# ------------------------------

@gpio_bp.route('/gpio/relays', methods=['GET'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Get all relays',
    'responses': {
        '200': {'description': 'List of all relays'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def get_all_relays():
    """Get all relays."""
    data, status = _call_gpio_service('get_all_relays')
    return jsonify(data), status


@gpio_bp.route('/gpio/relays/<name>', methods=['GET'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Get relay information',
    'parameters': [
        {
            'name': 'name',
            'in': 'path',
            'type': 'string',
            'required': True
        }
    ],
    'responses': {
        '200': {'description': 'Relay information'},
        '404': {'description': 'Relay not found'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def get_relay(name):
    """Get relay information."""
    data, status = _call_gpio_service('get_relay', name)
    return jsonify(data), status


@gpio_bp.route('/gpio/relays', methods=['POST'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Add a new relay',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['name', 'gpio'],
                'properties': {
                    'name': {'type': 'string'},
                    'gpio': {'type': 'integer', 'minimum': 1, 'maximum': 40}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Relay added successfully'},
        '400': {'description': 'Bad request'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def add_relay():
    """Add a new relay."""
    data = request.get_json()
    if not data or 'name' not in data or 'gpio' not in data:
        return jsonify({'error': 'Missing required fields: name, gpio'}), 400
    
    # Ensure normal_state is provided (default to "off")
    normal_state = data.get('normal_state', 'off')
    
    response_data, status = _call_gpio_service('add_relay', data['name'], data['gpio'], normal_state)
    return jsonify(response_data), status


@gpio_bp.route('/gpio/relays/<name>', methods=['DELETE'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Remove a relay',
    'parameters': [
        {
            'name': 'name',
            'in': 'path',
            'type': 'string',
            'required': True
        }
    ],
    'responses': {
        '200': {'description': 'Relay removed successfully'},
        '404': {'description': 'Relay not found'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def remove_relay(name):
    """Remove a relay."""
    data, status = _call_gpio_service('remove_relay', name)
    return jsonify(data), status


@gpio_bp.route('/gpio/relays/<name>/<action>', methods=['POST'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Control a relay',
    'parameters': [
        {
            'name': 'name',
            'in': 'path',
            'type': 'string',
            'required': True
        },
        {
            'name': 'action',
            'in': 'path',
            'type': 'string',
            'enum': ['on', 'off', 'toggle'],
            'required': True
        }
    ],
    'responses': {
        '200': {'description': 'Relay controlled successfully'},
        '400': {'description': 'Invalid action'},
        '404': {'description': 'Relay not found'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def control_relay(name, action):
    """Control a relay: on, off, or toggle."""
    if action not in ['on', 'off', 'toggle']:
        return jsonify({'error': "Action must be 'on', 'off', or 'toggle'"}), 400
    
    data, status = _call_gpio_service('control_relay', name, action)
    return jsonify(data), status


@gpio_bp.route('/gpio/relays/<name>/normal_state', methods=['GET'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Get relay normal state',
    'parameters': [
        {
            'name': 'name',
            'in': 'path',
            'type': 'string',
            'required': True
        }
    ],
    'responses': {
        '200': {'description': 'Relay normal state'},
        '404': {'description': 'Relay not found'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def get_relay_normal_state(name):
    """Get relay normal state."""
    data, status = _call_gpio_service('get_relay_normal_state', name)
    return jsonify(data), status


@gpio_bp.route('/gpio/relays/<name>/normal_state/<normal_state>', methods=['POST'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Set relay normal state',
    'parameters': [
        {
            'name': 'name',
            'in': 'path',
            'type': 'string',
            'required': True
        },
        {
            'name': 'normal_state',
            'in': 'path',
            'type': 'string',
            'enum': ['on', 'off'],
            'required': True
        }
    ],
    'responses': {
        '200': {'description': 'Relay normal state updated'},
        '400': {'description': 'Invalid normal_state'},
        '404': {'description': 'Relay not found'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def set_relay_normal_state(name, normal_state):
    """Set relay normal state."""
    if normal_state not in ['on', 'off']:
        return jsonify({'error': "normal_state must be 'on' or 'off'"}), 400
    
    data, status = _call_gpio_service('set_relay_normal_state', name, normal_state)
    return jsonify(data), status


@gpio_bp.route('/gpio/relays/<name>/gpio/<int:gpio>', methods=['POST'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Update relay GPIO pin',
    'parameters': [
        {
            'name': 'name',
            'in': 'path',
            'type': 'string',
            'required': True
        },
        {
            'name': 'gpio',
            'in': 'path',
            'type': 'integer',
            'minimum': 1,
            'maximum': 40,
            'required': True
        }
    ],
    'responses': {
        '200': {'description': 'Relay GPIO updated'},
        '400': {'description': 'Invalid GPIO pin'},
        '404': {'description': 'Relay not found'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def update_relay_gpio(name, gpio):
    """Update relay GPIO pin."""
    if gpio < 1 or gpio > 40:
        return jsonify({'error': 'GPIO pin must be between 1 and 40'}), 400
    
    data, status = _call_gpio_service('update_relay_gpio', name, gpio)
    return jsonify(data), status


# ------------------------------
# History
# ------------------------------

@gpio_bp.route('/gpio/history', methods=['GET'])
@swag_from({
    'tags': ['GPIO'],
    'summary': 'Get API call and status change history',
    'responses': {
        '200': {'description': 'History data'},
        '503': {'description': 'GPIO service unavailable'}
    }
})
def get_history():
    """Get API call and status change history."""
    data, status = _call_gpio_service('get_history')
    return jsonify(data), status

