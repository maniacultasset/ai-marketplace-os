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
        # JSON Schema patterns are ECMA-262, whose \s also covers U+FEFF; Rust's does not
        ("a\ufeffb", False),
        ("\ufeff", False),
        ("a\u00a0b", False),
        ("a\u2028b", False),
        ("a\u3000b", False),
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


# ECMA-262 WhiteSpace + LineTerminator: what `\s` means to a JSON Schema validator and to JS clients.
_ECMA_SPACE = frozenset("\t\n\v\f\r \u00a0\u1680\u2028\u2029\u202f\u205f\u3000\ufeff") | {
    chr(c) for c in range(0x2000, 0x200B)
}


@settings(max_examples=300)
@given(st.text(min_size=1, max_size=64))
def test_accepted_sku_matches_the_contract_pattern(sku: str) -> None:
    # The server never accepts a SKU that the declared pattern, read as ECMA-262, refuses.
    body = {"sku": sku, "title": "t", "category": "kurta_sets"}
    try:
        stored = ProductCreate.model_validate(body).sku  # StrictModel strips the ends first
    except ValidationError:
        return
    assert "]" not in stored and not set(stored) & _ECMA_SPACE


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


def test_product_create_category_optional() -> None:
    """SI14 D3 (user, 2026-11-09, reversing T115 (a)): `products.category` is nullable (0027), so
    a create may leave it out; a sent category is still a slug."""
    assert ProductCreate.model_validate({"sku": "a", "title": "t"}).category is None
    assert (
        ProductCreate.model_validate({"sku": "a", "title": "t", "category": None}).category is None
    )
    with pytest.raises(ValidationError):
        ProductCreate.model_validate({"sku": "a", "title": "t", "category": "Kurta Sets"})
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
    [{}, {"reason_code": "off_brand_voice"}, {"feedback": "too long title"}],
)
def test_reject_needs_reason_and_feedback(extra: dict[str, Any]) -> None:
    body = _approve(1, **extra)
    body["items"][0]["decision"] = "reject"
    with pytest.raises(ValidationError):
        ApproveRequest.model_validate(body)


def test_reject_with_reason_and_feedback_ok() -> None:
    body = _approve(1, reason_code="off_brand_voice", feedback="Tone is off.", regenerate=True)
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


# ---- SI9 T2: ListingOut.external_id, ProductOut.photo_read ----

_LISTING_OUT: dict[str, Any] = {
    "id": "00000000-0000-4000-8000-000000000001",
    "product_id": "00000000-0000-4000-8000-000000000002",
    "marketplace": "amazon",
    "status": "published",
    "marketplace_state": "inactive",
    "status_changed_at": "2026-11-07T00:00:00Z",
    "created_at": "2026-11-07T00:00:00Z",
    "updated_at": "2026-11-07T00:00:00Z",
}


def test_listing_out_external_id_optional() -> None:
    from app.api.v1.schemas.listings import ListingOut

    assert ListingOut.model_validate(_LISTING_OUT).external_id is None
    assert ListingOut.model_validate({**_LISTING_OUT, "external_id": "B0TEST1234"}).external_id == (
        "B0TEST1234"
    )
    with pytest.raises(ValidationError):
        ListingOut.model_validate({**_LISTING_OUT, "external_id": "x" * 129})


_PHOTO_READ: dict[str, Any] = {
    "status": "read",
    "at": "2026-11-07T00:00:00Z",
    "written": 2,
    "asked": 1,
}


@pytest.mark.parametrize("status", ["read", "unchanged", "failed", "not_read"])
def test_photo_read_statuses(status: str) -> None:
    from app.api.v1.schemas.products import PhotoReadOut

    assert PhotoReadOut.model_validate({**_PHOTO_READ, "status": status}).status == status


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "reading"},
        {"written": -1},
        {"written": 17},
        {"asked": -1},
        {"asked": 17},
        {"at": "2026-11-07T00:00:00"},
    ],
)
def test_photo_read_refused(bad: dict[str, Any]) -> None:
    from app.api.v1.schemas.products import PhotoReadOut

    with pytest.raises(ValidationError):
        PhotoReadOut.model_validate({**_PHOTO_READ, **bad})


def test_product_out_photo_read_optional() -> None:
    from app.api.v1.schemas.products import ProductOut

    body = {
        "id": "00000000-0000-4000-8000-000000000002",
        "sku": "FAB-KURTA-001",
        "title": "Kurta",
        "status": "draft",
        "category": "ethnic_wear_kurta_sets",
        "created_at": "2026-11-07T00:00:00Z",
        "updated_at": "2026-11-07T00:00:00Z",
    }
    assert ProductOut.model_validate(body).photo_read is None
    got = ProductOut.model_validate({**body, "photo_read": _PHOTO_READ}).photo_read
    assert got is not None and got.written == 2


