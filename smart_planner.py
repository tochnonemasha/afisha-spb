import math
from datetime import datetime, timedelta
from database import SessionLocal, Event

# Временные слоты
TIME_SLOTS = {
    "morning":   {"label": "Утро",    "start": "09:00", "end": "13:00"},
    "afternoon": {"label": "День",    "start": "13:00", "end": "18:00"},
    "evening":   {"label": "Вечер",   "start": "18:00", "end": "23:59"},
    "any":       {"label": "Любое",   "start": "00:00", "end": "23:59"},
}

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

def haversine(lat1, lon1, lat2, lon2):
    """Расстояние между двумя точками в км"""
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def parse_event_date(date_str):
    """Парсит дату из разных форматов в datetime"""
    if not date_str:
        return None
    try:
        # Формат: "2025-07-15 19:00"
        return datetime.strptime(str(date_str)[:16], "%Y-%m-%d %H:%M")
    except:
        pass
    try:
        # Формат: "15 июля 2025"
        import re
        m = re.match(r'(\d{1,2})\s+(\w+)\s*(\d{4})?', str(date_str))
        if m:
            day = int(m.group(1))
            month_name = m.group(2).lower()
            month = MONTHS_RU.get(month_name, 0)
            year = int(m.group(3)) if m.group(3) else datetime.now().year
            if month:
                return datetime(year, month, day, 12, 0)
    except:
        pass
    return None

def event_in_slot(event_dt, slot_key):
    """Проверяет попадает ли время мероприятия в слот"""
    if slot_key == "any" or not slot_key:
        return True
    slot = TIME_SLOTS.get(slot_key, TIME_SLOTS["any"])
    start_h, start_m = map(int, slot["start"].split(":"))
    end_h, end_m = map(int, slot["end"].split(":"))
    if not event_dt:
        return True  # нет времени — не исключаем
    event_h = event_dt.hour
    event_m = event_dt.minute
    event_mins = event_h * 60 + event_m
    start_mins = start_h * 60 + start_m
    end_mins = end_h * 60 + end_m
    return start_mins <= event_mins <= end_mins

def geo_compatible(day_events, candidate, max_km=5.0):
    """Проверяет географическую совместимость нового события с уже выбранными"""
    c_lat = candidate.get("venue_lat")
    c_lon = candidate.get("venue_lon")
    if not c_lat or not c_lon:
        return True  # нет координат — не ограничиваем

    distances = []
    for e in day_events:
        if e.get("venue_lat") and e.get("venue_lon"):
            d = haversine(e["venue_lat"], e["venue_lon"], c_lat, c_lon)
            distances.append(d)

    if not distances:
        return True
    return (sum(distances) / len(distances)) <= max_km


