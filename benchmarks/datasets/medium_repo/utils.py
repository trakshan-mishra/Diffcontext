def validate(data):
    return isinstance(data, dict) and "name" in data

def sanitize(text):
    return text.strip().lower()

def hash_password(password):
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()