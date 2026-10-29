"""Validated keyword actions and transactional delivery outbox.

SMTP I/O runs in the worker, after the recording transaction commits. Rules
fire once per recording; transport retries can repeat an SMTP submission if
the process dies after the server accepts mail but before the sent-state commit.
"""

from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
import html
import json
import logging
import smtplib
import ssl
import string
import threading
import time
from urllib.parse import quote, urlparse
import uuid

from config import Config, DATA_ROOT
from app.utils.sqlite_utils import connect_sqlite
from .settings_manager import get_settings_manager

logger = logging.getLogger(__name__)
_config_lock = threading.RLock()
_worker_lock = threading.Lock()
_worker = None
_stop = threading.Event()


class PartialEmailDeliveryError(Exception):
    """Some recipients accepted mail; retrying everybody would duplicate it."""

EMAIL_DEFAULTS = {
    'host': '', 'port': 587, 'security': 'starttls',
    'username': '', 'password': '', 'dashboard_url': '',
}
TEMPLATE_FIELDS = (
    'keyword', 'keywords', 'matched_text', 'transcript', 'recording_id',
    'channel', 'timestamp', 'recording_url', 'dashboard_url',
)


def _text(value, name, limit=1000):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError(f'{name} must be text of at most {limit} characters')
    return value.strip()


def _boolean(value, name):
    if not isinstance(value, bool):
        raise ValueError(f'{name} must be true or false')
    return value


def _address(value, name):
    value = _text(value, name, 254)
    if '\r' in value or '\n' in value:
        raise ValueError(f'{name} must be a single email address')
    display, address = parseaddr(value)
    if display or address != value or address.count('@') != 1 or any(c.isspace() for c in address):
        raise ValueError(f'{name} must be a plain email address')
    local, domain = address.split('@')
    if not local or not domain or any(c in address for c in '<>,;:'):
        raise ValueError(f'{name} must be a plain email address')
    return address


def _addresses(value, name):
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError(f'{name} must be an array of at most 50 email addresses')
    return list(dict.fromkeys(_address(item, name) for item in value))


def validate_template(value, name, subject=False):
    value = _text(value, name, 500 if subject else 20000)
    if not value or (subject and any(c in value for c in '\r\n')):
        raise ValueError(f'{name} is required and subject must be a single line')
    try:
        for _, field, spec, conversion in string.Formatter().parse(value):
            if field is not None and (field not in TEMPLATE_FIELDS or spec or conversion):
                raise ValueError('Unsupported template placeholder')
    except ValueError:
        raise ValueError(f'{name} contains an invalid placeholder; use: {", ".join(TEMPLATE_FIELDS)}') from None
    return value


def get_email_settings(public=False):
    stored = get_settings_manager().get_setting('keyword_email', {})
    settings = {key: stored.get(key, default) for key, default in EMAIL_DEFAULTS.items()}
    settings['configured'] = bool(settings['host'] and (not settings['username'] or settings['password']))
    if public:
        configured = bool(settings.pop('password', ''))
        settings['password_configured'] = configured
    return settings


def save_email_settings(data):
    if not isinstance(data, dict) or set(data) - (set(EMAIL_DEFAULTS) | {'clear_password'}):
        raise ValueError('Unknown email settings fields')
    with _config_lock:
        settings = get_email_settings()
        settings.pop('configured', None)
        for key in set(EMAIL_DEFAULTS) - {'password'}:
            if key in data:
                settings[key] = data[key]
        if data.get('clear_password') is not None and _boolean(data['clear_password'], 'clear_password'):
            settings['password'] = ''
        if data.get('password'):
            _text(data['password'], 'password', 1000)
            settings['password'] = data['password']
        for key in ('host', 'username'):
            settings[key] = _text(settings[key], key)
            if '\r' in settings[key] or '\n' in settings[key]:
                raise ValueError(f'{key} must be a single line')
        if any(c.isspace() for c in settings['host']) or '://' in settings['host']:
            raise ValueError('SMTP host must be a hostname or IP address')
        if isinstance(settings['port'], bool) or not isinstance(settings['port'], int) or not 1 <= settings['port'] <= 65535:
            raise ValueError('SMTP port must be an integer between 1 and 65535')
        if settings['security'] not in ('starttls', 'ssl', 'none'):
            raise ValueError('SMTP security must be starttls, ssl, or none')
        settings['dashboard_url'] = _text(settings['dashboard_url'], 'dashboard_url', 2000).rstrip('/')
        if settings['dashboard_url']:
            url = urlparse(settings['dashboard_url'])
            if url.scheme not in ('http', 'https') or not url.netloc or url.username or url.password or url.query or url.fragment:
                raise ValueError('Dashboard URL must be an HTTP(S) base URL without credentials, query, or fragment')
        if settings['username'] and not settings['password']:
            raise ValueError('SMTP password is required when a username is set')
        if not get_settings_manager().set_setting('keyword_email', settings):
            raise RuntimeError('Unable to save email settings')
    return get_email_settings(public=True)


