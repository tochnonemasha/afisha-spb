import os
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Float,
    Boolean,
    DateTime,
    Text,
    ForeignKey
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime

# Берём URL из переменной окружения Railway, иначе SQLite для локальной разработки
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite:///afisha_spb.db"
)

# Railway даёт URL вида postgres://, SQLAlchemy требует postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


# ─── ТАБЛИЦЫ ──────────────────────────────────────────────

class Event(Base):
    """Мероприятия"""
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    external_id = Column(Integer, unique=True, nullable=True)
    title = Column(String(500), nullable=False)
    short_title = Column(String(300))
    description = Column(Text)
    category = Column(String(200))
    tags = Column(Text)
    start_datetime = Column(String(50))
    end_datetime = Column(String(50))
    venue_title = Column(String(300))
    venue_address = Column(String(500))
    venue_lat = Column(Float)
    venue_lon = Column(Float)
    price_min = Column(Integer, default=0)
    price_raw = Column(String(200))
    is_free = Column(Boolean, default=False)
    age_restriction = Column(String(10), default="0+")
    benefits = Column(String(300), default="")
    image_url = Column(Text)
    site_url = Column(Text)
    source = Column(String(50), default="kudago")
    is_private = Column(Boolean, default=False)
    max_participants = Column(Integer, nullable=True)
    parsed_at = Column(String(50))
    created_at = Column(DateTime, default=datetime.now)

    # Связь с взаимодействиями
    interactions = relationship("UserInteraction", back_populates="event")

    def __repr__(self):
        return f"<Event {self.id}: {self.title[:50]}>"


class User(Base):
    """Пользователи"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    email = Column(String(200), unique=True, nullable=False)
    password_hash = Column(String(500))
    user_type = Column(String(20), default="resident")
    # resident = житель города
    # guest    = гость города (турист)
    registered_at = Column(DateTime, default=datetime.now)

    # Связи
    interactions = relationship("UserInteraction", back_populates="user")
    preferences = relationship("UserPreference", back_populates="user")
    smart_plans = relationship("SmartPlan", back_populates="user")

    def __repr__(self):
        return f"<User {self.id}: {self.name}>"


class UserInteraction(Base):
    """
    Взаимодействия пользователей с мероприятиями.
    Это главная таблица для рекомендательного алгоритма.
    """
    __tablename__ = "user_interactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)

    # Типы взаимодействий и их веса для алгоритма:
    # view      = просмотр карточки   → вес 1
    # favorite  = добавил в избранное → вес 3
    # attended  = посетил             → вес 5
    # rated     = поставил оценку     → значение оценки (1-5)
    interaction_type = Column(String(20), nullable=False)
    rating = Column(Float, nullable=True)  # только для rated
    timestamp = Column(DateTime, default=datetime.now)

    # Связи
    user = relationship("User", back_populates="interactions")
    event = relationship("Event", back_populates="interactions")

    def __repr__(self):
        return f"<Interaction user={self.user_id} event={self.event_id} type={self.interaction_type}>"


class UserPreference(Base):
    """
    Предпочтения пользователей по категориям.
    Обновляется автоматически на основе взаимодействий.
    """
    __tablename__ = "user_preferences"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    category = Column(String(200), nullable=False)
    weight = Column(Float, default=0.0)  # от 0 до 1
    updated_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="preferences")

    def __repr__(self):
        return f"<Preference user={self.user_id} cat={self.category} w={self.weight:.2f}>"


class SmartPlan(Base):
    """
    Планы умного подбора для гостей города.
    """
    __tablename__ = "smart_plans"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    date_from = Column(String(20))
    date_to = Column(String(20))
    budget = Column(Integer)
    interests = Column(Text)   # JSON список категорий
    time_slots = Column(Text)  # JSON временные предпочтения
    schedule = Column(Text)    # JSON итоговое расписание
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="smart_plans")

    def __repr__(self):
        return f"<SmartPlan {self.id}: {self.date_from}–{self.date_to}>"


# ─── СОЗДАНИЕ ТАБЛИЦ ──────────────────────────────────────
def init_db():
    """Создаёт все таблицы в базе данных"""
    Base.metadata.create_all(bind=engine)
     try:
        from sqlalchemy import text
        with engine.connect() as conn:
            # Для PostgreSQL
            conn.execute(text('ALTER TABLE events ADD COLUMN IF NOT EXISTS benefits VARCHAR(300) DEFAULT ""'))
            conn.commit()
            print("✅ Колонка benefits добавлена (или уже существует)")
    except Exception as e:
        print(f"⚠️ Не удалось добавить benefits: {e}")
    print("✅ База данных создана успешно")


def get_db():
    """Возвращает сессию базы данных"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
