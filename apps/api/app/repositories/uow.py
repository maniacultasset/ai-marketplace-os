"""`tenant_uow`: the tenant transaction services open (CODE-PLAN S12 T6; PLAN D6).

One `unit_of_work(tenant_id, user_id=...)` (S9) and the repositories a product write needs, all
on that one session, so the product row, its facts and its audit row commit or roll back
together. `user_id` is optional: API services pass `principal.user_id`; a worker job with no
user (`job_runs.created_by` NULL) opens it with the tenant alone and no `app.user_id` is set.
Services import this module, never `app.db`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.attribute_definitions import AttributeDefinitionRepository
from app.repositories.brand_chunks import BrandChunkRepository
from app.repositories.dictionary import DictionaryRevisionRepository
from app.repositories.error_map import ErrorMapRepository
from app.repositories.human_tasks import HumanTaskRepository
from app.repositories.identity import AuditRepository
from app.repositories.images import ImageRepository
from app.repositories.jobs import JobRunRepository
from app.repositories.marketplace_accounts import MarketplaceAccountRepository
from app.repositories.oauth_states import OAuthStateRepository
from app.repositories.products import ProductRepository
from app.repositories.rules import RuleRepository
from app.repositories.templates import CategoryTemplateRepository
from app.repositories.transactions import unit_of_work

__all__ = ["TenantUnitOfWork", "tenant_uow"]


@dataclass(frozen=True, slots=True)
class TenantUnitOfWork:
    session: AsyncSession
    products: ProductRepository
    attribute_definitions: AttributeDefinitionRepository
    audit: AuditRepository
    marketplace_accounts: MarketplaceAccountRepository
    oauth_states: OAuthStateRepository
    human_tasks: HumanTaskRepository
    # S14b T5 (C4): the seam and the rules API read these on the same session.
    rules: RuleRepository
    brand_chunks: BrandChunkRepository
    job_runs: JobRunRepository
    images: ImageRepository
    templates: CategoryTemplateRepository
    dictionary: DictionaryRevisionRepository
    # S20 T4: the adapter's error map (platform-global, SELECT only).
    error_map: ErrorMapRepository


@asynccontextmanager
async def tenant_uow(
    tenant_id: UUID, *, user_id: UUID | None = None
) -> AsyncIterator[TenantUnitOfWork]:
    async with unit_of_work(tenant_id, user_id=user_id) as session:
        yield TenantUnitOfWork(
            session=session,
            products=ProductRepository(session, tenant_id),
            attribute_definitions=AttributeDefinitionRepository(session),
            audit=AuditRepository(session),
            marketplace_accounts=MarketplaceAccountRepository(session, tenant_id),
            oauth_states=OAuthStateRepository(session, tenant_id),
            human_tasks=HumanTaskRepository(session, tenant_id),
            rules=RuleRepository(session, tenant_id),
            brand_chunks=BrandChunkRepository(session, tenant_id),
            job_runs=JobRunRepository(session, tenant_id),
            images=ImageRepository(session, tenant_id),
            templates=CategoryTemplateRepository(session),
            dictionary=DictionaryRevisionRepository(session),
            error_map=ErrorMapRepository(session),
        )
