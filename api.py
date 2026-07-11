from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import hashlib
import threading
import schedule
import time

from database import SessionLocal, init_db, User, UserInteraction, Event
from recommender import HybridRecommender

# ─── ИНИЦИАЛИЗАЦИЯ ────────────────────────────────────────
app = FastAPI(title="Афиша SPB — API", version="1.0")
# Подключаем папку со статическими файлами
app.mount("/static", StaticFiles(directory="static"), name="static")

# Главная страница открывается по корневому адресу
@app.get("/")
def root():
    return FileResponse("static/index.html")

# Любой HTML файл открывается по имени
@app.get("/{page}.html")
def get_page(page: str):
    file_path = f"static/{page}.html"
    import os
    if os.path.exists(file_path):
        return FileResponse(file_path)
    return FileResponse("static/index.html")
# Разрешаем запросы с любых сайтов (для фронтенда)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Глобальная модель — обучается один раз при старте
# и переобучается по расписанию
recommender = HybridRecommender()
model_lock = threading.Lock()


# ─── СХЕМЫ ДАННЫХ (что принимает и возвращает API) ────────

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    user_type: str = "resident"  # resident или guest

class LoginRequest(BaseModel):
    email: str
    password: str

class InteractionRequest(BaseModel):
    user_id: int
    event_id: int
    interaction_type: str  # view / favorite / attended / rated
    rating: Optional[float] = None

class SmartPlanRequest(BaseModel):
    user_id: Optional[int] = None
    date_from: str
    date_to: str
    budget: int
    interests: List[str]
    time_slots: Optional[dict] = None
    district: Optional[str] = None


# ─── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ──────────────────────────────

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── ЗАПУСК И ПЕРЕОБУЧЕНИЕ ────────────────────────────────

@app.on_event("startup")
async def startup():
    """При запуске сервера: создаём БД и обучаем модель"""
    init_db()

    import os
    from load_data import load_events_from_csv, seed_test_users

    # Загружаем ВСЕ CSV файлы всегда — дубли пропускаются автоматически
    csv_files = sorted([f for f in os.listdir(".") if f.startswith("events_") and f.endswith(".csv")])

    if csv_files:
        print("Загружаю CSV файлы: " + str(csv_files))
        for filename in csv_files:
            print("Загружаю: " + filename)
            load_events_from_csv(filename)
    else:
        print("CSV файлы не найдены")

    seed_test_users()
    retrain_model()
    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()
    print("Сервер запущен и готов к работе")

def retrain_model():
    """Переобучает модель на актуальных данных"""
    with model_lock:
        print("Переобучение модели [" + datetime.now().strftime("%H:%M:%S") + "]...")
        try:
            recommender.train()
            print("Модель переобучена успешно")
        except Exception as e:
            print("Ошибка переобучения: " + str(e))

def run_scheduler():
    """Фоновый поток: переобучение каждые 24 часа"""
    schedule.every(24).hours.do(retrain_model)
    while True:
        schedule.run_pending()
        time.sleep(60)

# ─── ЭНДПОИНТЫ: ПОЛЬЗОВАТЕЛИ ──────────────────────────────