def get_actions():
    return get_settings_manager().get_keyword_actions()


def save_action(data, action_id=None):
    fields = {'name', 'kind', 'autoplay', 'from_email', 'from_name', 'reply_to', 'to', 'cc', 'bcc', 'subject', 'body', 'body_format'}
    if not isinstance(data, dict) or set(data) - fields:
        raise ValueError('Unknown action fields')
    with _config_lock:
        manager = get_settings_manager()
        existing = manager.get_keyword_action(action_id) if action_id else None
        if action_id and existing is None:
            raise LookupError('Action not found')
        action = {**(existing or {}), **data}
        action['id'] = action_id or str(uuid.uuid4())
        action['name'] = _text(action.get('name', ''), 'name', 100)
        if not action['name']:
            raise ValueError('Action name is required')
        if action.get('kind') == 'email':
            if not get_email_settings()['configured']:
                raise ValueError('Set up email routing before creating an email action')
            action['from_email'] = _address(action.get('from_email', ''), 'from_email')
            action['from_name'] = _text(action.get('from_name', 'Boondock'), 'from_name', 100)
            if any(character in action['from_name'] for character in '\r\n'):
                raise ValueError('from_name must be a single line')
            action['reply_to'] = _address(action['reply_to'], 'reply_to') if action.get('reply_to') else ''
            for key in ('to', 'cc', 'bcc'):
                action[key] = _addresses(action.get(key, []), key)
            if not action['to']:
                raise ValueError('At least one To recipient is required')
            action['subject'] = validate_template(action.get('subject', ''), 'subject', subject=True)
            action['body'] = validate_template(action.get('body', ''), 'body')
            if action.get('body_format', 'text') not in ('text', 'html'):
                raise ValueError('Email format must be text or html')
            action['body_format'] = action.get('body_format', 'text')
        elif action.get('kind') == 'popup':
            action['autoplay'] = _boolean(action.get('autoplay', True), 'autoplay')
        else:
            raise ValueError('Action kind must be email or popup')
        if not manager.save_keyword_action(action, create=action_id is None):
            raise LookupError('Action not found')
    return action


def delete_action(action_id):
    with _config_lock:
        if not get_settings_manager().delete_keyword_action(action_id):
            raise LookupError('Action not found')


