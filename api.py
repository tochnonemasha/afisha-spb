from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from pathlib import Path
import hashlib
import threading
import schedule
import time
import os
import json
import urllib.parse
import urllib.request

from database import SessionLocal, init_db, User, UserInteraction, Event, EventReview
from recommender import HybridRecommender

# ─── ИНИЦИАЛИЗАЦИЯ ────────────────────────────────────────
app = FastAPI(title="Афиша SPB — API", version="1.0")


class AddressRequest(BaseModel):
    address: str = Field(..., min_length=3, max_length=500)


# CORS — фронтенд и API могут быть размещены на разных Railway-доменах
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Абсолютные пути не зависят от рабочей папки, из которой запущен uvicorn.
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Главная страница: поддерживаем как структуру static/, так и picker.html в корне.
@app.get("/")
def root():
    for candidate in (STATIC_DIR / "index.html", STATIC_DIR / "picker.html", APP_DIR / "picker.html"):
        if candidate.is_file():
            return FileResponse(str(candidate))
    return {"service": "afisha-spb-api", "status": "ok", "hint": "Откройте /docs или /api/health"}

@app.get("/{page}.html")
def get_page(page: str):
    for candidate in (STATIC_DIR / f"{page}.html", APP_DIR / f"{page}.html"):
        if candidate.is_file():
            return FileResponse(str(candidate))
    raise HTTPException(status_code=404, detail=f"Страница {page}.html не найдена")

@app.get("/api/health")
def health():
    # Проверка доступности процесса без подключения к БД и модели рекомендаций.
    return {"status": "ok", "service": "afisha-spb-api"}


@app.get("/api/address-suggest")
def address_suggest(q: str = ""):
    """Подсказки адресов Яндекса. Ключ остаётся на сервере."""
    query = (q or "").strip()
    api_key = os.getenv("YANDEX_SUGGEST_API_KEY", "").strip()
    if len(query) < 3 or not api_key:
        return {"success": True, "suggestions": []}
    params = urllib.parse.urlencode({
        "apikey": api_key,
        "text": query,
        "lang": "ru_RU",
        "results": 5,
        "print_address": 1,
    })
    request = urllib.request.Request(
        "https://suggest-maps.yandex.ru/v1/suggest?" + params,
        headers={"User-Agent": "AfishaSPBPlanner/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8"))
        suggestions = []
        for item in payload.get("results", []):
            title = (item.get("title") or {}).get("text", "").strip()
            subtitle = (item.get("subtitle") or {}).get("text", "").strip()
            value = ", ".join(part for part in (title, subtitle) if part)
            if value and value not in suggestions:
                suggestions.append(value)
        return {"success": True, "suggestions": suggestions[:5]}
    except Exception:
        # Подсказки необязательны: ввод адреса вручную продолжает работать.
        return {"success": False, "suggestions": []}


