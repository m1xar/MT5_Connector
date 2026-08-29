from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from utils.config import settings

bearer_scheme = HTTPBearer(auto_error=False)


async def verify_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str | None:
    if not settings.api_token:
        return None
    if credentials is None or credentials.credentials != settings.api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
        )
    return credentials.credentials
