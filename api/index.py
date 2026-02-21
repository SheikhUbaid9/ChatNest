from fastapi import FastAPI

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "ChatNest deployed on Vercel 🚀"}

@app.get("/health")
async def health():
    return {"ok": True}
