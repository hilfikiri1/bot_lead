"""Data structures passed to the Jinja2 catalog template."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ai.schemas import CatalogContent


@dataclass
class BrandTheme:
    """Branding / visual configuration for the catalog."""

    name: str
    primary_color: str
    accent_color: str
    text_color: str
    website: str = ""
    email: str = ""
    phone: str = ""
    logo_data_uri: str | None = None


@dataclass
class CatalogRenderContext:
    """Everything the HTML template needs to render a catalog."""

    brand: BrandTheme
    content: CatalogContent
    source_url: str
    generated_date: str
    main_image: str | None = None
    gallery_images: list[str] = field(default_factory=list)
    qr_code_data_uri: str | None = None

    @property
    def has_specifications(self) -> bool:
        return bool(self.content.specifications)

    @property
    def has_variants(self) -> bool:
        return bool(self.content.variants)

    @property
    def has_price_tiers(self) -> bool:
        return bool(self.content.price_tiers)
