"""Модели БД для афиши и системы умного подбора мероприятий."""
import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, DateTime,
    Text, ForeignKey, UniqueConstraint, Index, inspect, text,
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///afisha_spb.db")
# Совместимость с URL, который иногда выдаёт Railway.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine_options = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite:"):
    engine_options["connect_args"] = {"check_same_thread": False}
engine = create_engine(DATABASE_URL, echo=False, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


class Event(Base):
    """Мероприятие из парсера или добавленное вручную."""
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    external_id = Column(Integer, nullable=True, index=True)
    external_id_str = Column(String(100), nullable=True, index=True)
    title = Column(String(500), nullable=False)
    short_title = Column(String(300))
    description = Column(Text)
    category = Column(String(200), index=True)
    tags = Column(Text)
    # Храним нормализованные строки YYYY-MM-DD HH:MM:SS ради совместимости
    # с текущими API и планировщиком. Парсер дат в приложении преобразует их.
    start_datetime = Column(String(50), index=True)
    end_datetime = Column(String(50))
    duration_minutes = Column(Integer, nullable=True)
    venue_title = Column(String(300))
    venue_address = Column(String(500))
    venue_lat = Column(Float)
    venue_lon = Column(Float)
    price_min = Column(Integer, nullable=True, default=None)
    price_raw = Column(String(200))
    is_free = Column(Boolean, nullable=False, default=False, index=True)
    age_restriction = Column(String(10), nullable=False, default="0+")
    benefits = Column(String(300), nullable=False, default="")
    image_url = Column(Text)
    site_url = Column(Text)
    source = Column(String(50), nullable=False, default="manual", index=True)
    is_private = Column(Boolean, nullable=False, default=False)
    max_participants = Column(Integer, nullable=True)
    parsed_at = Column(String(50))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    interactions = relationship("UserInteraction", back_populates="event", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Event {self.id}: {self.title[:50]}>"


class User(Base):
    """Пользователи системы."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    email = Column(String(200), unique=True, nullable=False, index=True)
    password_hash = Column(String(500))
    user_type = Column(String(20), nullable=False, default="resident")
    registered_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    interactions = relationship("UserInteraction", back_populates="user", cascade="all, delete-orphan")
    preferences = relationship("UserPreference", back_populates="user", cascade="all, delete-orphan")
    smart_plans = relationship("SmartPlan", back_populates="user")

    def __repr__(self):
        return f"<User {self.id}: {self.name}>"


class UserInteraction(Base):
    """Просмотры, избранное, посещения и оценки для рекомендателя."""
    __tablename__ = "user_interactions"
    __table_args__ = (
        Index("ix_interaction_user_event_type", "user_id", "event_id", "interaction_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    interaction_type = Column(String(20), nullable=False)  # view/favorite/attended/rated
    rating = Column(Float, nullable=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    user = relationship("User", back_populates="interactions")
    event = relationship("Event", back_populates="interactions")

    def __repr__(self):
        return f"<Interaction user={self.user_id} event={self.event_id} type={self.interaction_type}>"


class EventReview(Base):
    """Отзыв и оценка пользователя конкретному мероприятию."""
    __tablename__ = "event_reviews"
    __table_args__ = (UniqueConstraint("user_id", "event_id", name="uq_event_review_user_event"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    rating = Column(Integer, nullable=False)
    text = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    event = relationship("Event")


class UserPreference(Base):
    """Веса интереса пользователя к категориям."""
    __tablename__ = "user_preferences"
    __table_args__ = (UniqueConstraint("user_id", "category", name="uq_user_preference_category"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    category = Column(String(200), nullable=False)
    weight = Column(Float, nullable=False, default=0.0)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="preferences")

    def __repr__(self):
        return f"<Preference user={self.user_id} cat={self.category} w={self.weight:.2f}>"


class SmartPlan(Base):
    """Сохранённый запрос и итоговое расписание умного подбора."""
    __tablename__ = "smart_plans"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    date_from = Column(String(20))
    date_to = Column(String(20))
    budget = Column(Integer)
    budget_per_day = Column(Integer, nullable=True)
    group_size = Column(Integer, nullable=False, default=1)
    district = Column(String(200), nullable=True)
    age_group = Column(String(20), nullable=True)
    max_events_per_day = Column(Integer, nullable=True)
    interests = Column(Text)   # JSON список категорий
    time_slots = Column(Text)  # JSON временные предпочтения / обязательные точки
    schedule = Column(Text)    # JSON итоговое расписание
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    user = relationship("User", back_populates="smart_plans")

    def __repr__(self):
        return f"<SmartPlan {self.id}: {self.date_from}–{self.date_to}>"


def _add_missing_columns(table_name, columns):
    """Добавляет недостающие совместимые колонки в существующую БД без удаления данных."""
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns(table_name)}
    with engine.begin() as conn:
        for column_name, sql_type in columns.items():
            if column_name not in existing:
                conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {sql_type}'))
                print(f"Миграция: добавлена колонка {table_name}.{column_name}")


def init_db():
    """Создаёт таблицы и добавляет новые поля в уже существующую БД."""
    Base.metadata.create_all(bind=engine)
    # create_all не изменяет существующие таблицы, поэтому новые поля добавляем отдельно.
    # Типы ниже совместимы с SQLite и PostgreSQL.
    _add_missing_columns("events", {
        "benefits": "VARCHAR(300) DEFAULT ''",
        "external_id_str": "VARCHAR(100)",
        "duration_minutes": "INTEGER",
        "max_participants": "INTEGER",
        "parsed_at": "VARCHAR(50)",
    })
    _add_missing_columns("smart_plans", {
        "budget_per_day": "INTEGER",
        "group_size": "INTEGER DEFAULT 1",
        "district": "VARCHAR(200)",
        "age_group": "VARCHAR(20)",
        "max_events_per_day": "INTEGER",
    })
    print("База данных готова")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
