"""ASGI entry point: `uvicorn repairos.main:app` (make run-gateway). Slots come from config/providers.yaml."""
from repairos.api.app import create_app

app = create_app()
