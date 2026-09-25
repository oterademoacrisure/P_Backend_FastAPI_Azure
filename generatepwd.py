"""
Generates a full UserCredential document -- ready to paste straight into
Cosmos DB's Data Explorer (New Item) -- for the PayerIQ login
(app/services/auth_service.py). Hashes the password with bcrypt the same way
login() verifies it, and sets id == username so the two can never drift out
of sync (a mismatch there is a common cause of "Invalid username or
password" even with a correct password).

Usage:
    python generatepwd.py username
    python generatepwd.py            # prompts for username too

Role and tenantId are hardcoded below (DEFAULT_ROLE / DEFAULT_TENANT_ID) --
edit those constants if you need a different value.
"""

import argparse
import json
from datetime import datetime, timezone
from getpass import getpass

import bcrypt

DEFAULT_ROLE = "user"
DEFAULT_TENANT_ID = "default"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a UserCredential document for Cosmos DB.")
    parser.add_argument("username", nargs="?", help="Username (also used as the document id)")
    args = parser.parse_args()

    username = args.username or input("Username: ").strip()
    if not username:
        print("No username given -- aborting.")
        return

    password = getpass("Password: ")
    if not password:
        print("No password given -- aborting.")
        return

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")

    user_doc = {
        "id": username,
        "username": username,
        "passwordHash": password_hash,
        "role": DEFAULT_ROLE,
        "tenantId": DEFAULT_TENANT_ID,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lastLogin": None,
        "isActive": True,
    }

    print()
    print(json.dumps(user_doc, indent=2))


if __name__ == "__main__":
    main()
