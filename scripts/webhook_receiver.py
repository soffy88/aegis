#!/usr/bin/env python3
"""Simple webhook receiver for qualification tests."""

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Any
import json

app = FastAPI()

class WebhookPayload(BaseModel):
    event_type: str
    payload: dict[str, Any]
    timestamp: str

received = []

@app.post("/webhook")
async def receive_webhook(payload: WebhookPayload):
    received.append(payload.model_dump())
    return {"status": "received"}

@app.get("/webhooks")
async def get_webhooks():
    return received

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8088)