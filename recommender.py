import pandas as pd
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import MinMaxScaler
from scipy.sparse.linalg import svds
from scipy.sparse import csr_matrix
from database import SessionLocal, Event, User, UserInteraction
import warnings
warnings.filterwarnings("ignore")


# ─── ВЕСА ТИПОВ ВЗАИМОДЕЙСТВИЙ ────────────────────────────
# Положительные сигналы. Оценки 1–5 переводятся в signed feedback:
# 1 -> -2, 2 -> -1, 3 -> 0, 4 -> +1, 5 -> +2.
INTERACTION_WEIGHTS = {
    "view": 0.5,
    "favorite": 2.5,
    "attended": 2.0,
}
RATING_MIN = 1.0
RATING_MAX = 5.0


class HybridRecommender:
    """
    Гибридная рекомендательная система мероприятий.

    Объединяет два компонента:
    1. Коллаборативная фильтрация (SVD) — кто похож на тебя,
       то же понравится и тебе
    2. Контентная фильтрация (TF-IDF + cosine) — рекомендует
       похожие на те, что тебе уже нравились

    Итоговая оценка:
    score = α * CF_score + (1-α) * CBF_score

    α растёт с накоплением истории пользователя:
    - новый пользователь (0 взаимодействий): α = 0 (только контент)
    - опытный пользователь (50+ взаимодействий): α = 0.6
    """

    def __init__(self):
        self.events_df = None
        self.users_df = None
        self.interactions_df = None
        self.interaction_matrix = None
        self.user_factors = None
        self.item_factors = None
        self.tfidf_matrix = None
        self.tfidf_vectorizer = None
        self.content_features = None
        self.is_trained = False
        # Фикс: инициализируем заранее чтобы не было AttributeError
        self.predicted_matrix = None
        self.user_idx = {}
        self.event_idx = {}
        self.user_ids = []
        self.event_ids = []

    # ─── ЗАГРУЗКА ДАННЫХ ──────────────────────────────────
    def load_data(self):
        """Загружает данные из базы данных"""
        print("📊 Загружаю данные из базы...")
        db = SessionLocal()

        try:
            # Мероприятия
            events = db.query(Event).all()
            self.events_df = pd.DataFrame([{
                "id": e.id,
                "title": e.title,
                "description": e.description or "",
                "category": e.category or "other",
                "tags": e.tags or "",
                "venue_title": e.venue_title or "",
                "venue_lat": e.venue_lat,
                "venue_lon": e.venue_lon,
                "price_min": e.price_min or 0,
                "is_free": e.is_free or False,
                "age_restriction": e.age_restriction or "0+",
                "start_datetime": e.start_datetime or "",
            } for e in events], columns=[
                "id", "title", "description", "category", "tags", "venue_title",
                "venue_lat", "venue_lon", "price_min", "is_free", "age_restriction", "start_datetime"
            ])

            # Пользователи
            users = db.query(User).all()
            self.users_df = pd.DataFrame([{
                "id": u.id,
                "name": u.name,
                "user_type": u.user_type,
            } for u in users], columns=["id", "name", "user_type"])

            # Взаимодействия
            interactions = db.query(UserInteraction).all()
            self.interactions_df = pd.DataFrame([{
                "user_id": i.user_id,
                "event_id": i.event_id,
                "interaction_type": i.interaction_type,
                "rating": i.rating,
                "timestamp": i.timestamp,
            } for i in interactions], columns=["user_id", "event_id", "interaction_type", "rating", "timestamp"])

            print(f"   Мероприятий: {len(self.events_df)}")
            print(f"   Пользователей: {len(self.users_df)}")
            print(f"   Взаимодействий: {len(self.interactions_df)}")

        finally:
            db.close()

    # ─── ВЫЧИСЛЕНИЕ ВЕСОВ ВЗАИМОДЕЙСТВИЙ ──────────────────
    @staticmethod
    def interaction_signal(interaction_type, rating=None):
        """Преобразует действие в signed feedback; отрицательная оценка не становится интересом."""
        if interaction_type == "rated":
            if rating is None:
                return 0.0
            try:
                rating = float(rating)
            except (TypeError, ValueError):
                return 0.0
            rating = min(RATING_MAX, max(RATING_MIN, rating))
            return rating - 3.0
        return float(INTERACTION_WEIGHTS.get(interaction_type, 0.0))

    def compute_interaction_weights(self, interactions_df=None):
        """Одна итоговая обратная связь на пару пользователь–мероприятие.

        Повторные просмотры не накапливаются бесконечно. Для рейтинга сохраняется
        последний выставленный рейтинг, а другие действия учитываются по одному.
        """
        columns = ["user_id", "event_id", "interaction_type", "rating", "timestamp"]
        source_df = self.interactions_df if interactions_df is None else interactions_df
        if source_df is None or source_df.empty:
            return pd.DataFrame(columns=["user_id", "event_id", "weight"])

        df = source_df.copy()
        for col in columns:
            if col not in df.columns:
                df[col] = None
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        rows = []
        for (uid, eid), group in df.groupby(["user_id", "event_id"], dropna=False):
            score = 0.0
            for kind, kind_group in group.groupby("interaction_type", dropna=False):
                if kind == "rated":
                    rated = kind_group.sort_values("timestamp", na_position="first")
                    signal = self.interaction_signal("rated", rated.iloc[-1]["rating"])
                else:
                    signal = self.interaction_signal(kind)
                score += signal
            # Ограничиваем вклад одной пары, чтобы много разных действий не доминировали.
            score = float(np.clip(score, -3.0, 5.0))
            rows.append({"user_id": uid, "event_id": eid, "weight": score})
        return pd.DataFrame(rows, columns=["user_id", "event_id", "weight"])

    # ─── ОБУЧЕНИЕ: КОЛЛАБОРАТИВНАЯ ФИЛЬТРАЦИЯ (SVD) ───────
    def train_collaborative(self, n_factors=50):
        """
        Обучает SVD-модель на матрице взаимодействий.
        n_factors — количество латентных факторов.
        """
        print("\n🔧 Обучаю коллаборативный компонент (SVD)...")

        if (self.interactions_df is None or len(self.interactions_df) < 10
                or self.users_df is None or self.users_df.empty
                or self.events_df is None or self.events_df.empty):
            print("   ⚠️  Недостаточно данных для SVD — использую контентную модель")
            return

        weighted = self.compute_interaction_weights()

        # Создаём индексы пользователей и мероприятий
        user_ids = self.users_df["id"].tolist()
        event_ids = self.events_df["id"].tolist()
        user_idx = {uid: i for i, uid in enumerate(user_ids)}
        event_idx = {eid: i for i, eid in enumerate(event_ids)}

        self.user_idx = user_idx
        self.event_idx = event_idx
        self.user_ids = user_ids
        self.event_ids = event_ids

        # Строим матрицу пользователь × мероприятие
        n_users = len(user_ids)
        n_events = len(event_ids)
        matrix = np.zeros((n_users, n_events))

        for _, row in weighted.iterrows():
            u = user_idx.get(int(row["user_id"]))
            e = event_idx.get(int(row["event_id"]))
            if u is not None and e is not None:
                matrix[u][e] = row["weight"]

        self.interaction_matrix = matrix

        # SVD разложение
        # k — количество латентных факторов (не больше min размеров матрицы - 1)
        k = min(n_factors, min(n_users, n_events) - 1)
        if k < 1:
            print("   ⚠️  Матрица слишком маленькая для SVD")
            return

        sparse_matrix = csr_matrix(matrix)
        try:
            U, sigma, Vt = svds(sparse_matrix, k=k)
        except (ValueError, RuntimeError) as exc:
            print(f"   ⚠️  SVD не обучен: {exc}")
            self.predicted_matrix = None
            return
        order = np.argsort(sigma)[::-1]
        U, sigma, Vt = U[:, order], sigma[order], Vt[order, :]

        # Сохраняем факторы
        self.user_factors = U
        self.sigma = sigma
        self.item_factors = Vt.T

        # Восстановленная матрица предсказаний
        self.predicted_matrix = np.dot(
            np.dot(U, np.diag(sigma)), Vt
        )

        print(f"   ✅ SVD обучен: {n_users} пользователей × {n_events} мероприятий, k={k}")

    # ─── ОБУЧЕНИЕ: КОНТЕНТНАЯ ФИЛЬТРАЦИЯ (TF-IDF) ─────────
    def train_content(self):
        """
        Строит TF-IDF матрицу описаний мероприятий
        и числовые признаки для контентной фильтрации.
        """
        print("\n🔧 Строю контентный компонент (TF-IDF)...")

        df = self.events_df.copy()
        if df.empty:
            self.tfidf_matrix = None
            self.content_features = np.empty((0, 2))
            print("   ⚠️  Каталог мероприятий пуст")
            return

        # Текстовый признак: объединяем название + описание + теги + категорию
        df["text_features"] = (
            df["title"].fillna("") + " " +
            df["title"].fillna("") + " " +  # удваиваем название (важнее)
            df["category"].fillna("") + " " +
            df["tags"].fillna("") + " " +
            df["description"].fillna("").str[:300]
        )

        # TF-IDF векторизация
        self.tfidf_vectorizer = TfidfVectorizer(
            max_features=500,
            ngram_range=(1, 2),
            min_df=1,
            analyzer="word",
        )
        self.tfidf_matrix = self.tfidf_vectorizer.fit_transform(
            df["text_features"]
        )

        # Числовые признаки
        scaler = MinMaxScaler()

        # Цена нормализованная
        price_norm = scaler.fit_transform(
            df[["price_min"]].fillna(0)
        )

        # Бесплатно = 1, платно = 0
        is_free = df["is_free"].fillna(False).astype(int).values.reshape(-1, 1)

        # Числовые признаки объединяем
        self.content_features = np.hstack([
            price_norm,
            is_free,
        ])

        print(f"   ✅ TF-IDF: {self.tfidf_matrix.shape[0]} мероприятий, {self.tfidf_matrix.shape[1]} признаков")

    # ─── ГЛАВНЫЙ МЕТОД: ОБУЧЕНИЕ ───────────────────────────
    def train(self):
        """Полный цикл обучения модели"""
        print("\n" + "=" * 50)
        print("🚀 ОБУЧЕНИЕ РЕКОМЕНДАТЕЛЬНОЙ МОДЕЛИ")
        print("=" * 50)

        self.load_data()
        self.train_collaborative()
        self.train_content()
        self.is_trained = True

        print("\n✅ Модель обучена и готова к работе!")

    # ─── ДИНАМИЧЕСКИЙ КОЭФФИЦИЕНТ α ───────────────────────
    def get_alpha(self, user_id: int) -> float:
        """Плавно повышает долю CF по мере накопления уникальных взаимодействий."""
        if self.interactions_df is None or self.interactions_df.empty:
            return 0.0
        user_rows = self.interactions_df[
            self.interactions_df["user_id"].astype(str) == str(user_id)
        ]
        n_interactions = user_rows[["event_id", "interaction_type"]].astype(str).drop_duplicates().shape[0]
        if n_interactions == 0:
            return 0.0
        # Достигает около 0.6 примерно после 30 уникальных взаимодействий.
        alpha = 0.6 * (1.0 - np.exp(-n_interactions / 12.0))
        return round(float(min(0.6, alpha)), 3)

    # ─── КОЛЛАБОРАТИВНЫЕ ОЦЕНКИ ───────────────────────────
    def get_cf_scores(self, user_id: int) -> dict:
        """
        Возвращает оценки коллаборативной фильтрации.
        Если пользователь новый или модель не обучена — возвращает пустой dict,
        тогда алгоритм использует только контентный компонент.
        """
        if self.predicted_matrix is None:
            return {}

        # Фикс проблемы 3: новый пользователь не в матрице
        u_idx = self.user_idx.get(user_id)
        if u_idx is None:
            u_idx = self.user_idx.get(str(user_id))
        if u_idx is None:
            # DB/JSON-клиенты иногда смешивают int и str в идентификаторах.
            u_idx = next((idx for uid, idx in self.user_idx.items() if str(uid) == str(user_id)), None)
        if u_idx is None:
            return {}

        scores = self.predicted_matrix[u_idx].copy()

        # Нормализуем в диапазон [0, 1]
        if scores.max() > scores.min():
            scores = (scores - scores.min()) / (scores.max() - scores.min())

        return {self.event_ids[i]: float(scores[i]) for i in range(len(scores))}

    # ─── КОНТЕНТНЫЕ ОЦЕНКИ ────────────────────────────────
    def _content_scores_from_interactions(self, interactions_df):
        """Считает профиль как позитивные признаки минус негативные."""
        if self.tfidf_matrix is None or self.events_df is None or self.events_df.empty:
            return {}
        event_ids = self.events_df["id"].tolist()
        event_id_to_idx = {str(eid): i for i, eid in enumerate(event_ids)}
        if interactions_df is None or interactions_df.empty:
            return self.get_cold_start_scores()

        temp = interactions_df.copy()
        if "weight" not in temp.columns:
            temp["weight"] = temp.apply(
                lambda r: self.interaction_signal(r.get("interaction_type"), r.get("rating")), axis=1
            )
        # Суммируем положительные и отрицательные сигналы отдельно, чтобы низкий рейтинг
        # отталкивал от похожих событий, а не усиливал их.
        profile = np.zeros(self.tfidf_matrix.shape[1], dtype=float)
        total_abs_weight = 0.0
        for _, row in temp.iterrows():
            idx = event_id_to_idx.get(str(row.get("event_id")))
            weight = float(row.get("weight", 0.0) or 0.0)
            if idx is None or weight == 0:
                continue
            profile += weight * self.tfidf_matrix[idx].toarray().ravel()
            total_abs_weight += abs(weight)
        if total_abs_weight == 0 or np.linalg.norm(profile) == 0:
            return self.get_cold_start_scores()

        similarities = cosine_similarity(csr_matrix(profile.reshape(1, -1)), self.tfidf_matrix)[0]
        # cosine находится в [-1, 1]; переводим в [0, 1] без min-max растягивания каждого пользователя.
        similarities = np.clip((similarities + 1.0) / 2.0, 0.0, 1.0)
        return {event_ids[i]: float(similarities[i]) for i in range(len(event_ids))}

    def get_cbf_scores(self, user_id: int) -> dict:
        """Контентные оценки по истории, сохранённой при последней загрузке данных."""
        if self.interactions_df is None:
            return self.get_cold_start_scores()
        user_ints = self.interactions_df[self.interactions_df["user_id"].astype(str) == str(user_id)]
        return self._content_scores_from_interactions(user_ints)

    def get_cold_start_scores(self) -> dict:
        """Ранжирование без истории: популярность по положительным сигналам + небольшая новизна.

        Если данных о взаимодействиях ещё нет, возвращает одинаковый нейтральный балл;
        в таком случае персонализировать порядок объективно не по чему.
        """
        if self.events_df is None or self.events_df.empty:
            return {}
        event_ids = self.events_df["id"].tolist()
        popularity = {eid: 0.0 for eid in event_ids}
        if self.interactions_df is not None and not self.interactions_df.empty:
            weighted = self.compute_interaction_weights()
            for _, row in weighted.iterrows():
                eid = row["event_id"]
                if eid in popularity and row["weight"] > 0:
                    popularity[eid] += float(row["weight"])
        values = np.array([popularity[eid] for eid in event_ids], dtype=float)
        if values.max() > values.min():
            values = (values - values.min()) / (values.max() - values.min())
        else:
            values = np.full(len(event_ids), 0.5, dtype=float)
        # Небольшой бонус мероприятиям с корректной будущей датой, без исключения дат здесь:
        # строгий фильтр актуальности должен применяться отдельным слоем API/планировщика.
        return {event_ids[i]: float(values[i]) for i in range(len(event_ids))}

    # ─── ОНЛАЙН ОБНОВЛЕНИЕ ПРОФИЛЯ ────────────────────────
    def get_realtime_cbf_scores(self, user_id: int) -> dict:
        """Пересчитывает контентный профиль по актуальной истории из БД."""
        if self.tfidf_matrix is None:
            return {}
        db = SessionLocal()
        try:
            interactions = db.query(UserInteraction).filter(
                UserInteraction.user_id == user_id
            ).order_by(UserInteraction.timestamp.asc()).all()
            rows = [{
                "user_id": i.user_id,
                "event_id": i.event_id,
                "interaction_type": i.interaction_type,
                "rating": i.rating,
                "timestamp": i.timestamp,
            } for i in interactions]
        except Exception:
            # Ошибка БД не должна оставлять сессию открытой.
            return self.get_cbf_scores(user_id)
        finally:
            db.close()

        if not rows:
            return self.get_cold_start_scores()
        fresh_df = pd.DataFrame(rows)
        # Единая логика агрегации для онлайн- и офлайн-расчёта.
        weighted = self.compute_interaction_weights(fresh_df)
        return self._content_scores_from_interactions(weighted)

    # ─── ГЛАВНЫЙ МЕТОД: РЕКОМЕНДАЦИИ ──────────────────────
    def get_category_affinity_scores(self, user_id: int) -> dict:
        """Explicit category preference from attended/favorite/high-rated events.

        TF-IDF may fail to connect sports events with differently worded descriptions;
        this category layer adds a controlled boost to semantically related categories.
        """
        if self.events_df is None or self.events_df.empty or self.interactions_df is None:
            return {}
        aliases = {
            "sport": ("sport", "спорт", "матч", "соревнован", "футбол", "хоккей", "баскетбол",
                      "волейбол", "теннис", "бокс", "регби", "турнир", "забег", "марафон",
                      "гонк", "фитнес", "лыж", "конькобеж", "единоборств"),
            "music": ("concert", "концерт", "музык", "music", "оркестр", "джаз", "опера"),
            "theater": ("theater", "театр", "спектакл", "балет", "драматург"),
            "exhibition": ("exhibition", "выстав", "экспозиц", "музей", "галерея"),
            "cinema": ("cinema", "кино", "фильм", "кинопоказ"),
            "lecture": ("lecture", "лекц", "дискусс", "семинар"),
            "festival": ("festival", "фестивал"),
        }

        def groups_for(row):
            text = " ".join(str(row.get(key) or "") for key in ("category", "title", "tags", "description")).casefold()
            return {group for group, terms in aliases.items() if any(term in text for term in terms)}

        event_by_id = {str(row["id"]): row for _, row in self.events_df.iterrows()}
        positive_groups = set()
        user_rows = self.interactions_df[self.interactions_df["user_id"].astype(str) == str(user_id)]
        for _, interaction in user_rows.iterrows():
            kind = str(interaction.get("interaction_type") or "").casefold()
            positive = kind in {"attended", "favorite"}
            if kind == "rated":
                try:
                    positive = float(interaction.get("rating") or 0) >= 4
                except (TypeError, ValueError):
                    positive = False
            if not positive:
                continue
            event = event_by_id.get(str(interaction.get("event_id")))
            if event is not None:
                positive_groups.update(groups_for(event))
        if not positive_groups:
            return {}
        return {
            str(row["id"]): (1.0 if groups_for(row) & positive_groups else 0.0)
            for _, row in self.events_df.iterrows()
        }

    def recommend(
        self,
        user_id: int,
        n: int = 10,
        exclude_seen: bool = True,
        category_filter: str = None,
        max_price: int = None,
        realtime: bool = True,
    ) -> list:
        """
        Возвращает топ-N рекомендаций для пользователя.

        user_id         — ID пользователя
        n               — количество рекомендаций
        exclude_seen    — исключить только избранное и посещённые (не просмотры)
        category_filter — фильтр по категории
        max_price       — максимальная цена
        realtime        — использовать актуальные данные из БД для CBF
        """
        if not self.is_trained:
            raise RuntimeError("Модель не обучена. Запустите train() сначала.")
        n = max(1, min(int(n or 10), 500))

        # Определяем α для этого пользователя. Для нового пользователя CF не применяется.
        alpha = self.get_alpha(user_id)

        # Получаем оценки коллаборативного компонента из обученной матрицы
        cf_scores = self.get_cf_scores(user_id)

        # Контентный компонент: если realtime=True — берём актуальные данные из БД
        if realtime:
            cbf_scores = self.get_realtime_cbf_scores(user_id)
        else:
            cbf_scores = self.get_cbf_scores(user_id)

        # Фикс проблемы 2: исключаем только избранное и посещённые,
        # НЕ просмотры — иначе при маленькой базе рекомендации кончаются
        seen_event_ids = set()
        if exclude_seen and self.interactions_df is not None and len(self.interactions_df) > 0:
            user_rows = self.interactions_df[self.interactions_df["user_id"].astype(str) == str(user_id)]
            strong_ids = user_rows[user_rows["interaction_type"].isin(["favorite", "attended"])]["event_id"].tolist()
            low_rated_ids = user_rows[(user_rows["interaction_type"] == "rated") & (pd.to_numeric(user_rows["rating"], errors="coerce") <= 2)]["event_id"].tolist()
            seen_event_ids = {str(value) for value in (strong_ids + low_rated_ids)}

        # Вычисляем итоговые оценки. Категориальный профиль дополняет TF-IDF/CF:
        # посещённые спортивные события повышают приоритет других видов спорта.
        category_scores = self.get_category_affinity_scores(user_id)
        results = []
        for _, event in self.events_df.iterrows():
            eid = event["id"]

            # Пропускаем только сильно взаимодействованные
            if str(eid) in seen_event_ids:
                continue

            # Фильтр по категории
            if category_filter:
                if category_filter.lower() not in (event["category"] or "").lower():
                    continue

            # Фильтр по цене
            if max_price is not None:
                if not event["is_free"] and event["price_min"] > max_price:
                    continue

            # Гибридная оценка: если нет CF (новый пользователь) — только CBF
            # Нормализация ключей исключает потерю баллов при int/str ID.
            cf = cf_scores.get(eid, cf_scores.get(str(eid), 0.0))
            cbf = cbf_scores.get(eid, cbf_scores.get(str(eid), 0.5))

            if cf_scores:
                hybrid_score = alpha * cf + (1 - alpha) * cbf
            else:
                # Новый пользователь — только контентный компонент
                hybrid_score = cbf

            category_affinity = float(category_scores.get(str(eid), 0.0))
            if category_scores:
                hybrid_score = 0.65 * float(hybrid_score) + 0.35 * category_affinity

            results.append({
                "event_id": eid,
                "title": event["title"],
                "category": event["category"],
                "venue": event["venue_title"],
                "start_datetime": event["start_datetime"],
                "price_min": int(event["price_min"]),
                "is_free": bool(event["is_free"]),
                "score": round(hybrid_score, 4),
                "category_affinity_score": round(category_affinity, 4),
                "alpha": alpha,
                "cf_score": round(cf, 4),
                "cbf_score": round(cbf, 4),
            })

        # Сортируем по убыванию оценки
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:n]

    # ─── ДЕМОНСТРАЦИЯ ─────────────────────────────────────
    def demo(self):
        """
        Показывает рекомендации для всех тестовых пользователей.
        """
        print("\n" + "=" * 50)
        print("🎯 ДЕМОНСТРАЦИЯ РЕКОМЕНДАЦИЙ")
        print("=" * 50)

        if self.users_df is None or len(self.users_df) == 0:
            print("⚠️  Нет пользователей в базе")
            return

        for _, user in self.users_df.iterrows():
            uid = user["id"]
            alpha = self.get_alpha(uid)
            n_ints = len(
                self.interactions_df[self.interactions_df["user_id"] == uid]
            ) if self.interactions_df is not None else 0

            print(f"\n👤 {user['name']} (id={uid}, α={alpha}, взаимодействий={n_ints})")
            print("-" * 45)

            recs = self.recommend(uid, n=5)
            if not recs:
                print("   Нет рекомендаций")
                continue

            for i, rec in enumerate(recs, 1):
                price_str = "Бесплатно" if rec["is_free"] else f"от {rec['price_min']} ₽"
                print(f"   {i}. {rec['title'][:45]}")
                print(f"      📂 {rec['category']} | 💰 {price_str}")
                print(f"      📊 score={rec['score']} (CF={rec['cf_score']}, CBF={rec['cbf_score']})")


if __name__ == "__main__":
    recommender = HybridRecommender()
    recommender.train()
    recommender.demo()
