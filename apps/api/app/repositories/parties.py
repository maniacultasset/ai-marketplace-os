"""`business_parties`, the tenant address book (DATABASE.md §5.6 :1135-1150; S13 B11).

`BusinessPartyRepository.match_or_create(fields)` is the import's match-or-create on the
generated `dedupe_key` (`bp_dedupe_uq`, live rows): the stored party gains the new role and
keeps everything else; a new party is created from the fields as given (never an invented
name). `brand_consumer_care()` reads the brand default consumer-care contact.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.facts import BrandProfile, BusinessParty
from app.repositories.base import TenantScopedRepository


@dataclass(frozen=True, slots=True)
class PartyFields:
    role: str
    legal_name: str | None = None
    address_line1: str | None = None
    postal_code: str | None = None
    email: str | None = None
    phone: str | None = None
    country_code: str = "IN"


@dataclass(frozen=True, slots=True)
class Contact:
    party_id: uuid.UUID
    email: str | None
    phone: str | None


class BusinessPartyRepository(TenantScopedRepository[BusinessParty]):
    model = BusinessParty

    async def match_or_create(self, fields: PartyFields) -> uuid.UUID:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("business_parties write for another tenant's unit of work")
        stmt = pg_insert(BusinessParty).values(
            tenant_id=self.tenant_id,
            roles=[fields.role],
            legal_name=fields.legal_name,
            address_line1=fields.address_line1,
            postal_code=fields.postal_code,
            country_code=fields.country_code,
            email=fields.email,
            phone=fields.phone,
            source="seller",
        )
        table = BusinessParty.__table__
        # one role per call, so appending it when it is missing keeps the array a set
        roles = case(
            (stmt.excluded.roles.contained_by(table.c.roles), table.c.roles),
            else_=table.c.roles.concat(stmt.excluded.roles),
        )
        upsert = stmt.on_conflict_do_update(
            index_elements=[table.c.tenant_id, table.c.dedupe_key],
            index_where=table.c.deleted_at.is_(None),
            set_={"roles": roles},
        ).returning(table.c.id)
        party: uuid.UUID = (await self.db.execute(upsert)).scalar_one()
        return party

    async def contact(self, party_id: uuid.UUID) -> Contact | None:
        row = (
            await self.db.execute(
                self._select()
                .where(BusinessParty.id == party_id, BusinessParty.deleted_at.is_(None))
                .with_only_columns(BusinessParty.email, BusinessParty.phone)
            )
        ).one_or_none()
        return None if row is None else Contact(party_id, row.email, row.phone)


class BrandProfileRepository(TenantScopedRepository[BrandProfile]):
    model = BrandProfile

    async def consumer_care(self) -> Contact | None:
        """The brand default consumer-care party's contact, if the brand has one."""
        party_id = (
            await self.db.execute(
                self._select().with_only_columns(BrandProfile.consumer_care_party_id)
            )
        ).scalar_one_or_none()
        if party_id is None:
            return None
        return await BusinessPartyRepository(self.db, self.tenant_id).contact(party_id)
