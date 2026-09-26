import secrets

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

api_key_header = APIKeyHeader(name="X-API-KEY", auto_error=False)


def verify_api_key(request: Request, api_key: str | None = Security(api_key_header)) -> None:
    expected: str = request.app.state.api_token
    if not api_key or not secrets.compare_digest(api_key.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid or missing API key"
        )
