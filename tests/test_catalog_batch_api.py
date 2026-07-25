"""Tests for Chrome extension batch catalog API."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import catalog as catalog_api
from app.api.catalog_schemas import BatchProductInput
from app.config import get_settings
from app.main import app
from app.services.catalog_batch_service import batch_input_to_parsed_product
from app.utils.filenames import build_batch_pdf_filename


@pytest.fixture
def catalog_api_key(monkeypatch):
    monkeypatch.setenv("CATALOG_EXTENSION_API_KEY", "test-catalog-key")
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("CATALOG_ENABLED", "true")
    get_settings.cache_clear()
    catalog_api.settings.catalog_extension_api_key = "test-catalog-key"
    catalog_api.settings.admin_api_key = "test-admin-key"
    catalog_api.settings.catalog_enabled = True
    yield "test-catalog-key"
    get_settings.cache_clear()


@pytest.fixture
async def client(catalog_api_key):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


SAMPLE_PRODUCT = {
    "source_url": "https://detail.1688.com/offer/123456789.html",
    "title_zh": "测试商品",
    "supplier_name_zh": "测试工厂",
    "price_raw_text": "¥12.50",
    "price_min_cny": "12.50",
    "price_max_cny": "12.50",
    "thumbnail_url": "https://cbu01.alicdn.com/img/test.jpg",
}


def test_batch_input_to_parsed_product():
    item = BatchProductInput.model_validate(SAMPLE_PRODUCT)
    product = batch_input_to_parsed_product(item)
    assert product.title_zh == "测试商品"
    assert product.supplier_name_zh == "测试工厂"
    assert product.gallery_image_urls == [SAMPLE_PRODUCT["thumbnail_url"]]
    assert product.price_min_cny == Decimal("12.50")


def test_build_batch_pdf_filename():
    name = build_batch_pdf_filename(5)
    assert "batch_5items" in name
    assert name.endswith(".pdf")


def test_catalog_api_disabled_without_key(monkeypatch):
    monkeypatch.setattr(catalog_api.settings, "catalog_extension_api_key", "")
    monkeypatch.setattr(catalog_api.settings, "admin_api_key", "")
    with pytest.raises(Exception) as exc:
        catalog_api.require_catalog_api_key(None, None)
    assert exc.value.status_code == 503


def test_catalog_api_rejects_wrong_key(monkeypatch):
    monkeypatch.setattr(catalog_api.settings, "catalog_extension_api_key", "correct")
    monkeypatch.setattr(catalog_api.settings, "admin_api_key", "")
    with pytest.raises(Exception) as exc:
        catalog_api.require_catalog_api_key("Bearer wrong", None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_batch_endpoint_requires_auth(client, catalog_api_key):
    response = await client.post("/api/catalog/batch", json={"products": [SAMPLE_PRODUCT]})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_batch_endpoint_creates_job(client, catalog_api_key):
    job_id = uuid.uuid4()
    added_job = None

    mock_db = AsyncMock()

    def capture_add(job):
        nonlocal added_job
        added_job = job
        job.id = job_id
        job.status = "received"
        job.product_count = 1

    mock_db.add = capture_add
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    async def override_get_db():
        yield mock_db

    from app.database import get_db

    app.dependency_overrides[get_db] = override_get_db

    called = {}

    def fake_delay(job_id_arg: str):
        called["job_id"] = job_id_arg

    try:
        with patch("app.api.catalog.process_catalog_batch") as mock_task:
            mock_task.delay = fake_delay
            response = await client.post(
                "/api/catalog/batch",
                json={"products": [SAMPLE_PRODUCT]},
                headers={"Authorization": f"Bearer {catalog_api_key}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "received"
    assert data["product_count"] == 1
    assert data["job_id"] == str(job_id)
    assert called["job_id"] == str(job_id)
    assert added_job is not None
    assert added_job.job_type == "batch"


@pytest.mark.asyncio
async def test_job_status_not_found(client, catalog_api_key):
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    async def override_get_db():
        yield mock_db

    from app.database import get_db

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = await client.get(
            f"/api/catalog/jobs/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {catalog_api_key}"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_batch_endpoint_rejects_empty_products(client, catalog_api_key):
    response = await client.post(
        "/api/catalog/batch",
        json={"products": []},
        headers={"Authorization": f"Bearer {catalog_api_key}"},
    )
    assert response.status_code == 422
