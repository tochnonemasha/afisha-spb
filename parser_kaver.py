import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import re
from datetime import datetime

BASE_URL = "https://kaverafisha.ru"
CITY = "saint-petersburg"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Категории Кавёра
SECTIONS = [
    "events/lists/standup",
    "events/lists/concerts",
    "events/lists/spectacles",
    "events/lists/festivals",
    "events/listings/exhibition",
    "events/listings/master-classes",
    "events/listings/nightlife",
]

def get_page(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            return BeautifulSoup(response.text, "html.parser")
        else:
            print("  Ошибка " + str(response.status_code) + ": " + url)
            return None
    except Exception as e:
        print("  Ошибка сети: " + str(e))
        return None

def parse_event_links(section_url):
    """Собирает ссылки на мероприятия со страницы раздела"""
    soup = get_page(section_url)
    if not soup:
        return []

    links = []
    # Ищем все ссылки на мероприятия
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Ссылки на мероприятия выглядят так: /saint-petersburg/events/название-id
        if "/saint-petersburg/events/" in href and href.count("/") == 3:
            full_url = BASE_URL + href if href.startswith("/") else href
            if full_url not in links:
                links.append(full_url)

    return links

def parse_event_page(url):
    """Парсит страницу конкретного мероприятия"""
    soup = get_page(url)
    if not soup:
        return None

    try:
        # Название
        title = ""
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

        # Описание
        description = ""
        desc_block = soup.find("div", class_=re.compile("description|content|about", re.I))
        if desc_block:
            description = desc_block.get_text(strip=True)[:1000]

        # Дата
        date_str = ""
        date_block = soup.find(string=re.compile(r'\d{1,2}\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)', re.I))
        if date_block:
            date_str = date_block.strip()

        # Адрес
        address = ""
        addr_patterns = [
            soup.find("a", href=re.compile("maps|yandex", re.I)),
            soup.find(string=re.compile(r'(улица|проспект|переулок|набережная|площадь|ул\.|пр\.)', re.I)),
        ]
        for p in addr_patterns:
            if p:
                address = p.get_text(strip=True) if hasattr(p, 'get_text') else str(p).strip()
                if len(address) > 10:
                    break

        # Цена
        price_min = 0
        is_free = False
        price_text = soup.find(string=re.compile(r'(от\s*\d+|бесплатно|\d+\s*₽)', re.I))
        if price_text:
            price_text = str(price_text)
            if "бесплатно" in price_text.lower():
                is_free = True
            else:
                numbers = re.findall(r'\d+', price_text)
                if numbers:
                    price_min = int(numbers[0])

        # Картинка
        image_url = ""
        img = soup.find("meta", property="og:image")
        if img:
            image_url = img.get("content", "")

        # Категория из URL
        category = "other"
        url_parts = url.split("/")
        if len(url_parts) > 4:
            slug = url_parts[-1]
            if "standup" in slug or "stand" in slug:
                category = "stand-up"
            elif "kontsert" in slug or "concert" in slug or "muzyk" in slug:
                category = "concert"
            elif "spektakl" in slug or "teatr" in slug:
                category = "theater"
            elif "festival" in slug:
                category = "festival"
            elif "master" in slug or "workshop" in slug:
                category = "education"
            elif "vystavk" in slug or "exhibition" in slug:
                category = "exhibition"
            elif "vecherink" in slug or "party" in slug:
                category = "party"

        if not title:
            return None

        return {
            "external_id": None,
            "title": title,
            "short_title": title[:100],
            "description": description,
            "category": category,
            "tags": "",
            "start_datetime": date_str,
            "end_datetime": "",
            "venue_title": "",
            "venue_address": address[:300],
            "venue_lat": None,
            "venue_lon": None,
            "price_min": price_min,
            "price_raw": str(price_min) + " руб" if price_min > 0 else ("Бесплатно" if is_free else ""),
            "is_free": is_free,
            "age_restriction": "0+",
            "image_url": image_url,
            "site_url": url,
            "source": "kaver",
            "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    except Exception as e:
        print("  Ошибка парсинга страницы: " + str(e))
        return None

def main():
    print("=" * 50)
    print("ПАРСЕР МЕРОПРИЯТИЙ — Кавёр")
    print("=" * 50)

    # Сначала устанавливаем beautifulsoup4
    print("\nУстанови библиотеку если ещё не установил:")
    print("pip install beautifulsoup4")
    print()

    all_links = []

    # Собираем ссылки по разделам
    for section in SECTIONS:
        url = BASE_URL + "/" + CITY + "/" + section
        print("\nРаздел: " + url)
        links = parse_event_links(url)
        print("Найдено ссылок: " + str(len(links)))
        all_links.extend(links)
        time.sleep(1)

    # Убираем дубли
    all_links = list(set(all_links))
    print("\nВсего уникальных ссылок: " + str(len(all_links)))

    # Парсим каждое мероприятие
    all_events = []
    for i, link in enumerate(all_links):
        print("Обрабатываю " + str(i+1) + "/" + str(len(all_links)) + ": " + link[-50:])
        event = parse_event_page(link)
        if event:
            all_events.append(event)
        time.sleep(0.8)  # пауза чтобы не перегружать сервер

    if not all_events:
        print("\nНичего не найдено — сайт может использовать JavaScript для загрузки")
        print("В этом случае данные KudaGo будет достаточно для диплома")
        return None

    df = pd.DataFrame(all_events)
    df = df.drop_duplicates(subset=["title"])
    print("\nИтого мероприятий: " + str(len(df)))

    filename = "events_kaver_" + datetime.now().strftime("%Y%m%d_%H%M") + ".csv"
    df.to_csv(filename, index=False, encoding="utf-8-sig")
    print("Сохранено в: " + filename)

    return df

if __name__ == "__main__":
    df = main()