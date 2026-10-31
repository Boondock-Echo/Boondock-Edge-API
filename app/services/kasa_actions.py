"""Local Kasa discovery and on/off control. All network I/O happens on the API host."""

import asyncio
import ipaddress
import threading

from .settings_manager import get_settings_manager

_settings_lock = threading.RLock()


def get_settings(public=True):
    stored = get_settings_manager().get_setting('keyword_kasa', {})
    username = stored.get('username', '')
    password = stored.get('password', '')
    result = {'username': username, 'configured': bool(username and password)}
    if public:
        result['password_configured'] = bool(password)
    else:
        result['password'] = password
    return result


def save_settings(data):
    if not isinstance(data, dict) or set(data) - {'username', 'password', 'clear_password'}:
        raise ValueError('Unknown Kasa settings fields')
    with _settings_lock:
        settings = get_settings(public=False)
        username = data.get('username', settings['username'])
        password = data.get('password', '')
        clear = data.get('clear_password', False)
        if not isinstance(username, str) or len(username) > 254 or any(c in username for c in '\r\n\x00'):
            raise ValueError('Kasa username must be a single line of at most 254 characters')
        if not isinstance(password, str) or len(password) > 1000 or '\x00' in password:
            raise ValueError('Kasa password must be text of at most 1000 characters')
        if not isinstance(clear, bool) or (clear and password):
            raise ValueError('Choose either a new password or clear_password: true')
        username = username.strip()
        if username != settings['username'] and settings['password'] and not password and not clear:
            raise ValueError('Enter a password when changing the Kasa account')
        password = '' if clear else (password or settings['password'])
        if password and not username:
            raise ValueError('Kasa username is required with a password')
        if username and not password and not clear:
            raise ValueError('Kasa password is required')
        if not get_settings_manager().set_setting('keyword_kasa', {'username': username, 'password': password}):
            raise RuntimeError('Unable to save Kasa settings')
    return get_settings()


def _credentials():
    from kasa import Credentials
    settings = get_settings(public=False)
    return Credentials(settings['username'], settings['password']) if settings['configured'] else None


def validate_host(value):
    try:
        address = ipaddress.ip_address(value) if isinstance(value, str) else None
    except ValueError:
        address = None
    if address is None or address.version != 4 or address.is_unspecified or address.is_multicast:
        raise ValueError('Device address must be an IPv4 address')
    return str(address)


def validate_config(data):
    host = validate_host(data.get('device_host'))
    outlet = data.get('outlet_id')
    if not isinstance(outlet, str) or not outlet.strip() or len(outlet) > 200:
        raise ValueError('Read outlets and select an outlet first')
    if data.get('command') not in ('turn_on', 'turn_off'):
        raise ValueError('Kasa command must be turn_on or turn_off')
    return {'device_host': host, 'outlet_id': outlet.strip(), 'command': data['command']}


def _supported(device):
    from kasa import DeviceType
    from kasa.iot import IotBulb, IotPlug, IotStrip
    from kasa.smart import SmartDevice
    return isinstance(device, (IotBulb, IotPlug, IotStrip)) or (
        isinstance(device, SmartDevice) and device.device_type in {
            DeviceType.Plug, DeviceType.Strip, DeviceType.Bulb, DeviceType.LightStrip,
            DeviceType.WallSwitch, DeviceType.Dimmer,
        }
    )


def _targets(device):
    return device.children or [device]


async def _ready_device(device):
    from kasa import Device
    from kasa.protocols import IotProtocol

    if isinstance(device.protocol, IotProtocol):
        # UDP discovery labels all IOT.SMARTPLUGSWITCH devices as IotPlug.
        # Connect queries sysinfo to distinguish strips, plugs, and switches,
        # including IOT devices which now authenticate over KLAP v2.
        config = device.config
        await device.disconnect()
        return await Device.connect(config=config)
    await device.update()
    return device


def _authentication_message():
    if not get_settings()['configured']:
        return 'No Kasa account credentials are saved. Save the account email and password, then discover again.'
    return ('Device authentication was not accepted. Check the account email (case-sensitive), '
            'password, and Third-Party Compatibility in the Kasa app. Firmware or library '
            'compatibility can also cause this error.')


async def _discover():
    from kasa import Discover
    from kasa.exceptions import AuthenticationError

    devices = await Discover.discover(discovery_timeout=3, credentials=_credentials())

    async def describe(host, device):
        result = {'device_host': host, 'model': device.model, 'name': device.alias or '',
                  'supported': False, 'outlets': []}
        try:
            device = await asyncio.wait_for(_ready_device(device), timeout=8)
            if _supported(device):
                result.update(name=device.alias or '', supported=True, outlets=[
                    {'id': target.device_id, 'name': target.alias, 'is_on': target.is_on}
                    for target in _targets(device)
                ])
            else:
                result['reason'] = 'This device does not support the available on/off actions.'
        except AuthenticationError:
            result['reason'] = _authentication_message()
        except Exception:
            result['reason'] = 'Device responded to discovery but could not be read.'
        finally:
            await device.disconnect()
        return result

    results = await asyncio.gather(*(describe(host, device) for host, device in devices.items()))
    return {'devices': sorted(results, key=lambda item: (item['name'].lower(), item['device_host']))}


def discover_devices():
    return asyncio.run(asyncio.wait_for(_discover(), timeout=15))


async def _request(host, action=None):
    from kasa import Discover
    from kasa.exceptions import AuthenticationError

    device = await Discover.discover_single(host, discovery_timeout=3, credentials=_credentials())
    if device is None:
        raise ValueError('No Kasa device responded at this address')
    try:
        device = await _ready_device(device)
        if not _supported(device):
            raise ValueError('This device does not support the available on/off actions')
        if action:
            outlet = next((child for child in _targets(device) if child.device_id == action['outlet_id']), None)
            if outlet is None:
                raise ValueError('Selected outlet was not found; read outlets again')
            await getattr(outlet, action['command'])()
            await device.update()
            if outlet.is_on != (action['command'] == 'turn_on'):
                raise RuntimeError('Outlet did not reach the requested state')
        return {
            'device_host': host, 'model': device.model, 'name': device.alias,
            'outlets': [{'id': child.device_id, 'name': child.alias, 'is_on': child.is_on}
                        for child in _targets(device)],
        }
    except AuthenticationError:
        raise ValueError(_authentication_message()) from None
    finally:
        await device.disconnect()


def read_outlets(host):
    return asyncio.run(asyncio.wait_for(_request(validate_host(host)), timeout=15))


def execute(action):
    config = validate_config(action)
    return asyncio.run(asyncio.wait_for(_request(config['device_host'], config), timeout=15))
