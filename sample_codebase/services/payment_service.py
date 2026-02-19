"""Sample payment service for testing TrustBot validation."""

from utils.validator import validate_amount
from services.auth_service import AuthService


class PaymentService:
    def __init__(self, auth_service: AuthService, payment_gateway):
        self.auth = auth_service
        self.gateway = payment_gateway

    def process_payment(self, token: str, amount: float, currency: str) -> dict:
        session = self.auth.verify_session(token)
        user_id = session["user_id"]

        validate_amount(amount, currency)

        result = self.gateway.charge(
            user_id=user_id,
            amount=amount,
            currency=currency,
        )

        return {
            "transaction_id": result["id"],
            "status": result["status"],
            "amount": amount,
            "currency": currency,
        }

    def refund(self, token: str, transaction_id: str) -> dict:
        session = self.auth.verify_session(token)
        result = self.gateway.refund(transaction_id=transaction_id)
        return {"refund_id": result["id"], "status": result["status"]}
