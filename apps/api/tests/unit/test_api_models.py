"""S3 T7-T9 (AC5, AC6, AC8, AC12): the API-local request and response models."""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import re
import uuid
from typing import Any

import app.api.v1.schemas as schemas_pkg
import pytest
from app.api.v1.schemas.analytics import AnalyticsSummary
from app.api.v1.schemas.auth import LoginRequest, LogoutRequest, RefreshRequest
from app.api.v1.schemas.batches import ApproveRequest, PublishRequest, RegenerateItem
from app.api.v1.schemas.common import StorageKey
from app.api.v1.schemas.images import UploadUrlRequest
from app.api.v1.schemas.listings import ListingContentPatch, RegenerateRequest
from app.api.v1.schemas.marketplaces import ConnectRequest, MarketplaceAccountOut
from app.api.v1.schemas.products import ProductCreate, ProductPage
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.base import StrictModel
from pydantic import BaseModel, TypeAdapter, ValidationError

PID = str(uuid.UUID(int=1))


def _schema_modules() -> list[Any]:
    return [
        importlib.import_module(f"{schemas_pkg.__name__}.{info.name}")
        for info in pkgutil.iter_modules(schemas_pkg.__path__)
    ]


def request_models() -> list[type[StrictModel]]:
    found: dict[str, type[StrictModel]] = {}
    for module in _schema_modules():
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, StrictModel)
                and obj is not StrictModel
                and obj.__module__ == module.__name__
            ):
                found[obj.__name__] = obj
    return [found[name] for name in sorted(found)]


def _minimal(model: type[BaseModel]) -> dict[str, Any]:
    samples: dict[str, dict[str, Any]] = {
        "LoginRequest": {"email": "a@b.co", "password": "pw"},
        "RefreshRequest": {"refresh_token": "t"},
        "LogoutRequest": {"refresh_token": "t"},
        "ProductCreate": {"sku": "SKU-1", "title": "Kurta", "category": "kurta_sets"},
    }
    return samples[model.__name__]


@pytest.mark.parametrize("model", [LoginRequest, RefreshRequest, LogoutRequest, ProductCreate])
def test_extra_key_rejected(model: type[BaseModel]) -> None:
    body = _minimal(model)
    model.model_validate(body)
    with pytest.raises(ValidationError):
        model.model_validate({**body, "surprise": 1})


def test_every_request_model_is_strict() -> None:
    models = request_models()
    assert models
    for model in models:
        assert model.model_config.get("extra") == "forbid", model.__name__


@pytest.mark.parametrize(
    ("sku", "ok"),
    [
        ("", False),
        ("a", True),
        ("a" * 64, True),
        ("a" * 65, False),
        ("a b", False),
        ("a]b", False),
        ("a\tb", False),
        ("कुर्ता-1", True),
        ("SKU[1", True),
    ],
)
def test_sku_rules(sku: str, ok: bool) -> None:
    body = {"sku": sku, "title": "t", "category": "kurta_sets"}
    if ok:
        assert ProductCreate.model_validate(body).sku == sku
    else:
        with pytest.raises(ValidationError):
            ProductCreate.model_validate(body)


@pytest.mark.parametrize("title", ["", "   ", "x" * 501])
def test_title_rejected(title: str) -> None:
    with pytest.raises(ValidationError):
        ProductCreate.model_validate({"sku": "a", "title": title, "category": "kurta_sets"})


def test_login_repr_hides_password() -> None:
    login = LoginRequest.model_validate({"email": "a@b.co", "password": "hunter2-secret"})
    assert "hunter2-secret" not in repr(login)
    assert "hunter2-secret" not in str(login)
    assert login.password.get_secret_value() == "hunter2-secret"


@pytest.mark.parametrize(
    ("field", "limit"), [("email", 320), ("refresh_token", 4096), ("password", 1024)]
)
def test_auth_input_caps(field: str, limit: int) -> None:
    model: type[BaseModel] = LoginRequest if field in {"email", "password"} else RefreshRequest
    body = _minimal(model)
    at = "a" * (limit - 5) + "@b.co" if field == "email" else "a" * limit
    model.model_validate({**body, field: at})
    over = "a" * (limit - 4) + "@b.co" if field == "email" else "a" * (limit + 1)
    with pytest.raises(ValidationError):
        model.model_validate({**body, field: over})
    if field == "refresh_token":
        with pytest.raises(ValidationError):
            LogoutRequest.model_validate({field: over})


