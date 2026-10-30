"""Group permission choices come from the API's actual authorization policies."""
from flask import Flask
import pytest

from app.routes.groups_routes import groups_bp
from app.routes.tags_routes import tags_bp
from app.services.settings_manager import get_settings_manager


@pytest.fixture
def group_client(admin_auth):
    app = Flask(__name__)
    app.register_blueprint(groups_bp, url_prefix='/api')
    app.register_blueprint(tags_bp, url_prefix='/api')
    return app.test_client(), admin_auth


def test_permission_catalog_matches_registered_routes_and_requires_admin(group_client):
    client, headers = group_client
    assert client.get('/api/groups/permissions').status_code == 401
    response = client.get('/api/groups/permissions', headers=headers)
    assert response.status_code == 200
    assert response.get_json() == ['tag.read', 'tag.write']
    manager = get_settings_manager()
    assert manager.save_user('member@example.com', {
        'name': 'Member', 'password': 'unused', 'role': 'member', 'groups': [],
    })
    token, _ = manager.issue_credential('user', 'member@example.com', token='member-test-token')
    assert client.get('/api/groups/permissions', headers={'Authorization': f'Bearer {token}'}).status_code == 403


def test_create_and_edit_group_with_selected_permissions(group_client):
    client, headers = group_client
    response = client.post('/api/groups', json={
        'name': 'Operators', 'permissions': ['tag.read', 'tag.read'],
    }, headers=headers)
    assert response.status_code == 201
    group = response.get_json()
    assert group['permissions'] == ['tag.read']
    response = client.patch(f"/api/groups/{group['id']}", json={
        'permissions': ['tag.read', 'tag.write'],
    }, headers=headers)
    assert response.status_code == 200
    assert response.get_json()['permissions'] == ['tag.read', 'tag.write']
    response = client.patch(f"/api/groups/{group['id']}", json={'permissions': []}, headers=headers)
    assert response.status_code == 200
    assert response.get_json()['permissions'] == []


@pytest.mark.parametrize('permissions', ['tag.read', [12]])
def test_invalid_group_permissions_are_rejected_without_changes(group_client, permissions):
    client, headers = group_client
    response = client.post('/api/groups', json={
        'name': 'Invalid', 'permissions': permissions,
    }, headers=headers)
    assert response.status_code == 400
    assert get_settings_manager().get_group('Invalid') is None
    group_id = get_settings_manager().save_group({'name': 'Existing', 'permissions': ['tag.read']})
    response = client.patch(f'/api/groups/{group_id}', json={'permissions': permissions}, headers=headers)
    assert response.status_code == 400
    assert get_settings_manager().get_group_by_id(group_id)['permissions'] == ['tag.read']

@pytest.mark.parametrize('permissions', [
    ['made.up'], ['tag.read', 'made.up'], ["'); DROP TABLE groups; --"],
])
def test_unknown_permission_strings_are_stored_as_data(group_client, permissions):
    client, headers = group_client
    response = client.post('/api/groups', json={
        'name': 'Operators', 'permissions': permissions,
    }, headers=headers)
    assert response.status_code == 201
    group = response.get_json()
    assert group['permissions'] == permissions
    response = client.patch(f"/api/groups/{group['id']}", json={
        'permissions': permissions + ['tag.write'],
    }, headers=headers)
    assert response.status_code == 200
    assert response.get_json()['permissions'] == permissions + ['tag.write']
    assert client.get('/api/groups', headers=headers).status_code == 200