# ---- SI11 (contract 2.1.0 MINOR): the resolution read, IssueOut.lane/cause, ListingOut.remote_check ----

_T = "2026-11-08T09:30:00Z"


def _attempt(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "submission",
        "number": 1,
        "version": 1,
        "at": _T,
        "actor": "system",
        "outcome": "rejected",
        "failed": [
            {
                "field": "attributes.color",
                "message": "Amazon does not accept this colour.",
                "lane": "auto_repair",
                "cause": "mapping",
                "marketplace_code": "90220",
            }
        ],
        "changed": [
            {"kind": "repair", "field": "attributes.color", "rule": "enum_fold", "summary": "x"}
        ],
    }
    return base | overrides


def _resolution(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "state": "idle",
        "cycles_used": 0,
        "attempts": [],
        "next_step": {"kind": "none"},
    }
    return base | overrides


@pytest.mark.parametrize(
    "state",
    [
        "checking",
        "fixing",
        "resubmitted",
        "fix_ready",
        "needs_you",
        "needs_marketplace",
        "stopped",
        "live",
        "idle",
    ],
)
def test_resolution_round_trips_each_state(state: str) -> None:
    from app.api.v1.schemas.resolution import ResolutionOut

    out = ResolutionOut.model_validate(_resolution(state=state, attempts=[_attempt()]))
    again = ResolutionOut.model_validate(out.model_dump(mode="json"))
    assert again == out and again.max_cycles == 3


def test_resolution_attempt_superseded_and_precheck() -> None:
    from app.api.v1.schemas.resolution import AttemptOut

    assert AttemptOut.model_validate(_attempt(outcome="superseded")).outcome == "superseded"
    assert AttemptOut.model_validate(_attempt(kind="precheck", number=0, outcome="issues"))


@pytest.mark.parametrize(
    "bad",
    [
        {"state": "done"},
        {"cycles_used": 4},
        {"cycles_used": -1},
        {"attempts": [_attempt()] * 11},
        {"attempts": [_attempt(number=-1)]},
        {"attempts": [_attempt(outcome="maybe")]},
        {"attempts": [_attempt(failed=[{**_attempt()["failed"][0], "lane": "robot"}])]},
        {"attempts": [_attempt(failed=[{**_attempt()["failed"][0], "message": "m" * 1001}])]},
        {"next_step": {"kind": "none", "text": "t" * 301}},
        {"next_step": {"kind": "teleport"}},
    ],
)
def test_resolution_refuses(bad: dict[str, Any]) -> None:
    from app.api.v1.schemas.resolution import ResolutionOut

    with pytest.raises(ValidationError):
        ResolutionOut.model_validate(_resolution(**bad))


def test_issue_and_listing_old_payloads_still_validate() -> None:
    from app.api.v1.schemas.issues import IssueOut
    from app.api.v1.schemas.listings import ListingOut

    issue = {
        "id": PID,
        "listing_id": PID,
        "marketplace": "amazon",
        "source": "amazon_adapter",
        "field_path": "attributes.color",
        "error_code": "VALUE_NOT_ALLOWED",
        "error_class": "invalid_value",
        "retry_class": "user_action",
        "severity": "error",
        "auto_fixable": False,
        "message": "m",
        "occurrences": 1,
        "first_seen_at": _T,
        "last_seen_at": _T,
        "status": "open",
        "created_at": _T,
        "updated_at": _T,
    }
    parsed = IssueOut.model_validate(issue)
    assert (parsed.lane, parsed.cause) == (None, None)
    assert IssueOut.model_validate(issue | {"lane": "seller", "cause": "seller_fact"}).lane == (
        "seller"
    )
    with pytest.raises(ValidationError):
        IssueOut.model_validate(issue | {"lane": "robot"})
    listing = {
        "id": PID,
        "product_id": PID,
        "marketplace": "amazon",
        "status": "validated",
        "marketplace_state": "not_submitted",
        "status_changed_at": _T,
        "created_at": _T,
        "updated_at": _T,
    }
    assert ListingOut.model_validate(listing).remote_check is None
    check = {"version_id": PID, "status": "pending"}
    assert ListingOut.model_validate(listing | {"remote_check": check}).remote_check
    with pytest.raises(ValidationError):
        ListingOut.model_validate(listing | {"remote_check": check | {"status": "done"}})
