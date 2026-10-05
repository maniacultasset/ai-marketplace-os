"""S3 T7-T9 (AC5, AC6, AC8, AC12): the API-local request and response models."""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import uuid
from typing import Any

import app.api.v1.schemas as schemas_pkg
import pytest
from app.api.v1.schemas.auth import LoginRequest, LogoutRequest, RefreshRequest
from app.api.v1.schemas.common import StorageKey
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
        "ProductCreate": {"sku": "SKU-1", "title": "Kurta"},
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
    body = {"sku": sku, "title": "t"}
    if ok:
        assert ProductCreate.model_validate(body).sku == sku
    else:
        with pytest.raises(ValidationError):
            ProductCreate.model_validate(body)


@pytest.mark.parametrize("title", ["", "   ", "x" * 501])
def test_title_rejected(title: str) -> None:
    with pytest.raises(ValidationError):
        ProductCreate.model_validate({"sku": "a", "title": title})


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
    # spec L11318-11320: the site's own example body, unchanged.
    body = {
        "sku": "FAB-KURTA-001",
        "title": "Organic Cotton Kurta Set",
        "attributes": {"color": "indigo", "material": "organic cotton", "size": "M"},
        "images": ["t/0b1c/img/1.jpg"],
    }
    product = ProductCreate.model_validate(body)
    assert product.model_dump(exclude_defaults=True) == body


def test_product_create_category_optional() -> None:
    assert ProductCreate.model_validate({"sku": "a", "title": "t"}).category is None
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
        ProductCreate.model_validate({"sku": "a", "title": "t", "attributes": attributes})


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_attributes_non_finite_rejected(raw: str) -> None:
    text = '{"sku": "a", "title": "t", "attributes": {"weight": ' + raw + "}}"
    assert json.loads(text)  # Python's json accepts these literals, as FastAPI's parser does
    with pytest.raises(ValidationError):
        ProductCreate.model_validate(json.loads(text))


def test_attributes_scalars_accepted() -> None:
    attrs = {"color": "red", "count": 3, "weight": 1.5, "organic": True}
    got = ProductCreate.model_validate({"sku": "a", "title": "t", "attributes": attrs})
    assert got.attributes == attrs
    assert type(got.attributes["organic"]) is bool
    assert type(got.attributes["count"]) is int


def test_images_capped() -> None:
    keys = [f"t/x/{i}.jpg" for i in range(51)]
    with pytest.raises(ValidationError):
        ProductCreate.model_validate({"sku": "a", "title": "t", "images": keys})
    ProductCreate.model_validate({"sku": "a", "title": "t", "images": keys[:50]})


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