def test_product_page_component_name() -> None:
    assert ProductPage.__name__ == "ProductPage"
    assert set(ProductPage.model_fields) == {"data", "page"}


def _schema_has_tenant_id(node: Any) -> bool:
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict) and "tenant_id" in props:
            return True
        return any(_schema_has_tenant_id(v) for v in node.values())
    if isinstance(node, list):
        return any(_schema_has_tenant_id(v) for v in node)
    return False


def test_request_models_have_no_tenant_id() -> None:
    for model in request_models():
        schema = model.model_json_schema(mode="validation")
        assert not _schema_has_tenant_id(schema), model.__name__


def test_product_create_site_body() -> None:
    # spec L11318-11320: the site's own example body, plus the category T115 makes required
    # (user, 2026-10-06; products.category is NOT NULL).
    body = {
        "sku": "FAB-KURTA-001",
        "title": "Organic Cotton Kurta Set",
        "category": "kurta_sets",
        "attributes": {"color": "indigo", "material": "organic cotton", "size": "M"},
        "images": ["t/0b1c/img/1.jpg"],
    }
    product = ProductCreate.model_validate(body)
    assert product.model_dump(exclude_defaults=True) == body


def test_product_create_category_required() -> None:
    """T115 (user, 2026-10-06): `products.category` is NOT NULL, so the contract requires it."""
    with pytest.raises(ValidationError):
        ProductCreate.model_validate({"sku": "a", "title": "t"})
    with pytest.raises(ValidationError):
        ProductCreate.model_validate({"sku": "a", "title": "t", "category": None})
    assert ProductCreate.model_validate({"sku": "a", "title": "t", "category": "kurta_sets"})


@pytest.mark.parametrize(
    "attributes",
    [
        {"color": {"nested": "x"}},
        {"color": ["a", "b"]},
        {"color": None},
        {"weight": "x" * 2001},
        {"Color": "red"},
        {"1color": "red"},
        {"col or": "red"},
        {f"k{i}": i for i in range(201)},
    ],
)
def test_attributes_rejected(attributes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProductCreate.model_validate(
            {"sku": "a", "title": "t", "category": "kurta_sets", "attributes": attributes}
        )


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_attributes_non_finite_rejected(raw: str) -> None:
    text = '{"sku": "a", "title": "t", "attributes": {"weight": ' + raw + "}}"
    assert json.loads(text)  # Python's json accepts these literals, as FastAPI's parser does
    with pytest.raises(ValidationError):
        ProductCreate.model_validate(json.loads(text))


def test_attributes_scalars_accepted() -> None:
    attrs = {"color": "red", "count": 3, "weight": 1.5, "organic": True}
    got = ProductCreate.model_validate(
        {"sku": "a", "title": "t", "category": "kurta_sets", "attributes": attrs}
    )
    assert got.attributes == attrs
    assert type(got.attributes["organic"]) is bool
    assert type(got.attributes["count"]) is int


def test_images_capped() -> None:
    keys = [f"t/x/{i}.jpg" for i in range(51)]
    with pytest.raises(ValidationError):
        ProductCreate.model_validate(
            {"sku": "a", "title": "t", "category": "kurta_sets", "images": keys}
        )
    ProductCreate.model_validate(
        {"sku": "a", "title": "t", "category": "kurta_sets", "images": keys[:50]}
    )


_STORAGE_KEY = TypeAdapter(StorageKey)


@pytest.mark.parametrize(
    "key",
    [
        "../x",
        "a/../b",
        "a/..",
        "a..b",
        "/abs",
        "a//b",
        "s3://b/k",
        "https://x/k",
        "a/",
        "",
        "a" * 513,
        "a\x00b",
        "छवि.jpg",
        "a b.jpg",
        ".hidden",
    ],
)
def test_storage_key_rejects(key: str) -> None:
    with pytest.raises(ValidationError):
        _STORAGE_KEY.validate_python(key)


@pytest.mark.parametrize("key", ["t/0b1c/img/1.jpg", "a", "t/x_y-z/v1.2.png", "a" * 512])
def test_storage_key_accepts(key: str) -> None:
    assert _STORAGE_KEY.validate_python(key) == key


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet="ab./-_:\x00 ", max_size=24))
def test_storage_key_never_traverses(raw: str) -> None:
    try:
        key = _STORAGE_KEY.validate_python(raw)
    except ValidationError:
        return
    assert ".." not in key.split("/")
    assert ".." not in key
    assert not key.startswith("/")
    assert "//" not in key
    assert ":" not in key


