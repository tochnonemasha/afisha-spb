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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Referer": "https://kaverafisha.ru/",
}

SECTIONS = [
    ("events/lists/standup",       "stand-up"),
    ("events/lists/concerts",      "concert"),
    ("events/lists/spectacles",    "theater"),
    ("events/lists/festivals",     "festival"),
    ("events/listings/exhibition", "exhibition"),
    ("events/listings/master-classes", "education"),
    ("events/listings/nightlife",  "party"),
]

# Льготы
BENEFIT_KEYWORDS = {
    "Пушкинская карта": ["пушкинская карта", "пушкинской картой", "пушкинской карте"],
    "Студентам": ["студент", "студентам", "студенческий"],
    "Пенсионерам": ["пенсионер", "пенсионерам"],
    "Детям": ["детский билет", "дети бесплатно", "до 7 лет", "до 12 лет"],
}

def detect_benefits(text):
    if not text:
        return ""
    text_lower = text.lower()
    found = [name for name, kws in BENEFIT_KEYWORDS.items() if any(kw in text_lower for kw in kws)]
    return ", ".join(found)

def capitalize_title(title):
    if not title:
        return title
    title = title.strip()
    return title[0].upper() + title[1:] if title else title

def clean_title(title):
    """Убирает лишний мусор из названия с Кавёра"""
    if not title:
        return ""
    # Убираем «в городе Санкт-Петербург, дата: купить билеты — Кавёр»
    title = re.sub(r'\s+в городе.*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r',?\s*купить билеты.*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*—\s*Кавёр.*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r',?\s*\d{1,2}\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+\d{4}.*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r',?\s*до\s+\d{1,2}\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря).*$', '', title, flags=re.IGNORECASE)
    return title.strip()

