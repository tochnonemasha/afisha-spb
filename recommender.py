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
INTERACTION_WEIGHTS = {
    "view":     1.0,
    "favorite": 3.0,
    "attended": 5.0,
    "rated":    None,  # используется само значение оценки
}


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
            } for e in events])

            # Пользователи
            users = db.query(User).all()
            self.users_df = pd.DataFrame([{
                "id": u.id,
                "name": u.name,
                "user_type": u.user_type,
            } for u in users])

            # Взаимодействия
            interactions = db.query(UserInteraction).all()
            self.interactions_df = pd.DataFrame([{
                "user_id": i.user_id,
                "event_id": i.event_id,
                "interaction_type": i.interaction_type,
                "rating": i.rating,
                "timestamp": i.timestamp,
            } for i in interactions])

            print(f"   Мероприятий: {len(self.events_df)}")
            print(f"   Пользователей: {len(self.users_df)}")
            print(f"   Взаимодействий: {len(self.interactions_df)}")

        finally:
            db.close()

    # ─── ВЫЧИСЛЕНИЕ ВЕСОВ ВЗАИМОДЕЙСТВИЙ ──────────────────
    def compute_interaction_weights(self):
        """
        Переводит типы взаимодействий в числовые веса.
        view=1, favorite=3, attended=5, rated=значение_оценки
        """
        df = self.interactions_df.copy()

        def get_weight(row):
            itype = row["interaction_type"]
            if itype == "rated" and row["rating"] is not None:
                return float(row["rating"])
            return INTERACTION_WEIGHTS.get(itype, 1.0)

        df["weight"] = df.apply(get_weight, axis=1)

        # Если один пользователь взаимодействовал с одним
        # мероприятием несколько раз — берём максимальный вес
        df = df.groupby(["user_id", "event_id"])["weight"].max().reset_index()
        return df

    # ─── ОБУЧЕНИЕ: КОЛЛАБОРАТИВНАЯ ФИЛЬТРАЦИЯ (SVD) ───────
    def train_collaborative(self, n_factors=50):
        """
        Обучает SVD-модель на матрице взаимодействий.
        n_factors — количество латентных факторов.
        """
        print("\n🔧 Обучаю коллаборативный компонент (SVD)...")

        if len(self.interactions_df) < 10:
            print("   ⚠️  Мало взаимодействий для SVD — пропускаю")
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
        U, sigma, Vt = svds(sparse_matrix, k=k)

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
        """
        Вычисляет коэффициент α для пользователя.
        α = 0 для новых пользователей (только контент)
        α → 0.6 по мере накопления истории (гибрид)
        """
        if self.interactions_df is None or len(self.interactions_df) == 0:
            return 0.0

        n_interactions = len(
            self.interactions_df[self.interactions_df["user_id"] == user_id]
        )

        # Логистическая функция: плавный рост от 0 до 0.6
        alpha = 0.6 / (1 + np.exp(-0.1 * (n_interactions - 20)))
        return round(alpha, 3)

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
            return {}

        scores = self.predicted_matrix[u_idx].copy()

        # Нормализуем в диапазон [0, 1]
        if scores.max() > scores.min():
            scores = (scores - scores.min()) / (scores.max() - scores.min())

        return {self.event_ids[i]: float(scores[i]) for i in range(len(scores))}

    # ─── КОНТЕНТНЫЕ ОЦЕНКИ ────────────────────────────────
    def get_cbf_scores(self, user_id: int) -> dict:
        """
        Возвращает оценки контентной фильтрации.
        Строит профиль пользователя как взвешенное среднее
        векторов мероприятий с которыми он взаимодействовал.
        """
        if self.tfidf_matrix is None:
            return {}

        # Взаимодействия пользователя
        user_ints = self.interactions_df[
            self.interactions_df["user_id"] == user_id
        ]

        if len(user_ints) == 0:
            # Новый пользователь — возвращаем равные оценки
            n = len(self.events_df)
            return {eid: 0.5 for eid in self.events_df["id"].tolist()}

        # Вычисляем веса взаимодействий
        weighted = self.compute_interaction_weights()
        user_weighted = weighted[weighted["user_id"] == user_id]

        # Строим профиль пользователя
        profile_vector = np.zeros(self.tfidf_matrix.shape[1])
        total_weight = 0

        event_id_to_idx = {eid: i for i, eid in enumerate(self.events_df["id"].tolist())}

        for _, row in user_weighted.iterrows():
            eid = int(row["event_id"])
            e_idx = event_id_to_idx.get(eid)
            if e_idx is not None:
                weight = row["weight"]
                profile_vector += weight * self.tfidf_matrix[e_idx].toarray()[0]
                total_weight += weight

        if total_weight > 0:
            profile_vector /= total_weight

        # Косинусное подобие профиля со всеми мероприятиями
        profile_sparse = csr_matrix(profile_vector)
        similarities = cosine_similarity(
            profile_sparse, self.tfidf_matrix
        )[0]

        # Нормализуем
        if similarities.max() > similarities.min():
            similarities = (
                (similarities - similarities.min()) /
                (similarities.max() - similarities.min())
            )

        event_ids = self.events_df["id"].tolist()
        return {event_ids[i]: float(similarities[i]) for i in range(len(similarities))}

    # ─── ОНЛАЙН ОБНОВЛЕНИЕ ПРОФИЛЯ ────────────────────────
    def get_realtime_cbf_scores(self, user_id: int) -> dict:
        """
        Получает актуальные взаимодействия из БД прямо сейчас
        (не из кэша interactions_df) и пересчитывает контентный профиль.
        Это решает проблему задержки — рекомендации меняются сразу
        после действия пользователя, не ожидая переобучения.
        """
        if self.tfidf_matrix is None:
            return {}

        try:
            db = SessionLocal()
            interactions = db.query(UserInteraction).filter(
                UserInteraction.user_id == user_id
            ).all()
            db.close()
        except Exception:
            return self.get_cbf_scores(user_id)

        if not interactions:
            n = len(self.events_df)
            return {eid: 0.5 for eid in self.events_df["id"].tolist()}

        event_id_to_idx = {eid: i for i, eid in enumerate(self.events_df["id"].tolist())}
        profile_vector = np.zeros(self.tfidf_matrix.shape[1])
        total_weight = 0

        for interaction in interactions:
            itype = interaction.interaction_type
            if itype == "rated" and interaction.rating:
                weight = float(interaction.rating)
            else:
                weight = INTERACTION_WEIGHTS.get(itype, 1.0)

            e_idx = event_id_to_idx.get(interaction.event_id)
            if e_idx is not None:
                profile_vector += weight * self.tfidf_matrix[e_idx].toarray()[0]
                total_weight += weight

        if total_weight > 0:
            profile_vector /= total_weight

        profile_sparse = csr_matrix(profile_vector)
        similarities = cosine_similarity(profile_sparse, self.tfidf_matrix)[0]

        if similarities.max() > similarities.min():
            similarities = (similarities - similarities.min()) / (similarities.max() - similarities.min())

        event_ids = self.events_df["id"].tolist()
        return {event_ids[i]: float(similarities[i]) for i in range(len(similarities))}

    # ─── ГЛАВНЫЙ МЕТОД: РЕКОМЕНДАЦИИ ──────────────────────
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

        # Определяем α для этого пользователя
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
            strong_interactions = self.interactions_df[
                (self.interactions_df["user_id"] == user_id) &
                (self.interactions_df["interaction_type"].isin(["favorite", "attended"]))
            ]["event_id"].tolist()
            seen_event_ids = set(strong_interactions)

        # Вычисляем итоговые оценки
        results = []
        for _, event in self.events_df.iterrows():
            eid = event["id"]

            # Пропускаем только сильно взаимодействованные
            if eid in seen_event_ids:
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
            cf = cf_scores.get(eid, 0.0)
            cbf = cbf_scores.get(eid, 0.5)

            if cf_scores:
                hybrid_score = alpha * cf + (1 - alpha) * cbf
            else:
                # Новый пользователь — только контентный компонент
                hybrid_score = cbf

            results.append({
                "event_id": eid,
                "title": event["title"],
                "category": event["category"],
                "venue": event["venue_title"],
                "start_datetime": event["start_datetime"],
                "price_min": int(event["price_min"]),
                "is_free": bool(event["is_free"]),
                "score": round(hybrid_score, 4),
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
