"""Administrator-only authorization group routes."""
from flask import Blueprint, current_app, jsonify, request
from ..middleware.auth_middleware import require_admin
from ..services.settings_manager import get_settings_manager

groups_bp = Blueprint('groups', __name__)
_settings_manager = get_settings_manager()

def _valid_permissions():
    """Use the permissions enforced by the registered API routes as the catalog."""
    return sorted({
        permission
        for view in current_app.view_functions.values()
        for permission in getattr(view, 'access_policy', {}).get('permissions', [])
    })


@groups_bp.route('/groups/permissions', methods=['GET'])
@require_admin
def get_group_permissions():
    return jsonify(_valid_permissions()), 200


def _permissions(data):
    value = data.get('permissions')
    if not isinstance(value, list) or not all(
        isinstance(permission, str) and permission.strip() for permission in value
    ):
        return None
    return list(dict.fromkeys(permission.strip() for permission in value))


@groups_bp.route('/groups', methods=['GET'])
@require_admin
def get_groups():
    return jsonify(list(_settings_manager.get_all_groups().values())), 200

@groups_bp.route('/groups/<int:group_id>', methods=['GET'])
@require_admin
def get_group(group_id):
    group = _settings_manager.get_group_by_id(group_id)
    return (jsonify(group), 200) if group else (jsonify({'error': 'Group not found'}), 404)

@groups_bp.route('/groups', methods=['POST'])
@require_admin
def create_group():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    permissions = _permissions(data)
    if not name or permissions is None:
        return jsonify({'error': 'name and permissions list are required'}), 400
    if _settings_manager.get_group(name):
        return jsonify({'error': 'Group already exists'}), 409
    group_id = _settings_manager.save_group({
        'name': name,
        'description': data.get('description', ''),
        'is_default': bool(data.get('is_default', False)),
        'permissions': permissions,
        'default_preferences': data.get('default_preferences', {
            'inbox': {}, 'reports': {}
        }),
        'keywords': data.get('keywords', []),
    })
    if group_id < 0:
        return jsonify({'error': 'Unable to create group'}), 500
    return jsonify(_settings_manager.get_group_by_id(group_id)), 201


@groups_bp.route('/groups/<int:group_id>', methods=['PATCH'])
@require_admin
def update_group(group_id):
    group = _settings_manager.get_group_by_id(group_id)
    if not group:
        return jsonify({'error': 'Group not found'}), 404
    data = request.get_json(silent=True) or {}
    if 'permissions' in data:
        permissions = _permissions(data)
        if permissions is None:
            return jsonify({'error': 'permissions must be a list of strings'}), 400
        group['permissions'] = permissions
    for field in ('name', 'description', 'is_default', 'default_preferences', 'keywords'):
        if field in data:
            group[field] = data[field]
    if not str(group.get('name') or '').strip():
        return jsonify({'error': 'Group name is required'}), 400
    if _settings_manager.save_group(group, group_id) < 0:
        return jsonify({'error': 'Unable to update group'}), 500
    return jsonify(_settings_manager.get_group_by_id(group_id)), 200


@groups_bp.route('/groups/<int:group_id>', methods=['DELETE'])
@require_admin
def delete_group(group_id):
    if not _settings_manager.get_group_by_id(group_id):
        return jsonify({'error': 'Group not found'}), 404
    if not _settings_manager.delete_group(group_id):
        return jsonify({'error': 'Group is default or referenced and cannot be deleted'}), 409
    return '', 204


@groups_bp.route('/keywords/<int:keyword_id>', methods=['DELETE'])
@require_admin
def delete_keyword(keyword_id):
    if not _settings_manager.delete_keyword(keyword_id):
        return jsonify({'error': 'Keyword not found'}), 404
    return '', 204


@groups_bp.route('/keywords/<int:keyword_id>/actions', methods=['PUT'])
@require_admin
def set_keyword_actions(keyword_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {'action_ids'}:
        return jsonify({'error': 'Provide action_ids only'}), 400
    try:
        return jsonify(_settings_manager.set_keyword_actions(keyword_id, data['action_ids']))
    except LookupError as error:
        return jsonify({'error': str(error)}), 404
    except ValueError as error:
        return jsonify({'error': str(error)}), 400