@app.post("/api/geocode")
def geocode_api(data: AddressRequest):
    """Проверяет, что адрес действительно распознан геокодером Яндекса."""
    address = data.address.strip()
    api_key = os.getenv("YANDEX_GEOCODER_API_KEY", "").strip()
    if len(address) < 3:
        raise HTTPException(status_code=422, detail="Введите адрес полностью.")
    if not api_key:
        raise HTTPException(status_code=503, detail="Не настроен YANDEX_GEOCODER_API_KEY.")
    params = urllib.parse.urlencode({"apikey": api_key, "geocode": address, "format": "json", "results": 1, "lang": "ru_RU"})
    request = urllib.request.Request("https://geocode-maps.yandex.ru/1.x/?" + params, headers={"User-Agent": "AfishaSPBPlanner/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
        members = payload.get("response", {}).get("GeoObjectCollection", {}).get("featureMember", [])
        if not members:
            return {"success": False, "message": "Такой адрес не найден. Выберите адрес из подсказок Яндекса."}
        obj = members[0].get("GeoObject", {})
        meta = obj.get("metaDataProperty", {}).get("GeocoderMetaData", {})
        precision = (meta.get("precision") or "").lower()
        # Не принимаем запросы, распознанные только как страна/город, если пользователь явно ввёл мусор.
        pos = obj.get("Point", {}).get("pos", "").split()
        if len(pos) != 2:
            return {"success": False, "message": "Не удалось определить координаты. Уточните адрес."}
        lon, lat = map(float, pos)
        return {"success": True, "address": meta.get("text") or address, "latitude": lat, "longitude": lon, "precision": precision}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="Сервис проверки адреса временно недоступен. Попробуйте ещё раз.")

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

class ReviewCreateRequest(BaseModel):
    user_id: int
    event_id: int
    rating: int = Field(..., ge=1, le=5)
    text: str = Field("", max_length=3000)

class InteractionRequest(BaseModel):
    user_id: int
    event_id: int
    interaction_type: str  # view / favorite / attended / rated
    rating: Optional[float] = None

class EventCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    short_title: Optional[str] = None
    description: Optional[str] = None
    category: str = "other"
    tags: Optional[str] = None
    start_datetime: Optional[str] = None
    end_datetime: Optional[str] = None
    duration_minutes: Optional[int] = Field(None, ge=1, le=1440)
    venue_title: Optional[str] = None
    venue_address: Optional[str] = None
    venue_lat: Optional[float] = Field(None, ge=-90, le=90)
    venue_lon: Optional[float] = Field(None, ge=-180, le=180)
    price_min: Optional[int] = Field(None, ge=0)
    price_raw: Optional[str] = None
    is_free: bool = False
    age_restriction: str = "0+"
    benefits: Optional[str] = None
    image_url: Optional[str] = None
    site_url: Optional[str] = None
    is_private: bool = False
    max_participants: Optional[int] = Field(None, ge=1)

class SmartPlanRequest(BaseModel):
    user_id: Optional[int] = None
    date_from: str
    date_to: str
    interests: List[str]
    budget_total: Optional[int] = None
    budget_per_day: Optional[int] = None
    group_size: int = 1
    time_slots: Optional[dict] = None
    district: Optional[str] = None
    max_events_per_day: int = Field(3, ge=1, le=5)
    age_group: Optional[str] = None
    mandatory_points: List[dict] = Field(default_factory=list)
    travel_mode: str = Field("transit", pattern="^(walk|transit|car|flexible)$")
    travel_buffer_minutes: int = Field(10, ge=0, le=60)


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

    # Ищем CSV относительно папки api.py, а не текущей рабочей директории.
    # На хостинге cwd может отличаться от корня проекта; из-за этого каталог
    # мероприятий раньше мог не загружаться, и план строился по старой маленькой БД.
    csv_files = sorted(APP_DIR.glob("events_*.csv"))

    if csv_files:
        print("Загружаю CSV файлы: " + str([f.name for f in csv_files]))
        for csv_path in csv_files:
            print("Загружаю: " + str(csv_path))
            load_events_from_csv(str(csv_path))
    else:
        print(f"CSV файлы не найдены в {APP_DIR}")

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


@app.delete("/api/interactions/favorite/{user_id}/{event_id}")
def remove_favorite(user_id: int, event_id: int, db=Depends(get_db)):
    """Удалить мероприятие из избранного"""
    db.query(UserInteraction).filter(
        UserInteraction.user_id == user_id,
        UserInteraction.event_id == event_id,
        UserInteraction.interaction_type == "favorite"
    ).delete()
    db.commit()
    return {"success": True}

@app.put("/api/users/{user_id}")
def update_user(user_id: int, data: dict, db=Depends(get_db)):
    """Обновить профиль пользователя"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if "name" in data and data["name"]:
        user.name = data["name"]
    if "email" in data and data["email"]:
        user.email = data["email"]
    db.commit()
    return {"success": True, "user": {"id": user.id, "name": user.name, "email": user.email}}

# ─── ЭНДПОИНТЫ: МЕРОПРИЯТИЯ ───────────────────────────────

@app.get("/api/events")
def get_events(
    category: Optional[str] = None,
    max_price: Optional[int] = None,
    benefits: Optional[str] = None,
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db=Depends(get_db)
):
    query = db.query(Event)

    if category:
        query = query.filter(Event.category.contains(category))
    if max_price is not None:
        query = query.filter(
            (Event.is_free == True) | (Event.price_min <= max_price)
        )
    if benefits:
        query = query.filter(Event.benefits.contains(benefits))
    total = query.count()
    events = query.offset(offset).limit(limit).all()
    event_ids = [e.id for e in events]
    review_rows = db.query(EventReview).filter(EventReview.event_id.in_(event_ids)).all() if event_ids else []
    ratings_by_event = {}
    for review in review_rows:
        ratings_by_event.setdefault(review.event_id, []).append(review.rating)

    return {
        "total": total,
        "events": [{
            "id": e.id,
            "title": e.title,
            "category": e.category or "Другое",
            "benefits": e.benefits or "",
            "start_datetime": e.start_datetime or "",
            "end_datetime": e.end_datetime or "",
            "duration_minutes": e.duration_minutes,
            "venue_title": e.venue_title or "Не указано",
            "venue_address": e.venue_address or "",
            "venue_lat": e.venue_lat,
            "venue_lon": e.venue_lon,
            "price_min": e.price_min,
            "is_free": bool(e.is_free),
            "age_restriction": e.age_restriction or "0+",
            "image_url": e.image_url or "",
            "is_private": bool(e.is_private),
            "source": e.source or "kudago",
            "tags": e.tags or "",
            "average_rating": round(sum(ratings_by_event.get(e.id, [])) / len(ratings_by_event[e.id]), 1) if ratings_by_event.get(e.id) else None,
            "review_count": len(ratings_by_event.get(e.id, [])),
        } for e in events]
    }


@app.get("/api/events/{event_id}")
def get_event(event_id: int, db=Depends(get_db)):
    """Получить полную информацию о мероприятии"""
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Мероприятие не найдено")

    review_rows = db.query(EventReview).filter(EventReview.event_id == event_id).all()
    average_rating = round(sum(r.rating for r in review_rows) / len(review_rows), 1) if review_rows else None
    return {
        "id": event.id,
        "title": event.title,
        "description": event.description,
        "average_rating": average_rating,
        "review_count": len(review_rows),
        "category": event.category,
        "tags": event.tags,
        "start_datetime": event.start_datetime,
        "end_datetime": event.end_datetime,
        "duration_minutes": event.duration_minutes,
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


@app.post("/api/events")
def create_event(data: EventCreateRequest, db=Depends(get_db)):
    """Добавляет мероприятие вручную. Бесплатные события всегда имеют цену 0."""
    title = data.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Название мероприятия не может быть пустым")

    def normalize_datetime(value):
        if not value or not value.strip():
            return None
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            # В БД и текущем планировщике используется локальная строка без timezone.
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Неверный формат даты: {value}. Используй YYYY-MM-DD HH:MM")

    start_dt = normalize_datetime(data.start_datetime)
    end_dt = normalize_datetime(data.end_datetime)
    duration = data.duration_minutes
    if duration is None and start_dt and end_dt:
        start_obj = datetime.strptime(start_dt, "%Y-%m-%d %H:%M:%S")
        end_obj = datetime.strptime(end_dt, "%Y-%m-%d %H:%M:%S")
        minutes = int((end_obj - start_obj).total_seconds() // 60)
        duration = minutes if minutes > 0 else None
    is_free = bool(data.is_free)
    price = 0 if is_free else data.price_min
    event = Event(
        title=title,
        short_title=(data.short_title or "").strip() or None,
        description=data.description,
        category=(data.category or "other").strip() or "other",
        tags=data.tags,
        start_datetime=start_dt,
        end_datetime=end_dt,
        duration_minutes=duration,
        venue_title=data.venue_title,
        venue_address=data.venue_address,
        venue_lat=data.venue_lat,
        venue_lon=data.venue_lon,
        price_min=price,
        price_raw=("Бесплатно" if is_free and not data.price_raw else data.price_raw),
        is_free=is_free,
        age_restriction=data.age_restriction or "0+",
        benefits=data.benefits or "",
        image_url=data.image_url,
        site_url=data.site_url,
        source="manual",
        is_private=data.is_private,
        max_participants=data.max_participants,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return {
        "message": "Мероприятие добавлено",
        "event": {
            "id": event.id,
            "title": event.title,
            "category": event.category,
            "start_datetime": event.start_datetime or "",
            "end_datetime": event.end_datetime or "",
            "duration_minutes": event.duration_minutes,
            "venue_title": event.venue_title or "",
            "venue_address": event.venue_address or "",
            "venue_lat": event.venue_lat,
            "venue_lon": event.venue_lon,
            "price_min": event.price_min,
            "is_free": event.is_free,
            "age_restriction": event.age_restriction,
            "source": event.source,
        },
    }


# ─── ОТЗЫВЫ И ОЦЕНКИ ─────────────────────────────────────
@app.get("/api/events/{event_id}/reviews")
def get_event_reviews(event_id: int, db=Depends(get_db)):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Мероприятие не найдено")
    reviews = db.query(EventReview).filter(EventReview.event_id == event_id).order_by(EventReview.updated_at.desc()).all()
    return {
        "average_rating": round(sum(r.rating for r in reviews) / len(reviews), 1) if reviews else None,
        "review_count": len(reviews),
        "reviews": [{
            "id": r.id,
            "user_id": r.user_id,
            "user_name": r.user.name if r.user else "Пользователь",
            "rating": r.rating,
            "text": r.text,
            "created_at": r.updated_at.isoformat() if r.updated_at else None,
        } for r in reviews],
    }


@app.get("/api/users/{user_id}/reviews")
def get_user_reviews(user_id: int, db=Depends(get_db)):
    """Отзывы конкретного пользователя для личного кабинета."""
    rows = db.query(EventReview).filter(EventReview.user_id == user_id).order_by(EventReview.updated_at.desc()).all()
    event_ids = [r.event_id for r in rows]
    events = db.query(Event).filter(Event.id.in_(event_ids)).all() if event_ids else []
    event_map = {e.id: e for e in events}
    return {"reviews": [{
        "id": r.id,
        "user_id": r.user_id,
        "event_id": r.event_id,
        "event_title": event_map[r.event_id].title if r.event_id in event_map else f"Мероприятие #{r.event_id}",
        "rating": r.rating,
        "text": r.text,
        "created_at": r.updated_at.isoformat() if r.updated_at else None,
    } for r in rows]}


@app.post("/api/reviews")
def save_event_review(data: ReviewCreateRequest, db=Depends(get_db)):
    user = db.query(User).filter(User.id == data.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден. Войдите в аккаунт заново.")
    event = db.query(Event).filter(Event.id == data.event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Мероприятие не найдено")

    review = db.query(EventReview).filter(
        EventReview.user_id == data.user_id,
        EventReview.event_id == data.event_id,
    ).first()
    clean_text = data.text.strip()
    if review:
        review.rating = data.rating
        review.text = clean_text
        review.updated_at = datetime.utcnow()
    else:
        review = EventReview(user_id=data.user_id, event_id=data.event_id, rating=data.rating, text=clean_text)
        db.add(review)

    # Сохраняем рейтинг и в журнале взаимодействий, который использует рекомендатель.
    rated = db.query(UserInteraction).filter(
        UserInteraction.user_id == data.user_id,
        UserInteraction.event_id == data.event_id,
        UserInteraction.interaction_type == "rated",
    ).order_by(UserInteraction.timestamp.desc()).first()
    if rated:
        rated.rating = float(data.rating)
        rated.timestamp = datetime.utcnow()
    else:
        db.add(UserInteraction(user_id=data.user_id, event_id=data.event_id, interaction_type="rated", rating=float(data.rating)))

    db.commit()
    return {"success": True, "message": "Отзыв сохранён", "rating": data.rating}


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

@app.get("/api/interactions/attended/{user_id}")
def get_attended(user_id: int, db=Depends(get_db)):
    """Посещённые мероприятия пользователя"""
    attended = db.query(UserInteraction).filter(
        UserInteraction.user_id == user_id,
        UserInteraction.interaction_type == "attended"
    ).order_by(UserInteraction.timestamp.desc()).all()

    event_ids = [a.event_id for a in attended]
    events = db.query(Event).filter(Event.id.in_(event_ids)).all()
    event_map = {e.id: e for e in events}

    return {"events": [{
        "id": e_id,
        "title": event_map[e_id].title if e_id in event_map else "",
        "category": event_map[e_id].category if e_id in event_map else "",
        "venue_title": event_map[e_id].venue_title if e_id in event_map else "",
        "start_datetime": event_map[e_id].start_datetime if e_id in event_map else "",
        "price_min": event_map[e_id].price_min if e_id in event_map else 0,
        "is_free": event_map[e_id].is_free if e_id in event_map else False,
        "image_url": event_map[e_id].image_url if e_id in event_map else "",
        "age_restriction": event_map[e_id].age_restriction if e_id in event_map else "0+",
    } for e_id in event_ids if e_id in event_map]}


@app.get("/api/interactions/views/{user_id}")
def get_views(user_id: int, db=Depends(get_db)):
    """Недавно просмотренные мероприятия"""
    views = db.query(UserInteraction).filter(
        UserInteraction.user_id == user_id,
        UserInteraction.interaction_type == "view"
    ).order_by(UserInteraction.timestamp.desc()).limit(20).all()

    # Для каждого мероприятия оставляем только последнее время просмотра.
    latest = {}
    for interaction in views:  # уже отсортировано от новых к старым
        latest.setdefault(interaction.event_id, str(interaction.timestamp))
    event_ids = list(latest.keys())
    events = db.query(Event).filter(Event.id.in_(event_ids)).all() if event_ids else []
    event_map = {e.id: e for e in events}

    return {"events": [{
        "id": e_id,
        "title": event_map[e_id].title,
        "category": event_map[e_id].category,
        "venue_title": event_map[e_id].venue_title,
        "image_url": event_map[e_id].image_url,
        "viewed_at": latest.get(e_id, ""),
    } for e_id in event_ids if e_id in event_map]}

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
    по датам, интересам, бюджету и временным слотам.
    """
    from smart_planner import SmartPlanner
    planner = SmartPlanner(db, recommender)

    plan = planner.create_plan(
        date_from=data.date_from,
        date_to=data.date_to,
        interests=data.interests,
        budget_total=data.budget_total,
        budget_per_day=data.budget_per_day,
        group_size=data.group_size or 1,
        time_slots=data.time_slots,
        district=data.district,
        max_events_per_day=data.max_events_per_day or 3,
        user_id=data.user_id,
        age_group=data.age_group,
        mandatory_points=data.mandatory_points,
        travel_mode=data.travel_mode,
        travel_buffer_minutes=data.travel_buffer_minutes,
    )

    # Полный список для ручного выбора: он не зависит от фильтров интересов/бюджета.
    try:
        date_from = datetime.strptime(data.date_from, "%Y-%m-%d").date()
        date_to = datetime.strptime(data.date_to, "%Y-%m-%d").date()
        all_events = db.query(Event).order_by(Event.start_datetime.asc(), Event.title.asc()).limit(2000).all()
        options = []
        for e in all_events:
            event_date = (e.start_datetime or "")[:10]
            if event_date and not (data.date_from <= event_date <= data.date_to):
                continue
            options.append({
                "id": e.id, "title": e.title, "category": e.category or "Другое",
                "venue_title": e.venue_title or "", "venue_address": e.venue_address or "",
                "venue_lat": e.venue_lat, "venue_lon": e.venue_lon,
                "start_datetime": e.start_datetime or "", "duration_minutes": e.duration_minutes or 120,
                "price_per_person": e.price_min or 0, "is_free": bool(e.is_free),
                "age_restriction": e.age_restriction or "0+", "image_url": e.image_url or "",
            })
        plan["candidate_options"] = options
    except Exception:
        plan["candidate_options"] = []
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
