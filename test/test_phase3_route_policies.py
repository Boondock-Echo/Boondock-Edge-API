from app.routes import auth_routes, external_api_routes, groups_routes, recordings_routes, users_routes
from app.routes import (
    branding_routes, channels_routes, frequencies_routes, incident_reports_routes,
    logs_routes, maintenance_routes, pagination_routes, settings_routes, tags_routes,
    transcription_routes,
)


def _policy(function):
    return function.access_policy['type']


def test_authentication_routes_have_declared_policies():
    assert not hasattr(auth_routes.login, 'access_policy')
    assert _policy(auth_routes.logout) == 'auth'
    assert _policy(auth_routes.verify_token) == 'auth'
    assert all(_policy(function) == 'auth' for function in (
        auth_routes.mfa_setup,
        auth_routes.mfa_verify_setup,
        auth_routes.mfa_disable,
        auth_routes.mfa_status,
    ))


def test_every_user_and_group_route_is_admin_only():
    user_handlers = (
        users_routes.get_users, users_routes.get_user_by_email,
        users_routes.create_user, users_routes.update_user, users_routes.delete_user,
        users_routes.get_user_permissions, users_routes.get_user_devices,
        users_routes.remove_device, users_routes.admin_enable_mfa,
        users_routes.admin_disable_mfa, users_routes.admin_reset_mfa,
        users_routes.admin_enforce_mfa,
    )
    group_handlers = (
        groups_routes.get_groups, groups_routes.get_group,
        groups_routes.create_group, groups_routes.update_group,
        groups_routes.delete_group, groups_routes.get_permissions,
    )
    assert all(_policy(function) == 'admin' for function in user_handlers + group_handlers)


def test_corrected_external_and_queue_policies():
    assert external_api_routes.list_transcriptions.access_policy['permissions'] == [
        'transcriptions.read'
    ]
    assert all(_policy(function) == 'admin' for function in (
        recordings_routes.upload_audio_queue,
        recordings_routes.get_queue_status,
        recordings_routes.requeue_task,
    ))


def test_next_ten_route_files_have_visible_policies(monkeypatch):
    monkeypatch.setenv('WERKZEUG_RUN_MAIN', 'false')
    from app import create_app

    app = create_app()
    blueprint_names = {
        'branding', 'channels', 'frequencies', 'incident_reports', 'logs',
        'maintenance', 'pagination', 'tags', 'transcription',
    }
    for rule in app.url_map.iter_rules():
        if rule.endpoint.split('.')[0] in blueprint_names:
            assert hasattr(app.view_functions[rule.endpoint], 'access_policy'), rule.rule

    protected_settings = (
        settings_routes.get_settings, settings_routes.get_summary_metrics,
        settings_routes.update_settings, settings_routes.restart_system_service,
        settings_routes.reboot_application, settings_routes.add_keyword,
        settings_routes.remove_keyword,
    )
    assert all(hasattr(handler, 'access_policy') for handler in protected_settings)


def test_third_route_batch_is_protected_or_explicitly_public(monkeypatch):
    monkeypatch.setenv('WERKZEUG_RUN_MAIN', 'false')
    from app import create_app

    app = create_app()
    public = {
        ('GET', '/api/docs/<path:filename>'),
        ('GET', '/docs'),
        ('GET', '/docs/'),
        ('GET', '/docs/<path:path>'),
        ('GET', '/api/health/devices'),
        ('GET', '/api/health/devices/<mac>'),
        ('GET', '/api/health/system'),
    }
    blueprints = {
        'hallucinations', 'hotspot', 'radio', 'recorders', 'history',
        'release_notes', 's3', 'docs', 'docusaurus', 'health', 'notification',
    }
    for rule in app.url_map.iter_rules():
        if rule.endpoint.split('.')[0] not in blueprints:
            continue
        view = app.view_functions[rule.endpoint]
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            assert (method, rule.rule) in public or hasattr(view, 'access_policy'), rule.rule


def test_remaining_route_files_are_protected_or_explicitly_public(monkeypatch):
    monkeypatch.setenv('WERKZEUG_RUN_MAIN', 'false')
    from app import create_app

    app = create_app()
    public = {
        ('POST', '/api/event'),
        ('POST', '/api/v1/events'),
        ('GET', '/static/<path:filename>'),
        ('GET', '/assets/<path:filename>'),
        ('GET', '/<path:path>'),
        ('GET', '/'),
    }
    for rule in app.url_map.iter_rules():
        if rule.endpoint.split('.')[0] not in {'device', 'react', 'release_package'}:
            continue
        view = app.view_functions[rule.endpoint]
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            assert (method, rule.rule) in public or hasattr(view, 'access_policy'), rule.rule

    assert all(
        app.view_functions[rule.endpoint].access_policy['type'] == 'admin'
        for rule in app.url_map.iter_rules()
        if rule.endpoint.split('.')[0] == 'release_package'
    )


def test_every_api_route_has_a_policy_or_is_public(monkeypatch):
    monkeypatch.setenv('WERKZEUG_RUN_MAIN', 'false')
    from app import create_app

    app = create_app()
    public = {
        ('POST', '/api/auth/login'),
        ('POST', '/api/event'),
        ('POST', '/api/v1/events'),
        ('GET', '/api/time'),
        ('GET', '/api/ping'),
        ('GET', '/api/docs/<path:filename>'),
        ('GET', '/api/health/devices'),
        ('GET', '/api/health/devices/<mac>'),
        ('GET', '/api/health/system'),
    }
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith('/api/'):
            continue
        view = app.view_functions[rule.endpoint]
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            assert (method, rule.rule) in public or hasattr(view, 'access_policy'), (
                method, rule.rule
            )
