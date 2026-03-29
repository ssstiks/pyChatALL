"""Authentication module for JWT and API_KEY support."""
import os
from datetime import datetime, timedelta
from typing import Optional
from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
import jwt
import config

# Configuration
API_KEY_NAME = "X-API-KEY"
API_KEY_HEADER = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
BEARER_SCHEME = HTTPBearer(auto_error=False)

# JWT settings
JWT_SECRET = os.getenv("JWT_SECRET", "dev-jwt-secret-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24


def verify_api_key(api_key: str = Security(API_KEY_HEADER)) -> str:
    """Verify API key from header."""
    if not api_key:
        raise HTTPException(status_code=403, detail="API key required")
    # Accept BOT_TOKEN as valid API key for backward compatibility
    if api_key == config.BOT_TOKEN:
        return api_key
    raise HTTPException(status_code=403, detail="Invalid API key")


def verify_bearer_token(token: Optional[HTTPAuthorizationCredentials] = Security(BEARER_SCHEME)) -> str:
    """Verify JWT token from Authorization header."""
    if not token:
        raise HTTPException(status_code=403, detail="Bearer token required")
    try:
        payload = jwt.decode(token.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub", "")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=403, detail="Invalid token")


def get_combined_auth(api_key: Optional[str] = Security(API_KEY_HEADER),
                      bearer: Optional[HTTPAuthorizationCredentials] = Security(BEARER_SCHEME)) -> str:
    """Accept either API_KEY or Bearer token."""
    if api_key and api_key == config.BOT_TOKEN:
        return api_key
    if bearer:
        try:
            payload = jwt.decode(bearer.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            return payload.get("sub", "")
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            pass
    raise HTTPException(status_code=403, detail="Invalid or missing credentials")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt
