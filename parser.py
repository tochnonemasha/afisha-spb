import requests
import pandas as pd
import re
from datetime import datetime
import time

BASE_URL = "https://kudago.com/public-api/v1.4"
CITY = "spb"
LANG = "ru"
PAGE_SIZE = 100

CATEGORIES = [
    "concert",
    "theater",
    "exhibition",
    "cinema",
    "education",
    "festival",
    "party",
    "sport",
    "stand-up",
    "excursion",
    "quest",
    "yarmarki-i-festivali",
    "kids",
]

# Ключевые слова льгот в описании и тегах
BENEFIT_KEYWORDS = {
    "pushkin_card": ["пушкинская карта", "пушкинской картой", "пушкинской карте", "pushkin card"],
    "students": ["студент", "студентам", "студенческий", "студенческая"],
    "pensioners": ["пенсионер", "пенсионерам", "пенсионный"],
    "children": ["детский билет", "дети бесплатно", "до 7 лет", "до 12 лет"],
    "disabled": ["инвалид", "ограниченными возможностями", "льготный"],
}

def detect_benefits(text):
    """Определяет льготы из текста описания и тегов"""
    if not text:
        return ""
    text_lower = text.lower()
    found = []
    if any(kw in text_lower for kw in BENEFIT_KEYWORDS["pushkin_card"]):
        found.append("Пушкинская карта")
    if any(kw in text_lower for kw in BENEFIT_KEYWORDS["students"]):
        found.append("Студентам")
    if any(kw in text_lower for kw in BENEFIT_KEYWORDS["pensioners"]):
        found.append("Пенсионерам")
    if any(kw in text_lower for kw in BENEFIT_KEYWORDS["children"]):
        found.append("Детям")
    if any(kw in text_lower for kw in BENEFIT_KEYWORDS["disabled"]):
        found.append("Льготный")
    return ", ".join(found)

def capitalize_title(title):
    """Делает первую букву заглавной, остальные не трогает"""
    if not title:
        return title
    title = title.strip()
    if len(title) == 0:
        return title
    return title[0].upper() + title[1:]

def parse_date(date_value):
    if not date_value:
        return None
    try:
        if isinstance(date_value, str):
            try:
                dt = datetime.fromisoformat(date_value.replace('Z', '+00:00'))
                return dt.strftime("%Y-%m-%d %H:%M")
            except:
                pass
            try:
                timestamp = int(date_value)
                if 946684800 < timestamp < 4102444800:
                    dt = datetime.fromtimestamp(timestamp)
                    return dt.strftime("%Y-%m-%d %H:%M")
            except:
                pass
            return date_value
        elif isinstance(date_value, (int, float)):
            if 946684800 < date_value < 4102444800:
                dt = datetime.fromtimestamp(date_value)
                return dt.strftime("%Y-%m-%d %H:%M")
            return None
    except:
        return None

def clean_html(text):
    if not text:
        return ""
    # Убираем HTML теги
    clean = re.compile('<.*?>')
    text = re.sub(clean, '', text)
    # Убираем множественные пробелы и переносы
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()

def extract_price(price_str):
    if not price_str:
        return 0
    numbers = re.findall(r'\d+', str(price_str))
    if numbers:
        return int(numbers[0])
    return 0

def fetch_events(category=None, pages=3):
    all_events = []
    for page in range(1, pages + 1):
        params = {
            "location": CITY,
            "lang": LANG,
            "page_size": PAGE_SIZE,
            "page": page,
            # Запрашиваем больше полей включая body_text для полного описания
            "fields": (
                "id,title,description,body_text,short_title,"
                "dates,place,price,is_free,"
                "categories,tags,age_restriction,"
                "images,site_url,tagline"
            ),
            "expand": "place,dates",
            "actual_since": int(datetime.now().timestamp()),
            "order_by": "publication_date",
        }
        if category:
            params["categories"] = category

        try:
            response = requests.get(
                BASE_URL + "/events/",
                params=params,
                timeout=15
            )
            if response.status_code == 200:
                data = response.json()
                events = data.get("results", [])
                all_events.extend(events)
                print("  Страница " + str(page) + ": " + str(len(events)) + " событий")
                if len(events) < PAGE_SIZE:
                    break
            else:
                print("  Ошибка " + str(response.status_code))
                break
        except Exception as e:
            print("  Ошибка сети: " + str(e))
            break
        time.sleep(0.5)
    return all_events

