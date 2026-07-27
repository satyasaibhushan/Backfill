from fastapi import APIRouter, Request

from backfill.auth import IdentityDep, MutationIdentityDep
from backfill.schemas import ControlRead, TickResult

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    return {
        "status": "ok",
        "version": "0.1.0",
        "authConfigured": settings.auth_configured,
        "schedulerConfigured": settings.scheduler_enabled,
    }


@router.get("/me")
def me(identity: IdentityDep) -> dict[str, str]:
    return {"login": identity.login}


@router.get("/control")
def control(request: Request, identity: IdentityDep) -> ControlRead:
    scheduler = request.app.state.scheduler
    return ControlRead(
        scheduler_enabled=request.app.state.settings.scheduler_enabled,
        paused=scheduler.is_paused(),
        running_count=scheduler.running_count(),
        execution_providers=sorted(request.app.state.settings.execution_providers),
    )


@router.post("/control/pause")
def pause(request: Request, identity: MutationIdentityDep) -> ControlRead:
    request.app.state.scheduler.set_paused(True)
    return control(request, identity)


@router.post("/control/resume")
def resume(request: Request, identity: MutationIdentityDep) -> ControlRead:
    request.app.state.scheduler.set_paused(False)
    return control(request, identity)


@router.post("/control/tick")
async def tick(request: Request, identity: MutationIdentityDep) -> TickResult:
    return await request.app.state.scheduler.tick()
