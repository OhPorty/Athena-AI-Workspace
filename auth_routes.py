import os

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

from auth import AuthManager, password_problem
from user_context import current_user, set_current_user, reset_current_user

# ---------------------------------------------------------------------------
# Authentication -- single-user login gate. Adapted from Odysseus's proven
# pattern: an outermost ASGI middleware rejects/redirects every request
# except a small explicit exemption list, so nothing (no page, no API route)
# is reachable without a valid session, matching Odysseus's own "0 UI access
# without login" behavior.
# ---------------------------------------------------------------------------
auth_manager = AuthManager(os.path.join(os.path.dirname(os.path.abspath(__file__)), "athena_auth.json"))
SESSION_COOKIE = "athena_auth_token"

router = APIRouter()

AUTH_EXEMPT_EXACT = {
    "/login",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/verify-2fa",
    "/api/auth/status",
    "/health",
    "/sw.js",
}
# /workspace-view is a page served from a link with a secret token in it (see workspace_view.py); the token is its login.
AUTH_EXEMPT_PREFIXES = ["/static", "/workspace-view"]


def _is_auth_exempt(path: str) -> bool:
    if path in AUTH_EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES)


# What an account that has not finished first-login setup may still reach.
ONBOARDING_ALLOWED = {"/api/auth/change-password", "/api/auth/2fa/setup", "/api/auth/2fa/confirm", "/api/auth/logout"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_auth_exempt(path):
            return await call_next(request)
        if not auth_manager.is_configured:
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"error": "Setup required"})
            return RedirectResponse(url="/login", status_code=302)
        token = request.cookies.get(SESSION_COOKIE)
        user = auth_manager.session_user(token)
        if user is None:
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"error": "Not authenticated"})
            return RedirectResponse(url="/login", status_code=302)
        pending = auth_manager.onboarding(user)
        if pending and path not in ONBOARDING_ALLOWED:
            if path.startswith("/api/"):
                return JSONResponse(status_code=403, content={"error": "Finish setting up this account first.", "onboarding": pending})
            return RedirectResponse(url="/login", status_code=302)
        request.state.current_user = user
        ctx = set_current_user(user)
        try:
            return await call_next(request)
        finally:
            reset_current_user(ctx)


class AuthSetupIn(BaseModel):
    username: str
    password: str


class AuthLoginIn(BaseModel):
    username: str
    password: str


class Auth2FAVerifyIn(BaseModel):
    pending_token: str
    code: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class Totp2FAConfirmIn(BaseModel):
    code: str


class Totp2FADisableIn(BaseModel):
    password: str


def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        key=SESSION_COOKIE, value=token, max_age=60 * 60 * 24 * 30,
        httponly=True, secure=True, samesite="lax",
    )


@router.get("/login", response_class=HTMLResponse)
def login_page():
    with open(os.path.join("static", "login.html")) as f:
        return f.read()


@router.get("/api/auth/status")
def auth_status(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    return auth_manager.status(token)


@router.post("/api/auth/setup")
def auth_setup(req: AuthSetupIn, response: Response):
    if auth_manager.is_configured:
        return JSONResponse(status_code=400, content={"error": "Already configured"})
    problem = password_problem(req.password, req.username)
    if problem:
        return JSONResponse(status_code=400, content={"error": problem})
    if not auth_manager.setup(req.username, req.password):
        return JSONResponse(status_code=400, content={"error": "The username needs 2 to 32 lowercase letters, digits, dot, dash or underscore."})
    token = auth_manager.create_session(req.username.strip().lower())
    _set_session_cookie(response, token)
    return {"ok": True}


@router.post("/api/auth/login")
def auth_login(req: AuthLoginIn, response: Response):
    username = req.username.strip().lower()
    if not auth_manager.verify_password(username, req.password):
        return JSONResponse(status_code=401, content={"error": "Invalid credentials"})
    if auth_manager.totp_enabled_for(username):
        pending_token = auth_manager.create_pending_2fa(username)
        return {"requires_totp": True, "pending_token": pending_token}
    token = auth_manager.create_session(username)
    _set_session_cookie(response, token)
    return {"ok": True, "onboarding": auth_manager.onboarding(username)}


@router.post("/api/auth/verify-2fa")
def auth_verify_2fa(req: Auth2FAVerifyIn, response: Response):
    username = auth_manager.consume_pending_2fa(req.pending_token)
    if not username:
        return JSONResponse(status_code=401, content={"error": "Login expired -- please try again"})
    if not auth_manager.totp_verify(username, req.code):
        return JSONResponse(status_code=401, content={"error": "Invalid code"})
    token = auth_manager.create_session(username)
    _set_session_cookie(response, token)
    return {"ok": True, "onboarding": auth_manager.onboarding(username)}


@router.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth_manager.revoke_token(token)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.post("/api/auth/change-password")
def auth_change_password(req: ChangePasswordIn):
    if not auth_manager.verify_password(current_user(), req.current_password):
        return JSONResponse(status_code=400, content={"error": "The current password is not right."})
    problem = password_problem(req.new_password, current_user())
    if problem or req.new_password == req.current_password:
        return JSONResponse(status_code=400, content={"error": problem or "The new password must be different from the current one."})
    if not auth_manager.change_password(current_user(), req.current_password, req.new_password):
        return JSONResponse(status_code=400, content={"error": "The password could not be changed."})
    return {"ok": True, "onboarding": auth_manager.onboarding(current_user())}


@router.post("/api/auth/2fa/setup")
def auth_2fa_setup():
    secret = auth_manager.totp_generate_secret(current_user())
    if not secret:
        return JSONResponse(status_code=400, content={"error": "Account not configured"})
    return {"secret": secret, "otpauth_uri": auth_manager.totp_provisioning_uri(current_user(), secret)}


@router.post("/api/auth/2fa/confirm")
def auth_2fa_confirm(req: Totp2FAConfirmIn):
    backup_codes = auth_manager.totp_confirm_enable(current_user(), req.code)
    if backup_codes is None:
        return JSONResponse(status_code=400, content={"error": "Invalid code"})
    return {"ok": True, "backup_codes": backup_codes}


@router.post("/api/auth/2fa/disable")
def auth_2fa_disable(req: Totp2FADisableIn):
    if not auth_manager.totp_disable(current_user(), req.password):
        return JSONResponse(status_code=400, content={"error": "Incorrect password"})
    return {"ok": True}