@app.post("/api/register")
def register(data: RegisterRequest, db=Depends(get_db)):
    """Регистрация нового пользователя"""

    # Проверяем — нет ли уже такого email
    existing = db.query(User).filter(User.email == data.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email уже зарегистрирован")

    user = User(
        name=data.name,
        email=data.email,
        password_hash=hash_password(data.password),
        user_type=data.user_type,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return {
        "success": True,
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "user_type": user.user_type,
        }
    }


@app.post("/api/login")
def login(data: LoginRequest, db=Depends(get_db)):
    """Вход в систему"""

    user = db.query(User).filter(User.email == data.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if user.password_hash != hash_password(data.password):
        raise HTTPException(status_code=401, detail="Неверный пароль")

    return {
        "success": True,
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "user_type": user.user_type,
        }
    }


@app.get("/api/users/{user_id}")
def get_user(user_id: int, db=Depends(get_db)):
    """Получить профиль пользователя"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Статистика взаимодействий
    interactions = db.query(UserInteraction).filter(
        UserInteraction.user_id == user_id
    ).all()

    n_views = sum(1 for i in interactions if i.interaction_type == "view")
    n_favs = sum(1 for i in interactions if i.interaction_type == "favorite")
    n_attended = sum(1 for i in interactions if i.interaction_type == "attended")

    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "user_type": user.user_type,
        "registered_at": str(user.registered_at),
        "stats": {
            "views": n_views,
            "favorites": n_favs,
            "attended": n_attended,
            "total": len(interactions),
        }
    }


# ─── ЭНДПОИНТЫ: МЕРОПРИЯТИЯ ───────────────────────────────

@app.get("/api/events")
def get_events(
    category: Optional[str] = None,
    max_price: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
    db=Depends(get_db)
):
    query = db.query(Event)

    if category:
        query = query.filter(Event.category.contains(category))
    if max_price is not None:
        query = query.filter(
            (Event.is_free == True) | (Event.price_min <= max_price)
        )

    total = query.count()
    events = query.offset(offset).limit(limit).all()

    return {
        "total": total,
        "events": [{
            "id": e.id,
            "title": e.title,
            "category": e.category or "Другое",
            "start_datetime": e.start_datetime or "",
            "venue_title": e.venue_title or "Не указано",
            "venue_address": e.venue_address or "",
            "venue_lat": e.venue_lat,
            "venue_lon": e.venue_lon,
            "price_min": e.price_min or 0,
            "is_free": bool(e.is_free),
            "age_restriction": e.age_restriction or "0+",
            "image_url": e.image_url or "",
            "is_private": bool(e.is_private),
            "source": e.source or "kudago",
            "tags": e.tags or "",
        } for e in events]
    }


@app.get("/api/events/{event_id}")
def get_event(event_id: int, db=Depends(get_db)):
    """Получить полную информацию о мероприятии"""
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Мероприятие не найдено")

    return {
        "id": event.id,
        "title": event.title,
        "description": event.description,
        "category": event.category,
        "tags": event.tags,
        "start_datetime": event.start_datetime,
        "end_datetime": event.end_datetime,
        "venue_title": event.venue_title,
        "venue_address": event.venue_address,
        "venue_lat": event.venue_lat,
        "venue_lon": event.venue_lon,
        "price_min": event.price_min,
        "price_raw": event.price_raw,
        "is_free": event.is_free,
        "age_restriction": event.age_restriction,
        "image_url": event.image_url,
        "site_url": event.site_url,
        "is_private": event.is_private,
    }


# ─── ЭНДПОИНТЫ: ВЗАИМОДЕЙСТВИЯ ────────────────────────────

@app.post("/api/interactions")
def add_interaction(data: InteractionRequest, db=Depends(get_db)):
    """
    Записывает взаимодействие пользователя с мероприятием.
    Это главный источник данных для обучения модели.
    """

    # Проверяем существование пользователя и мероприятия
    user = db.query(User).filter(User.id == data.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    event = db.query(Event).filter(Event.id == data.event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Мероприятие не найдено")

    interaction = UserInteraction(
        user_id=data.user_id,
        event_id=data.event_id,
        interaction_type=data.interaction_type,
        rating=data.rating,
    )
    db.add(interaction)
    db.commit()

    return {"success": True, "message": "Взаимодействие записано"}


@app.get("/api/users/{user_id}/favorites")
def get_favorites(user_id: int, db=Depends(get_db)):
    """Получить избранные мероприятия пользователя"""

    favs = db.query(UserInteraction).filter(
        UserInteraction.user_id == user_id,
        UserInteraction.interaction_type == "favorite"
    ).all()

    event_ids = [f.event_id for f in favs]
    events = db.query(Event).filter(Event.id.in_(event_ids)).all()

    return {
        "favorites": [{
            "id": e.id,
            "title": e.title,
            "category": e.category,
            "start_datetime": e.start_datetime,
            "venue_title": e.venue_title,
            "price_min": e.price_min,
            "is_free": e.is_free,
            "image_url": e.image_url,
        } for e in events]
    }


# ─── ЭНДПОИНТЫ: РЕКОМЕНДАЦИИ ──────────────────────────────

@app.get("/api/recommendations/{user_id}")
def get_recommendations(
    user_id: int,
    n: int = 10,
    category: Optional[str] = None,
    max_price: Optional[int] = None,
):
    """
    Главный эндпоинт рекомендаций.
    Возвращает персонализированный список мероприятий.
    """
    if not recommender.is_trained:
        raise HTTPException(
            status_code=503,
            detail="Модель ещё обучается, попробуйте через минуту"
        )

    try:
        recs = recommender.recommend(
            user_id=user_id,
            n=n,
            category_filter=category,
            max_price=max_price,
        )
        alpha = recommender.get_alpha(user_id)

        return {
            "user_id": user_id,
            "alpha": alpha,
            "algorithm": "hybrid_svd_tfidf",
            "recommendations": recs,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/retrain")
def trigger_retrain():
    """
    Ручной запуск переобучения модели.
    Можно вызвать после того как набралось много новых данных.
    """
    thread = threading.Thread(target=retrain_model, daemon=True)
    thread.start()
    return {"success": True, "message": "Переобучение запущено в фоне"}


# ─── ЭНДПОИНТ: УМНЫЙ ПОДБОР ───────────────────────────────

@app.post("/api/smart-plan")
def create_smart_plan(data: SmartPlanRequest, db=Depends(get_db)):
    """
    Составляет персонализированное расписание
    для гостя города по датам, интересам и бюджету.
    """
    from smart_planner import SmartPlanner
    planner = SmartPlanner(db, recommender)

    plan = planner.create_plan(
        user_id=data.user_id,
        date_from=data.date_from,
        date_to=data.date_to,
        budget=data.budget,
        interests=data.interests,
        time_slots=data.time_slots,
        district=data.district,
    )

    return {"success": True, "plan": plan}


# ─── СТАТУС СИСТЕМЫ ───────────────────────────────────────

@app.get("/api/status")
def status(db=Depends(get_db)):
    """Общая информация о состоянии системы"""
    n_events = db.query(Event).count()
    n_users = db.query(User).count()
    n_interactions = db.query(UserInteraction).count()

    return {
        "status": "ok",
        "model_trained": recommender.is_trained,
        "stats": {
            "events": n_events,
            "users": n_users,
            "interactions": n_interactions,
        }
    }
