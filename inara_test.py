"""
inara_test.py -- pulls your own commander profile from Inara.

Run this on YOUR machine, not in a sandbox -- it needs real network access
to inara.cz.

One-time setup:
    pip install requests

    Set your API key as an environment variable rather than typing it into
    this file. That way it never ends up in a git commit, a screenshot, or
    a copy of this script you hand to someone else.

        Windows (PowerShell):   $env:INARA_API_KEY = "your-key-here"
        macOS / Linux (bash):   export INARA_API_KEY="your-key-here"

Run:
    python inara_test.py
"""
import json
import os
import sys
from datetime import datetime, timezone

import requests

APP_NAME = "ED-Companion"          # change if you'd rather use a different name
APP_VERSION = "0.1"
INARA_ENDPOINT = "https://inara.cz/inapi/v1/"


def main():
    api_key = os.environ.get("INARA_API_KEY")
    if not api_key:
        print("INARA_API_KEY isn't set. See the setup instructions at the top of this file.")
        sys.exit(1)

    payload = {
        "header": {
            "appName": APP_NAME,
            "appVersion": APP_VERSION,
            # Tells Inara this call is a dev/test call, not real gameplay data.
            # Keep this True while we're testing; only set it False once this
            # is a finished tool actually tracking your live sessions.
            "isBeingDeveloped": True,
            "APIkey": api_key,
        },
        "events": [
            {
                "eventName": "getCommanderProfile",
                # Inara wants a real timestamp here, not a placeholder -- it
                # rejects timestamps older than 30 days. For a plain read
                # like this, "right now" is the correct value.
                "eventTimestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                # Empty eventData + a PERSONAL api key = "give me my own
                # profile". Pass {"searchName": "SomeOtherCmdr"} instead if
                # you ever want to look someone else up.
                "eventData": {},
            }
        ],
    }

    response = requests.post(INARA_ENDPOINT, json=payload, timeout=15)
    response.raise_for_status()  # raises if Inara's server itself errored (5xx) or network failed
    result = response.json()
    print(json.dumps(result, indent=2))

    # The event's own eventStatus (inside events[0]) is what actually tells
    # you if the *request* worked -- 200 = OK, 202 = OK with a warning
    # (multiple matches), 204 = no data found, 400 = something's wrong with
    # your request or API key.
    event_status = result.get("events", [{}])[0].get("eventStatus")
    print(f"\nevent status: {event_status}")


if __name__ == "__main__":
    main()
