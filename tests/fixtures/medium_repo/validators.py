"""Validation helpers, used by service.py (cross-file dependency)."""

def is_valid_email(email):
    return "@" in email and "." in email.split("@")[-1]

def is_positive(amount):
    return amount > 0
