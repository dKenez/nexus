from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader
from starlette.status import HTTP_403_FORBIDDEN
import os
from dotenv import load_dotenv

load_dotenv()

API_TOKEN = os.getenv("API_TOKEN")
api_key_header = APIKeyHeader(name="X-API-KEY", auto_error=False)


def verify_api_key(api_key: str = Security(api_key_header)):
    if api_key != API_TOKEN:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN,
            detail="Invalid or missing API Key",
        )
