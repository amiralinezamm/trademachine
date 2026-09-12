from fastapi import FastAPI

app = FastAPI(title="XAUUSD Trader Bot API")


@app.get("/health")
def health():
    return {"status": "ok"}