class SmartPlanner:
    def __init__(self, db, recommender=None):
        self.db = db
        self.recommender = recommender

    def create_plan(
        self,
        date_from: str,
        date_to: str,
        interests: list,
        budget_total: int = None,
        budget_per_day: int = None,
        time_slots: dict = None,      # {"2025-07-15": ["morning", "evening"], ...}
        district: str = None,
        group_size: int = 1,
        user_id: int = None,
        max_events_per_day: int = 3,
        benefits_filter: str = None,
    ) -> dict:
        """
        Составляет персонализированное расписание.

        Параметры:
        - date_from / date_to    — диапазон дат
        - interests              — категории интересов
        - budget_total           — общий бюджет на всё
        - budget_per_day         — бюджет на день
        - time_slots             — предпочтительное время по дням
        - district               — предпочтительный район
        - group_size             — количество человек
        - user_id                — для персонализации через recommender
        - max_events_per_day     — макс событий в день
        - benefits_filter        — льготы ("Пушкинская карта" и т.д.)
        """

        # Генерируем список дней
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d")
            end = datetime.strptime(date_to, "%Y-%m-%d")
        except:
            return {"error": "Неверный формат даты", "schedule": []}

        days = []
        current = start
        while current <= end:
            days.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)

        if not days:
            return {"error": "Нет дней в диапазоне", "schedule": []}

        # Рассчитываем бюджет на день
        if budget_per_day:
            daily_budget = budget_per_day * group_size
        elif budget_total:
            daily_budget = (budget_total // len(days)) * group_size
        else:
            daily_budget = None  # без ограничений

        # Получаем кандидатов из БД
        candidates = self._get_candidates(
            interests=interests,
            district=district,
            benefits_filter=benefits_filter,
        )

        if not candidates:
            return {
                "days": len(days),
                "total_events": 0,
                "total_cost": 0,
                "message": "Не найдено мероприятий по заданным параметрам",
                "schedule": []
            }

        # Получаем оценки релевантности
        scores = {}
        if user_id and self.recommender and self.recommender.is_trained:
            try:
                cbf = self.recommender.get_realtime_cbf_scores(user_id)
                scores = cbf
            except:
                pass

        # Добавляем оценки к кандидатам
        for c in candidates:
            c["score"] = scores.get(c["id"], 0.5)
            # Повышаем приоритет событий из предпочтительного района
            if district and district.lower() in (c.get("venue_address") or "").lower():
                c["score"] += 0.2

        # Сортируем по оценке
        candidates.sort(key=lambda x: x["score"], reverse=True)

        # Распределяем по дням
        schedule = []
        used_event_ids = set()
        total_cost = 0
        remaining_budget = budget_total * group_size if budget_total else None

        for day in days:
            day_plan = {
                "date": day,
                "date_label": self._format_date_label(day),
                "events": [],
                "day_cost": 0,
            }

            # Определяем слоты для этого дня
            if time_slots and day in time_slots:
                day_slots = time_slots[day] if isinstance(time_slots[day], list) else [time_slots[day]]
            else:
                # По умолчанию — все слоты
                day_slots = ["morning", "afternoon", "evening"]

            day_events_added = []
            day_cost = 0
            events_count = 0

            for slot in day_slots:
                if events_count >= max_events_per_day:
                    break

                for candidate in candidates:
                    if candidate["id"] in used_event_ids:
                        continue
                    if events_count >= max_events_per_day:
                        break

                    # Проверяем дату
                    event_dt = parse_event_date(candidate.get("start_datetime", ""))
                    if event_dt:
                        event_day = event_dt.strftime("%Y-%m-%d")
                        # Если событие привязано к конкретному дню
                        if len(str(candidate.get("start_datetime", ""))) >= 10:
                            if event_day != day:
                                continue

                    # Проверяем временной слот
                    if not event_in_slot(event_dt, slot):
                        continue

                    # Проверяем бюджет на день
                    event_cost = (candidate.get("price_min") or 0) * group_size
                    if daily_budget is not None:
                        if not candidate.get("is_free") and day_cost + event_cost > daily_budget:
                            continue

                    # Проверяем общий бюджет
                    if remaining_budget is not None:
                        if not candidate.get("is_free") and total_cost + event_cost > remaining_budget:
                            continue

                    # Проверяем географию
                    if not geo_compatible(day_events_added, candidate):
                        continue

                    # Добавляем событие
                    day_events_added.append(candidate)
                    used_event_ids.add(candidate["id"])
                    day_cost += event_cost if not candidate.get("is_free") else 0
                    total_cost += event_cost if not candidate.get("is_free") else 0
                    if remaining_budget is not None and not candidate.get("is_free"):
                        remaining_budget -= event_cost
                    events_count += 1

                    day_plan["events"].append({
                        "slot": slot,
                        "slot_label": TIME_SLOTS.get(slot, {}).get("label", slot),
                        "id": candidate["id"],
                        "title": candidate["title"],
                        "category": candidate["category"],
                        "venue_title": candidate.get("venue_title") or "",
                        "venue_address": candidate.get("venue_address") or "",
                        "venue_lat": candidate.get("venue_lat"),
                        "venue_lon": candidate.get("venue_lon"),
                        "start_datetime": candidate.get("start_datetime") or "",
                        "price_per_person": candidate.get("price_min") or 0,
                        "price_total": event_cost,
                        "is_free": bool(candidate.get("is_free")),
                        "image_url": candidate.get("image_url") or "",
                        "age_restriction": candidate.get("age_restriction") or "0+",
                        "benefits": candidate.get("benefits") or "",
                        "score": round(candidate["score"], 3),
                    })
                    break  # один слот — одно событие

            day_plan["day_cost"] = day_cost
            schedule.append(day_plan)

        total_events = sum(len(d["events"]) for d in schedule)

        return {
            "days": len(days),
            "total_events": total_events,
            "total_cost": total_cost,
            "total_cost_per_person": total_cost // group_size if group_size else total_cost,
            "group_size": group_size,
            "remaining_budget": remaining_budget,
            "coverage": round(total_events / (len(days) * len(day_slots)) * 100) if day_slots else 0,
            "schedule": schedule,
        }

    def _get_candidates(self, interests, district=None, benefits_filter=None):
        """Выбирает мероприятия-кандидаты из БД"""
        query = self.db.query(Event)

        # Фильтр по категориям интересов
        if interests:
            from sqlalchemy import or_
            filters = [Event.category.ilike("%" + cat + "%") for cat in interests]
            query = query.filter(or_(*filters))

        # Фильтр по льготам
        if benefits_filter:
            query = query.filter(Event.benefits.ilike("%" + benefits_filter + "%"))

        events = query.limit(500).all()

        result = []
        for e in events:
            # Фильтр по району (в адресе)
            if district:
                addr = (e.venue_address or "").lower()
                title_v = (e.venue_title or "").lower()
                if district.lower() not in addr and district.lower() not in title_v:
                    pass  # не исключаем жёстко, просто снизим приоритет

            result.append({
                "id": e.id,
                "title": e.title,
                "category": e.category or "",
                "venue_title": e.venue_title or "",
                "venue_address": e.venue_address or "",
                "venue_lat": e.venue_lat,
                "venue_lon": e.venue_lon,
                "start_datetime": e.start_datetime or "",
                "price_min": e.price_min or 0,
                "is_free": bool(e.is_free),
                "age_restriction": e.age_restriction or "0+",
                "image_url": e.image_url or "",
                "benefits": e.benefits or "",
                "source": e.source or "",
            })

        return result

    def _format_date_label(self, date_str):
        """Форматирует дату для отображения: 2025-07-15 → Вторник, 15 июля"""
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            days_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
            months_ru = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
                        "июля", "августа", "сентября", "октября", "ноября", "декабря"]
            return days_ru[d.weekday()] + ", " + str(d.day) + " " + months_ru[d.month]
        except:
            return date_str
