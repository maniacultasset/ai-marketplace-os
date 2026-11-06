"""Products (owner steps S12, S13). Create, list and read are S12's (CODE-PLAN S12 T9-T12): the
route maps the request onto the service and the view onto `ProductOut`; domain errors render
through the shared handlers (`app/api/errors.py`). The CSV import is S13b's: the route reads the `file` part and hands it to
`app.services.imports.upload`; a file or header problem is 422 `IMPORT_FAILED` with the file-level
report in the envelope's `error_report`."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.pagination import PageInfo, PageParams, encode_cursor
from app.api.v1.deps import IdempotencyKey, IfMatch, get_current_principal
from app.api.v1.schemas.brand_profile import PartyIn, PartyOut
from app.api.v1.schemas.imports import ImportAccepted
from app.api.v1.schemas.products import (
    ColourwayOut,
    FibreOut,
    ProductCreate,
    ProductImageOut,
    ProductOut,
    ProductPage,
    ProductPatch,
    SizeChart,
    SizeChartRow,
)
from app.api.v1.surface import OPS, route
from app.core.errors import ConflictError
from app.core.rbac import Principal
from app.services import product_edit, products
from app.services.imports import upload
from app.services.intake_values import PartyInput

router = APIRouter()


def party_input(party: PartyIn) -> PartyInput:
    return PartyInput(**party.model_dump())


def size_chart_dict(chart: SizeChart) -> dict[str, Any]:
    """The wire table with absent measurements dropped (`normalize_size_chart` checks it)."""
    return {
        "unit": chart.unit,
        "basis": chart.basis,
        "rows": [row.model_dump(exclude_none=True) for row in chart.rows],
    }


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def party_out(view: products.PartyView | None) -> PartyOut | None:
    if view is None:
        return None
    return PartyOut.model_construct(
        id=view.id,
        legal_name=view.legal_name,
        address_line1=view.address_line1,
        address_line2=view.address_line2,
        city=view.city,
        state_code=view.state_code,
        postal_code=view.postal_code,
        country_code=view.country_code,
        gstin=view.gstin,
        inherited=view.inherited,
    )


def _chart(chart: dict[str, Any] | None) -> SizeChart | None:
    if chart is None:
        return None
    rows = chart["rows"]
    assert isinstance(rows, list)  # noqa: S101 - the service built it
    return SizeChart.model_construct(
        unit=chart["unit"],
        basis=chart["basis"],
        rows=[SizeChartRow.model_construct(**row) for row in rows],
    )


def _out(view: products.ProductView) -> ProductOut:
    """A DB-sourced row is built, not validated: the DB CHECKs are the read-side truth, and
    `ProductOut`'s input patterns (`Sku`, the non-blank title, the slug) must not turn one stored row
    into a 500 for the whole tenant list. The service already keeps `attributes` to wire scalars."""
    fibres = view.fiber_composition
    return ProductOut.model_construct(
        style_number=view.style_number,
        mrp=view.mrp,
        selling_price=view.selling_price,
        currency=view.currency,
        fiber_composition=(
            None if fibres is None else [FibreOut.model_construct(**f) for f in fibres]
        ),
        size_chart=_chart(view.size_chart),
        size_chart_url=view.size_chart_url,
        manufacturer=party_out(view.manufacturer),
        image_details=[ProductImageOut.model_construct(**asdict(d)) for d in view.image_details],
        colourways=[
            ColourwayOut.model_construct(id=c.id, colour=c.colour, colour_sku=c.colour_sku)
            for c in view.colourways
        ],
        id=view.id,
        sku=view.sku,
        title=view.title,
        status=view.status,
        category=view.category,
        attributes=view.attributes,
        images=view.images,
        parent_product_id=view.parent_product_id,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


@route(router, OPS["products_create"])
async def products_create(
    body: ProductCreate, principal: CurrentPrincipal, response: Response
) -> ProductOut:
    """Create one product. The SKU is unique per brand (409 SKU_CONFLICT); an unknown attribute
    key is 422 UNKNOWN_FACT_KEY."""
    view = await products.create_product(
        principal,
        products.NewProduct(
            sku=body.sku,
            title=body.title,
            category=body.category,
            images=tuple(body.images),
            attributes=dict(body.attributes),
            style_number=body.style_number,
            mrp=None if body.mrp is None else Decimal(body.mrp),
            selling_price=None if body.selling_price is None else Decimal(body.selling_price),
            manufacturer=None if body.manufacturer is None else party_input(body.manufacturer),
            size_chart=None if body.size_chart is None else size_chart_dict(body.size_chart),
        ),
    )
    response.headers["ETag"] = view.etag
    return _out(view)


@route(router, OPS["products_list"])
async def products_list(
    page: Annotated[PageParams, Depends()], principal: CurrentPrincipal
) -> ProductPage:
    """List the brand's products, newest first."""
    views, has_more = await products.list_products(principal, limit=page.limit, after=page.after)
    last = views[-1] if views else None
    next_cursor = encode_cursor(last.created_at, last.id) if has_more and last else None
    return ProductPage(
        data=[_out(v) for v in views],
        page=PageInfo(next_cursor=next_cursor, has_more=has_more, limit=page.limit),
    )


@route(router, OPS["products_bulk"])
async def products_bulk(
    file: Annotated[UploadFile, File(description="The CSV file (multipart only, 10 MiB cap).")],
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> ImportAccepted:
    """Start a CSV import: 202 with the import and its job. Bad rows are isolated, never failing
    the batch, and the import does not start generation."""
    data = await file.read()
    try:
        result = await upload.accept_upload(
            principal, idempotency_key, request.url.path, file.filename, data
        )
    except ConflictError as exc:
        if exc.code is not ErrorCode.IDEMPOTENCY_KEY_REUSED:
            raise
        raise ApiError(409, exc.code) from None
    report = result.report
    if report is not None:
        raise ApiError(422, ErrorCode.IMPORT_FAILED, report=report)
    return ImportAccepted.model_validate(result.body)


@route(router, OPS["products_get"])
async def products_get(
    product_id: Annotated[UUID, PathParam()], principal: CurrentPrincipal, response: Response
) -> ProductOut:
    """Read one product with its `ETag` (send it back as `If-Match` on PATCH). Another brand's id
    is 404, never 403."""
    view = await products.get_product(principal, product_id)
    response.headers["ETag"] = view.etag
    return _out(view)


def _patch(body: ProductPatch) -> dict[str, Any]:
    """The fields the client sent (absent stays absent; `null` is kept), in service types."""
    out: dict[str, Any] = {}
    for name in body.model_fields_set:
        value = getattr(body, name)
        if value is None:
            out[name] = None
        elif name in ("mrp", "selling_price"):
            out[name] = Decimal(value)
        elif name == "manufacturer":
            out[name] = party_input(value)
        elif name == "size_chart":
            out[name] = size_chart_dict(value)
        elif name == "attributes":
            out[name] = dict(value)
        else:
            out[name] = value
    return out


@route(router, OPS["products_update"])
async def products_update(
    product_id: Annotated[UUID, PathParam()],
    body: ProductPatch,
    if_match: IfMatch,
    principal: CurrentPrincipal,
    response: Response,
) -> ProductOut:
    """Edit a product. `If-Match` is the `ETag` from GET (409 STALE_VERSION when stale); a body
    that changes nothing is 200 with the current `ETag`. `null` clears a field; `sku` is
    immutable."""
    view = await product_edit.update_product(principal, product_id, _patch(body), if_match)
    response.headers["ETag"] = view.etag
    return _out(view)
