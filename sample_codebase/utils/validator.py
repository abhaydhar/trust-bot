"""Sample validation utility."""


def validate_amount(amount: float, currency: str) -> None:
    if amount <= 0:
        raise ValueError("Amount must be positive")
    if currency not in ("USD", "EUR", "GBP"):
        raise ValueError(f"Unsupported currency: {currency}")
