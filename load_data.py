import pandas as pd
from database import SessionLocal, init_db, Event
from datetime import datetime
import os
import glob

def find_latest_csv():
    """
    Находит самый свежий CSV файл в текущей директории
    """
    csv_files = glob.glob("events_*.csv")
    if not csv_files:
        return None
    
    # Сортируем по времени создания
    latest = max(csv_files, key=os.path.getctime)
    return latest

def load_events_from_csv(csv_path: str):
    """
    Загружает мероприятия из CSV файла (результат парсера)
    в базу данных SQLite.
    """
    print(f"📂 Читаю файл: {csv_path}")
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    print(f"   Найдено строк: {len(df)}")

    db = SessionLocal()
    added = 0
    skipped = 0

    for _, row in df.iterrows():
        # Проверяем — нет ли уже такого мероприятия
        raw_eid = row.get("external_id")
        external_id = None
        external_id_str = None

        if pd.notna(raw_eid) and str(raw_eid).strip() not in ("", "nan", "None"):
            raw_str = str(raw_eid).strip()
            try:
                external_id = int(float(raw_str))
            except (ValueError, OverflowError):
                # строковый ID например "tp_4058544"
                external_id_str = raw_str

        # Проверяем дубль по числовому или строковому ID
        if external_id is not None:
            exists = db.query(Event).filter(
                Event.external_id == external_id
            ).first()
            if exists:
                skipped += 1
                continue
        elif external_id_str is not None:
            exists = db.query(Event).filter(
                Event.external_id_str == external_id_str
            ).first()
            if exists:
                skipped += 1
                continue

        try:
            # Конвертируем даты в правильный формат для SQLite
            start_datetime = None
            end_datetime = None
            
            if pd.notna(row.get("start_datetime")):
                start_str = str(row["start_datetime"])
                try:
                    # Пробуем парсить дату
                    start_datetime = datetime.strptime(start_str, "%Y-%m-%d %H:%M")
                except:
                    start_datetime = start_str  # сохраняем как строку
            
            if pd.notna(row.get("end_datetime")):
                end_str = str(row["end_datetime"])
                try:
                    end_datetime = datetime.strptime(end_str, "%Y-%m-%d %H:%M")
                except:
                    end_datetime = end_str

            # Получаем title и делаем первую букву заглавной
            raw_title = str(row.get("title", "")).strip()
            title = (raw_title[0].upper() + raw_title[1:]) if raw_title else "Без названия"

            def safe_str(val, default=""):
                s = str(val) if pd.notna(val) else default
                return "" if s in ("nan", "None", "none") else s

            event = Event(
                external_id=external_id,
                external_id_str=external_id_str,
                title=title[:500],
                short_title=safe_str(row.get("short_title"))[:300],
                description=safe_str(row.get("description"))[:3000],
                category=safe_str(row.get("category"), "other"),
                tags=safe_str(row.get("tags")),
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                venue_title=safe_str(row.get("venue_title"))[:300],
                venue_address=safe_str(row.get("venue_address"))[:500],
                venue_lat=float(row["venue_lat"]) if pd.notna(row.get("venue_lat")) else None,
                venue_lon=float(row["venue_lon"]) if pd.notna(row.get("venue_lon")) else None,
                price_min=int(float(row["price_min"])) if pd.notna(row.get("price_min")) else 0,
                price_raw=safe_str(row.get("price_raw"))[:200],
                is_free=bool(row.get("is_free", False)),
                age_restriction=safe_str(row.get("age_restriction"), "0+"),
                benefits=safe_str(row.get("benefits")),
                image_url=safe_str(row.get("image_url")),
                site_url=safe_str(row.get("site_url")),
                source=safe_str(row.get("source"), "kudago"),
            )
            db.add(event)
            added += 1
        except Exception as e:
            print(f"  ⚠️ Ошибка при загрузке события {row.get('title', '')}: {e}")
            continue

    db.commit()
    db.close()

    print(f"✅ Добавлено новых: {added}")
    print(f"⏭  Пропущено дублей: {skipped}")
    return added


