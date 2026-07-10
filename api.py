from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"message": "API работает!"}

@app.get("/api/status")
def status():
    return {"status": "ok", "message": "Сервер запущен"}

@app.get("/api/events")
def events():
    return {
        "total": 3,
        "events": [
            {"id": 1, "title": "Тестовое событие", "category": "Концерт"}
        ]
    }
