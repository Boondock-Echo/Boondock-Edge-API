"""Administrator-only keyword action and email delivery configuration."""

from flask import Blueprint, jsonify, request

from ..middleware.auth_middleware import require_admin
from ..services import keyword_actions as service

keyword_actions_bp = Blueprint('keyword_actions', __name__)


def _body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('A JSON object is required')
    return data


@keyword_actions_bp.route('/settings/kasa', methods=['GET', 'PATCH'])
@require_admin
def kasa_settings():
    try:
        if request.method == 'GET':
            return jsonify(service.kasa_actions.get_settings())
        return jsonify(service.kasa_actions.save_settings(_body()))
    except ValueError as error:
        return jsonify({'error': str(error)}), 400


@keyword_actions_bp.route('/settings/email', methods=['GET', 'PATCH'])
@require_admin
def email_settings():
    try:
        if request.method == 'GET':
            return jsonify(service.get_email_settings(public=True))
        return jsonify(service.save_email_settings(_body()))
    except ValueError as error:
        return jsonify({'error': str(error)}), 400


@keyword_actions_bp.route('/settings/email/test', methods=['POST'])
@require_admin
def test_email():
    try:
        data = _body()
        if set(data) - {'to', 'from_email', 'from_name', 'reply_to'} or not {'to', 'from_email'} <= set(data):
            raise ValueError('Provide To and From email addresses')
        recipient = service._address(data['to'], 'to')
        from_name = service._text(data.get('from_name', ''), 'from_name', 100)
        if '\r' in from_name or '\n' in from_name:
            raise ValueError('from_name must be a single line')
        service.send_email({
            'to': [recipient], 'cc': [], 'bcc': [],
            'from_email': service._address(data['from_email'], 'from_email'),
            'from_name': from_name, 'reply_to': service._address(data['reply_to'], 'reply_to') if data.get('reply_to') else '',
            'subject': 'Boondock email delivery test', 'body_format': 'text',
            'body': 'Your Boondock SMTP configuration is working.',
        }, {})
        return jsonify({'message': 'Test email accepted by the SMTP server'})
    except ValueError as error:
        return jsonify({'error': str(error)}), 400
    except Exception as error:
        return jsonify({'error': f'Email delivery failed ({type(error).__name__}). Check SMTP settings and server access.'}), 502


@keyword_actions_bp.route('/keyword-actions', methods=['GET', 'POST'])
@require_admin
def actions():
    if request.method == 'GET':
        return jsonify({'actions': service.get_actions(), 'template_fields': service.TEMPLATE_FIELDS})
    try:
        return jsonify(service.save_action(_body())), 201
    except ValueError as error:
        return jsonify({'error': str(error)}), 400


@keyword_actions_bp.route('/keyword-actions/preview', methods=['POST'])
@require_admin
def preview():
    try:
        data = _body()
        if set(data) - {'subject', 'body', 'body_format'}:
            raise ValueError('Unknown preview fields')
        subject = service.validate_template(data.get('subject', ''), 'subject', subject=True)
        body = service.validate_template(data.get('body', ''), 'body')
        body_format = data.get('body_format', 'text')
        if body_format not in ('text', 'html'):
            raise ValueError('Email format must be text or html')
        settings = service.get_email_settings()
        message, _ = service.render_email({
            'to': ['preview@example.test'], 'from_email': 'preview@example.test', 'subject': subject, 'body': body, 'body_format': body_format,
        }, {
            'keyword': 'engine fire', 'keywords': 'engine fire, urgent',
            'matched_text': 'ENGINE FIRE, urgent', 'transcript': 'Car 42 reports ENGINE FIRE. Assistance is urgent.',
            'recording_id': '42', 'channel': 'Race control', 'timestamp': '2026-10-28T14:30:00+00:00',
            'filename': 'recordings/example.wav',
        }, settings)
        content = message.get_body(preferencelist=('html', 'plain'))
        return jsonify({'subject': str(message['Subject']), 'body': content.get_content(), 'body_format': body_format})
    except ValueError as error:
        return jsonify({'error': str(error)}), 400


@keyword_actions_bp.route('/keyword-actions/<action_id>', methods=['PATCH', 'DELETE'])
@require_admin
def action(action_id):
    try:
        if request.method == 'DELETE':
            service.delete_action(action_id)
            return '', 204
        return jsonify(service.save_action(_body(), action_id))
    except LookupError as error:
        return jsonify({'error': str(error)}), 404
    except ValueError as error:
        return jsonify({'error': str(error)}), 400


@keyword_actions_bp.route('/keyword-actions/deliveries', methods=['GET'])
@require_admin
def deliveries():
    return jsonify(service.delivery_history())


@keyword_actions_bp.route('/keyword-actions/kasa/outlets', methods=['POST'])
@require_admin
def kasa_outlets():
    try:
        data = _body()
        if set(data) != {'device_host'}:
            raise ValueError('Provide the device_host only')
        return jsonify(service.kasa_actions.read_outlets(data['device_host']))
    except ValueError as error:
        return jsonify({'error': str(error)}), 400
    except Exception:
        return jsonify({'error': 'Could not read the Kasa device. Check its address and network access from the API host.'}), 502


@keyword_actions_bp.route('/keyword-actions/kasa/test', methods=['POST'])
@require_admin
def test_kasa():
    try:
        data = _body()
        if set(data) != {'device_host', 'outlet_id', 'command'}:
            raise ValueError('Provide device_host, outlet_id, and command')
        result = service.kasa_actions.execute(data)
        return jsonify({**result, 'message': 'Kasa outlet command completed and state verified.'})
    except ValueError as error:
        return jsonify({'error': str(error)}), 400
    except Exception:
        return jsonify({'error': 'Kasa command could not be verified. Read outlets to check the current state and network access.'}), 502


@keyword_actions_bp.route('/keyword-actions/kasa/discover', methods=['POST'])
@require_admin
def discover_kasa():
    try:
        if _body():
            raise ValueError('Discovery does not accept configuration fields')
        return jsonify(service.kasa_actions.discover_devices())
    except ValueError as error:
        return jsonify({'error': str(error)}), 400
    except Exception:
        return jsonify({'error': 'Kasa discovery failed. Check local network and UDP broadcast access from the API host.'}), 502
