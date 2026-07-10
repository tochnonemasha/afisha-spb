import requests
import pandas as pd
import json
from datetime import datetime
import time

# ─── НАСТРОЙКИ ───────────────────────────────────────────
BASE_URL = "https://kudago.com/public-api/v1.4"
CITY = "spb"
LANG = "ru"
PAGE_SIZE = 100  # максимум за один запрос

# Категории которые нас интересуют (исправленные)
CATEGORIES = [
    "concert",        # концерты
    "theater",        # театр
    "exhibition",     # выставки
    "cinema",         # кино
    "education",      # лекции и мастер-классы
    "festival",       # фестивали
    # "sport",        # спорт - не работает
    # "stand-up",     # стендап - не работает
    # "excursion",    # экскурсии - не работает
    "party",          # вечеринки
]

# ─── ФУНКЦИЯ ПОЛУЧЕНИЯ МЕРОПРИЯТИЙ ───────────────────────
def fetch_events(category=None, pages=3):
    """
    Получает мероприятия с KudaGo API.
    category - категория (если None - все категории)
    pages - сколько страниц скачать (100 событий на странице)
    """
    all_events = []

    for page in range(1, pages + 1):
        params = {
            "location": CITY,
            "lang": LANG,
            "page_size": PAGE_SIZE,
            "page": page,
            "fields": (
                "id,title,description,short_title,"
                "dates,place,price,is_free,"
                "categories,tags,age_restriction,"
                "images,site_url"
            ),
            "expand": "place,dates",
            "actual_since": int(datetime.now().timestamp()),  # только будущие
            "order_by": "publication_date",
        }

        # Если указана категория — фильтруем
        if category:
            params["categories"] = category

        try:
            response = requests.get(
                f"{BASE_URL}/events/",
                params=params,
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                events = data.get("results", [])
                all_events.extend(events)
                print(f"  Страница {page}: получено {len(events)} событий")

                # Если событий меньше чем PAGE_SIZE — больше страниц нет
                if len(events) < PAGE_SIZE:
                    break
            else:
                print(f"  Ошибка {response.status_code}: {response.text[:200]}")
                break

        except requests.exceptions.RequestException as e:
            print(f"  Ошибка сети: {e}")
            break

        # Пауза чтобы не перегружать API
        time.sleep(0.5)

    return all_events


# ─── ФУНКЦИЯ ОБРАБОТКИ ДАТЫ ──────────────────────────────
def parse_date(date_value):
    """
    Безопасное преобразование даты из разных форматов.
    """
    if not date_value:
        return None
    
    try:
        # Если это строка
        if isinstance(date_value, str):
            # Пробуем ISO формат
            try:
                dt = datetime.fromisoformat(date_value.replace('Z', '+00:00'))
                return dt.strftime("%Y-%m-%d %H:%M")
            except:
                pass
            
            # Пробуем как timestamp в строке
            try:
                timestamp = int(date_value)
                # Проверяем, что дата разумная (между 2000 и 2100 годом)
                if 946684800 < timestamp < 4102444800:  # 2000-2100 годы
                    dt = datetime.fromtimestamp(timestamp)
                    return dt.strftime("%Y-%m-%d %H:%M")
            except:
                pass
            
            return date_value  # возвращаем как есть
        
        # Если это число
        elif isinstance(date_value, (int, float)):
            # Проверяем, что дата разумная
            if 946684800 < date_value < 4102444800:  # 2000-2100 годы
                dt = datetime.fromtimestamp(date_value)
                return dt.strftime("%Y-%m-%d %H:%M")
            else:
                return None  # игнорируем нереалистичные даты
        
        else:
            return str(date_value)
            
    except Exception as e:
        return None


# ─── ФУНКЦИЯ ОБРАБОТКИ ДАННЫХ ─────────────────────────────
def process_event(raw_event):
    """
    Преобразует сырые данные с API в удобный формат.
    """
    # Получаем первую дату мероприятия
    dates = raw_event.get("dates", [])
    start_date = None
    end_date = None
    
    if dates:
        start_ts = dates[0].get("start")
        end_ts = dates[0].get("end")
        
        start_date = parse_date(start_ts)
        end_date = parse_date(end_ts)

    # Получаем информацию о месте
    place = raw_event.get("place") or {}
    venue_title = place.get("title", "Не указано")
    venue_address = place.get("address", "Не указано")
    venue_lat = None
    venue_lon = None
    coords = place.get("coords")
    if coords:
        venue_lat = coords.get("lat")
        venue_lon = coords.get("lon")

    # Получаем категории
    categories = raw_event.get("categories", [])
    category_str = ", ".join(categories) if categories else "other"

    # Получаем теги
    tags = raw_event.get("tags", [])
    tags_str = ", ".join(tags) if tags else ""

    # Получаем фото
    images = raw_event.get("images", [])
    image_url = images[0].get("image", "") if images else ""

    # Получаем цену
    price_str = raw_event.get("price", "")
    is_free = raw_event.get("is_free", False)
    price_min = 0 if is_free else extract_price(price_str)

    # Очищаем описание от HTML тегов
    description = raw_event.get("description", "") or ""
    description = clean_html(description)
    short_title = raw_event.get("short_title", "") or ""

    return {
        "external_id": raw_event.get("id"),
        "title": raw_event.get("title", "Без названия"),
        "short_title": short_title,
        "description": description[:1000],  # обрезаем до 1000 символов
        "category": category_str,
        "tags": tags_str,
        "start_datetime": start_date,
        "end_datetime": end_date,
        "venue_title": venue_title,
        "venue_address": venue_address,
        "venue_lat": venue_lat,
        "venue_lon": venue_lon,
        "price_min": price_min,
        "price_raw": price_str,
        "is_free": is_free,
        "age_restriction": raw_event.get("age_restriction", "0+"),
        "image_url": image_url,
        "site_url": raw_event.get("site_url", ""),
        "source": "kudago",
        "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ─── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ──────────────────────────────
def extract_price(price_str):
    """
    Пытается извлечь числовое значение цены из строки.
    Например: 'от 500 рублей' → 500
    """
    if not price_str:
        return 0
    import re
    numbers = re.findall(r'\d+', str(price_str))
    if numbers:
        return int(numbers[0])
    return 0


def clean_html(text):
    """
    Убирает HTML теги из текста описания.
    """
    import re
    clean = re.compile('<.*?>')
    return re.sub(clean, '', text).strip()


# ─── ОСНОВНАЯ ЛОГИКА ──────────────────────────────────────
def main():
    print("=" * 50)
    print("ПАРСЕР МЕРОПРИЯТИЙ — KudaGo API")
    print("=" * 50)

    all_processed = []

    # Скачиваем по каждой категории
    for category in CATEGORIES:
        print(f"\n📂 Категория: {category}")
        raw_events = fetch_events(category=category, pages=2)
        print(f"  Всего получено: {len(raw_events)}")

        for raw in raw_events:
            processed = process_event(raw)
            all_processed.append(processed)

    # Убираем дубликаты по external_id
    df = pd.DataFrame(all_processed)
    before = len(df)
    df = df.drop_duplicates(subset=["external_id"])
    after = len(df)
    print(f"\n✅ Дубликатов удалено: {before - after}")
    print(f"✅ Итого уникальных мероприятий: {after}")

    # Убираем события без даты или с некорректной датой
    df = df[df["start_datetime"].notna()]
    df = df[df["start_datetime"] != "None"]
    print(f"✅ После фильтрации без даты: {len(df)}")

    # Сортируем по дате
    df = df.sort_values("start_datetime")
    
    # Оставляем только будущие события (после текущей даты)
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        df = df[df["start_datetime"] >= now]
        print(f"✅ После фильтрации будущих событий: {len(df)}")
    except:
        pass

    # Сохраняем в CSV
    filename = f"events_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    df.to_csv(filename, index=False, encoding="utf-8-sig")
    print(f"\n💾 Сохранено в файл: {filename}")

    # Показываем первые 5 для проверки
    print("\n📋 Первые 5 мероприятий:")
    print("-" * 50)
    for _, row in df.head(5).iterrows():
        print(f"  {row['title']}")
        print(f"  📅 {row['start_datetime']}")
        print(f"  📍 {row['venue_title']}")
        
        if row['is_free']:
            print(f"  💰 Бесплатно")
        else:
            print(f"  💰 от {row['price_min']} ₽")
            
        print(f"  🏷  {row['category']}")
        print("-" * 50)
    
    # Статистика по категориям
    print("\n📊 Статистика по категориям:")
    print(df['category'].value_counts().head(10))

    return df


if __name__ == "__main__":
    df = main()