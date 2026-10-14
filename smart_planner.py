import math
import json
from pathlib import Path
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

# Границы районов загружаются из districts.geojson, лежащего рядом с этим файлом.
_DISTRICTS_CACHE = None
_DISTRICTS_PATH = Path(__file__).resolve().parent / "districts.geojson"
_WEB_MERCATOR_RADIUS = 6378137.0


def _normalize_name(value):
    """Нормализует название района для сопоставления с полем NAME в GeoJSON."""
    import re
    value = str(value or "").casefold().replace("ё", "е")
    value = re.sub(r"[^а-яa-z0-9]+", " ", value)
    return " ".join(value.split())


def _load_district_features():
    global _DISTRICTS_CACHE
    if _DISTRICTS_CACHE is not None:
        return _DISTRICTS_CACHE

    if not _DISTRICTS_PATH.is_file():
        raise FileNotFoundError(
            f"Не найден файл границ районов: {_DISTRICTS_PATH}. "
            "Положите districts.geojson рядом с smart_planner.py."
        )

    with _DISTRICTS_PATH.open("r", encoding="utf-8-sig") as file:
        geojson = json.load(file)

    features = geojson.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("В districts.geojson отсутствует непустой список features.")

    # В используемом наборе dm18 геометрия задана в EPSG:3857,
    # а названия районов хранятся в properties.NAME.
    _DISTRICTS_CACHE = features
    return _DISTRICTS_CACHE


def _district_feature_for_name(district):
    wanted = _normalize_name(district)
    if not wanted or wanted == "любой район":
        return None

    features = _load_district_features()
    # Сначала ищем по стандартному полю NAME, затем проверяем все текстовые
    # свойства на случай отличий в версии файла.
    for feature in features:
        props = feature.get("properties") or {}
        names = [props.get("NAME")] if props.get("NAME") else []
        names.extend(v for k, v in props.items() if k != "NAME" and isinstance(v, str))
        for name in names:
            normalized = _normalize_name(name)
            if normalized == wanted or normalized.startswith(wanted + " "):
                return feature

    available = sorted({
        str((f.get("properties") or {}).get("NAME"))
        for f in features if (f.get("properties") or {}).get("NAME")
    })
    raise ValueError(
        f"Район «{district}» не найден в districts.geojson. "
        f"Примеры названий в файле: {', '.join(available[:8])}"
    )


def _event_point_for_geometry(lat, lon, geometry):
    """Возвращает координаты точки в той же системе, что и GeoJSON."""
    coordinates = geometry.get("coordinates") or []
    sample = coordinates
    while isinstance(sample, list) and sample and isinstance(sample[0], list):
        sample = sample[0]
    # GeoJSON от dm18 использует EPSG:3857 (координаты в метрах).
    # Если файл заменён на обычный GeoJSON в градусах, поддерживаем и его.
    if isinstance(sample, list) and len(sample) >= 2 and (abs(sample[0]) > 180 or abs(sample[1]) > 90):
        lat = max(min(float(lat), 85.05112878), -85.05112878)
        x = _WEB_MERCATOR_RADIUS * math.radians(float(lon))
        y = _WEB_MERCATOR_RADIUS * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
        return x, y
    return float(lon), float(lat)


def _point_on_segment(x, y, x1, y1, x2, y2):
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > 1e-7:
        return False
    return (min(x1, x2) - 1e-7 <= x <= max(x1, x2) + 1e-7 and
            min(y1, y2) - 1e-7 <= y <= max(y1, y2) + 1e-7)


