from fastapi import APIRouter, Request

from backfill.auth import IdentityDep, MutationIdentityDep
from backfill.providers.models import ProviderSnapshot

router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get("/")
def list_providers(request: Request, identity: IdentityDep) -> list[ProviderSnapshot]:
    return request.app.state.providers.latest()


@router.post("/refresh")
async def refresh_providers(
    request: Request, identity: MutationIdentityDep
) -> list[ProviderSnapshot]:
    return await request.app.state.providers.refresh(force=True)
