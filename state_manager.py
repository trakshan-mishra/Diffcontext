import json
import os

STATE_FILE = "state.json"


def load_state():

    if not os.path.exists(STATE_FILE):
        return {}

    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)