def _point_in_ring(point, ring):
    x, y = point
    inside = False
    if not ring or len(ring) < 3:
        return False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = float(ring[i][0]), float(ring[i][1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        if _point_on_segment(x, y, xi, yi, xj, yj):
            return True
        if (yi > y) != (yj > y):
            crossing_x = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < crossing_x:
                inside = not inside
        j = i
    return inside


def _point_in_polygon(point, rings):
    # Первый контур — внешняя граница; последующие контуры — отверстия.
    if not rings or not _point_in_ring(point, rings[0]):
        return False
    return not any(_point_in_ring(point, hole) for hole in rings[1:])


def _event_is_in_district(lat, lon, feature):
    if lat is None or lon is None:
        return False
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False

    geometry = feature.get("geometry") or {}
    point = _event_point_for_geometry(lat, lon, geometry)
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []

    if geometry_type == "Polygon":
        return _point_in_polygon(point, coordinates)
    if geometry_type == "MultiPolygon":
        return any(_point_in_polygon(point, polygon) for polygon in coordinates)
    return False


def parse_event_date(date_str):
    """Парсит дату из разных форматов в datetime"""
    if not date_str:
        return None
    try:
        # Формат: "2025-07-15 19:00"
        normalized = str(date_str).strip().replace("T", " ")
        return datetime.strptime(normalized[:16], "%Y-%m-%d %H:%M")
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

def estimate_travel(origin, destination, mode="transit"):
    """Приблизительная оценка времени между двумя точками без внешнего роутера.

    Расстояние по прямой преобразуется в оценку дорожного пути. Это эвристика,
    не фактический маршрут и не учитывает расписание транспорта/пробки.
    """
    try:
        a_lat, a_lon = origin.get("venue_lat"), origin.get("venue_lon")
        b_lat, b_lon = destination.get("venue_lat"), destination.get("venue_lon")
        if any(v is None for v in (a_lat, a_lon, b_lat, b_lon)):
            return {"minutes": 30, "distance_km": None, "known_coordinates": False,
                    "method": "fallback_no_coordinates"}
        a_lat, a_lon, b_lat, b_lon = map(float, (a_lat, a_lon, b_lat, b_lon))
        direct_km = haversine(a_lat, a_lon, b_lat, b_lon)
    except (TypeError, ValueError):
        return {"minutes": 30, "distance_km": None, "known_coordinates": False,
                "method": "fallback_no_coordinates"}

    # Коэффициенты учитывают, что реальный путь обычно длиннее прямой линии.
    walk_minutes = direct_km * 1.30 / 4.5 * 60 + 3
    transit_minutes = direct_km * 1.55 / 22 * 60 + 15  # подход, ожидание, пересадки
    car_minutes = direct_km * 1.35 / 25 * 60 + 10      # посадка/высадка; без пробок
    estimates = {"walk": walk_minutes, "transit": transit_minutes, "car": car_minutes}
    if mode == "flexible":
        minutes = min(estimates.values())
    else:
        minutes = estimates.get(mode, transit_minutes)
    # Даже для близких точек закладываем несколько минут на выход/вход.
    minutes = max(5, int(math.ceil(minutes / 5.0) * 5))
    return {"minutes": minutes, "distance_km": round(direct_km, 2),
            "known_coordinates": True, "method": "straight_line_estimate"}


def travel_mode_label(mode):
    return {"walk": "пешком", "transit": "общественный транспорт (оценка)",
            "car": "такси/автомобиль (без учёта пробок)",
            "flexible": "самый быстрый расчётный вариант"}.get(mode, "общественный транспорт (оценка)")


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
        time_slots: dict = None,
        district: str = None,
        group_size: int = 1,
        user_id: int = None,
        max_events_per_day: int = 3,
        benefits_filter: str = None,
        age_group: str = None,
        mandatory_points: list = None,
        travel_mode: str = "transit",
        travel_buffer_minutes: int = 10,
    ) -> dict:
        """Builds a daily itinerary, reserving mandatory points before events."""
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d").date()
            end = datetime.strptime(date_to, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return {"error": "Неверный формат даты", "schedule": []}
        if end < start:
            return {"error": "Дата окончания раньше даты начала", "schedule": []}

        days = []
        current = start
        while current <= end:
            days.append(current.isoformat())
            current += timedelta(days=1)

        group_size = max(1, int(group_size or 1))
        max_events_per_day = max(1, int(max_events_per_day or 3))
        daily_budget = int(budget_per_day) if budget_per_day is not None else None
        total_budget = int(budget_total) if budget_total is not None else None
        if travel_mode not in {"walk", "transit", "car", "flexible"}:
            travel_mode = "transit"
        travel_buffer_minutes = max(0, min(60, int(travel_buffer_minutes or 0)))

        candidates = self._get_candidates(
            interests=interests, district=district, benefits_filter=benefits_filter,
            age_group=age_group,
        )
        scores = {}
        if user_id and self.recommender and getattr(self.recommender, "is_trained", False):
            try:
                scores = self.recommender.get_realtime_cbf_scores(user_id) or {}
            except Exception:
                scores = {}
        for candidate in candidates:
            candidate["score"] = float(scores.get(candidate["id"], 0.5) or 0.5)
        candidates.sort(key=lambda item: item["score"], reverse=True)

        schedule = [{
            "date": day, "date_label": self._format_date_label(day), "events": [],
            "day_cost": 0, "mandatory_count": 0,
        } for day in days]
        day_by_date = {day["date"]: day for day in schedule}
        day_timeline = {day: [] for day in days}
        used_event_ids = set()
        total_cost = 0
        mandatory_points = mandatory_points or []

        def point_datetime(day, time_value):
            try:
                return datetime.strptime(f"{day} {time_value}", "%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                return None

        def duration_minutes(item):
            try:
                return max(15, int(item.get("duration_minutes") or item.get("duration") or 120))
            except (TypeError, ValueError):
                return 120

        def overlaps(start_dt, end_dt, entries):
            return any(start_dt < e["end_dt"] and end_dt > e["start_dt"] for e in entries)

        def travel_check(start_dt, end_dt, location, entries):
            """Проверяет дорогу до ближайшей предыдущей и следующей точки дня."""
            ordered = sorted(entries, key=lambda e: e["start_dt"])
            previous = next((e for e in reversed(ordered) if e["end_dt"] <= start_dt), None)
            following = next((e for e in ordered if e["start_dt"] >= end_dt), None)
            checks = []
            if previous:
                estimate = estimate_travel(previous, location, travel_mode)
                gap = int((start_dt - previous["end_dt"]).total_seconds() // 60)
                required = estimate["minutes"] + travel_buffer_minutes
                checks.append((gap >= required, previous, estimate, gap, required, "from_previous"))
            if following:
                estimate = estimate_travel(location, following, travel_mode)
                gap = int((following["start_dt"] - end_dt).total_seconds() // 60)
                required = estimate["minutes"] + travel_buffer_minutes
                checks.append((gap >= required, following, estimate, gap, required, "to_next"))
            return all(item[0] for item in checks), checks

        # 1) Reserve mandatory points first. They are kept even if their cost exceeds the budget,
        # because the user explicitly marked them as required; the response reports the overrun.
        for idx, point in enumerate(mandatory_points):
            day = str(point.get("date") or point.get("day") or "")[:10]
            if day not in day_by_date:
                continue
            title = str(point.get("title") or point.get("name") or "Обязательная точка").strip()
            start_time = str(point.get("start_time") or point.get("time") or "09:00")[:5]
            end_time = str(point.get("end_time") or "")[:5]
            start_dt = point_datetime(day, start_time)
            if not start_dt:
                continue
            if end_time:
                end_dt = point_datetime(day, end_time)
                if not end_dt or end_dt <= start_dt:
                    end_dt = start_dt + timedelta(minutes=duration_minutes(point))
            else:
                end_dt = start_dt + timedelta(minutes=duration_minutes(point))
            try:
                price_pp = max(0, float(point.get("price_per_person") or point.get("cost_per_person") or 0))
            except (TypeError, ValueError):
                price_pp = 0
            cost = int(price_pp * group_size)
            if overlaps(start_dt, end_dt, day_timeline[day]):
                return {"error": f"Обязательные точки на {day} пересекаются по времени около {start_time}. Измените время одной из точек.", "schedule": []}
            entry = {
                "slot": "mandatory", "slot_label": "Обязательная точка",
                "id": point.get("event_id", f"custom-{idx + 1}"),
                "title": title, "category": point.get("category") or "Личная точка",
                "venue_title": point.get("venue_title") or "",
                "venue_address": point.get("address") or point.get("venue_address") or "",
                "venue_lat": point.get("venue_lat", point.get("lat")),
                "venue_lon": point.get("venue_lon", point.get("lon")),
                "start_datetime": start_dt.strftime("%Y-%m-%d %H:%M"),
                "planned_start": start_dt.strftime("%H:%M"), "planned_end": end_dt.strftime("%H:%M"),
                "duration_minutes": int((end_dt - start_dt).total_seconds() // 60),
                "price_per_person": price_pp, "price_total": cost,
                "is_free": cost == 0, "image_url": point.get("image_url") or "",
                "age_restriction": point.get("age_restriction") or "0+",
                "benefits": "", "score": 1.0, "mandatory": True,
                "url": point.get("url") or "", "custom": not bool(point.get("event_id")),
            }
            day_by_date[day]["events"].append(entry)
            day_timeline[day].append({
                "start_dt": start_dt, "end_dt": end_dt,
                "venue_lat": entry.get("venue_lat"), "venue_lon": entry.get("venue_lon"),
                "title": entry["title"], "id": entry["id"],
            })
            day_by_date[day]["mandatory_count"] += 1
            day_by_date[day]["day_cost"] += cost
            total_cost += cost
            if point.get("event_id"):
                used_event_ids.add(point["event_id"])

        # Обязательные точки нельзя переставлять автоматически: если между ними
        # не хватает времени на дорогу, возвращаем понятное предупреждение-ошибку.
        for day in days:
            fixed = sorted(day_timeline[day], key=lambda e: e["start_dt"])
            for previous, following in zip(fixed, fixed[1:]):
                if previous["end_dt"] <= following["start_dt"]:
                    estimate = estimate_travel(previous, following, travel_mode)
                    gap = int((following["start_dt"] - previous["end_dt"]).total_seconds() // 60)
                    required = estimate["minutes"] + travel_buffer_minutes
                    if gap < required:
                        return {"error": (f"Между обязательными точками «{previous.get('title', 'Точка')}» и "
                                f"«{following.get('title', 'Точка')}» на {day} только {gap} мин, "
                                f"а для дороги и запаса ориентировочно нужно {required} мин. "
                                "Измените время или выберите другой способ передвижения."), "schedule": []}

        # 2) Place ranked events into free time windows without colliding with fixed points.
        for day in days:
            day_plan = day_by_date[day]
            day_slots = (time_slots.get(day) if time_slots and day in time_slots else None)
            if not day_slots:
                day_slots = ["morning", "afternoon", "evening"]
            elif not isinstance(day_slots, list):
                day_slots = [day_slots]
            day_slots = [s for s in day_slots if s in TIME_SLOTS and s != "any"] or ["morning", "afternoon", "evening"]
            events_added = len(day_plan["events"])
            for slot in day_slots:
                if events_added >= max_events_per_day:
                    break
                slot_info = TIME_SLOTS[slot]
                slot_start = point_datetime(day, slot_info["start"])
                slot_end = point_datetime(day, "23:59" if slot_info["end"] == "23:59" else slot_info["end"])
                if not slot_start or not slot_end:
                    continue
                for candidate in candidates:
                    if events_added >= max_events_per_day:
                        break
                    if candidate["id"] in used_event_ids:
                        continue
                    event_dt = parse_event_date(candidate.get("start_datetime"))
                    if event_dt and len(str(candidate.get("start_datetime", ""))) >= 10:
                        if event_dt.strftime("%Y-%m-%d") != day:
                            continue
                    event_duration = duration_minutes(candidate)
                    # Fixed-time events keep their published time. Flexible events are placed
                    # into the first free interval inside this slot, scanning in 15-minute steps.
                    raw_start = str(candidate.get("start_datetime") or "").strip().replace("T", " ")
                    has_clock_time = len(raw_start) >= 16 and raw_start[10:11] == " " and raw_start[11:13].isdigit() and raw_start[13:14] == ":"
                    if event_dt and has_clock_time:
                        start_dt = event_dt
                        if start_dt < slot_start or start_dt > slot_end:
                            continue
                        end_dt = start_dt + timedelta(minutes=event_duration)
                        if end_dt > slot_end or overlaps(start_dt, end_dt, day_timeline[day]):
                            continue
                    else:
                        start_dt = slot_start
                        end_dt = start_dt + timedelta(minutes=event_duration)
                        while end_dt <= slot_end and (
                            overlaps(start_dt, end_dt, day_timeline[day]) or
                            not travel_check(start_dt, end_dt, candidate, day_timeline[day])[0]
                        ):
                            start_dt += timedelta(minutes=15)
                            end_dt = start_dt + timedelta(minutes=event_duration)
                        if end_dt > slot_end:
                            continue
                    if overlaps(start_dt, end_dt, day_timeline[day]):
                        continue
                    if not travel_check(start_dt, end_dt, candidate, day_timeline[day])[0]:
                        continue
                    event_cost = int((candidate.get("price_min") or 0) * group_size) if not candidate.get("is_free") else 0
                    current_day_cost = day_plan["day_cost"]
                    if daily_budget is not None and current_day_cost + event_cost > daily_budget:
                        continue
                    if total_budget is not None and total_cost + event_cost > total_budget:
                        continue
                    entry = {
                        "slot": slot, "slot_label": TIME_SLOTS[slot]["label"],
                        "id": candidate["id"], "title": candidate["title"],
                        "category": candidate["category"], "venue_title": candidate.get("venue_title") or "",
                        "venue_address": candidate.get("venue_address") or "",
                        "venue_lat": candidate.get("venue_lat"), "venue_lon": candidate.get("venue_lon"),
                        "start_datetime": candidate.get("start_datetime") or start_dt.strftime("%Y-%m-%d %H:%M"),
                        "planned_start": start_dt.strftime("%H:%M"), "planned_end": end_dt.strftime("%H:%M"),
                        "duration_minutes": event_duration,
                        "price_per_person": candidate.get("price_min") or 0, "price_total": event_cost,
                        "is_free": bool(candidate.get("is_free")), "image_url": candidate.get("image_url") or "",
                        "age_restriction": candidate.get("age_restriction") or "0+",
                        "benefits": candidate.get("benefits") or "", "score": round(candidate["score"], 3),
                        "mandatory": False, "custom": False, "url": "",
                    }
                    day_plan["events"].append(entry)
                    day_timeline[day].append({
                        "start_dt": start_dt, "end_dt": end_dt,
                        "venue_lat": entry.get("venue_lat"), "venue_lon": entry.get("venue_lon"),
                        "title": entry["title"], "id": entry["id"],
                    })
                    used_event_ids.add(candidate["id"])
                    day_plan["day_cost"] += event_cost
                    total_cost += event_cost
                    events_added += 1
                    break

        # Сортируем события, считаем расстояния по прямой и показываем оценку времени
        # между соседними событиями. Это эвристика, не фактические маршруты.
        total_distance = 0.0
        total_travel_minutes = 0
        for day in schedule:
            day["events"].sort(key=lambda e: e.get("planned_start") or "00:00")
            route_distance = 0.0
            travel_legs = []
            for index in range(1, len(day["events"])):
                previous = day["events"][index - 1]
                event = day["events"][index]
                estimate = estimate_travel(previous, event, travel_mode)
                gap = None
                try:
                    prev_end = datetime.strptime(f"{day['date']} {previous['planned_end']}", "%Y-%m-%d %H:%M")
                    next_start = datetime.strptime(f"{day['date']} {event['planned_start']}", "%Y-%m-%d %H:%M")
                    gap = int((next_start - prev_end).total_seconds() // 60)
                except (KeyError, TypeError, ValueError):
                    pass
                travel_legs.append({
                    "from_id": previous.get("id"), "from_title": previous.get("title", ""),
                    "to_id": event.get("id"), "to_title": event.get("title", ""),
                    "distance_km": estimate["distance_km"],
                    "estimated_minutes": estimate["minutes"], "available_minutes": gap,
                    "buffer_minutes": travel_buffer_minutes,
                    "has_coordinates": estimate["known_coordinates"],
                    "mode": travel_mode, "mode_label": travel_mode_label(travel_mode),
                    "warning": (gap is not None and gap < estimate["minutes"] + travel_buffer_minutes),
                })
                total_travel_minutes += estimate["minutes"]
                if estimate["distance_km"] is not None:
                    route_distance += estimate["distance_km"]
            day["travel_legs"] = travel_legs
            day["distance_km"] = round(route_distance, 2)
            total_distance += route_distance
        total_events = sum(len(day["events"]) for day in schedule)
        return {
            "days": len(days), "total_events": total_events, "total_cost": total_cost,
            "total_distance_km": round(total_distance, 2),
            "total_cost_per_person": round(total_cost / group_size) if group_size else total_cost,
            "group_size": group_size,
            "travel_mode": travel_mode,
            "travel_mode_label": travel_mode_label(travel_mode),
            "travel_buffer_minutes": travel_buffer_minutes,
            "estimated_total_travel_minutes": total_travel_minutes,
            "travel_estimate_note": "Время в пути приблизительное: рассчитано по расстоянию между координатами без реальных дорог, расписания метро и пробок. Если координат нет, используется резервная оценка 30 минут.",
            "remaining_budget": max(0, total_budget - total_cost) if total_budget is not None else None,
            "budget_exceeded": total_budget is not None and total_cost > total_budget,
            "coverage": round(total_events / (len(days) * max_events_per_day) * 100) if days and max_events_per_day else 0,
            "candidate_options": [{
                "id": c["id"], "title": c["title"], "category": c["category"],
                "venue_title": c.get("venue_title") or "", "venue_address": c.get("venue_address") or "",
                "start_datetime": c.get("start_datetime") or "", "price_per_person": c.get("price_min") or 0,
                "is_free": bool(c.get("is_free")), "duration_minutes": duration_minutes(c),
                "venue_lat": c.get("venue_lat"), "venue_lon": c.get("venue_lon"),
                "age_restriction": c.get("age_restriction") or "0+", "image_url": c.get("image_url") or "",
            } for c in candidates[:100]],
            "schedule": schedule,
        }

    def _get_candidates(self, interests, district=None, benefits_filter=None, age_group=None):
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

        district_feature = None
        if district and _normalize_name(district) not in {"любой", "любой район"}:
            district_feature = _district_feature_for_name(district)

        result = []
        def age_number(value):
            import re
            match = re.search(r"(\d+)", str(value or "0+"))
            return int(match.group(1)) if match else 0

        max_allowed_age = age_number(age_group) if age_group else None
        for e in events:
            # Не считаем неизвестную стоимость платного события бесплатной.
            # Явно бесплатные события сохраняются с price_min=0 и не исключаются.
            if not e.is_free and e.price_min is None:
                continue
            # Возрастной фильтр: для выбранной группы исключаем события с более высоким порогом.
            if max_allowed_age is not None and age_number(e.age_restriction) > max_allowed_age:
                continue
            # Если район выбран, оставляем только мероприятия с координатами
            # внутри соответствующего административного полигона.
            if district_feature and not _event_is_in_district(
                e.venue_lat, e.venue_lon, district_feature
            ):
                continue

            result.append({
                "id": e.id,
                "title": e.title,
                "category": e.category or "",
                "venue_title": e.venue_title or "",
                "venue_address": e.venue_address or "",
                "venue_lat": e.venue_lat,
                "venue_lon": e.venue_lon,
                "start_datetime": e.start_datetime or "",
                "end_datetime": e.end_datetime or "",
                "duration_minutes": e.duration_minutes,
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