def get_page(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code == 200:
            return BeautifulSoup(response.text, "html.parser")
        else:
            print("  Ошибка " + str(response.status_code))
            return None
    except Exception as e:
        print("  Ошибка сети: " + str(e))
        return None

def parse_event_links(section_url):
    soup = get_page(section_url)
    if not soup:
        return []
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/saint-petersburg/events/" in href and href.count("/") == 3:
            full_url = BASE_URL + href if href.startswith("/") else href
            if full_url not in links:
                links.append(full_url)
    return links

def extract_image_from_page(soup, url):
    """
    Ищет фото мероприятия, пропуская баннеры и логотипы сайта.
    """
    # Слова которые указывают на баннер/лого сайта а не фото мероприятия
    SKIP_PATTERNS = [
        "logo", "banner", "default", "og-image", "share",
        "kaver", "placeholder", "icon", "sprite", "avatar",
        "background", "bg-", "header", "footer"
    ]

    # 1. Сначала ищем в JSON-LD разметке (самый надёжный источник)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            data = json.loads(script.string)
            if isinstance(data, dict):
                image = data.get("image") or data.get("image", {})
                if isinstance(image, str) and image.startswith("http"):
                    if not any(p in image.lower() for p in SKIP_PATTERNS):
                        return image
                elif isinstance(image, dict):
                    url_val = image.get("url", "")
                    if url_val and not any(p in url_val.lower() for p in SKIP_PATTERNS):
                        return url_val
        except:
            pass

    # 2. Ищем og:image но проверяем что это не баннер
    og_image = soup.find("meta", property="og:image")
    if og_image:
        candidate = og_image.get("content", "")
        if candidate and not any(p in candidate.lower() for p in SKIP_PATTERNS):
            return candidate

    # 3. Ищем главное изображение на странице
    # Обычно это первый большой img внутри article/main/section
    for container_tag in ["article", "main", "section", "div"]:
        containers = soup.find_all(container_tag, limit=5)
        for container in containers:
            for img in container.find_all("img"):
                src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
                if not src:
                    continue
                src_lower = src.lower()
                # Пропускаем маленькие иконки и элементы сайта
                if any(p in src_lower for p in SKIP_PATTERNS):
                    continue
                # Смотрим на размер если указан
                width = img.get("width", "0")
                height = img.get("height", "0")
                try:
                    w = int(str(width).replace("px", ""))
                    h = int(str(height).replace("px", ""))
                    if w >= 300 and h >= 200:
                        full_src = src if src.startswith("http") else BASE_URL + src
                        return full_src
                except:
                    pass
                # Если размер не указан — берём по контексту
                if any(kw in src_lower for kw in ["event", "photo", "poster", "cover", "afisha", "upload", "img/"]):
                    full_src = src if src.startswith("http") else BASE_URL + src
                    return full_src

    return ""

def parse_event_page(url, section_category="other"):
    soup = get_page(url)
    if not soup:
        return None

    try:
        # === НАЗВАНИЕ ===
        title = ""
        # Сначала og:title — там чище
        og_title = soup.find("meta", property="og:title")
        if og_title:
            title = og_title.get("content", "")
        if not title:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)

        title = clean_title(title)
        title = capitalize_title(title)

        if not title or len(title) < 3:
            return None

        # Пропускаем если название всё ещё содержит мусор
        if "кавёр" in title.lower() or "купить билеты" in title.lower():
            return None

        # === ОПИСАНИЕ — ищем максимально подробное ===
        description = ""

        # og:description как резервный вариант
        og_desc = soup.find("meta", property="og:description")
        og_desc_text = og_desc.get("content", "") if og_desc else ""

        # Ищем основной текстовый блок на странице
        text_candidates = []

        # Пробуем разные селекторы
        for selector in [
            "div.event-description", "div.description", "div.event-text",
            "div.content-text", "div.event-content", "article p",
            "div.text", "div.about", "section.description",
            "[class*='description']", "[class*='content']", "[class*='about']"
        ]:
            try:
                blocks = soup.select(selector)
                for block in blocks:
                    text = block.get_text(separator="\n", strip=True)
                    # Убираем навигационный мусор
                    if len(text) > 100 and "©" not in text and "cookies" not in text.lower():
                        text_candidates.append(text)
            except:
                pass

        # Если нашли кандидатов — берём самый длинный
        if text_candidates:
            description = max(text_candidates, key=len)
        elif og_desc_text and len(og_desc_text) > 50:
            description = og_desc_text

        # Если всё равно пусто — пробуем все параграфы
        if len(description) < 100:
            paragraphs = []
            for p in soup.find_all("p"):
                text = p.get_text(strip=True)
                if len(text) > 40 and "©" not in text:
                    paragraphs.append(text)
            if paragraphs:
                description = "\n\n".join(paragraphs[:5])

        description = description[:3000]

        # === ДАТА ===
        date_str = ""
        # Ищем в мета-описании
        all_text = soup.get_text(" ", strip=True)

        date_match = re.search(
            r'(\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+\d{4})?(?:\s+в?\s*\d{1,2}:\d{2})?)',
            all_text, re.IGNORECASE
        )
        if date_match:
            date_str = date_match.group(1).strip()

        # === ПЛОЩАДКА ===
        venue_title = ""
        venue_address = ""

        # Ищем название площадки
        venue_links = soup.find_all("a", href=re.compile("/saint-petersburg/places/"))
        if venue_links:
            venue_title = venue_links[0].get_text(strip=True)

        # Ищем адрес
        addr_match = re.search(
            r'((?:улица|ул\.|проспект|пр\.|переулок|пер\.|набережная|наб\.|площадь|пл\.|линия|шоссе)[^,\n\|]{3,60}(?:,\s*\d+[а-яА-Я/]*)?)',
            all_text, re.IGNORECASE
        )
        if addr_match:
            venue_address = addr_match.group(1).strip()

        # === ЦЕНА ===
        price_min = 0
        is_free = False

        price_match = re.search(r'от\s+(\d[\d\s]*)\s*[₽руб]', all_text, re.IGNORECASE)
        if price_match:
            price_str = price_match.group(1).replace(" ", "")
            try:
                price_min = int(price_str)
            except:
                pass
        elif re.search(r'бесплатно', all_text, re.IGNORECASE):
            is_free = True
        else:
            price_match2 = re.search(r'(\d{3,5})\s*₽', all_text)
            if price_match2:
                try:
                    price_min = int(price_match2.group(1))
                except:
                    pass

        # === ВОЗРАСТ ===
        age_restriction = "0+"
        age_match = re.search(r'\b(\d+)\+', all_text)
        if age_match:
            age_num = int(age_match.group(1))
            if age_num in [0, 6, 12, 16, 18]:
                age_restriction = str(age_num) + "+"

        # === ЛЬГОТЫ ===
        benefits = detect_benefits(description + " " + all_text[:500])

        # === ФОТО ===
        image_url = extract_image_from_page(soup, url)

        # === КАТЕГОРИЯ ===
        category = section_category

        return {
            "external_id": None,
            "title": title,
            "short_title": title[:100],
            "description": description,
            "category": category,
            "tags": "",
            "benefits": benefits,
            "start_datetime": date_str,
            "end_datetime": "",
            "venue_title": venue_title,
            "venue_address": venue_address[:300],
            "venue_lat": None,
            "venue_lon": None,
            "price_min": price_min,
            "price_raw": ("от " + str(price_min) + " ₽") if price_min > 0 else ("Бесплатно" if is_free else ""),
            "is_free": is_free,
            "age_restriction": age_restriction,
            "image_url": image_url,
            "site_url": url,
            "source": "kaver",
            "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    except Exception as e:
        print("  Ошибка парсинга: " + str(e))
        return None

def main():
    print("=" * 50)
    print("ПАРСЕР МЕРОПРИЯТИЙ — Кавёр")
    print("=" * 50)

    # section_url → категория
    section_links = {}  # url события → категория

    for section_path, category in SECTIONS:
        url = BASE_URL + "/" + CITY + "/" + section_path
        print("\nРаздел: " + url + " [" + category + "]")
        links = parse_event_links(url)
        print("Найдено ссылок: " + str(len(links)))
        for link in links:
            if link not in section_links:
                section_links[link] = category
        time.sleep(1)

    all_links = list(section_links.keys())
    print("\nВсего уникальных ссылок: " + str(len(all_links)))

    if not all_links:
        print("Ссылки не найдены")
        return None

    all_events = []
    for i, link in enumerate(all_links):
        cat = section_links.get(link, "other")
        print("Обрабатываю " + str(i+1) + "/" + str(len(all_links)) + ": " + link[-55:])
        event = parse_event_page(link, section_category=cat)
        if event:
            all_events.append(event)
            img_status = "есть" if event["image_url"] else "нет"
            desc_len = len(event["description"])
            print("  OK: " + event["title"][:45] + " | фото:" + img_status + " | описание:" + str(desc_len) + " симв.")
        time.sleep(0.8)

    if not all_events:
        print("Ничего не найдено")
        return None

    df = pd.DataFrame(all_events)
    # Убираем мусор в названиях
    df = df[~df["title"].str.contains("Кавёр|купить билеты", case=False, na=False)]
    df = df.drop_duplicates(subset=["title"])

    print("\nИтого мероприятий: " + str(len(df)))
    print("С фото: " + str(df[df["image_url"] != ""].shape[0]))
    print("С льготами: " + str(df[df["benefits"] != ""].shape[0]))
    print("Средняя длина описания: " + str(int(df["description"].str.len().mean())) + " симв.")

    print("\nПервые 3:")
    for _, row in df.head(3).iterrows():
        print("  " + str(row["title"]))
        print("  Категория: " + str(row["category"]))
        print("  Фото: " + str(row["image_url"])[:80])
        print("  Описание: " + str(row["description"])[:150] + "...")
        print()

    filename = "events_kaver_" + datetime.now().strftime("%Y%m%d_%H%M") + ".csv"
    df.to_csv(filename, index=False, encoding="utf-8-sig")
    print("Сохранено в: " + filename)

    return df

if __name__ == "__main__":
    df = main()
