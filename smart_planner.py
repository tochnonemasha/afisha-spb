from datetime import datetime, timedelta
import math
from database import Event

class SmartPlanner:
    """
    Составляет расписание мероприятий для гостя города.
    Учитывает: даты, временные слоты, бюджет, географию.
    """

    # Временные слоты
    TIME_SLOTS = {
        "morning": ("09:00", "13:00"),
        "afternoon": ("13:00", "18:00"),
        "evening": ("18:00", "23:59"),
    }

    def __init__(self, db, recommender):
        self.db = db
        self.recommender = recommender

    def create_plan(
        self,
        user_id,
        date_from: str,
        date_to: str,
        budget: int,
        interests: list,
        time_slots: dict = None,
        district: str = None,
    ) -> dict:

        # Генерируем список дней
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d")
        days = []
        current = start
        while current <= end:
            days.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)

        # Получаем все подходящие мероприятия
        candidates = self._get_candidates(interests, budget)

        # Получаем оценки релевантности
        if user_id and self.recommender.is_trained:
            scores = self.recommender.get_cbf_scores(user_id)
        else:
            scores = {e["id"]: 0.5 for e in candidates}

        # Добавляем оценки к кандидатам
        for c in candidates:
            c["score"] = scores.get(c["id"], 0.5)

        # Сортируем по оценке
        candidates.sort(key=lambda x: x["score"], reverse=True)

        # Распределяем по дням
        schedule = []
        remaining_budget = budget
        used_event_ids = set()

        for day in days:
            day_plan = {"date": day, "events": []}
            day_slots = time_slots.get(day, ["morning", "afternoon", "evening"]) \
                if time_slots else ["morning", "afternoon", "evening"]

            day_events = []

            for slot in day_slots:
                slot_start, slot_end = self.TIME_SLOTS.get(
                    slot, ("00:00", "23:59")
                )

                # Ищем подходящее мероприятие для слота
                for candidate in candidates:
                    if candidate["id"] in used_event_ids:
                        continue

                    # Проверяем дату
                    event_date = (candidate.get("start_datetime") or "")[:10]
                    if event_date != day:
                        continue

                    # Проверяем бюджет
                    price = candidate.get("price_min", 0)
                    if not candidate.get("is_free") and price > remaining_budget:
                        continue

                    # Проверяем географию (если есть предыдущие события дня)
                    if day_events and not self._geo_compatible(
                        day_events, candidate
                    ):
                        continue

                    # Подходит — добавляем
                    day_events.append(candidate)
                    used_event_ids.add(candidate["id"])
                    remaining_budget -= price if not candidate.get("is_free") else 0

                    day_plan["events"].append({
                        "slot": slot,
                        "id": candidate["id"],
                        "title": candidate["title"],
                        "category": candidate["category"],
                        "venue": candidate["venue_title"],
                        "address": candidate["venue_address"],
                        "start_datetime": candidate["start_datetime"],
                        "price": price,
                        "is_free": candidate.get("is_free", False),
                        "image_url": candidate.get("image_url", ""),
                        "score": round(candidate["score"], 3),
                    })
                    break  # один слот — одно мероприятие

            schedule.append(day_plan)

        total_cost = budget - remaining_budget
        total_events = sum(len(d["events"]) for d in schedule)

        return {
            "days": len(days),
            "total_events": total_events,
            "total_cost": total_cost,
            "remaining_budget": remaining_budget,
            "schedule": schedule,
        }

    def _get_candidates(self, interests: list, budget: int) -> list:
        """Получает мероприятия подходящие по интересам и бюджету"""
        events = self.db.query(Event).filter(
            (Event.is_free == True) | (Event.price_min <= budget)
        ).all()

        result = []
        for e in events:
            # Проверяем соответствие интересам
            cat = (e.category or "").lower()
            if interests and not any(
                interest.lower() in cat for interest in interests
            ):
                continue

            result.append({
                "id": e.id,
                "title": e.title,
                "category": e.category,
                "venue_title": e.venue_title,
                "venue_address": e.venue_address,
                "venue_lat": e.venue_lat,
                "venue_lon": e.venue_lon,
                "start_datetime": e.start_datetime,
                "price_min": e.price_min,
                "is_free": e.is_free,
                "image_url": e.image_url,
            })

        return result

    def _geo_compatible(self, day_events: list, candidate: dict) -> bool:
        """
        Проверяет что новое мероприятие географически
        совместимо с уже выбранными на этот день.
        Максимальное суммарное расстояние — 5 км.
        """
        candidate_lat = candidate.get("venue_lat")
        candidate_lon = candidate.get("venue_lon")

        if not candidate_lat or not candidate_lon:
            return True  # нет координат — не проверяем

        total_distance = 0
        count = 0

        for event in day_events:
            lat = event.get("venue_lat")
            lon = event.get("venue_lon")
            if lat and lon:
                dist = self._haversine(lat, lon, candidate_lat, candidate_lon)
                total_distance += dist
                count += 1

        if count == 0:
            return True

        avg_distance = total_distance / count
        return avg_distance <= 5.0  # не дальше 5 км в среднем

    def _haversine(self, lat1, lon1, lat2, lon2) -> float:
        """Расстояние между двумя точками в км"""
        R = 6371
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = (math.sin(dphi/2)**2 +
             math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2)
        return 2 * R * math.asin(math.sqrt(a))