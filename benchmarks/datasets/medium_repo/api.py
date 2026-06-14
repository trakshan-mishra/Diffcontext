from services import create_user, update_user

def register(payload):
    return create_user(payload)

def edit_profile(payload):
    return update_user(payload)

def delete_account(user_id):
    return {"deleted": user_id}