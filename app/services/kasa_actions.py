"""Local Kasa discovery and on/off control. All network I/O happens on the API host."""

import asyncio
import ipaddress


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
    from kasa.iot import IotBulb, IotPlug, IotStrip
    return isinstance(device, (IotBulb, IotPlug, IotStrip))


def _targets(device):
    return device.children or [device]


async def _discover():
    from kasa import Discover

    devices = await Discover.discover(discovery_timeout=3)

    async def describe(host, device):
        result = {'device_host': host, 'model': device.model, 'name': device.alias or '',
                  'supported': False, 'outlets': []}
        try:
            if not _supported(device):
                result['reason'] = 'This device requires a different protocol or account authentication.'
            else:
                await asyncio.wait_for(device.update(), timeout=5)
                result.update(name=device.alias or '', supported=True, outlets=[
                    {'id': target.device_id, 'name': target.alias, 'is_on': target.is_on}
                    for target in _targets(device)
                ])
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

    device = await Discover.discover_single(host, discovery_timeout=3)
    if device is None:
        raise ValueError('No Kasa device responded at this address')
    try:
        if not _supported(device):
            raise ValueError('This device requires a different protocol or account authentication')
        await device.update()
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
    finally:
        await device.disconnect()


def read_outlets(host):
    return asyncio.run(asyncio.wait_for(_request(validate_host(host)), timeout=15))


def execute(action):
    config = validate_config(action)
    return asyncio.run(asyncio.wait_for(_request(config['device_host'], config), timeout=15))
