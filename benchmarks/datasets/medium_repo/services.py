from utils import validate, sanitize, hash_password

def create_user(data):
    if not validate(data):
        raise ValueError("Invalid data")
    data["name"] = sanitize(data["name"])
    data["password"] = hash_password(data["password"])
    return data

def update_user(data):
    if not validate(data):
        raise ValueError("Invalid data")
    data["name"] = sanitize(data["name"])
    return data