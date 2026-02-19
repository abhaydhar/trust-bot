"""Sample authentication service for testing TrustBot validation."""

from utils.token_manager import generate_token, validate_token
from utils.password_hasher import hash_password, verify_password


class AuthService:
    def __init__(self, user_repository):
        self.user_repo = user_repository

    def login(self, username: str, password: str) -> dict:
        user = self.user_repo.find_by_username(username)
        if user is None:
            raise ValueError("User not found")

        if not verify_password(password, user["password_hash"]):
            raise ValueError("Invalid password")

        token = generate_token(user["id"], user["role"])
        return {"token": token, "user_id": user["id"]}

    def register(self, username: str, password: str, email: str) -> dict:
        existing = self.user_repo.find_by_username(username)
        if existing:
            raise ValueError("Username already taken")

        password_hash = hash_password(password)
        user = self.user_repo.create_user(
            username=username,
            password_hash=password_hash,
            email=email,
        )
        token = generate_token(user["id"], "user")
        return {"token": token, "user_id": user["id"]}

    def verify_session(self, token: str) -> dict:
        payload = validate_token(token)
        if payload is None:
            raise ValueError("Invalid or expired token")
        return payload