def enqueue_actions(connection, recording_id):
    """Queue actions from a returned transcription's saved matches, before commit."""
    row = connection.execute(
        'SELECT channel_id, filename, timestamp, transcription, is_hallucination, is_duplicate, matched_keywords FROM recordings WHERE id=?',
        (recording_id,),
    ).fetchone()
    if not row:
        return
    channel_id, filename, timestamp, transcript, hallucination, duplicate, stored_matches = row
    if hallucination or duplicate:
        return
    matches = json.loads(stored_matches) if isinstance(stored_matches, str) else stored_matches
    if not matches:
        return
    manager = get_settings_manager()
    channel = manager.get_channel(channel_id)
    if not channel:
        return
    triggers = manager.get_keyword_action_triggers([match['id'] for match in matches], channel_id)
    if not triggers:
        return
    text_bytes = transcript.encode('utf-16-le')
    timestamp = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat() if timestamp else ''
    now = int(time.time())
    for entry in triggers.values():
        action = entry['action']
        group_ids = sorted(entry['group_ids'])
        selected = [match for match in matches if match['id'] in entry['keyword_ids']]
        context = {
            'keyword': selected[0]['pattern'],
            'keywords': ', '.join(match['pattern'] for match in selected),
            'matched_text': ', '.join(dict.fromkeys(
                text_bytes[span['start'] * 2:span['end'] * 2].decode('utf-16-le')
                for match in selected for span in match['ranges']
            )),
            'transcript': transcript, 'recording_id': str(recording_id),
            'channel': channel.get('name') or f'Channel {channel_id}',
            'timestamp': timestamp,
            'filename': filename or '', 'channel_id': channel_id,
            'message_id': make_msgid(),
            'trigger_keyword_ids': [match['id'] for match in selected], 'group_ids': group_ids,
        }
        connection.execute(
            '''INSERT INTO keyword_action_deliveries
               (recording_id, action_id, group_id, kind, payload, state, created_at, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(recording_id, action_id) DO NOTHING''',
            (recording_id, action['id'], group_ids[0], action['kind'],
             json.dumps({'action': action, 'context': context}),
             'sent' if action['kind'] == 'popup' else 'pending', now,
             now if action['kind'] == 'popup' else None),
        )


def render_email(action, context, settings):
    context = {**context, 'dashboard_url': settings['dashboard_url']}
    context['recording_url'] = (
        settings['dashboard_url'] + '/recording/' + quote(str(context['recording_id']), safe='')
        if settings['dashboard_url'] and context.get('recording_id') else ''
    )
    message = EmailMessage()
    message['From'] = formataddr((action.get('from_name', 'Boondock'), action['from_email']))
    message['To'] = ', '.join(action['to'])
    if action.get('cc'):
        message['Cc'] = ', '.join(action['cc'])
    if action.get('reply_to'):
        message['Reply-To'] = action['reply_to']
    # Transcripts can contain newlines; subject substitution stays one line.
    message['Subject'] = action['subject'].format_map({key: ' '.join(str(value).splitlines()) for key, value in context.items()})
    message['Message-ID'] = context.get('message_id') or make_msgid()
    message['Date'] = datetime.now(timezone.utc)
    if action.get('body_format') == 'html':
        message.set_content(context.get('transcript', ''))
        message.add_alternative(action['body'].format_map({key: html.escape(str(value)) for key, value in context.items()}), subtype='html')
    else:
        message.set_content(action['body'].format_map(context))
    recipients = list(dict.fromkeys(action['to'] + action.get('cc', []) + action.get('bcc', [])))
    return message, recipients


