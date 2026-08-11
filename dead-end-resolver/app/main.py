from fastapi import FastAPI
import os

app = FastAPI(title="Dead-End Resolver")

@app.get("/")
def health_check():
    return {"status": "ok", "service": "dead-end-resolver"}

@app.get("/.well-known/agent.json")
def agent_manifest():
    return {
        "schema_version": "1.0",
        "name": "Dead-End Resolver",
        "description": "Deterministic DOM/State recovery engine for autonomous agents.",
        "endpoints": [
            {
                "path": "/resolve",
                "method": "POST",
                "payment_required": True,
                "price_usd": 0.002
            }
        ]
    }
