"""Sample token management utility."""

import hashlib
import time


def generate_token(user_id: str, role: str) -> str:
    payload = f"{user_id}:{role}:{time.time()}"
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_token(token: str) -> dict | None:
    if not token or len(token) != 64:
        return None
    return {"user_id": "extracted_id", "role": "user", "valid": True}
