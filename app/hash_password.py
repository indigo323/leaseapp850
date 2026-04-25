#!/usr/bin/env python3
"""
Generate a bcrypt hash for ADMIN_PASSWORD_HASH.

Usage:
    docker compose run --rm lease-app python /app/hash_password.py

Prompts for a password, prints the hash. Paste the hash into Portainer /
.env as ADMIN_PASSWORD_HASH. The plaintext password is never stored or
logged.
"""

import getpass
import sys

import bcrypt


def main() -> int:
    pw1 = getpass.getpass("Admin password: ")
    if len(pw1) < 10:
        print("Password must be at least 10 characters.", file=sys.stderr)
        return 1
    pw2 = getpass.getpass("Confirm:        ")
    if pw1 != pw2:
        print("Passwords don't match.", file=sys.stderr)
        return 1
    h = bcrypt.hashpw(pw1.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    print()
    print("Paste this value as ADMIN_PASSWORD_HASH:")
    print()
    print(h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
