"""One-lead-at-a-time Facebook lead intake pipeline.

See ``LEAD_INTAKE.md`` for the full contract. This package intentionally
reuses the existing Kommo, Google Sheets, Telegram and OpenAI clients
(``app.services.kommo_service``, ``app.services.google_sheets_service``,
``app.services.telegram_service``, ``app.services.product_title_service``)
instead of duplicating them.
"""