# ---- T8: listings and batches (D11) ----


def _approve(n: int, **extra: Any) -> dict[str, Any]:
    return {
        "items": [
            {
                "listing_id": str(uuid.UUID(int=i + 1)),
                "listing_version_id": str(uuid.UUID(int=10_000 + i)),
                "decision": "approve",
                **extra,
            }
            for i in range(n)
        ]
    }


@pytest.mark.parametrize(("n", "ok"), [(0, False), (1, True), (200, True), (201, False)])
def test_batch_item_bounds(n: int, ok: bool) -> None:
    publish = {"items": [{"listing_id": str(uuid.UUID(int=i + 1))} for i in range(n)]}
    if ok:
        ApproveRequest.model_validate(_approve(n))
        PublishRequest.model_validate(publish)
    else:
        with pytest.raises(ValidationError):
            ApproveRequest.model_validate(_approve(n))
        with pytest.raises(ValidationError):
            PublishRequest.model_validate(publish)


def test_reviewer_id_rejected() -> None:
    with pytest.raises(ValidationError):
        ApproveRequest.model_validate({**_approve(1), "reviewer_id": PID})
    with pytest.raises(ValidationError):
        ApproveRequest.model_validate(_approve(1, reviewer_id=PID))


@pytest.mark.parametrize(
    "extra",
    [{}, {"reason_code": "off_brand"}, {"feedback": "too long title"}],
)
def test_reject_needs_reason_and_feedback(extra: dict[str, Any]) -> None:
    body = _approve(1, **extra)
    body["items"][0]["decision"] = "reject"
    with pytest.raises(ValidationError):
        ApproveRequest.model_validate(body)


def test_reject_with_reason_and_feedback_ok() -> None:
    body = _approve(1, reason_code="off_brand", feedback="Tone is off.", regenerate=True)
    body["items"][0]["decision"] = "reject"
    assert ApproveRequest.model_validate(body).items[0].regenerate is True


def test_approve_cannot_regenerate() -> None:
    with pytest.raises(ValidationError):
        ApproveRequest.model_validate(_approve(1, regenerate=True))


def test_duplicate_listing_ids_rejected() -> None:
    body = _approve(2)
    body["items"][1]["listing_id"] = body["items"][0]["listing_id"]
    with pytest.raises(ValidationError):
        ApproveRequest.model_validate(body)
    with pytest.raises(ValidationError):
        PublishRequest.model_validate({"items": [{"listing_id": PID}, {"listing_id": PID}]})


@pytest.mark.parametrize(("size", "ok"), [(2000, True), (2001, False)])
def test_regenerate_instructions_cap(size: int, ok: bool) -> None:
    for model, body in (
        (RegenerateRequest, {"instructions": "x" * size}),
        (RegenerateItem, {"listing_id": PID, "instructions": "x" * size}),
    ):
        if ok:
            model.model_validate(body)
        else:
            with pytest.raises(ValidationError):
                model.model_validate(body)


def test_content_patch_shape() -> None:
    content = {"title": "T", "bullets": ["b"], "description": "d"}
    ListingContentPatch.model_validate({"content": content})
    with pytest.raises(ValidationError):
        ListingContentPatch.model_validate({"content": content, "listing_id": PID})


# ---- T9: marketplaces, images, rules, analytics ----
_SECRETISH = re.compile(r"token|secret|password|key", re.IGNORECASE)


