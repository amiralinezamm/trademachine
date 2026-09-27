"""Standalone entry-point for the admin panel API.

Runs on port 8001, separate from the signal-generation engine (main.py on 8000).
Imports: only psycopg2 and yaml (via levels_store + blackout) — no numpy/talib.
"""
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from src.api.admin_router import router as admin_router

app = FastAPI(title="XAUUSD Admin API")
app.include_router(admin_router)
