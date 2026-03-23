"""FORGE IMS FastAPI application."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import auth, assets, audit, bom, categories, cut_lists, kits, locations, ncm, procurement, reports, users, vendors
from app.core.config import settings
from app.core.database import Base, engine
from app.middleware.audit import AuditMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware

APP_VERSION = "1.3.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


docs_url = "/api/docs" if settings.API_DOCS_ENABLED else None
app = FastAPI(
    title="FORGE IMS",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=docs_url,
    redoc_url=None,
)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
if settings.ALLOWED_HOSTS and settings.ALLOWED_HOSTS != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.ALLOWED_HOSTS)
app.add_middleware(AuditMiddleware)

prefix = "/api/v1"
app.include_router(auth.router, prefix=prefix, tags=["Auth"])
app.include_router(assets.router, prefix=prefix, tags=["Assets"])
app.include_router(categories.router, prefix=prefix, tags=["Categories"])
app.include_router(categories.cycle_count_router, prefix=prefix, tags=["CycleCount"])
app.include_router(vendors.router, prefix=prefix, tags=["Vendors"])
app.include_router(bom.router, prefix=prefix, tags=["BOM"])
app.include_router(kits.router, prefix=prefix, tags=["Kits"])
app.include_router(ncm.router, prefix=prefix, tags=["NCM"])
app.include_router(audit.router, prefix=prefix, tags=["Audit"])
app.include_router(procurement.router, prefix=prefix, tags=["Procurement"])
app.include_router(reports.budget_router, prefix=prefix, tags=["Budget"])
app.include_router(reports.router, prefix=prefix, tags=["Reports"])
app.include_router(locations.router, prefix=prefix, tags=["Locations"])
app.include_router(cut_lists.router, prefix=prefix, tags=["CutLists"])
app.include_router(users.router, prefix=prefix, tags=["Users"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "forge-ims", "version": APP_VERSION}
