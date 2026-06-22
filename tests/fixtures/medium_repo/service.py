"""Service layer -- imports and calls into models.py and validators.py,
giving the graph builder real cross-file edges to find.
"""

from .models import User, Order
from .validators import is_valid_email, is_positive


def create_user(name, email):
    if not is_valid_email(email):
        raise ValueError("invalid email")
    return User(name, email)


def create_order(user, total):
    if not is_positive(total):
        raise ValueError("invalid total")
    order = Order(user, total)
    return order.summary()


def onboard_user(name, email, first_order_total):
    user = create_user(name, email)
    return create_order(user, first_order_total)
