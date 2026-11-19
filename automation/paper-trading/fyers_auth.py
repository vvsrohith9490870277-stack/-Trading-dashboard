#!/usr/bin/env python3
"""Refresh Fyers access token via TOTP+PIN. Never prints secrets.

Vendored from the attachment handed over with brief render-deploy-01 (the
script that used to live outside the repo on the box). The HTTP flow is kept
byte-for-byte equivalent, with the two patches engine.refresh() used to apply
by string-replacing the source built in as the default:
  - only the first OTP URL (send_login_otp_v2) is tried;
  - only the base64 PIN variant is tried for verify_pin_v2.
The core is refresh_token(creds) with no .env read or write and no print of
anything but status codes; main() keeps the CLI use (reads/writes the .env
next to this file, box path only).
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import sys
import time
from urllib.parse import parse_qs, urlparse

import pyotp
import requests

ENV_PATH = pathlib.Path(__file__).resolve().parent / ".env"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
BASE_VAGATOR = "https://api-t2.fyers.in/vagator/v2"
BASE_API = "https://api-t1.fyers.in/api/v3"


class FyersAuthError(RuntimeError):
    """Classified login failure (no secret in the message)."""


def load_env(path: pathlib.Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k] = v.strip().strip('"').strip("'")
    return env


def upsert_env(path: pathlib.Path, updates: dict[str, str]) -> None:
    lines = path.read_text().splitlines()
    keys_seen = set()
    out = []
    for line in lines:
        raw = line
        if line.strip() and not line.strip().startswith("#") and "=" in line:
            k = line.split("=", 1)[0]
            if k in updates:
                out.append(f"{k}={updates[k]}")
                keys_seen.add(k)
                continue
        out.append(raw)
    for k, v in updates.items():
        if k not in keys_seen:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)


def redact(text: str, secrets: list[str]) -> str:
    s = text
    for sec in secrets:
        if sec:
            s = s.replace(sec, "<redacted>")
    return s[:800]


def refresh_token(creds: dict[str, str], session=None, verbose: bool = False) -> dict[str, str]:
    """Run the PIN+TOTP login flow and return {'access_token', 'refresh_token'?}.

    creds: FYERS_CLIENT_ID, FYERS_APP_ID, FYERS_SECRET_ID, FYERS_PIN,
    FYERS_TOTP_SECRET, optional FYERS_REDIRECT_URI. No .env read or write.
    Only status codes (and endpoint names) are printed, and only when verbose.
    Raises FyersAuthError with a classified code on any failure.
    """
    fy_id = creds["FYERS_CLIENT_ID"]
    app_id_full = creds["FYERS_APP_ID"]
    app_secret = creds["FYERS_SECRET_ID"]
    pin = creds["FYERS_PIN"]
    totp_secret = creds["FYERS_TOTP_SECRET"]
    redirect = creds.get("FYERS_REDIRECT_URI", "https://trade.fyers.in/api-login/redirect-uri/index.html")
    app_id = app_id_full.split("-")[0]
    app_type = app_id_full.split("-")[1] if "-" in app_id_full else "100"
    secrets = [creds.get("FYERS_ACCESS_TOKEN", ""), app_secret, pin, totp_secret, fy_id]
    fy_id_b64 = base64.b64encode(fy_id.encode("ascii")).decode("ascii")
    pin_b64 = base64.b64encode(pin.encode("ascii")).decode("ascii")

    if session is None:
        session = requests.Session()
    if hasattr(session, "headers"):
        try:
            session.headers.update({"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"})
        except Exception:
            pass

    def say(*parts):
        if verbose:
            print(*parts)

    # 1. send login otp (fy_id must be base64). Only the first OTP URL is tried.
    try:
        r = session.post(f"{BASE_VAGATOR}/send_login_otp_v2",
                         json={"fy_id": fy_id_b64, "app_id": "2"}, timeout=30)
    except Exception:
        raise FyersAuthError("token_refresh_failed") from None
    say("send_otp send_login_otp_v2 HTTP", r.status_code)
    try:
        data = r.json()
    except Exception:
        say("send_otp non-json", redact(r.text, secrets))
        raise FyersAuthError("send_otp_failed") from None
    request_key = data.get("request_key") or (data.get("data") or {}).get("request_key")
    if not request_key:
        raise FyersAuthError("send_otp_failed")

    # 2. verify TOTP
    remaining = 30 - (time.time() % 30)
    if remaining < 5:
        time.sleep(remaining + 0.15)
    totp = pyotp.TOTP(totp_secret).now()
    try:
        r = session.post(f"{BASE_VAGATOR}/verify_otp", json={"request_key": request_key, "otp": totp}, timeout=30)
    except Exception:
        raise FyersAuthError("token_refresh_failed") from None
    say("verify_otp HTTP", r.status_code)
    try:
        data = r.json()
    except Exception:
        raise FyersAuthError("verify_otp_failed") from None
    request_key2 = data.get("request_key") or (data.get("data") or {}).get("request_key")
    if not request_key2:
        raise FyersAuthError("verify_otp_failed")

    # 3. verify PIN (base64 is the documented identifier; only that variant).
    access_session = None
    try:
        r = session.post(
            f"{BASE_VAGATOR}/verify_pin_v2",
            json={"request_key": request_key2, "identity_type": "pin", "identifier": pin_b64},
            timeout=30,
        )
    except Exception:
        raise FyersAuthError("token_refresh_failed") from None
    say("verify_pin b64 HTTP", r.status_code)
    try:
        data = r.json()
    except Exception:
        raise FyersAuthError("verify_pin_failed") from None
    access_session = (data.get("data") or {}).get("access_token") or data.get("access_token")
    if not access_session:
        raise FyersAuthError("verify_pin_failed")

    # 4. get auth_code
    payload = {
        "fyers_id": fy_id,
        "app_id": app_id,
        "redirect_uri": redirect,
        "appType": app_type,
        "code_challenge": "",
        "state": "sample_state",
        "scope": "",
        "nonce": "",
        "response_type": "code",
        "create_cookie": True,
    }
    try:
        r = session.post(f"{BASE_API}/token", json=payload, headers={"Authorization": f"Bearer {access_session}"}, timeout=30, allow_redirects=False)
    except Exception:
        raise FyersAuthError("token_refresh_failed") from None
    say("token HTTP", r.status_code)
    try:
        data = r.json()
    except Exception:
        raise FyersAuthError("no_auth_code") from None
    url = data.get("Url") or data.get("url") or ""
    auth_code = None
    if url:
        auth_code = (parse_qs(urlparse(url).query).get("auth_code") or [None])[0]
    if not auth_code:
        auth_code = data.get("auth_code") or (data.get("data") or {}).get("auth_code")
    if not auth_code:
        raise FyersAuthError("no_auth_code")

    # 5. validate auth code
    app_id_hash = hashlib.sha256(f"{app_id_full}:{app_secret}".encode()).hexdigest()
    try:
        r = session.post(
            f"{BASE_API}/validate-authcode",
            json={"grant_type": "authorization_code", "appIdHash": app_id_hash, "code": auth_code},
            timeout=30,
        )
    except Exception:
        raise FyersAuthError("token_refresh_failed") from None
    say("validate-authcode HTTP", r.status_code)
    try:
        data = r.json()
    except Exception:
        raise FyersAuthError("no_access_token") from None
    access_token = data.get("access_token")
    refresh_token_value = data.get("refresh_token")
    if not access_token:
        raise FyersAuthError("no_access_token")
    out: dict[str, str] = {"access_token": access_token}
    if refresh_token_value:
        out["refresh_token"] = refresh_token_value
    return out


def main() -> int:
    env = load_env(ENV_PATH)
    try:
        result = refresh_token(env, verbose=True)
    except FyersAuthError as ex:
        print("FAIL", str(ex))
        return 1
    except KeyError as ex:
        print("FAIL missing_credential", str(ex))
        return 1
    updates = {"FYERS_ACCESS_TOKEN": result["access_token"]}
    if result.get("refresh_token"):
        updates["FYERS_REFRESH_TOKEN"] = result["refresh_token"]
    upsert_env(ENV_PATH, updates)
    print("TOKEN_REFRESHED", "has_refresh", bool(result.get("refresh_token")),
          "access_len", len(result["access_token"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
