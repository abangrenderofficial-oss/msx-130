"""Generate admin credentials locally. Never capture Google password."""
from __future__ import annotations
import base64
import getpass
import hashlib
import os
import secrets
from pathlib import Path

def main():
    password = getpass.getpass("Set admin passphrase (16+ chars): ")
    if len(password) < 16 or getpass.getpass("Repeat: ") != password:
        raise SystemExit("Passphrase too short or does not match")
    salt = secrets.token_bytes(24)
    hashed = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    totp = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    worker = secrets.token_urlsafe(48)
    fields = {
        "MUSIX_ADMIN_PUBLIC_ORIGIN":"https://musix-admin.example.invalid",
        "MUSIX_ADMIN_PASSWORD_SCRYPT":salt.hex() + ":" + hashed.hex(),
        "MUSIX_ADMIN_TOTP_SECRET":totp,
        "MUSIX_WORKER_SECRET":worker,
        "MUSIX_ENCRYPTED_VAULT_DIR":"/mnt/encrypted-musix-youtube",
    }
    target = Path(__file__).resolve().parent / ".env"
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for key, value in fields.items():
            f.write(key + "=" + value + "\n")
    print("Created private configuration:", target)
    print("Add to an authenticator app (manual TOTP seed):", totp)
    print("Never put credentials, Google passwords or session cookies in GitHub.")

if __name__ == "__main__":
    main()
