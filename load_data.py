"""Безопасная загрузка мероприятий из CSV и необязательное создание тестовых данных."""
import glob
import os
import random
from datetime import datetime

import pandas as pd
from sqlalchemy import or_

from database import SessionLocal, init_db, Event


def find_latest_csv():
    csv_files = glob.glob("events_*.csv")
    return max(csv_files, key=os.path.getmtime) if csv_files else None


def is_missing(value):
    try:
        return pd.isna(value)
    except (TypeError, ValueError):
        return False


def safe_str(value, default=""):
    if is_missing(value):
        return default
    result = str(value).strip()
    if result.casefold() in {"nan", "none", "null", "nat"}:
        return default
    return result


def safe_bool(value, default=False):
    """Корректно разбирает CSV-значения true/false, 1/0, да/нет."""
    if is_missing(value):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    value = str(value).strip().casefold()
    if value in {"true", "1", "yes", "да", "y", "д"}:
        return True
    if value in {"false", "0", "no", "нет", "n", ""}:
        return False
    return default


def safe_int(value, default=None, minimum=None):
    if is_missing(value) or safe_str(value) == "":
        return default
    try:
        result = int(float(str(value).replace(" ", "").replace(",", ".")))
        if minimum is not None and result < minimum:
            return default
        return result
    except (TypeError, ValueError, OverflowError):
        return default


def safe_float(value, default=None):
    if is_missing(value) or safe_str(value) == "":
        return default
    try:
        result = float(str(value).strip().replace(" ", "").replace(",", "."))
        return result if pd.notna(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def safe_datetime_string(value):
    """Возвращает нормализованную строку YYYY-MM-DD HH:MM:SS или None.

    Храним даты строками, чтобы не ломать существующий API и smart_planner.py.
    Поддерживаются ISO-строки, обычные даты и форматы, распознаваемые pandas.
    """
    if is_missing(value):
        return None
    raw = str(value).strip()
    if not raw or raw.casefold() in {"nan", "none", "null", "nat"}:
        return None
    try:
        parsed = pd.to_datetime(raw, errors="raise")
        # Для текущей системы используем локальное время без timezone-суффикса.
        if getattr(parsed, "tzinfo", None) is not None:
            parsed = parsed.tz_localize(None) if getattr(parsed, "tz", None) is not None else parsed.replace(tzinfo=None)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError):
        print(f"  ⚠️ Не удалось разобрать дату {raw!r}; дата сохранена пустой")
        return None


def _external_ids(value):
    raw = safe_str(value)
    if not raw:
        return None, None
    try:
        # Принимаем 123 и 123.0, но не превращаем строковые ID с префиксами в числа.
        number = float(raw)
        if number.is_integer() and number >= 0:
            return int(number), None
    except (ValueError, OverflowError):
        pass
    return None, raw[:100]


def _find_duplicate(db, source, external_id, external_id_str, title, start_datetime, venue_title):
    """Ищет дубль в пределах источника; для ручных записей ID источника необязателен."""
    query = db.query(Event)
    if source:
        query = query.filter(Event.source == source)
    if external_id is not None:
        found = query.filter(Event.external_id == external_id).first()
        if found:
            return found
    if external_id_str:
        found = query.filter(Event.external_id_str == external_id_str).first()
        if found:
            return found
    # Если внешнего ID нет, применяем осторожный составной ключ.
    if not external_id and not external_id_str and title and start_datetime:
        return query.filter(
            Event.title == title,
            Event.start_datetime == start_datetime,
            Event.venue_title == (venue_title or ""),
        ).first()
    return None


