"""API v1 — сборка роутера."""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    agents,
    auth,
    communication,
    economy,
    ecosystem,
    health,
    internal,
    nodes,
    orchestration,
    platform,
    realtime,
    tasks,
    trust,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(agents.router)
api_router.include_router(nodes.self_router)
api_router.include_router(nodes.router)
api_router.include_router(tasks.router)
api_router.include_router(communication.router)
api_router.include_router(orchestration.router)
api_router.include_router(trust.router)
api_router.include_router(economy.router)
api_router.include_router(ecosystem.router)
api_router.include_router(platform.router)
api_router.include_router(internal.router)
api_router.include_router(realtime.router)

__all__ = ["api_router"]
