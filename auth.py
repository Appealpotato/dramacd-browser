import secrets
from typing import Optional

from fastapi import Header, HTTPException, Query

from config import API_KEY


async def require_api_key(
    x_api_key: Optional[str] = Header(default=None),
    api_key: Optional[str] = Query(default=None),
):
    """Optional API key guard for mutating endpoints."""
    if not API_KEY:
        return

    provided = x_api_key or api_key
    if not provided or not secrets.compare_digest(provided, API_KEY):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: invalid or missing API key.",
        )
