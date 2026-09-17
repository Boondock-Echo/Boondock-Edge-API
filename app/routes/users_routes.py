"""
User management routes.
Handles user CRUD operations, MFA admin functions, and device management.
"""
import json
import os
import logging
from flask import Blueprint, jsonify, request
from flasgger import swag_from

from ..utils.logging_setup import error_logger
from ..middleware.auth_middleware import require_admin
from ..utils.password_utils import hash_password
from ..utils.mfa_utils import generate_mfa_secret
from ..utils.profile_utils import get_all_features
from ..routes.route_utils import init_users
from ..services.settings_manager import get_settings_manager

_settings_manager = get_settings_manager()

users_bp = Blueprint('users', __name__)


@users_bp.route('/users', methods=['GET'])
@require_admin
@swag_from({
    'tags': ['Users'],
    'summary': 'Get all users',
    'responses': {
        '200': {'description': 'List of users'},
        '500': {'description': 'Server error'}
    }
})
def get_users():
    """Fetch all users (without passwords)"""
    init_users()
    try:
        users = _settings_manager.get_all_users()
        
        # Remove passwords from response
        safe_users = {}
        for email, user_data in users.items():
            safe_users[email] = {k: v for k, v in user_data.items() if k != 'password'}
        
        return jsonify(safe_users)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@users_bp.route('/users/<email>', methods=['GET'])
