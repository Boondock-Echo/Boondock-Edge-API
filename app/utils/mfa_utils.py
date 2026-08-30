# app/utils/mfa_utils.py
import pyotp
import qrcode
from io import BytesIO
import base64
import logging

log = logging.getLogger(__name__)

def generate_mfa_secret():
    """Generate a new TOTP secret for MFA."""
    return pyotp.random_base32()

def generate_mfa_qr_code(secret: str, email: str, issuer_name: str = "Boondock Edge") -> str:
    """
    Generate a QR code image as base64 string for MFA setup.
    
    Args:
        secret: TOTP secret key
        email: User's email address
        issuer_name: Name of the service/application
    
    Returns:
        Base64 encoded PNG image string
    """
    try:
        # Create TOTP URI
        totp = pyotp.TOTP(secret)
        uri = totp.provisioning_uri(
            name=email,
            issuer_name=issuer_name
        )
        
        # Generate QR code
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(uri)
        qr.make(fit=True)
        
        # Create image
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Convert to base64
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode()
        
        return f"data:image/png;base64,{img_str}"
    except Exception as e:
        log.error(f"Error generating QR code: {e}")
        raise

def verify_totp_code(secret: str, code: str, valid_window: int = 1) -> bool:
    """
    Verify a TOTP code against a secret.
    
    Args:
        secret: TOTP secret key
        code: 6-digit code from authenticator app
        valid_window: Number of time steps to allow for clock drift (default: 1)
    
    Returns:
        True if code is valid, False otherwise
    """
    try:
        if not secret or not code:
            return False
        
        # Remove any whitespace
        code = code.strip().replace(' ', '')
        
        # Validate code format (6 digits)
        if not code.isdigit() or len(code) != 6:
            return False
        
        totp = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=valid_window)
    except Exception as e:
        log.error(f"Error verifying TOTP code: {e}")
        return False

def get_totp_uri(secret: str, email: str, issuer_name: str = "Boondock Edge") -> str:
    """Get the TOTP provisioning URI (for manual entry if QR code fails)."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=issuer_name)

