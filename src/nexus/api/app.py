"""FastAPI application: routes, error mapping and health endpoints."""

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from nexus import __version__
from nexus.api.auth import verify_api_key
from nexus.api.routes import router
from nexus.core.orchestrator import CapacityError, NexusError
from nexus.core.recipes import MissingSecretError, RecipeNotFoundError


def build_api(**kwargs: object) -> FastAPI:
    api = FastAPI(title="nexus", version=__version__, **kwargs)  # ty: ignore[invalid-argument-type]
    api.include_router(router, prefix="/api", dependencies=[Depends(verify_api_key)])

    @api.exception_handler(RecipeNotFoundError)
    async def _not_found(_: Request, exc: RecipeNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_404_NOT_FOUND)

    @api.exception_handler(CapacityError)
    async def _capacity(_: Request, exc: CapacityError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_507_INSUFFICIENT_STORAGE)

    @api.exception_handler(NexusError)
    async def _conflict(_: Request, exc: NexusError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_409_CONFLICT)

    @api.exception_handler(MissingSecretError)
    async def _secret(_: Request, exc: MissingSecretError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @api.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/readyz", tags=["health"])
    async def readyz(request: Request) -> JSONResponse:
        try:
            async with request.app.state.sessions() as s:
                await s.execute(text("SELECT 1"))
        except Exception as exc:
            return JSONResponse(
                {"status": "unavailable", "detail": f"database: {exc}"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return JSONResponse({"status": "ok"})

    return api