@require_admin
@swag_from({
    'tags': ['Users'],
    'summary': 'Get user by email',
    'parameters': [
        {
            'name': 'email',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'User email address'
        }
    ],
    'responses': {
        '200': {'description': 'User data'},
        '404': {'description': 'User not found'},
        '500': {'description': 'Server error'}
    }
})
def get_user_by_email(email):
    """Fetch user data for a specific email (without password)."""
    init_users()
    try:
        user = _settings_manager.get_user(email)
        if user:
            # Remove password from response
            safe_user = {k: v for k, v in user.items() if k != 'password'}
            return jsonify({email: safe_user}), 200
        else:
            return jsonify({'error': 'User not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@users_bp.route('/users', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Users'],
    'summary': 'Create a new user',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['email', 'password', 'name', 'role'],
                'properties': {
                    'email': {'type': 'string'},
                    'password': {'type': 'string'},
                    'name': {'type': 'string'},
                    'role': {'type': 'string', 'enum': ['admin', 'member']}
                }
            }
        }
    ],
    'responses': {
        '201': {'description': 'User created successfully'},
        '400': {'description': 'Bad request'},
        '409': {'description': 'Email already exists'},
        '500': {'description': 'Server error'}
    }
})
def create_user():
    """Create a new user"""
    try:
        data = request.get_json()
        
        # Validate required fields
        required_fields = ['email', 'password', 'name', 'role']
        if not all(field in data for field in required_fields):
            return jsonify({'error': 'Missing required fields'}), 400
            
        # Validate role
        if data['role'] not in ['admin', 'member']:
            return jsonify({'error': 'Invalid role'}), 400
            
        if _settings_manager.get_user(data['email']):
            return jsonify({'error': 'Email already exists'}), 409

        groups = data.get('groups', [])
        if not isinstance(groups, list) or any(
            not isinstance(group_id, int) or not _settings_manager.get_group_by_id(group_id)
            for group_id in groups
        ):
            return jsonify({'error': 'Invalid groups'}), 400
        
        # Create new user object with hashed password
        new_user = {
            'name': data['name'],
            'password': hash_password(data['password']),  # Hash password before storing
            'role': data['role'],
            'status': 'Active',
            'groups': groups,
            'accessLevel': 'Level 3' if data['role'] == 'admin' else 'Level 1'
        }
        
        _settings_manager.save_user(data['email'], new_user)
            
        return jsonify({'message': 'User created successfully', 'user': {**new_user, 'email': data['email']}}), 201
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@users_bp.route('/users/<email>', methods=['PUT'])
@require_admin
@swag_from({
    'tags': ['Users'],
    'summary': 'Update user',
    'parameters': [
        {
            'name': 'email',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'User email address'
        },
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string'},
                    'role': {'type': 'string', 'enum': ['admin', 'member']},
                    'password': {'type': 'string'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'User updated successfully'},
        '400': {'description': 'Bad request'},
        '404': {'description': 'User not found'},
        '500': {'description': 'Server error'}
    }
})
def update_user(email):
    """Update an existing user"""
    try:
        data = request.get_json()
        
        user = _settings_manager.get_user(email)
        if not user:
            return jsonify({'error': 'User not found'}), 404
            
        # Update allowed fields
        if 'name' in data:
            user['name'] = data['name']
        if 'role' in data:
            if data['role'] not in ['admin', 'member']:
                return jsonify({'error': 'Invalid role'}), 400
            user['role'] = data['role']
            user['accessLevel'] = "Level 3" if data['role'] == 'admin' else "Level 1"
        if 'groups' in data:
            groups = data['groups']
            if not isinstance(groups, list) or any(
                not isinstance(group_id, int) or not _settings_manager.get_group_by_id(group_id)
                for group_id in groups
            ):
                return jsonify({'error': 'Invalid groups'}), 400
            user['groups'] = groups
        if 'password' in data and data['password']:
            user['password'] = hash_password(data['password'])
        _settings_manager.save_user(email, user)
            
        return jsonify({
            'message': 'User updated successfully',
            'user': {**user, 'email': email}
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@users_bp.route('/users/<email>', methods=['DELETE'])
@require_admin
@swag_from({
    'tags': ['Users'],
    'summary': 'Delete user',
    'parameters': [
        {
            'name': 'email',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'User email address'
        }
    ],
    'responses': {
        '200': {'description': 'User deleted successfully'},
        '404': {'description': 'User not found'},
        '500': {'description': 'Server error'}
    }
})
def delete_user(email):
    """Delete a user"""
    try:
        if not _settings_manager.get_user(email):
            return jsonify({'error': 'User not found'}), 404
            
        # Prevent deleting the last admin
        # remaining_admins = sum(1 for u in users.values() if u['role'] == 'admin')
        # if users[email]['role'] == 'admin' and remaining_admins <= 1:
        #     return jsonify({'error': 'Cannot delete the last admin user'}), 400
            
        _settings_manager.delete_user(email)
            
        return jsonify({'message': 'User deleted successfully'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@users_bp.route('/users/<email>/permissions', methods=['GET'])
@require_admin
@swag_from({
    'tags': ['Users'],
    'summary': 'Get user permissions based on their profile',
    'parameters': [
        {
            'name': 'email',
            'in': 'path',
            'type': 'string',
            'required': True
        }
    ],
    'responses': {
        '200': {'description': 'User permissions'},
        '401': {'description': 'Unauthorized'},
        '404': {'description': 'User not found'},
        '500': {'description': 'Server error'}
    }
})
def get_user_permissions(email):
    """Get user permissions based on their assigned profile."""
    try:
        user = _settings_manager.get_user(email)
        if not user:
            return jsonify({'error': 'User not found'}), 404

        principal = _settings_manager.get_principal('user', email)
        
        # Admin role always has all permissions
        if user.get('role') == 'admin':
            all_features = {f['key']: True for f in get_all_features()}
            return jsonify({
                'groups': user.get('groups', []),
                'features': all_features,
                'isAdmin': True,
                'authenticated': True
            }), 200
        
        return jsonify({
            'groups': user.get('groups', []),
            'features': {permission: True for permission in principal['permissions']},
            'isAdmin': False,
            'authenticated': True
        }), 200
        
    except Exception as e:
        logging.error(f"Get user permissions error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@users_bp.route('/users/<email>/devices', methods=['GET'])
@require_admin
@swag_from({
    'tags': ['Users'],
    'summary': 'Get devices and login history for user',
    'responses': {
        '200': {'description': 'Devices and login history returned'},
        '401': {'description': 'Unauthorized'},
        '404': {'description': 'User not found'},
        '500': {'description': 'Server error'}
    }
})
def get_user_devices(email):
    """Get devices and login history for a user."""
    try:
        user = _settings_manager.get_user(email)
        if not user:
            return jsonify({'error': 'User not found'}), 404

        return jsonify({
            'devices': user.get('devices', []),
            'login_history': user.get('login_history', [])
        }), 200

    except Exception as e:
        logging.error(f"Get user devices error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@users_bp.route('/users/<email>/devices/<device_id>', methods=['DELETE'])
@require_admin
@swag_from({
    'tags': ['Users'],
    'summary': 'Remove a device from user account',
    'responses': {
        '200': {'description': 'Device removed'},
        '401': {'description': 'Unauthorized'},
        '404': {'description': 'Device not found'},
        '500': {'description': 'Server error'}
    }
})
def remove_device(email, device_id):
    """Remove a device from user account."""
    try:
        user = _settings_manager.get_user(email)
        if not user:
            return jsonify({'error': 'User not found'}), 404

        devices = user.get('devices', [])

        # Find and remove device
        device_found = False
        user['devices'] = [d for d in devices if d.get('device_id') != device_id]
        device_found = len(user['devices']) < len(devices)

        if not device_found:
            return jsonify({'error': 'Device not found'}), 404

        _settings_manager.save_user(email, user)

        return jsonify({'message': 'Device removed successfully'}), 200

    except Exception as e:
        logging.error(f"Remove device error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@users_bp.route('/users/<email>/mfa/enable', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Users'],
    'summary': 'Enable MFA for a user (admin only)',
    'parameters': [
        {
            'name': 'email',
            'in': 'path',
            'type': 'string',
            'required': True
        }
    ],
    'responses': {
        '200': {'description': 'MFA enabled'},
        '403': {'description': 'Admin access required'},
        '404': {'description': 'User not found'},
        '500': {'description': 'Server error'}
    }
})
def admin_enable_mfa(email):
    """Enable MFA for a user (admin/superadmin only)."""
    try:
        user = _settings_manager.get_user(email)
        if not user:
            return jsonify({'error': 'User not found'}), 404

        # Generate secret if not exists
        if not user.get('mfa_secret'):
            secret = generate_mfa_secret()
            user['mfa_secret'] = secret

        user['mfa_enabled'] = True

        _settings_manager.save_user(email, user)

        return jsonify({'message': f'MFA enabled for {email}'}), 200

    except Exception as e:
        logging.error(f"Admin enable MFA error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@users_bp.route('/users/<email>/mfa/disable', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Users'],
    'summary': 'Disable MFA for a user (admin only)',
    'parameters': [
        {
            'name': 'email',
            'in': 'path',
            'type': 'string',
            'required': True
        }
    ],
    'responses': {
        '200': {'description': 'MFA disabled'},
        '403': {'description': 'Admin access required'},
        '404': {'description': 'User not found'},
        '500': {'description': 'Server error'}
    }
})
def admin_disable_mfa(email):
    """Disable MFA for a user (admin/superadmin only)."""
    try:
        user = _settings_manager.get_user(email)
        if not user:
            return jsonify({'error': 'User not found'}), 404

        user['mfa_enabled'] = False

        # Save users
        _settings_manager.save_user(email, user)

        return jsonify({'message': f'MFA disabled for {email}'}), 200

    except Exception as e:
        logging.error(f"Admin disable MFA error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@users_bp.route('/users/<email>/mfa/reset', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Users'],
    'summary': 'Reset/Clear MFA for a user (admin only) - allows user to login without MFA and setup again',
    'parameters': [
        {
            'name': 'email',
            'in': 'path',
            'type': 'string',
            'required': True
        }
    ],
    'responses': {
        '200': {'description': 'MFA reset successfully'},
        '403': {'description': 'Admin access required'},
        '404': {'description': 'User not found'},
        '500': {'description': 'Server error'}
    }
})
def admin_reset_mfa(email):
    """Reset/Clear MFA for a user (admin/superadmin only). This clears MFA completely so user can login with just password and setup MFA again."""
    try:
        user = _settings_manager.get_user(email)
        if not user:
            return jsonify({'error': 'User not found'}), 404

        # Clear all MFA data
        user['mfa_enabled'] = False
        user.pop('mfa_secret', None)
        user.pop('mfa_secret_temp', None)
        # Note: We don't clear mfa_enforced here - that's separate

        _settings_manager.save_user(email, user)

        return jsonify({'message': f'MFA reset for {email}. User can now login with password only and setup MFA again.'}), 200

    except Exception as e:
        logging.error(f"Admin reset MFA error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@users_bp.route('/users/<email>/mfa/enforce', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['Users'],
    'summary': 'Enforce MFA for a user (admin only) - shows reminder popup on login',
    'parameters': [
        {
            'name': 'email',
            'in': 'path',
            'type': 'string',
            'required': True
        },
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['enforce'],
                'properties': {
                    'enforce': {'type': 'boolean'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'MFA enforcement updated'},
        '403': {'description': 'Admin access required'},
        '404': {'description': 'User not found'},
        '500': {'description': 'Server error'}
    }
})
def admin_enforce_mfa(email):
    """Enforce MFA for a user (admin/superadmin only). When enforced, user will see reminder popup on login."""
    try:
        data = request.get_json()
        enforce = data.get('enforce', False)

        user = _settings_manager.get_user(email)
        if not user:
            return jsonify({'error': 'User not found'}), 404

        user['mfa_enforced'] = enforce

        _settings_manager.save_user(email, user)

        action = 'enforced' if enforce else 'removed'
        return jsonify({'message': f'MFA enforcement {action} for {email}'}), 200

    except Exception as e:
        logging.error(f"Admin enforce MFA error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
