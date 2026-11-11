"""The served app: `uvicorn app.main:app` (issue #120).

Building the app opens the database and returns every SENDING reminder and
scheduled invoice to PENDING. Only the server imports this module; tests and
scripts import app.web and build their own app on their own database.
"""

from app.web import create_app

app = create_app(scheduler_enabled=True)