def _all_property_names(schema: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            names |= set(props)
        for value in schema.values():
            names |= _all_property_names(value)
    elif isinstance(schema, list):
        for value in schema:
            names |= _all_property_names(value)
    return names


def test_account_out_has_no_secret_fields() -> None:
    names = _all_property_names(MarketplaceAccountOut.model_json_schema(mode="serialization"))
    assert names
    assert not {n for n in names if _SECRETISH.search(n)}


def _summary(**extra: Any) -> dict[str, Any]:
    return {"from": "2026-10-01", "to": "2026-10-05", "listings_generated": 3, **extra}


@pytest.mark.parametrize("rate", [0, 0.5, 1, None])
def test_rates_in_range(rate: float | None) -> None:
    AnalyticsSummary.model_validate(_summary(approval_rate=rate))


@pytest.mark.parametrize("rate", [-0.01, 1.01, float("nan"), float("inf")])
def test_rates_out_of_range(rate: float) -> None:
    with pytest.raises(ValidationError):
        AnalyticsSummary.model_validate(_summary(publish_success_rate=rate))


def test_date_range_from_not_after_to() -> None:
    AnalyticsSummary.model_validate(_summary(to="2026-10-01"))
    with pytest.raises(ValidationError):
        AnalyticsSummary.model_validate(_summary(to="2026-09-30"))


def test_analytics_serializes_from_alias() -> None:
    dumped = AnalyticsSummary.model_validate(_summary()).model_dump(mode="json")
    assert "from" in dumped
    assert "from_" not in dumped


@pytest.mark.parametrize("size", [0, 10_485_761])
def test_upload_url_bytes_bounds(size: int) -> None:
    body = {"product_id": PID, "slot": "main", "content_type": "image/png", "bytes": size}
    with pytest.raises(ValidationError):
        UploadUrlRequest.model_validate(body)
    UploadUrlRequest.model_validate({**body, "bytes": 10_485_760})


def test_upload_url_content_type_images_only() -> None:
    body = {"product_id": PID, "slot": "main", "content_type": "text/csv", "bytes": 1}
    with pytest.raises(ValidationError):
        UploadUrlRequest.model_validate(body)


def test_connect_marketplace_enum() -> None:
    ConnectRequest.model_validate({"marketplace": "amazon"})
    with pytest.raises(ValidationError):
        ConnectRequest.model_validate({"marketplace": "ebay"})


# ---- S3 fix round 1 (R1-3): TenantOut.slug follows ck_tenants_slug, not the category Slug ----
def _ck_tenants_slug_pattern() -> str:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "alembic/versions/0001_core_tenancy.py"
    ).read_text("utf-8")
    found: list[str] = re.findall(r"CONSTRAINT ck_tenants_slug CHECK \(slug ~ '([^']+)'\)", source)
    assert len(found) == 1, found
    return found[0]


def test_tenant_slug_pattern_matches_the_db_check() -> None:
    from app.api.v1.schemas.tenants import TenantOut

    schema = TenantOut.model_json_schema(mode="serialization")
    assert schema["properties"]["slug"]["pattern"] == _ck_tenants_slug_pattern()


@pytest.mark.parametrize(
    ("slug", "ok"),
    [
        ("acme-brand", True),
        ("ab", True),
        ("a" * 40, True),
        ("a" * 41, False),
        ("a", False),
        ("acme_brand", False),
        ("-acme", False),
        ("Acme", False),
    ],
)
def test_tenant_out_accepts_what_the_db_accepts(slug: str, ok: bool) -> None:
    from app.api.v1.schemas.tenants import TenantOut

    body = {"id": PID, "name": "Acme", "slug": slug, "status": "active"}
    assert (re.fullmatch(_ck_tenants_slug_pattern(), slug) is not None) is ok
    if ok:
        assert TenantOut.model_validate(body).slug == slug
    else:
        with pytest.raises(ValidationError):
            TenantOut.model_validate(body)


def test_published_contract_carries_the_tenant_slug_pattern() -> None:
    from pathlib import Path

    spec = json.loads(
        (
            Path(__file__).resolve().parents[4] / "packages/schemas/jsonschema/openapi.json"
        ).read_text("utf-8")
    )
    slug = spec["components"]["schemas"]["TenantOut"]["properties"]["slug"]
    assert slug["pattern"] == _ck_tenants_slug_pattern()
