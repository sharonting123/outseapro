from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
import uuid
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response

from .store import Store

COOKIE_NAME = "soutui_session"
SESSION_TTL = 30 * 24 * 3600
ADMIN_COOKIE_NAME = "soutui_admin_session"
ADMIN_SESSION_TTL = 12 * 3600
_SCRYPT_N = 2**14


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("密码至少 8 位")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=8, p=1, dklen=32)
    return "scrypt$%d$%s$%s" % (
        _SCRYPT_N,
        base64.urlsafe_b64encode(salt).decode(),
        base64.urlsafe_b64encode(digest).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, salt, wanted = encoded.split("$", 3)
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n), r=8, p=1, dklen=32,
        )
        return hmac.compare_digest(base64.urlsafe_b64encode(digest).decode(), wanted)
    except (ValueError, TypeError):
        return False


def register(store: Store, email: str, password: str, display_name: str, role: str = "customer") -> dict[str, Any]:
    email = email.strip().lower()
    if "@" not in email or len(email) > 254:
        raise ValueError("邮箱格式不正确")
    if not display_name.strip():
        raise ValueError("请输入昵称")
    if store.get_user_by_email(email):
        raise ValueError("该邮箱已注册")
    return store.create_user("u_" + uuid.uuid4().hex[:16], email, hash_password(password), display_name, role)


def authenticate(store: Store, email: str, password: str) -> dict[str, Any] | None:
    user = store.get_user_by_email(email, include_hash=True)
    if not user or not user.get("is_active") or not verify_password(password, user["password_hash"]):
        return None
    user.pop("password_hash", None)
    return user


def start_session(store: Store, response: Response, user_id: str) -> None:
    token = secrets.token_urlsafe(32)
    store.create_session(token, user_id, time.time() + SESSION_TTL)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=os.getenv("SOUTUI_SECURE_COOKIE", "0") == "1",
        path="/",
    )


def end_session(store: Store, request: Request, response: Response) -> None:
    store.delete_session(request.cookies.get(COOKIE_NAME, ""))
    response.delete_cookie(COOKIE_NAME, path="/")


def current_user(request: Request, store: Store) -> dict[str, Any] | None:
    return store.session_user(request.cookies.get(COOKIE_NAME, ""))


def require_user(request: Request, store: Store, role: str | None = None) -> dict[str, Any]:
    user = current_user(request, store)
    if not user:
        raise HTTPException(401, "请先登录")
    if role and user.get("role") not in (role, "admin"):
        raise HTTPException(403, "没有权限")
    return user


def bootstrap_merchant(store: Store) -> None:
    """Create the first merchant only when explicit environment credentials exist."""
    email = os.getenv("SOUTUI_BOOTSTRAP_MERCHANT_EMAIL", "").strip().lower()
    password = os.getenv("SOUTUI_BOOTSTRAP_MERCHANT_PASSWORD", "")
    if not email or not password or store.get_user_by_email(email):
        return
    store.create_user(
        "merchant_demo", email, hash_password(password),
        os.getenv("SOUTUI_BOOTSTRAP_MERCHANT_NAME", "平台商家"), "merchant",
    )


def authenticate_admin(store: Store, email: str, password: str) -> dict[str, Any] | None:
    admin = store.get_admin_by_email(email, include_hash=True)
    if not admin or not admin.get("is_active") or not verify_password(password, admin["password_hash"]):
        return None
    admin.pop("password_hash", None)
    return admin


def start_admin_session(store: Store, response: Response, admin_id: str) -> None:
    token = secrets.token_urlsafe(32)
    store.create_admin_session(token, admin_id, time.time() + ADMIN_SESSION_TTL)
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
        max_age=ADMIN_SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=os.getenv("SOUTUI_SECURE_COOKIE", "0") == "1",
        path="/admin",
    )


def end_admin_session(store: Store, request: Request, response: Response) -> None:
    store.delete_admin_session(request.cookies.get(ADMIN_COOKIE_NAME, ""))
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/admin")


def current_admin(request: Request, store: Store) -> dict[str, Any] | None:
    return store.session_admin(request.cookies.get(ADMIN_COOKIE_NAME, ""))


def bootstrap_admin(store: Store) -> None:
    """Provision a separate administrator only from explicit deployment credentials."""
    email = os.getenv("SOUTUI_BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("SOUTUI_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not email or not password or store.get_admin_by_email(email):
        return
    store.create_admin(
        "admin_" + uuid.uuid4().hex[:16],
        email,
        hash_password(password),
        os.getenv("SOUTUI_BOOTSTRAP_ADMIN_NAME", "平台管理员"),
    )
