"""`business_parties`, the tenant address book (DATABASE.md §5.6 :1135-1150; S13 B11).

`BusinessPartyRepository.match_or_create(fields)` is the import's match-or-create on the
generated `dedupe_key` (`bp_dedupe_uq`, live rows): the stored party gains the new role and
keeps everything else; a new party is created from the fields as given (never an invented
name). `brand_consumer_care()` reads the brand default consumer-care contact.

SI1 T8 (decision 16): `PartyFields` carries the full address (line 2, city, state, GSTIN). On a
dedupe match the role is appended; only with `update_on_match=True` (the brand profile route,
`settings:manage`) are line 2, city, state and GSTIN updated, each as `COALESCE(new, stored)`.
A match that changes nothing writes nothing (the row stays byte-identical). The brand profile
repository gains `ensure`, `get_for_update` and `set_default_manufacturer`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import ColumnElement, and_, case, cast, func, literal, not_, or_, update
from sqlalchemy.dialects.postgresql import JSONB
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
    address_line2: str | None = None
    city: str | None = None
    state_code: str | None = None
    gstin: str | None = None


# the columns only the brand profile route may update on a dedupe match (decision 16)
MATCH_UPDATABLE: Final = ("address_line2", "city", "state_code", "gstin")
MANUFACTURER_KEY: Final = "manufacturer"


@dataclass(frozen=True, slots=True)
class Contact:
    party_id: uuid.UUID
    email: str | None
    phone: str | None


class BusinessPartyRepository(TenantScopedRepository[BusinessParty]):
    model = BusinessParty

    def _check_uow(self) -> None:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("business_parties write for another tenant's unit of work")

    @staticmethod
    def _dedupe(fields: PartyFields) -> ColumnElement[str]:
        """The generated `dedupe_key` expression (revision 0001) over the given values."""
        if fields.legal_name is not None:
            line1 = func.lower(func.regexp_replace(literal(fields.address_line1), r"\s+", " ", "g"))
            return (
                func.lower(func.btrim(literal(fields.legal_name)))
                .concat("|")
                .concat(func.coalesce(line1, ""))
                .concat("|")
                .concat(func.coalesce(literal(fields.postal_code), ""))
                .concat("|")
                .concat(literal(fields.country_code))
            )
        digits = func.regexp_replace(literal(fields.phone), "[^0-9]", "", "g")
        return (
            literal("cc|")
            .concat(func.coalesce(func.lower(literal(fields.email)), ""))
            .concat("|")
            .concat(func.coalesce(digits, ""))
        )

    async def match_or_create(
        self, fields: PartyFields, *, update_on_match: bool = False
    ) -> uuid.UUID:
        party, _wrote = await self.match_or_create_wrote(fields, update_on_match=update_on_match)
        return party

    async def match_or_create_wrote(
        self, fields: PartyFields, *, update_on_match: bool = False
    ) -> tuple[uuid.UUID, bool]:
        """`match_or_create`, plus whether a row was inserted or changed."""
        self._check_uow()
        stmt = pg_insert(BusinessParty).values(
            tenant_id=self.tenant_id,
            roles=[fields.role],
            legal_name=fields.legal_name,
            address_line1=fields.address_line1,
            address_line2=fields.address_line2,
            city=fields.city,
            state_code=fields.state_code,
            postal_code=fields.postal_code,
            country_code=fields.country_code,
            email=fields.email,
            phone=fields.phone,
            gstin=fields.gstin,
            source="seller",
        )
        table = BusinessParty.__table__
        # one role per call, so appending it when it is missing keeps the array a set
        role_missing = not_(stmt.excluded.roles.contained_by(table.c.roles))
        roles = case((role_missing, table.c.roles.concat(stmt.excluded.roles)), else_=table.c.roles)
        set_: dict[str, Any] = {"roles": roles}
        changed: ColumnElement[bool] = role_missing
        if update_on_match:
            for name in MATCH_UPDATABLE:
                new, stored = stmt.excluded[name], table.c[name]
                set_[name] = func.coalesce(new, stored)
                changed = or_(changed, and_(new.is_not(None), new.is_distinct_from(stored)))
        upsert = stmt.on_conflict_do_update(
            index_elements=[table.c.tenant_id, table.c.dedupe_key],
            index_where=table.c.deleted_at.is_(None),
            set_=set_,
            where=changed,
        ).returning(table.c.id)
        party: uuid.UUID | None = (await self.db.execute(upsert)).scalar_one_or_none()
        if party is not None:
            return party, True
        # a match that changes nothing: the row is left untouched, so read its id
        found: uuid.UUID = (
            await self.db.execute(
                self._select()
                .where(
                    BusinessParty.deleted_at.is_(None),
                    BusinessParty.dedupe_key == self._dedupe(fields),
                )
                .with_only_columns(BusinessParty.id)
            )
        ).scalar_one()
        return found, False

    async def get(self, party_id: uuid.UUID) -> BusinessParty | None:
        """A live party of this tenant, or None."""
        row: BusinessParty | None = (
            await self.db.execute(
                self._select().where(
                    BusinessParty.id == party_id, BusinessParty.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        return row

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

    async def ensure(self) -> bool:
        """Create this tenant's empty profile row when it is missing; True when this call
        inserted it (T13 compares a fresh row against the `none` ETag)."""
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("brand_profiles write for another tenant's unit of work")
        stmt = (
            pg_insert(BrandProfile)
            .values(tenant_id=self.tenant_id)
            .on_conflict_do_nothing(index_elements=[BrandProfile.__table__.c.tenant_id])
            .returning(BrandProfile.tenant_id)
        )
        return (await self.db.execute(stmt)).first() is not None

    async def create_with_website(self, website_url: str) -> None:
        """ADR-0032: a new brand's profile row holding the website its owner gave (the brand
        page shows it; no AI step reads it). A row that already exists is left as it is."""
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("brand_profiles write for another tenant's unit of work")
        await self.db.execute(
            pg_insert(BrandProfile)
            .values(tenant_id=self.tenant_id, website_url=website_url)
            .on_conflict_do_nothing(index_elements=[BrandProfile.__table__.c.tenant_id])
        )

    async def get(self) -> BrandProfile | None:
        """This tenant's profile row, or None (a tenant may lack it)."""
        row: BrandProfile | None = (await self.db.execute(self._select())).scalar_one_or_none()
        return row

    async def default_manufacturer(self) -> uuid.UUID | None:
        """The brand default manufacturer's party id, or None (no row or none set)."""
        found: uuid.UUID | None = (
            await self.db.execute(
                self._select().with_only_columns(BrandProfile.default_manufacturer_party_id)
            )
        ).scalar_one_or_none()
        return found

    async def default_country(self) -> str | None:
        """SI12: the brand default country of origin, or None (no row or none set)."""
        found: str | None = (
            await self.db.execute(
                self._select().with_only_columns(BrandProfile.default_country_of_origin)
            )
        ).scalar_one_or_none()
        return found

    async def get_for_update(self) -> BrandProfile:
        """This tenant's profile row, locked (call `ensure` first)."""
        row: BrandProfile = (await self.db.execute(self._select().with_for_update())).scalar_one()
        return row

    async def touch(self) -> None:
        """Bump `updated_at` (the `set_updated_at` trigger) so the profile's ETag moves."""
        stmt = self._scoped_write(
            update(BrandProfile)
            .where(self._tenant_criterion(include_platform=False))
            .values({BrandProfile.provenance: BrandProfile.provenance})
        )
        await self.db.execute(stmt)

    async def set_default_manufacturer(
        self, party_id: uuid.UUID | None, fact: Mapping[str, Any]
    ) -> bool:
        """Write `default_manufacturer_party_id` and `provenance.manufacturer` (the value-less
        seller Fact; CP4), or clear both with `party_id=None`. True when a row was written."""
        column = BrandProfile.default_manufacturer_party_id
        if party_id is None:
            changed = or_(column.is_not(None), BrandProfile.provenance.has_key(MANUFACTURER_KEY))
            provenance = BrandProfile.provenance.op("-")(MANUFACTURER_KEY)
        else:
            new = cast(literal(json.dumps(dict(fact))), JSONB)
            changed = or_(
                column.is_distinct_from(party_id),
                BrandProfile.provenance[MANUFACTURER_KEY]["source"].is_distinct_from(new["source"]),
            )
            provenance = BrandProfile.provenance.op("||")(
                func.jsonb_build_object(MANUFACTURER_KEY, new)
            )
        stmt = self._scoped_write(
            update(BrandProfile)
            .where(self._tenant_criterion(include_platform=False), changed)
            .values({column: party_id, BrandProfile.provenance: provenance})
        ).returning(BrandProfile.tenant_id)
        return (await self.db.execute(stmt)).first() is not None

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
