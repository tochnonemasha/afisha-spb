import time
import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup
import re
import os

try:
    import chromedriver_binary
except ImportError:
    pass

def parse_kaverafisha():
    """
    Максимально упрощенный парсер Kaverafisha.ru
    """
    options = Options()
    
    # Путь к Яндекс.Браузеру
    yandex_paths = [
        "C:/Program Files/Yandex/YandexBrowser/Application/browser.exe",
        "C:/Program Files (x86)/Yandex/YandexBrowser/Application/browser.exe",
    ]
    
    yandex_found = False
    for path in yandex_paths:
        if os.path.exists(path):
            options.binary_location = path
            yandex_found = True
            break
    
    if not yandex_found:
        print("❌ Яндекс.Браузер не найден")
        return []
    
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    
    print("🌐 Запускаю браузер...")
    
    try:
        driver = webdriver.Chrome(options=options)
    except:
        try:
            driver = webdriver.Chrome()
        except Exception as e:
            print(f"❌ Ошибка: {e}")
            return []
    
    url = "https://kaverafisha.ru/saint-petersburg"
    print(f"🌐 Загружаю страницу...")
    
    try:
        driver.get(url)
        time.sleep(3)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        driver.quit()
        return []
    
    # Прокручиваем
    for i in range(5):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)
    
    html = driver.page_source
    driver.quit()
    
    soup = BeautifulSoup(html, 'html.parser')
    
    # Ищем все блоки с текстом
    events = []
    
    # Ищем блоки, которые содержат информацию о событии
    # Обычно это блоки с классом, содержащим 'event' или 'card'
    blocks = soup.find_all(['div', 'article', 'section', 'li'], 
                          class_=re.compile(r'event|card|item|post|ticket|product', re.I))
    
    if not blocks:
        # Если не нашли по классам - ищем по тексту
        blocks = soup.find_all(['div', 'article', 'li'])
    
    print(f"🔍 Найдено блоков: {len(blocks)}")
    
    for block in blocks:
        try:
            text = block.get_text(strip=True)
            if len(text) < 30:
                continue
            
            # Разбиваем на строки
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            if not lines:
                continue
            
            # Объединяем строки в один текст для поиска
            full_text = ' '.join(lines)
            
            # --- Ищем название ---
            title = ""
            # Ищем заголовок в тегах
            title_tag = block.find(['h1', 'h2', 'h3', 'h4', 'h5', 'strong', 'b'])
            if title_tag:
                title = title_tag.get_text(strip=True)
            else:
                # Берем первую длинную строку
                for line in lines:
                    if len(line) > 10 and not re.search(r'\d+[₽руб]', line):
                        title = line
                        break
            
            if not title or len(title) < 3:
                continue
            
            # --- Ищем место ---
            location = "Не указано"
            # Ищем в строках с адресом
            for line in lines:
                if re.search(r'(?:ул\.|пр\.|пер\.|наб\.|пл\.|ш\.|проспект|улица|переулок|набережная|площадь)', line, re.I):
                    if len(line) > 5:
                        location = line
                        break
                # Ищем названия заведений
                elif re.search(r'(?:бар|кафе|ресторан|клуб|театр|музей|галерея|центр|дворец|парк|зал|студия)', line, re.I):
                    if len(line) > 5 and len(line) < 100:
                        location = line
                        break
            
            # --- Ищем цену ---
            price = "Не указано"
            price_clean = None
            is_free = False
            
            # Проверяем на бесплатно
            if re.search(r'бесплатн[а-я]*|free', full_text, re.I):
                price = "Бесплатно"
                price_clean = 0
                is_free = True
            else:
                # Ищем цену с рублями
                match = re.search(r'(?:от\s*)?(\d+)\s*[₽руб]', full_text)
                if match:
                    price = f"от {match.group(1)} ₽"
                    price_clean = int(match.group(1))
            
            # --- Описание ---
            description = ""
            desc_tag = block.find('p')
            if desc_tag:
                description = desc_tag.get_text(strip=True)[:300]
            
            # Фильтруем мусор
            if not title.startswith('Войти') and not title.startswith('События') and 'меню' not in title.lower():
                events.append({
                    "title": title[:200],
                    "description": description,
                    "location": location[:100],
                    "price_raw": price,
                    "price_min": price_clean,
                    "is_free": is_free,
                    "source": "kaverafisha"
                })
                
        except Exception as e:
            continue
    
    # Удаляем дубликаты
    seen = set()
    unique = []
    for e in events:
        if e['title'] not in seen:
            seen.add(e['title'])
            unique.append(e)
    
    return unique


def main():
    print("=" * 50)
    print("ПАРСЕР АФИШИ — Kaverafisha.ru")
    print("=" * 50)
    
    events = parse_kaverafisha()
    
    if not events:
        print("❌ Мероприятия не найдены")
        return
    
    df = pd.DataFrame(events)
    print(f"✅ Собрано: {len(df)} мероприятий")
    
    # Очистка
    df = df.drop_duplicates(subset=['title'])
    df = df[~df['title'].str.contains('Войти|События|Места|Популярное', na=False)]
    df = df[df['title'].str.len() > 5]
    
    # Фильтруем записи, где название похоже на адрес
    df = df[~df['title'].str.match(r'^[А-Яа-я\s,\.]+\s+\d+[А-Яа-я]?$', na=False)]
    df = df[~df['title'].str.match(r'^[А-Яа-я\s]+(?:ул|пр|пер|наб|пл|ш)\.', na=False)]
    
    print(f"✅ После очистки: {len(df)} мероприятий")
    
    if len(df) == 0:
        print("❌ После очистки не осталось мероприятий")
        return
    
    filename = f"kaverafisha_events_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.csv"
    df.to_csv(filename, index=False, encoding="utf-8-sig")
    print(f"💾 Сохранено: {filename}")
    
    print("\n📋 Результаты:")
    print("-" * 60)
    for _, row in df.iterrows():
        print(f"  {row['title'][:60]}")
        print(f"  📍 {row['location']}")
        print(f"  💰 {row['price_raw']}")
        print("-" * 60)
    
    print("\n📊 Статистика:")
    print(f"  Всего: {len(df)}")
    print(f"  Бесплатных: {len(df[df['is_free'] == True])}")
    print(f"  С ценой: {len(df[df['price_min'].notna()])}")
    print(f"  С местом: {len(df[df['location'] != 'Не указано'])}")
    
    return df


if __name__ == "__main__":
    main()