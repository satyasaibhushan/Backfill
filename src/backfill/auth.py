from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from backfill.config import Settings


class Identity(BaseModel):
    login: str


def _is_loopback(request: Request) -> bool:
    client_host = request.client.host if request.client else ""
    return client_host in {"127.0.0.1", "::1", "localhost", "testclient"}


def require_identity(
    request: Request,
    tailscale_login: Annotated[str | None, Header(alias="Tailscale-User-Login")] = None,
) -> Identity:
    settings: Settings = request.app.state.settings
    if not settings.auth_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backfill authentication is not configured",
        )
    if settings.auth_mode == "dev":
        if not _is_loopback(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Development authentication is loopback-only",
            )
        identity = Identity(login=settings.allowed_login or "")
    else:
        if not tailscale_login:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Tailscale Serve identity is required",
            )
        identity = Identity(login=tailscale_login.strip().lower())

    if identity.login != settings.allowed_login:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tailscale login is not allowed",
        )
    return identity


IdentityDep = Annotated[Identity, Depends(require_identity)]


def require_dashboard_mutation(
    request: Request,
    identity: IdentityDep,
    request_source: Annotated[str | None, Header(alias="X-Backfill-Request")] = None,
    origin: Annotated[str | None, Header()] = None,
) -> Identity:
    settings: Settings = request.app.state.settings
    if settings.auth_mode == "tailscale":
        if request_source != "dashboard":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Missing request marker",
            )
        if not settings.public_origin or origin != settings.public_origin.rstrip("/"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid request origin",
            )
    return identity


MutationIdentityDep = Annotated[Identity, Depends(require_dashboard_mutation)]
