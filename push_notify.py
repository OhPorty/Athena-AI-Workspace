"""Telling the person something while Athena works: a push message to their phone or browser (Web Push).

A device opts in once from Settings (the browser asks permission and hands over a subscription). After that `notify` can reach it
even when the tab is closed or the phone is locked. Messages are encrypted for the device (RFC 8291) and signed as coming from this
Athena (VAPID), using a keypair kept in athena_vapid.pem next to the app, which is private to it like the other secrets.
"""
import base64
import json
import os
import time
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter
from pydantic import BaseModel

import db
import ownership
import http_ece
from logging_setup import logger
from py_vapid import Vapid02

router = APIRouter()

VAPID_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "athena_vapid.pem")
CONTACT = os.environ.get("ATHENA_PUSH_CONTACT", "mailto:athena-push@example.com")     # push services want a mailto: contact; any works for Chrome/Firefox
TTL_SECONDS = 24 * 60 * 60


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _conn():
    conn = db.get_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS push_subscriptions (endpoint TEXT PRIMARY KEY, p256dh TEXT, auth TEXT, label TEXT, created_at REAL)")
    ownership.ensure_owner_column(conn, "push_subscriptions")
    return conn


def _vapid():
    if os.path.isfile(VAPID_PATH):
        return Vapid02.from_file(VAPID_PATH)
    vapid = Vapid02()
    vapid.generate_keys()
    vapid.save_key(VAPID_PATH)
    os.chmod(VAPID_PATH, 0o600)
    return vapid


def public_key():
    """The key a browser needs to subscribe, in the form its push API takes."""
    raw = _vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64(raw)


def subscriptions():
    where, owner_params = ownership.owner_filter()
    conn = _conn()
    try:
        rows = conn.execute(f"SELECT endpoint, p256dh, auth, label FROM push_subscriptions WHERE {where} ORDER BY created_at", owner_params).fetchall()
    finally:
        conn.close()
    return [{"endpoint": r[0], "p256dh": r[1], "auth": r[2], "label": r[3]} for r in rows]


def save_subscription(endpoint, p256dh, auth, label=""):
    if not str(endpoint).startswith("https://") or not p256dh or not auth:
        return False
    conn = _conn()
    try:
        conn.execute("INSERT OR REPLACE INTO push_subscriptions (endpoint, p256dh, auth, label, created_at, owner) VALUES (?, ?, ?, ?, ?, ?)",
                     (endpoint, p256dh, auth, label[:80], time.time(), ownership.stored_owner()))
        conn.commit()
    finally:
        conn.close()
    return True


def remove_subscription(endpoint):
    where, owner_params = ownership.owner_filter()
    conn = _conn()
    try:
        conn.execute(f"DELETE FROM push_subscriptions WHERE endpoint = ? AND {where}", (endpoint, *owner_params))
        conn.commit()
    finally:
        conn.close()


def encrypt_payload(payload, p256dh, auth):
    """-> the encrypted request body for one device (aes128gcm, a fresh key each time)."""
    server_key = ec.generate_private_key(ec.SECP256R1())
    return http_ece.encrypt(payload, private_key=server_key, dh=_unb64(p256dh), auth_secret=_unb64(auth), version="aes128gcm")


def vapid_headers(endpoint):
    parts = urlparse(endpoint)
    claims = {"sub": CONTACT, "aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600}
    return _vapid().sign(claims)


def send(title, body, url="/", post=None):
    """Push to every subscribed device. `post` is for tests."""
    devices = subscriptions()
    if not devices:
        return {"sent": 0, "note": "No device has notifications turned on yet. The person can turn them on in Settings, on the device that should get them."}
    post = post or (lambda endpoint, content, headers: httpx.post(endpoint, content=content, headers=headers, timeout=15))
    payload = json.dumps({"title": title, "body": body, "url": url}).encode()
    sent = failed = 0
    for d in devices:
        try:
            headers = {**vapid_headers(d["endpoint"]), "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(TTL_SECONDS)}
            resp = post(d["endpoint"], encrypt_payload(payload, d["p256dh"], d["auth"]), headers)
            if resp.status_code in (404, 410):
                remove_subscription(d["endpoint"])
                failed += 1
            elif resp.status_code < 300:
                sent += 1
            else:
                failed += 1
                logger.warning(f"push to {urlparse(d['endpoint']).netloc} answered HTTP {resp.status_code}")
        except Exception as e:
            failed += 1
            logger.warning(f"push failed: {e!r}")
    return {"sent": sent, "failed": failed, "devices": len(devices)}


# ---------- routes ----------

class SubscriptionIn(BaseModel):
    endpoint: str
    keys: dict
    label: str = ""


class UnsubscribeIn(BaseModel):
    endpoint: str


@router.get("/api/push/key")
def api_key():
    return {"key": public_key()}


@router.get("/api/push/status")
def api_status():
    return {"devices": len(subscriptions())}


@router.post("/api/push/subscribe")
def api_subscribe(req: SubscriptionIn):
    return {"ok": save_subscription(req.endpoint, req.keys.get("p256dh", ""), req.keys.get("auth", ""), req.label)}


@router.post("/api/push/unsubscribe")
def api_unsubscribe(req: UnsubscribeIn):
    remove_subscription(req.endpoint)
    return {"ok": True}


@router.post("/api/push/test")
def api_test():
    return send("Athena", "Notifications are working on this device.")
