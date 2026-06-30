"""HTTP API package.

Routers are imported explicitly by :mod:`app.main`. Keeping this module light
avoids importing Telegram/Celery integrations during unrelated unit tests.
"""