def seed_test_users():
    """
    Создаёт тестовых пользователей и взаимодействия
    для демонстрации алгоритма.
    """
    from database import User, UserInteraction
    import random

    db = SessionLocal()

    # Проверяем — уже есть пользователи?
    if db.query(User).count() > 0:
        print("⏭  Тестовые пользователи уже существуют")
        db.close()
        return

    print("👤 Создаю тестовых пользователей...")

    # Тестовые пользователи с разными профилями
    test_users = [
        {"name": "Анна",    "email": "anna@test.ru",    "user_type": "resident"},
        {"name": "Кирилл",  "email": "kirill@test.ru",  "user_type": "resident"},
        {"name": "Мария",   "email": "maria@test.ru",   "user_type": "guest"},
        {"name": "Дмитрий", "email": "dmitry@test.ru",  "user_type": "resident"},
        {"name": "Елена",   "email": "elena@test.ru",   "user_type": "guest"},
        {"name": "Павел",   "email": "pavel@test.ru",   "user_type": "resident"},
        {"name": "Наташа",  "email": "natasha@test.ru", "user_type": "resident"},
        {"name": "Игорь",   "email": "igor@test.ru",    "user_type": "guest"},
    ]

    users = []
    for u in test_users:
        user = User(**u, password_hash="test_hash")
        db.add(user)
        users.append(user)

    db.flush()  # получаем id пользователей

    # Получаем все мероприятия из базы
    events = db.query(Event).all()
    if not events:
        print("⚠️  Нет мероприятий в базе — сначала запусти парсер")
        db.close()
        return

    print(f"   Мероприятий в базе: {len(events)}")

    # Симулируем взаимодействия
    interactions_added = 0

    # Профили предпочтений для каждого пользователя
    user_profiles = [
        ["theater", "concert"],           # Анна — театр и концерты
        ["sport", "education"],           # Кирилл — спорт и лекции
        ["exhibition", "excursion"],      # Мария — выставки и экскурсии
        ["concert", "party", "stand-up"], # Дмитрий — тусовки
        ["education", "exhibition"],      # Елена — культура
        ["sport", "festival"],            # Павел — активный отдых
        ["theater", "exhibition"],        # Наташа — классика
        ["festival", "concert"],          # Игорь — фестивали
    ]

    for i, user in enumerate(users):
        preferred_cats = user_profiles[i] if i < len(user_profiles) else []

        # Фильтруем мероприятия по предпочтениям пользователя
        preferred_events = []
        other_events = []
        
        for e in events:
            if e.category and any(cat in e.category for cat in preferred_cats):
                preferred_events.append(e)
            else:
                other_events.append(e)

        # 70% взаимодействий — с предпочтительными категориями
        n_preferred = min(25, len(preferred_events))
        n_other = min(10, len(other_events))

        chosen_preferred = random.sample(preferred_events, n_preferred) if len(preferred_events) >= n_preferred else preferred_events
        chosen_other = random.sample(other_events, n_other) if len(other_events) >= n_other else other_events
        chosen_events = list(chosen_preferred) + list(chosen_other)

        for event in chosen_events:
            # Для предпочтительных — чаще ставим высокие оценки
            if event in chosen_preferred:
                itype = random.choices(
                    ["view", "favorite", "attended"],
                    weights=[0.3, 0.3, 0.4]
                )[0]
                rating = random.uniform(3.5, 5.0) if itype == "attended" else None
            else:
                itype = random.choices(
                    ["view", "favorite", "attended"],
                    weights=[0.6, 0.3, 0.1]
                )[0]
                rating = random.uniform(2.0, 4.0) if itype == "attended" else None

            interaction = UserInteraction(
                user_id=user.id,
                event_id=event.id,
                interaction_type=itype,
                rating=rating,
            )
            db.add(interaction)
            interactions_added += 1

    db.commit()
    db.close()

    print(f"✅ Создано пользователей: {len(users)}")
    print(f"✅ Создано взаимодействий: {interactions_added}")


if __name__ == "__main__":
    import sys

    # Создаём таблицы
    init_db()

    # Если передан путь к CSV — загружаем
    if len(sys.argv) > 1:
        csv_file = sys.argv[1]
        if os.path.exists(csv_file):
            load_events_from_csv(csv_file)
        else:
            print(f"❌ Файл не найден: {csv_file}")
    else:
        # Пытаемся найти последний CSV файл
        latest_csv = find_latest_csv()
        if latest_csv:
            print(f"📂 Найден последний CSV: {latest_csv}")
            load_events_from_csv(latest_csv)
        else:
            print("⚠️  CSV не указан и не найден в папке")
            print("   Используй: python load_data.py events_....csv")

    # Создаём тестовых пользователей
    seed_test_users()