def send_email(action, context, settings=None):
    settings = settings or get_email_settings()
    if not settings['configured']:
        raise ValueError('Email routing is not configured')
    message, recipients = render_email(action, context, settings)
    if context.get('recording_id'):
        if not context.get('filename'):
            raise ValueError('Recording has no audio filename')
        root = DATA_ROOT.resolve()
        audio = (root / context['filename']).resolve()
        if not audio.is_relative_to(root):
            raise ValueError('Recording audio must be inside the data directory')
        message.add_attachment(
            audio.read_bytes(), maintype='audio', subtype='wav', filename=audio.name,
        )
    options = {'host': settings['host'], 'port': settings['port'], 'timeout': 15}
    if settings['security'] == 'ssl':
        client = smtplib.SMTP_SSL(**options, context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(**options)
    with client:
        client.ehlo()
        if settings['security'] == 'starttls':
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if settings['username']:
            client.login(settings['username'], settings['password'])
        refused = client.send_message(message, from_addr=action['from_email'], to_addrs=recipients)
        if refused:
            raise PartialEmailDeliveryError()


def process_email_delivery(database=None, now=None):
    """Claim and attempt one due delivery. A lease permits crash recovery."""
    settings = get_email_settings()
    if not settings['configured']:
        return False
    now = int(time.time()) if now is None else now
    connection = connect_sqlite(database or Config.get_recordings_db_path(), row_factory=True)
    try:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute(
            '''SELECT * FROM keyword_action_deliveries WHERE kind='email' AND
               ((state='pending' AND next_attempt_at<=?) OR (state='sending' AND lease_until<=?))
               ORDER BY id LIMIT 1''', (now, now),
        ).fetchone()
        if not row:
            connection.rollback()
            return False
        connection.execute(
            "UPDATE keyword_action_deliveries SET state='sending', attempts=attempts+1, lease_until=? WHERE id=?",
            (now + 120, row['id']),
        )
        connection.commit()
        payload = json.loads(row['payload'])
        action = get_settings_manager().get_keyword_action(row['action_id'])
        context = payload['context']
        trigger_ids = context.get('trigger_keyword_ids', payload['action'].get('keyword_ids', []))
        trigger = get_settings_manager().get_keyword_action_triggers(
            trigger_ids, context['channel_id'], action_id=row['action_id'],
        ).get(row['action_id'])
        permitted = trigger and trigger['group_ids'].intersection(context.get('group_ids', [row['group_id']]))
        recording_exists = connection.execute('SELECT 1 FROM recordings WHERE id=?', (row['recording_id'],)).fetchone()
        if not recording_exists or not action or action['kind'] != 'email' or not permitted:
            connection.execute("UPDATE keyword_action_deliveries SET state='cancelled', lease_until=0 WHERE id=?", (row['id'],))
        else:
            try:
                send_email({**action, **payload['action']}, payload['context'], settings)
            except Exception as error:
                attempts = row['attempts'] + 1
                # Keep server replies, credentials, and message content out of errors.
                connection.execute(
                    '''UPDATE keyword_action_deliveries SET state=?, next_attempt_at=?, lease_until=0, last_error=? WHERE id=?''',
                    ('failed' if attempts >= 3 or isinstance(error, PartialEmailDeliveryError) else 'pending', now + 30 * 2 ** (attempts - 1),
                     type(error).__name__, row['id']),
                )
            else:
                connection.execute(
                    "UPDATE keyword_action_deliveries SET state='sent', sent_at=?, lease_until=0, last_error=NULL WHERE id=?",
                    (int(time.time()), row['id']),
                )
        connection.commit()
        return True
    finally:
        connection.close()


def popup_actions_for_recordings(connection, ids, principal):
    groups = {group['id'] for group in principal.get('groups', []) if isinstance(group, dict)}
    if not ids or not groups:
        return {}
    result = {}
    placeholders = ','.join('?' for _ in ids)
    for row in connection.execute(
        f"SELECT id, recording_id, action_id, group_id, payload FROM keyword_action_deliveries WHERE kind='popup' AND recording_id IN ({placeholders})", ids,
    ):
        payload = json.loads(row['payload']) if isinstance(row['payload'], str) else row['payload']
        action = payload['action']
        context = payload['context']
        trigger = get_settings_manager().get_keyword_action_triggers(
            context.get('trigger_keyword_ids', action.get('keyword_ids', [])),
            context['channel_id'], action_id=row['action_id'],
        ).get(row['action_id'])
        if not trigger or trigger['action']['kind'] != 'popup' or not groups.intersection(
            trigger['group_ids'], context.get('group_ids', [row['group_id']])
        ):
            continue
        result.setdefault(row['recording_id'], []).append({
            'id': row['id'], 'name': action['name'], 'autoplay': action.get('autoplay', True),
        })
    return result


def delivery_history():
    connection = connect_sqlite(Config.get_recordings_db_path(), row_factory=True)
    try:
        return [dict(row) for row in connection.execute(
            '''SELECT id, recording_id, action_id, kind, state, attempts, last_error, created_at, sent_at
               FROM keyword_action_deliveries ORDER BY id DESC LIMIT 50'''
        )]
    finally:
        connection.close()


def start_worker():
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return
        _stop.clear()

        def run():
            while not _stop.is_set():
                try:
                    if process_email_delivery():
                        continue
                except Exception:
                    logger.exception('Keyword action worker could not process its outbox')
                _stop.wait(2)

        _worker = threading.Thread(target=run, name='keyword-email-delivery', daemon=True)
        _worker.start()


def stop_worker():
    _stop.set()