def process_event(raw_event):
    # Даты
    dates = raw_event.get("dates", [])
    start_date = None
    end_date = None
    if dates:
        start_date = parse_date(dates[0].get("start"))
        end_date = parse_date(dates[0].get("end"))

    # Место
    place = raw_event.get("place") or {}
    venue_title = place.get("title", "")
    venue_address = place.get("address", "")
    venue_lat = None
    venue_lon = None
    coords = place.get("coords")
    if coords:
        venue_lat = coords.get("lat")
        venue_lon = coords.get("lon")

    # Категории и теги
    categories = raw_event.get("categories", [])
    category_str = ", ".join(categories) if categories else "other"
    tags = raw_event.get("tags", [])
    tags_str = ", ".join(tags) if tags else ""

    # Фото — берём лучшего качества
    images = raw_event.get("images", [])
    image_url = ""
    if images:
        # Ищем самое большое изображение
        best = images[0]
        for img in images:
            thumb = img.get("thumbnails", {})
            if thumb:
                # Берём оригинал если есть
                image_url = img.get("image", "") or list(thumb.values())[-1]
                break
        if not image_url:
            image_url = images[0].get("image", "")

    # Цена
    price_str = raw_event.get("price", "")
    is_free = raw_event.get("is_free", False)
    price_min = 0 if is_free else extract_price(price_str)

    # Описание — берём максимально полное
    # body_text обычно содержит полный текст без HTML
    body_text = raw_event.get("body_text", "") or ""
    description_html = raw_event.get("description", "") or ""
    tagline = raw_event.get("tagline", "") or ""

    # Приоритет: body_text > description (очищенный от HTML) > tagline
    if body_text and len(body_text) > 100:
        description = clean_html(body_text)
    elif description_html:
        description = clean_html(description_html)
    else:
        description = tagline

    # Льготы — ищем в описании, тегах и цене
    benefits_text = description + " " + tags_str + " " + (price_str or "")
    benefits = detect_benefits(benefits_text)

    # Название — делаем первую букву заглавной
    title = capitalize_title(raw_event.get("title", "Без названия"))
    short_title = capitalize_title(raw_event.get("short_title", "") or "")

    return {
        "external_id": raw_event.get("id"),
        "title": title,
        "short_title": short_title,
        "description": description[:3000],  # увеличили с 1000 до 3000
        "category": category_str,
        "tags": tags_str,
        "benefits": benefits,
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

def main():
    print("=" * 50)
    print("ПАРСЕР МЕРОПРИЯТИЙ — KudaGo API")
    print("=" * 50)

    all_processed = []

    for category in CATEGORIES:
        print("\nКатегория: " + category)
        raw_events = fetch_events(category=category, pages=3)
        print("Всего получено: " + str(len(raw_events)))
        for raw in raw_events:
            try:
                processed = process_event(raw)
                all_processed.append(processed)
            except Exception as e:
                print("  Пропускаю: " + str(e))

    df = pd.DataFrame(all_processed)
    before = len(df)
    df = df.drop_duplicates(subset=["external_id"])
    print("\nДубликатов удалено: " + str(before - len(df)))

    # Убираем без даты
    df = df[df["start_datetime"].notna()]
    df = df[df["start_datetime"] != "None"]

    # Только будущие
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        df = df[df["start_datetime"] >= now]
    except:
        pass

    df = df.sort_values("start_datetime")
    print("Итого уникальных мероприятий: " + str(len(df)))

    # Статистика льгот
    with_benefits = df[df["benefits"] != ""].shape[0]
    print("Мероприятий с льготами: " + str(with_benefits))

    filename = "events_" + datetime.now().strftime("%Y%m%d_%H%M") + ".csv"
    df.to_csv(filename, index=False, encoding="utf-8-sig")
    print("Сохранено в файл: " + filename)

    print("\nПервые 3 мероприятия:")
    print("-" * 50)
    for _, row in df.head(3).iterrows():
        print("Название: " + str(row["title"]))
        print("Дата: " + str(row["start_datetime"]))
        print("Место: " + str(row["venue_title"]))
        print("Льготы: " + str(row["benefits"]))
        desc = str(row["description"])
        print("Описание (" + str(len(desc)) + " симв.): " + desc[:200] + "...")
        print("-" * 50)

    return df

if __name__ == "__main__":
    df = main()