def load_events_from_csv(csv_path: str, source_override=None):
    """Загружает CSV в БД. Бесплатные события сохраняются и получают цену 0."""
    print(f"📂 Читаю файл: {csv_path}")
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    print(f"   Найдено строк: {len(df)}")
    if df.empty:
        print("⚠️ CSV пустой")
        return 0

    db = SessionLocal()
    added = skipped = errors = 0
    try:
        for row_number, (_, row) in enumerate(df.iterrows(), start=2):
            try:
                source = safe_str(source_override or row.get("source"), "kudago") or "kudago"
                raw_eid = row.get("external_id")
                external_id, external_id_str = _external_ids(raw_eid)
                raw_title = safe_str(row.get("title"), "Без названия")
                title = (raw_title[0].upper() + raw_title[1:])[:500] if raw_title else "Без названия"
                start_dt = safe_datetime_string(row.get("start_datetime"))
                end_dt = safe_datetime_string(row.get("end_datetime"))
                venue_title = safe_str(row.get("venue_title"))[:300]

                if _find_duplicate(db, source, external_id, external_id_str, title, start_dt, venue_title):
                    skipped += 1
                    continue

                is_free = safe_bool(row.get("is_free"), default=False)
                price = safe_int(row.get("price_min"), default=None, minimum=0)
                if is_free:
                    # Бесплатность задаётся отдельным явным полем и всегда имеет цену 0.
                    price = 0

                duration = safe_int(row.get("duration_minutes"), default=None, minimum=1)
                if duration is None and start_dt and end_dt:
                    try:
                        start_obj = datetime.strptime(start_dt, "%Y-%m-%d %H:%M:%S")
                        end_obj = datetime.strptime(end_dt, "%Y-%m-%d %H:%M:%S")
                        minutes = int((end_obj - start_obj).total_seconds() // 60)
                        duration = minutes if minutes > 0 else None
                    except ValueError:
                        duration = None

                event = Event(
                    external_id=external_id,
                    external_id_str=external_id_str,
                    title=title,
                    short_title=safe_str(row.get("short_title"))[:300] or None,
                    description=safe_str(row.get("description"))[:10000] or None,
                    category=safe_str(row.get("category"), "other") or "other",
                    tags=safe_str(row.get("tags")) or None,
                    start_datetime=start_dt,
                    end_datetime=end_dt,
                    duration_minutes=duration,
                    venue_title=venue_title or None,
                    venue_address=safe_str(row.get("venue_address"))[:500] or None,
                    venue_lat=safe_float(row.get("venue_lat")),
                    venue_lon=safe_float(row.get("venue_lon")),
                    price_min=price,
                    price_raw=safe_str(row.get("price_raw"))[:200] or ("Бесплатно" if is_free else None),
                    is_free=is_free,
                    age_restriction=safe_str(row.get("age_restriction"), "0+") or "0+",
                    benefits=safe_str(row.get("benefits"))[:300],
                    image_url=safe_str(row.get("image_url")) or None,
                    site_url=safe_str(row.get("site_url")) or None,
                    source=source[:50],
                    is_private=safe_bool(row.get("is_private"), default=False),
                    max_participants=safe_int(row.get("max_participants"), default=None, minimum=1),
                    parsed_at=safe_str(row.get("parsed_at")) or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                )
                db.add(event)
                added += 1
                # Периодически фиксируем изменения, чтобы большие файлы не держали одну транзакцию.
                if added % 250 == 0:
                    db.flush()
            except Exception as exc:
                errors += 1
                print(f"  ⚠️ Строка CSV {row_number}, событие {safe_str(row.get('title'), '(без названия)')!r}: {exc}")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print(f"✅ Добавлено новых: {added}")
    print(f"⏭ Пропущено дублей: {skipped}")
    print(f"⚠️ Ошибок: {errors}")
    return added


def seed_test_users():
    """Создаёт демонстрационных пользователей/взаимодействия, если пользователей ещё нет."""
    from database import User, UserInteraction

    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            print("⏭ Тестовые пользователи уже существуют")
            return

        print("👤 Создаю тестовых пользователей...")
        test_users = [
            {"name": "Анна", "email": "anna@test.ru", "user_type": "resident"},
            {"name": "Кирилл", "email": "kirill@test.ru", "user_type": "resident"},
            {"name": "Мария", "email": "maria@test.ru", "user_type": "guest"},
            {"name": "Дмитрий", "email": "dmitry@test.ru", "user_type": "resident"},
            {"name": "Елена", "email": "elena@test.ru", "user_type": "guest"},
            {"name": "Павел", "email": "pavel@test.ru", "user_type": "resident"},
            {"name": "Наташа", "email": "natasha@test.ru", "user_type": "resident"},
            {"name": "Игорь", "email": "igor@test.ru", "user_type": "guest"},
        ]
        users = [User(**u, password_hash="test_hash") for u in test_users]
        db.add_all(users)
        db.flush()

        events = db.query(Event).all()
        if not events:
            print("⚠️ В базе нет мероприятий. Пользователи созданы, взаимодействия не добавлены.")
            db.commit()
            return

        profiles = [
            ["theater", "concert"], ["sport", "education"],
            ["exhibition", "excursion"], ["concert", "party", "stand-up"],
            ["education", "exhibition"], ["sport", "festival"],
            ["theater", "exhibition"], ["festival", "concert"],
        ]
        interactions_added = 0
        for i, user in enumerate(users):
            preferred_cats = profiles[i]
            preferred_events = [e for e in events if e.category and any(cat in e.category.casefold() for cat in preferred_cats)]
            other_events = [e for e in events if e not in preferred_events]
            chosen_preferred = random.sample(preferred_events, min(25, len(preferred_events)))
            chosen_other = random.sample(other_events, min(10, len(other_events)))
            for event in chosen_preferred + chosen_other:
                is_preferred = event in chosen_preferred
                itype = random.choices(
                    ["view", "favorite", "attended", "rated"],
                    weights=([25, 30, 25, 20] if is_preferred else [60, 20, 10, 10]),
                )[0]
                rating = random.uniform(3.5, 5.0) if is_preferred and itype == "rated" else (
                    random.uniform(2.0, 4.0) if itype == "rated" else None
                )
                db.add(UserInteraction(user_id=user.id, event_id=event.id,
                                       interaction_type=itype, rating=rating))
                interactions_added += 1
        db.commit()
        print(f"✅ Создано пользователей: {len(users)}")
        print(f"✅ Создано взаимодействий: {interactions_added}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    import sys
    init_db()
    if len(sys.argv) > 1:
        for csv_file in sys.argv[1:]:
            if os.path.exists(csv_file):
                load_events_from_csv(csv_file)
            else:
                print(f"❌ Файл не найден: {csv_file}")
    else:
        csv_files = sorted(glob.glob("events_*.csv"), key=os.path.getmtime)
        if csv_files:
            for csv_file in csv_files:
                load_events_from_csv(csv_file)
        else:
            print("⚠️ CSV не указан и не найден. Используй: python load_data.py events_....csv")
    seed_test_users()
