from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("RADAR_DB_PATH", str(BASE_DIR / "radar.db"))).resolve()
load_dotenv(BASE_DIR / ".env")

def load_flask_secret() -> str:
    """Load a stable secret from the environment; use a local file only in development."""
    configured = os.getenv("FLASK_SECRET_KEY", "").strip()
    if configured:
        return configured

    # Railway containers should not depend on writing secrets into the app directory.
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("PORT"):
        return secrets.token_hex(32)

    secret_path = BASE_DIR / ".flask_secret"
    if secret_path.exists():
        value = secret_path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_hex(32)
    secret_path.write_text(value, encoding="utf-8")
    return value


app = Flask(__name__)
app.secret_key = load_flask_secret()
app.config.update(
    JSON_AS_ASCII=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,  # local HTTP development; enable HTTPS in production
    MAX_CONTENT_LENGTH=1_000_000,
)

API_TIMEOUT = 20
CACHE_TTL_SECONDS = 600
BOOKS_PAGE_SIZE = 40
OTHER_PAGE_SIZE = 40
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
_NEURAL_MODEL_CACHE: dict[str, Any] | None = None
_NEURAL_MODEL_SIGNATURE: tuple[int, int, int] | None = None
_NEURAL_MODEL_LOCK = threading.Lock()

CATEGORIES = {
    "Livros", "Músicas", "Séries", "Filmes", "Jogos", "Mangás", "HQs"
}

SOURCE_BY_CATEGORY = {
    "Livros": "google-books",
    "Músicas": "lastfm",
    "Séries": "tmdb",
    "Filmes": "tmdb",
    "Jogos": "rawg",
    "Mangás": "anilist",
    "HQs": "comicvine",
}


class MissingConfiguration(Exception):
    """A provider needs an API key/token that is not configured locally."""


def cached_call(key: str, callback: Callable[[], Any]) -> Any:
    """Cache successful external responses briefly to reduce repeated calls."""
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]

    result = callback()
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, result)
    return result


def make_cache_key(provider: str, query: str, page: int = 1) -> str:
    value = f"{provider}:{query.strip().casefold()}:page:{page}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def request_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
) -> Any:
    """Request JSON and retry HTTP 429 a small number of times."""
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "RadarCultural/1.0 (local cultural discovery project)",
    }
    if headers:
        request_headers.update(headers)

    response = None
    for attempt in range(3):
        response = requests.request(
            method=method,
            url=url,
            params=params,
            headers=request_headers,
            json=json_body,
            timeout=(5, API_TIMEOUT),
        )
        if response.status_code != 429 or attempt == 2:
            break

        retry_after = response.headers.get("Retry-After", "").strip()
        try:
            wait_seconds = float(retry_after)
        except (TypeError, ValueError):
            wait_seconds = 2 ** attempt
        # Respect a short Retry-After, but never sleep for an excessive period.
        time.sleep(max(1.0, min(wait_seconds, 5.0)))

    assert response is not None
    response.raise_for_status()

    try:
        return response.json()
    except ValueError as error:
        raise RuntimeError("A API respondeu em um formato inesperado (não JSON).") from error


def clean_text(value: Any, fallback: str = "") -> str:
    """Remove markup from provider descriptions before returning it to the UI."""
    if value is None:
        return fallback
    text = str(value)
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1800] if text else fallback


def secure_https(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    value = value.replace("http://", "https://", 1)
    return value if value.startswith("https://") else None


def normalized_item(
    *, source: str, external_id: Any, title: str, creator: str, category: str,
    year: Any = None, vibe: str = "", description: str = "",
    image: str | None = None, url: str | None = None,
    external_rating: Any = None,
) -> dict[str, Any]:
    item_id = str(external_id)
    return {
        "id": f"{source}:{item_id}",
        "externalId": item_id,
        "source": source,
        "title": clean_text(title, "Sem título"),
        "creator": clean_text(creator, "Informação não disponível"),
        "category": category,
        "year": str(year)[:4] if year else "—",
        "vibe": clean_text(vibe, category),
        "description": clean_text(description, "Descrição não disponível."),
        "image": secure_https(image),
        "url": secure_https(url),
        "externalRating": external_rating,
    }


# -----------------------------------------------------------------------------
# CATALOG PROVIDERS: when q is empty, each adapter loads a real provider catalog.
# When q has text, it performs a search in that same provider.
# -----------------------------------------------------------------------------

def search_books(query: str, page: int = 1) -> tuple[list[dict[str, Any]], bool]:
    """Google Books: maxResults is capped at 40 per request; startIndex paginates."""
    # Consultas públicas do Google Books podem ser feitas sem API key.
    # Se a chave estiver configurada, ela identifica o projeto/cota do usuário.
    google_key = os.getenv("GOOGLE_BOOKS_API_KEY", "").strip()

    start_index = (page - 1) * BOOKS_PAGE_SIZE
    params: dict[str, Any] = {
        "q": query if query else "subject:fiction",
        "maxResults": BOOKS_PAGE_SIZE,
        "startIndex": start_index,
        "printType": "books",
        "orderBy": "relevance" if query else "newest",
    }
    if google_key:
        params["key"] = google_key
    data = cached_call(
        make_cache_key("google-books", query or "browse-fiction", page),
        lambda: request_json("https://www.googleapis.com/books/v1/volumes", params=params),
    )

    results = []
    for book in data.get("items", []):
        info = book.get("volumeInfo") or {}
        authors = info.get("authors") or []
        links = info.get("imageLinks") or {}
        cover = links.get("extraLarge") or links.get("large") or links.get("medium") or links.get("thumbnail") or links.get("smallThumbnail")
        categories = info.get("categories") or []
        results.append(normalized_item(
            source="google-books",
            external_id=book.get("id", "unknown"),
            title=info.get("title", "Sem título"),
            creator=", ".join(authors) if authors else "Autor não informado",
            category="Livros",
            year=info.get("publishedDate"),
            vibe=", ".join(categories[:2]) if categories else "Livro",
            description=info.get("description", "Descrição não disponível."),
            image=cover,
            url=info.get("infoLink"),
            external_rating=info.get("averageRating"),
        ))

    total_items = int(data.get("totalItems") or 0)
    has_more = start_index + len(data.get("items", [])) < total_items
    return results, has_more


def search_tmdb(query: str, category: str, page: int = 1) -> tuple[list[dict[str, Any]], bool]:
    token = os.getenv("TMDB_READ_ACCESS_TOKEN", "").strip()
    if not token:
        raise MissingConfiguration("Configure TMDB_READ_ACCESS_TOKEN no arquivo .env.")

    media_type = "movie" if category == "Filmes" else "tv"
    endpoint = f"https://api.themoviedb.org/3/search/{media_type}" if query else f"https://api.themoviedb.org/3/{media_type}/popular"
    headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
    params = {"language": "pt-BR", "page": page}
    if query:
        params.update({"query": query, "include_adult": "false"})

    data = cached_call(
        make_cache_key(f"tmdb-{media_type}", query or "browse-popular", page),
        lambda: request_json(endpoint, params=params, headers=headers),
    )

    results = []
    for entry in data.get("results", []):
        title = entry.get("title") if media_type == "movie" else entry.get("name")
        release = entry.get("release_date") if media_type == "movie" else entry.get("first_air_date")
        poster_path = entry.get("poster_path")
        poster = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
        tmdb_id = entry.get("id")
        results.append(normalized_item(
            source="tmdb",
            external_id=f"{media_type}:{tmdb_id}",
            title=title or "Sem título",
            creator="Filme" if media_type == "movie" else "Série",
            category=category,
            year=release,
            vibe=f"Nota TMDB: {entry.get('vote_average', 0):.1f}/10" if entry.get("vote_average") else category,
            description=entry.get("overview", "Sinopse não disponível."),
            image=poster,
            url=f"https://www.themoviedb.org/{media_type}/{tmdb_id}" if tmdb_id else None,
            external_rating=entry.get("vote_average"),
        ))
    return results, page < int(data.get("total_pages") or page)


def search_games(query: str, page: int = 1) -> tuple[list[dict[str, Any]], bool]:
    api_key = os.getenv("RAWG_API_KEY", "").strip()
    if not api_key:
        raise MissingConfiguration("Configure RAWG_API_KEY no arquivo .env.")

    params: dict[str, Any] = {"key": api_key, "page_size": OTHER_PAGE_SIZE, "page": page}
    if query:
        params.update({"search": query, "search_precise": "true"})
    else:
        params["ordering"] = "-added"

    data = cached_call(
        make_cache_key("rawg", query or "browse-recently-added", page),
        lambda: request_json("https://api.rawg.io/api/games", params=params),
    )

    results = []
    for game in data.get("results", []):
        genres = [g.get("name", "") for g in game.get("genres", []) if g.get("name")]
        developers = [d.get("name", "") for d in game.get("developers", []) if d.get("name")]
        game_id = game.get("id")
        slug = game.get("slug")
        results.append(normalized_item(
            source="rawg",
            external_id=game_id,
            title=game.get("name", "Sem título"),
            creator=", ".join(developers[:2]) if developers else "Desenvolvedora não informada",
            category="Jogos",
            year=game.get("released"),
            vibe=", ".join(genres[:2]) if genres else "Jogo",
            description=game.get("description_raw") or game.get("description") or "Descrição não disponível.",
            image=game.get("background_image"),
            url=f"https://rawg.io/games/{slug}" if slug else None,
            external_rating=game.get("rating"),
        ))
    return results, bool(data.get("next"))


def search_manga(query: str, page: int = 1) -> tuple[list[dict[str, Any]], bool]:
    graphql = """
    query ($search: String, $page: Int!, $perPage: Int!) {
      Page(page: $page, perPage: $perPage) {
        pageInfo { hasNextPage }
        media(
          search: $search
          type: MANGA
          sort: [POPULARITY_DESC, SCORE_DESC]
          isAdult: false
        ) {
          id
          title { romaji english native }
          description(asHtml: false)
          startDate { year }
          coverImage { extraLarge large }
          genres
          averageScore
          siteUrl
        }
      }
    }
    """
    payload = {
        "query": graphql,
        "variables": {"search": query or None, "page": page, "perPage": OTHER_PAGE_SIZE},
    }
    data = cached_call(
        make_cache_key("anilist", query or "browse-popular-manga", page),
        lambda: request_json(
            "https://graphql.anilist.co",
            method="POST",
            headers={"Content-Type": "application/json"},
            json_body=payload,
        ),
    )

    if data.get("errors"):
        messages = [entry.get("message", "erro GraphQL") for entry in data["errors"]]
        raise RuntimeError("AniList: " + "; ".join(messages[:2]))

    page_data = data.get("data", {}).get("Page", {})
    media = page_data.get("media", [])
    results = []
    for manga in media:
        titles = manga.get("title") or {}
        title = titles.get("english") or titles.get("romaji") or titles.get("native") or "Sem título"
        genres = manga.get("genres") or []
        cover = manga.get("coverImage") or {}
        results.append(normalized_item(
            source="anilist",
            external_id=manga.get("id"),
            title=title,
            creator="Mangá catalogado no AniList",
            category="Mangás",
            year=(manga.get("startDate") or {}).get("year"),
            vibe=", ".join(genres[:2]) if genres else "Mangá",
            description=manga.get("description", "Sinopse não disponível."),
            image=cover.get("extraLarge") or cover.get("large"),
            url=manga.get("siteUrl"),
            external_rating=manga.get("averageScore"),
        ))
    return results, bool(page_data.get("pageInfo", {}).get("hasNextPage"))


def search_music(query: str, page: int = 1) -> tuple[list[dict[str, Any]], bool]:
    api_key = os.getenv("LASTFM_API_KEY", "").strip()
    if not api_key:
        raise MissingConfiguration("Configure LASTFM_API_KEY no arquivo .env.")

    method = "track.search" if query else "chart.gettoptracks"
    params: dict[str, Any] = {
        "method": method,
        "api_key": api_key,
        "format": "json",
        "limit": OTHER_PAGE_SIZE,
        "page": page,
    }
    if query:
        params["track"] = query
    data = cached_call(
        make_cache_key("lastfm", query or "browse-top-tracks", page),
        lambda: request_json(
            "https://ws.audioscrobbler.com/2.0/",
            params=params,
            headers={"User-Agent": "RadarCultural/1.0"},
        ),
    )

    if data.get("error"):
        raise RuntimeError(f"Last.fm: {data.get('message', 'erro não especificado')}")

    if query:
        track_container = data.get("results", {}).get("trackmatches", {})
    else:
        track_container = data.get("tracks", {})
    tracks = track_container.get("track", [])
    if isinstance(tracks, dict):
        tracks = [tracks]

    attrs = track_container.get("@attr", {})
    total_pages = int(attrs.get("totalPages") or attrs.get("totalpages") or page)

    results = []
    for track in tracks:
        artist_value = track.get("artist") or "Artista não informado"
        artist = artist_value.get("name") if isinstance(artist_value, dict) else artist_value
        title = track.get("name") or "Faixa sem título"
        images = track.get("image") or []
        image = next((entry.get("#text") for entry in reversed(images) if entry.get("#text")), None)
        mbid = track.get("mbid")
        track_id = mbid or track.get("url") or f"{artist}:{title}"
        listeners = track.get("listeners")
        description = f"Faixa de {artist}."
        if listeners:
            description += f" {listeners} ouvintes registrados no Last.fm."
        results.append(normalized_item(
            source="lastfm",
            external_id=track_id,
            title=title,
            creator=artist,
            category="Músicas",
            vibe="Faixa musical",
            description=description,
            image=image,
            url=track.get("url"),
            external_rating=None,
        ))
    return results, page < total_pages


def search_comics(query: str, page: int = 1) -> tuple[list[dict[str, Any]], bool]:
    api_key = os.getenv("COMICVINE_API_KEY", "").strip()
    if not api_key:
        raise MissingConfiguration("Configure COMICVINE_API_KEY no arquivo .env.")

    headers = {
        "User-Agent": "RadarCultural/1.0 (personal media discovery project)",
        "Accept": "application/json",
    }
    params: dict[str, Any] = {
        "api_key": api_key,
        "format": "json",
        "limit": OTHER_PAGE_SIZE,
        "offset": (page - 1) * OTHER_PAGE_SIZE,
        "field_list": "id,name,start_year,publisher,image,description,deck,site_detail_url",
    }
    if query:
        endpoint = "https://comicvine.gamespot.com/api/search/"
        params.update({"resources": "volume", "query": query})
    else:
        endpoint = "https://comicvine.gamespot.com/api/volumes/"
        params["sort"] = "date_added:desc"

    data = cached_call(
        make_cache_key("comicvine", query or "browse-recent-volumes", page),
        lambda: request_json(endpoint, params=params, headers=headers),
    )

    if data.get("status_code") not in (None, 1):
        raise RuntimeError(clean_text(data.get("error"), "Comic Vine retornou um erro."))

    comics = data.get("results", [])
    results = []
    for comic in comics:
        publisher = comic.get("publisher") or {}
        image_data = comic.get("image") or {}
        image = image_data.get("medium_url") or image_data.get("screen_url") or image_data.get("small_url") or image_data.get("original_url")
        comic_id = comic.get("id")
        results.append(normalized_item(
            source="comicvine",
            external_id=comic_id,
            title=comic.get("name") or "HQ sem título",
            creator=(publisher.get("name") if isinstance(publisher, dict) else None) or "Editora não informada",
            category="HQs",
            year=comic.get("start_year"),
            vibe="Quadrinhos",
            description=comic.get("deck") or comic.get("description") or "Descrição não disponível.",
            image=image,
            url=comic.get("site_detail_url"),
        ))
    total = int(data.get("number_of_total_results") or 0)
    return results, (page * OTHER_PAGE_SIZE < total)


SEARCHERS = {
    "Livros": search_books,
    "Músicas": search_music,
    "Séries": lambda query, page=1: search_tmdb(query, "Séries", page),
    "Filmes": lambda query, page=1: search_tmdb(query, "Filmes", page),
    "Jogos": search_games,
    "Mangás": search_manga,
    "HQs": search_comics,
}


# -----------------------------------------------------------------------------
# SQLite reviews
# -----------------------------------------------------------------------------


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def database_connection():
    connection = connect_db()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    """Create the app schema and preserve existing anonymous reviews."""
    required_columns = {
        "id", "source", "external_id", "category", "title",
        "username", "rating", "comment", "created_at",
    }

    with database_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                username_normalized TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER NOT NULL,
                preference_type TEXT NOT NULL CHECK (preference_type IN ('category', 'genre')),
                value TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                PRIMARY KEY (user_id, preference_type, value),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                interaction_type TEXT NOT NULL CHECK (
                    interaction_type IN ('view', 'favorite', 'unfavorite', 'rating')
                ),
                value REAL NOT NULL DEFAULT 0.5,
                item_tags TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'reviews'"
        ).fetchone()
        if table:
            existing_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(reviews)").fetchall()
            }
            if not required_columns.issubset(existing_columns):
                connection.execute("ALTER TABLE reviews RENAME TO reviews_legacy")

        connection.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                username TEXT NOT NULL,
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                comment TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
            )
        """)

        legacy = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'reviews_legacy'"
        ).fetchone()
        if legacy:
            legacy_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(reviews_legacy)").fetchall()
            }
            mappings = {
                "source": "fonte", "external_id": "id_conteudo", "category": "categoria",
                "title": "titulo", "username": "nome_usuario", "rating": "nota",
                "comment": "comentario", "created_at": "data_criacao",
            }
            if set(mappings.values()).issubset(legacy_columns):
                connection.execute("""
                    INSERT OR IGNORE INTO reviews (
                        id, source, external_id, category, title,
                        username, rating, comment, created_at
                    )
                    SELECT id, fonte, id_conteudo, categoria, titulo,
                           nome_usuario, nota, comentario, data_criacao
                    FROM reviews_legacy
                    WHERE nota BETWEEN 1 AND 5
                """)
            connection.execute("DROP TABLE reviews_legacy")

        review_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(reviews)").fetchall()
        }
        if "user_id" not in review_columns:
            connection.execute(
                "ALTER TABLE reviews ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
            )

        connection.execute("CREATE INDEX IF NOT EXISTS idx_reviews_content ON reviews(source, external_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews(user_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_interactions_user ON interactions(user_id, created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_interactions_item ON interactions(source, external_id)")


# -----------------------------------------------------------------------------
# USER ACCOUNTS, PREFERENCES, AND INTERACTION HISTORY
# -----------------------------------------------------------------------------


def current_user() -> dict[str, Any] | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    with database_connection() as connection:
        row = connection.execute(
            "SELECT id, username, created_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not row:
        session.clear()
        return None
    return dict(row)


def login_required_json(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Entre na sua conta para usar esta funcionalidade."}), 401
        return function(*args, **kwargs)
    return wrapper


GENRE_ALIASES = {
    "fantasia": "fantasy",
    "ficcao cientifica": "science fiction",
    "ficcao-cientifica": "science fiction",
    "terror": "horror",
    "misterio": "mystery",
    "aventura": "adventure",
    "acao": "action",
    "comedia": "comedy",
    "suspense": "thriller",
    "animacao": "animation",
    "romance": "romance",
    "drama": "drama",
    "rpg": "role-playing",
    "corrida": "racing",
    "estrategia": "strategy",
    "musical": "music",
}


def normalize_tag(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"[^a-z0-9 -]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return GENRE_ALIASES.get(normalized, normalized)


def parse_item_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = re.split(r"[,;/|]", value)
    elif isinstance(value, list):
        candidates = value
    else:
        candidates = []
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        tag = normalize_tag(clean_text(raw).strip()[:60])[:40]
        if tag and tag not in seen:
            seen.add(tag)
            cleaned.append(tag)
        if len(cleaned) >= 12:
            break
    return cleaned


@app.post("/api/auth/register")
def register_user():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    password = data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return jsonify({"error": "Informe usuário e senha."}), 400
    username = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,24}", username):
        return jsonify({"error": "Use de 3 a 24 caracteres: letras, números, ponto, hífen ou sublinhado."}), 400
    if len(password) < 8 or len(password) > 128:
        return jsonify({"error": "A senha deve ter entre 8 e 128 caracteres."}), 400
    normalized = username.casefold()
    password_hash = generate_password_hash(password)
    try:
        with database_connection() as connection:
            cursor = connection.execute(
                "INSERT INTO users (username, username_normalized, password_hash) VALUES (?, ?, ?)",
                (username, normalized, password_hash),
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        return jsonify({"error": "Esse nome de usuário já está cadastrado."}), 409
    session.clear()
    session["user_id"] = user_id
    return jsonify({"user": current_user()}), 201


@app.post("/api/auth/login")
def login_user():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    password = data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return jsonify({"error": "Informe usuário e senha."}), 400
    with database_connection() as connection:
        row = connection.execute(
            "SELECT id, username, password_hash FROM users WHERE username_normalized = ?",
            (username.strip().casefold(),),
        ).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Usuário ou senha incorretos."}), 401
    session.clear()
    session["user_id"] = int(row["id"])
    return jsonify({"user": current_user()})


@app.post("/api/auth/logout")
def logout_user():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def auth_me():
    user = current_user()
    return jsonify({"authenticated": bool(user), "user": user})


@app.route("/api/preferences", methods=["GET", "PUT"])
@login_required_json
def user_preferences():
    user_id = int(session["user_id"])
    if request.method == "GET":
        with database_connection() as connection:
            rows = connection.execute(
                "SELECT preference_type, value FROM user_preferences WHERE user_id = ? ORDER BY value",
                (user_id,),
            ).fetchall()
        categories = [row["value"] for row in rows if row["preference_type"] == "category"]
        genres = [row["value"] for row in rows if row["preference_type"] == "genre"]
        return jsonify({"categories": categories, "genres": genres})

    data = request.get_json(silent=True) or {}
    categories = data.get("categories", [])
    genres = data.get("genres", [])
    if not isinstance(categories, list) or not isinstance(genres, list):
        return jsonify({"error": "Preferências inválidas."}), 400
    allowed_categories = CATEGORIES
    categories = list(dict.fromkeys(
        value for value in categories if isinstance(value, str) and value in allowed_categories
    ))[:len(CATEGORIES)]
    genres = list(dict.fromkeys(
        normalize_tag(clean_text(value).strip())[:40]
        for value in genres
        if isinstance(value, str) and clean_text(value).strip()
    ))[:20]
    with database_connection() as connection:
        connection.execute("DELETE FROM user_preferences WHERE user_id = ?", (user_id,))
        connection.executemany(
            "INSERT INTO user_preferences (user_id, preference_type, value) VALUES (?, 'category', ?)",
            [(user_id, value) for value in categories],
        )
        connection.executemany(
            "INSERT INTO user_preferences (user_id, preference_type, value) VALUES (?, 'genre', ?)",
            [(user_id, value) for value in genres],
        )
    return jsonify({"ok": True, "categories": categories, "genres": genres})


@app.post("/api/interactions")
@login_required_json
def create_interaction():
    data = request.get_json(silent=True) or {}
    source = data.get("source")
    external_id = data.get("external_id")
    category = data.get("category")
    title = data.get("title")
    interaction_type = data.get("interaction_type")
    tags = parse_item_tags(data.get("tags", []))
    allowed_types = {"view", "favorite", "unfavorite"}
    if not all(isinstance(value, str) for value in (source, external_id, category, title, interaction_type)):
        return jsonify({"error": "Dados da interação inválidos."}), 400
    source, external_id, category, title, interaction_type = [
        value.strip() for value in (source, external_id, category, title, interaction_type)
    ]
    if source not in set(SOURCE_BY_CATEGORY.values()) or category not in CATEGORIES:
        return jsonify({"error": "Conteúdo ou categoria inválidos."}), 400
    if interaction_type not in allowed_types:
        return jsonify({"error": "Tipo de interação inválido."}), 400
    target_value = {"view": 0.55, "favorite": 0.95, "unfavorite": 0.05}[interaction_type]
    user_id = int(session["user_id"])
    with database_connection() as connection:
        if interaction_type == "view":
            recent = connection.execute("""
                SELECT id FROM interactions
                WHERE user_id = ? AND source = ? AND external_id = ?
                  AND interaction_type = 'view'
                  AND created_at >= datetime('now', 'localtime', '-30 minutes')
                LIMIT 1
            """, (user_id, source, external_id)).fetchone()
            if recent:
                return jsonify({"ok": True, "recorded": False, "reason": "view_recently_recorded"})
        connection.execute("""
            INSERT INTO interactions (
                user_id, source, external_id, category, title,
                interaction_type, value, item_tags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, source, external_id, category, title,
              interaction_type, target_value, json.dumps(tags, ensure_ascii=False)))
    return jsonify({"ok": True, "recorded": True}), 201


# -----------------------------------------------------------------------------
# SMALL NEURAL COLLABORATIVE FILTERING MODEL (NumPy)
# -----------------------------------------------------------------------------


def interaction_training_rows() -> list[dict[str, Any]]:
    with database_connection() as connection:
        rows = connection.execute("""
            SELECT id, user_id, source, external_id, value, interaction_type
            FROM interactions
            ORDER BY id DESC
            LIMIT 12000
        """).fetchall()
    return [dict(row) for row in rows]


def train_neural_collaborative_model(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Train a small embedding + MLP model when there is enough cross-user data.

    This is a genuine neural collaborative-filtering baseline, not a pretrained model.
    It is intentionally disabled for sparse datasets to avoid pretending that a handful
    of ratings can produce reliable collaborative recommendations.
    """
    user_values = sorted({int(row["user_id"]) for row in rows})
    item_values = sorted({f'{row["source"]}:{row["external_id"]}' for row in rows})
    if len(rows) < 20 or len(user_values) < 2 or len(item_values) < 6:
        return None

    user_map = {value: index for index, value in enumerate(user_values)}
    item_map = {value: index for index, value in enumerate(item_values)}
    u_indices = np.asarray([user_map[int(row["user_id"])] for row in rows], dtype=np.int64)
    keys = [f'{row["source"]}:{row["external_id"]}' for row in rows]
    i_indices = np.asarray([item_map[key] for key in keys], dtype=np.int64)
    targets = np.asarray([float(row["value"]) for row in rows], dtype=np.float32)
    weights = np.asarray([
        0.55 if row["interaction_type"] == "view" else
        1.15 if row["interaction_type"] == "favorite" else
        0.90 if row["interaction_type"] == "unfavorite" else 1.35
        for row in rows
    ], dtype=np.float32)

    rng = np.random.default_rng(17)
    dimensions = 12
    hidden = 24
    user_embeddings = rng.normal(0, 0.12, (len(user_values), dimensions)).astype(np.float32)
    item_embeddings = rng.normal(0, 0.12, (len(item_values), dimensions)).astype(np.float32)
    w1 = rng.normal(0, 0.10, (dimensions * 2, hidden)).astype(np.float32)
    b1 = np.zeros(hidden, dtype=np.float32)
    w2 = rng.normal(0, 0.10, (hidden, 1)).astype(np.float32)
    b2 = np.zeros(1, dtype=np.float32)
    learning_rate = 0.025
    batch_size = min(64, len(rows))

    for _epoch in range(70):
        order = rng.permutation(len(rows))
        for begin in range(0, len(rows), batch_size):
            batch = order[begin:begin + batch_size]
            ub = u_indices[batch]
            ib = i_indices[batch]
            user_vec = user_embeddings[ub].copy()
            item_vec = item_embeddings[ib].copy()
            x = np.concatenate([user_vec, item_vec], axis=1)
            pre_activation = x @ w1 + b1
            hidden_vec = np.maximum(pre_activation, 0)
            logits = hidden_vec @ w2 + b2
            predictions = 1.0 / (1.0 + np.exp(-np.clip(logits[:, 0], -20, 20)))
            error = (predictions - targets[batch]) * weights[batch]
            dlogits = (2.0 / len(batch)) * error * predictions * (1.0 - predictions)
            grad_w2 = hidden_vec.T @ dlogits[:, None]
            grad_b2 = np.sum(dlogits, axis=0)
            dhidden = dlogits[:, None] @ w2.T
            dpre = dhidden * (pre_activation > 0)
            grad_w1 = x.T @ dpre
            grad_b1 = np.sum(dpre, axis=0)
            dx = dpre @ w1.T
            grad_user = dx[:, :dimensions]
            grad_item = dx[:, dimensions:]

            np.add.at(user_embeddings, ub, -learning_rate * grad_user)
            np.add.at(item_embeddings, ib, -learning_rate * grad_item)
            w1 -= learning_rate * grad_w1
            b1 -= learning_rate * grad_b1
            w2 -= learning_rate * grad_w2
            b2 -= learning_rate * grad_b2

    return {
        "users": user_map,
        "items": item_map,
        "user_embeddings": user_embeddings,
        "item_embeddings": item_embeddings,
        "w1": w1,
        "b1": b1,
        "w2": w2,
        "b2": b2,
        "dimensions": dimensions,
        "training_rows": len(rows),
    }


def get_cached_neural_model(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Reuse the last trained model until a new interaction changes the dataset."""
    global _NEURAL_MODEL_CACHE, _NEURAL_MODEL_SIGNATURE
    signature = (
        len(rows),
        int(rows[0]["id"]) if rows else 0,
        int(rows[-1]["id"]) if rows else 0,
    )
    with _NEURAL_MODEL_LOCK:
        if signature != _NEURAL_MODEL_SIGNATURE:
            _NEURAL_MODEL_CACHE = train_neural_collaborative_model(rows)
            _NEURAL_MODEL_SIGNATURE = signature
        return _NEURAL_MODEL_CACHE


def predict_neural_score(model: dict[str, Any] | None, user_id: int, item_key: str) -> float | None:
    if not model or user_id not in model["users"] or item_key not in model["items"]:
        return None
    user_vec = model["user_embeddings"][model["users"][user_id]]
    item_vec = model["item_embeddings"][model["items"][item_key]]
    x = np.concatenate([user_vec, item_vec])[None, :]
    hidden = np.maximum(x @ model["w1"] + model["b1"], 0)
    logit = float((hidden @ model["w2"] + model["b2"])[0, 0])
    return float(1.0 / (1.0 + np.exp(-np.clip(logit, -20, 20))))


def build_user_taste_profile(user_id: int) -> dict[str, Any]:
    category_scores: dict[str, float] = {}
    genre_scores: dict[str, float] = {}
    with database_connection() as connection:
        prefs = connection.execute(
            "SELECT preference_type, value FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        events = connection.execute("""
            SELECT category, interaction_type, value, item_tags
            FROM interactions WHERE user_id = ?
            ORDER BY id DESC LIMIT 2000
        """, (user_id,)).fetchall()
    for row in prefs:
        if row["preference_type"] == "category":
            category_scores[row["value"]] = category_scores.get(row["value"], 0.0) + 1.0
        else:
            normalized_genre = normalize_tag(row["value"])
            genre_scores[normalized_genre] = genre_scores.get(normalized_genre, 0.0) + 1.0

    for event in events:
        kind = event["interaction_type"]
        if kind == "rating":
            signal = (float(event["value"]) - 0.5) * 2.0
        elif kind == "favorite":
            signal = 0.9
        elif kind == "unfavorite":
            signal = -0.65
        else:
            signal = 0.12
        category = event["category"]
        category_scores[category] = category_scores.get(category, 0.0) + signal
        try:
            tags = json.loads(event["item_tags"] or "[]")
        except (TypeError, ValueError):
            tags = []
        for tag in tags:
            if isinstance(tag, str):
                normalized = normalize_tag(tag)
                genre_scores[normalized] = genre_scores.get(normalized, 0.0) + signal

    category_max = max([abs(value) for value in category_scores.values()] or [1.0])
    genre_max = max([abs(value) for value in genre_scores.values()] or [1.0])
    return {
        "categories": {key: value / category_max for key, value in category_scores.items()},
        "genres": {key: value / genre_max for key, value in genre_scores.items()},
    }


@app.post("/api/recommendations")
@login_required_json
def personalized_recommendations():
    data = request.get_json(silent=True) or {}
    candidates = data.get("items", [])
    if not isinstance(candidates, list):
        return jsonify({"error": "A lista de conteúdos é inválida."}), 400
    candidates = candidates[:250]
    user_id = int(session["user_id"])
    taste = build_user_taste_profile(user_id)
    rows = interaction_training_rows()
    model = get_cached_neural_model(rows)
    output = []

    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        source = raw.get("source")
        external_id = str(raw.get("externalId", ""))[:200]
        category = raw.get("category")
        title = clean_text(raw.get("title", ""), "Sem título")[:200]
        if source not in set(SOURCE_BY_CATEGORY.values()) or category not in CATEGORIES or not external_id:
            continue
        tags = parse_item_tags(raw.get("vibe", ""))
        category_score = float(taste["categories"].get(category, 0.0))
        matched_tags = [(tag, taste["genres"].get(normalize_tag(tag), 0.0)) for tag in tags]
        tag_score = (sum(value for _, value in matched_tags) / len(matched_tags)) if matched_tags else 0.0
        content_score = float(np.clip(0.5 + 0.22 * category_score + 0.28 * tag_score, 0.0, 1.0))
        # If the user has not built a profile yet, provider ratings act only as a
        # mild discovery tie-breaker, not as a claim about the user's personal taste.
        external_rating = raw.get("externalRating")
        if not taste["categories"] and not taste["genres"] and isinstance(external_rating, (int, float)):
            rating_scale = 100.0 if source == "anilist" else 10.0 if source == "tmdb" else 5.0
            rating_signal = float(np.clip(float(external_rating) / rating_scale, 0.0, 1.0))
            content_score = 0.85 * content_score + 0.15 * rating_signal
        item_key = f"{source}:{external_id}"
        neural_score = predict_neural_score(model, user_id, item_key)
        if neural_score is not None:
            score = 0.72 * neural_score + 0.28 * content_score
        else:
            score = content_score
        liked = [tag for tag, value in matched_tags if value > 0.15]
        if liked:
            reason = "Combina com seus interesses em " + ", ".join(liked[:2])
        elif category_score > 0.15:
            reason = f"Mais alinhado à sua preferência por {category.lower()}"
        elif neural_score is not None:
            reason = "Sugestão calculada pelo modelo colaborativo"
        else:
            reason = "Conheça este conteúdo e avalie para personalizar suas próximas sugestões"
        output.append({
            "id": str(raw.get("id", item_key))[:240],
            "source": source,
            "externalId": external_id,
            "category": category,
            "title": title,
            "creator": clean_text(raw.get("creator", ""), "")[:160],
            "image": secure_https(raw.get("image")),
            "year": str(raw.get("year", "—"))[:12],
            "vibe": clean_text(raw.get("vibe", ""), "")[:160],
            "score": round(float(score), 4),
            "reason": reason,
            "neural": neural_score is not None,
        })

    output.sort(key=lambda entry: (entry["score"], entry["title"].casefold()), reverse=True)
    model_ready = model is not None
    reason_text = (
        "Rede neural colaborativa ativa" if model_ready
        else "Perfil de interesses ativo; rede neural em fase de aprendizado"
    )
    return jsonify({
        "items": output[:12],
        "engine": "neural-collaborative" if model_ready else "content-profile",
        "neural_ready": model_ready,
        "training_interactions": len(rows),
        "users_in_training": len({int(row["user_id"]) for row in rows}),
        "message": reason_text,
    })


@app.get("/api/health")
def api_health():
    """Tell the browser whether Flask is running and which API keys are configured."""
    providers = {
        "Livros": {"provider": "Google Books", "configured": True, "key_required": False, "key_present": bool(os.getenv("GOOGLE_BOOKS_API_KEY", "").strip())},
        "Músicas": {"provider": "Last.fm", "configured": bool(os.getenv("LASTFM_API_KEY", "").strip()), "key_required": True},
        "Séries": {"provider": "TMDB", "configured": bool(os.getenv("TMDB_READ_ACCESS_TOKEN", "").strip()), "key_required": True},
        "Filmes": {"provider": "TMDB", "configured": bool(os.getenv("TMDB_READ_ACCESS_TOKEN", "").strip()), "key_required": True},
        "Jogos": {"provider": "RAWG", "configured": bool(os.getenv("RAWG_API_KEY", "").strip()), "key_required": True},
        "Mangás": {"provider": "AniList", "configured": True, "key_required": False},
        "HQs": {"provider": "Comic Vine", "configured": bool(os.getenv("COMICVINE_API_KEY", "").strip()), "key_required": True},
    }
    return jsonify({"status": "ok", "backend": "Flask", "providers": providers})


@app.get("/api/search")
def api_search():
    category = request.args.get("category", "").strip()
    query = request.args.get("q", "").strip()
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        return jsonify({"error": "Página inválida."}), 400

    if category not in CATEGORIES:
        return jsonify({"error": "Categoria inválida."}), 400
    if len(query) > 120:
        return jsonify({"error": "A pesquisa deve ter no máximo 120 caracteres."}), 400
    if page < 1 or page > 500:
        return jsonify({"error": "A página deve estar entre 1 e 500."}), 400

    try:
        results, has_more = SEARCHERS[category](query, page)
        return jsonify({
            "category": category,
            "provider": SOURCE_BY_CATEGORY[category],
            "mode": "search" if query else "browse",
            "page": page,
            "count": len(results),
            "has_more": has_more,
            "results": results,
        })
    except MissingConfiguration as error:
        return jsonify({
            "error": str(error),
            "provider": SOURCE_BY_CATEGORY[category],
            "configuration_required": True,
        }), 503
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else 502
        app.logger.warning("Provider HTTP error (%s) for %s", status, category)
        if status in (401, 403):
            message = "A API recusou a credencial. Confira se a chave/token está correta no .env."
        elif status == 429 and category == "Livros":
            message = (
                "Google Books limitou as requisições (HTTP 429). Aguarde antes de tentar novamente e "
                "confira a cota do projeto no Google Cloud Console, caso esteja usando uma API key. "
                "A chave identifica o projeto, mas não remove limites impostos pelo Google."
            )
        elif status == 429:
            message = "A API limitou as requisições (HTTP 429). Aguarde um pouco antes de tentar novamente."
        elif status >= 500:
            message = "O serviço externo está temporariamente indisponível."
        else:
            message = f"A API respondeu com HTTP {status}. Confira os parâmetros da consulta."
        return jsonify({
            "error": message,
            "provider": SOURCE_BY_CATEGORY[category],
            "provider_status": status,
        }), 429 if status == 429 else 502
    except requests.Timeout:
        app.logger.warning("Provider timeout for %s", category)
        return jsonify({
            "error": "A API demorou para responder. Verifique a conexão e tente novamente.",
            "provider": SOURCE_BY_CATEGORY[category],
        }), 504
    except requests.ConnectionError:
        app.logger.exception("Provider connection error for %s", category)
        return jsonify({
            "error": "O Python não conseguiu alcançar a API. Verifique sua internet, DNS ou firewall.",
            "provider": SOURCE_BY_CATEGORY[category],
        }), 502
    except (requests.RequestException, ValueError, RuntimeError) as error:
        app.logger.exception("External API error for category %s", category)
        return jsonify({
            "error": str(error) if isinstance(error, RuntimeError) else "Falha inesperada na resposta da API. Veja o terminal do Flask para detalhes.",
            "provider": SOURCE_BY_CATEGORY[category],
        }), 502
    except Exception:
        app.logger.exception("Unexpected error in provider adapter for %s", category)
        return jsonify({
            "error": "Erro interno ao preparar os resultados. Veja o terminal do Flask para detalhes.",
            "provider": SOURCE_BY_CATEGORY[category],
        }), 500


@app.post("/api/reviews")
def create_review():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Envie os dados no formato JSON."}), 400

    source = data.get("source")
    external_id = data.get("external_id")
    category = data.get("category")
    title = data.get("title")
    username = data.get("username")
    rating = data.get("rating")
    comment = data.get("comment")
    tags = parse_item_tags(data.get("tags", []))

    text_fields = [source, external_id, category, title, username, comment]
    if not all(isinstance(value, str) for value in text_fields):
        return jsonify({"error": "Preencha todos os campos corretamente."}), 400

    source, external_id, category, title, username, comment = [value.strip() for value in text_fields]
    allowed_sources = set(SOURCE_BY_CATEGORY.values())

    if source not in allowed_sources:
        return jsonify({"error": "Fonte do conteúdo inválida."}), 400
    if category not in CATEGORIES or SOURCE_BY_CATEGORY[category] != source:
        return jsonify({"error": "Categoria e fonte não correspondem."}), 400
    if not external_id or len(external_id) > 200:
        return jsonify({"error": "Identificador do conteúdo inválido."}), 400
    if not title or len(title) > 200:
        return jsonify({"error": "Título inválido."}), 400
    if len(username) < 2 or len(username) > 40:
        return jsonify({"error": "O nome deve ter entre 2 e 40 caracteres."}), 400
    if type(rating) is not int or rating < 1 or rating > 5:
        return jsonify({"error": "A nota deve ser um número inteiro de 1 a 5."}), 400
    if len(comment) < 5 or len(comment) > 1200:
        return jsonify({"error": "O comentário deve ter entre 5 e 1200 caracteres."}), 400

    user = current_user()
    user_id = int(user["id"]) if user else None
    if user:
        username = user["username"]

    with database_connection() as connection:
        cursor = connection.execute("""
            INSERT INTO reviews (source, external_id, category, title, username, rating, comment, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (source, external_id, category, title, username, rating, comment, user_id))
        if user_id is not None:
            connection.execute("""
                INSERT INTO interactions (
                    user_id, source, external_id, category, title,
                    interaction_type, value, item_tags
                ) VALUES (?, ?, ?, ?, ?, 'rating', ?, ?)
            """, (user_id, source, external_id, category, title,
                  (rating - 1) / 4.0, json.dumps(tags, ensure_ascii=False)))
        row = connection.execute(
            "SELECT id, source, external_id, category, title, username, rating, comment, created_at FROM reviews WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()

    return jsonify(dict(row)), 201


@app.get("/api/reviews")
def list_reviews():
    source = request.args.get("source", "").strip()
    external_id = request.args.get("external_id", "").strip()
    if not source or not external_id:
        return jsonify({"error": "Informe source e external_id."}), 400

    with database_connection() as connection:
        rows = connection.execute("""
            SELECT id, source, external_id, category, title, username, rating, comment, created_at
            FROM reviews
            WHERE source = ? AND external_id = ?
            ORDER BY id DESC
            LIMIT 100
        """, (source, external_id)).fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/api/reviews/summary")
def review_summary():
    source = request.args.get("source", "").strip()
    external_id = request.args.get("external_id", "").strip()
    if not source or not external_id:
        return jsonify({"error": "Informe source e external_id."}), 400

    with database_connection() as connection:
        rows = connection.execute("""
            SELECT rating, COUNT(*) AS quantity
            FROM reviews
            WHERE source = ? AND external_id = ?
            GROUP BY rating
        """, (source, external_id)).fetchall()

    distribution = {str(star): 0 for star in range(1, 6)}
    total = 0
    score_sum = 0
    for row in rows:
        star = int(row["rating"])
        quantity = int(row["quantity"])
        distribution[str(star)] = quantity
        total += quantity
        score_sum += star * quantity

    return jsonify({
        "total": total,
        "average": round(score_sum / total, 2) if total else 0,
        "distribution": distribution,
    })


# -----------------------------------------------------------------------------
# Flask serves the front-end too, so browser requests stay on the same origin.
# -----------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/style.css")
def style():
    return send_from_directory(BASE_DIR, "style.css")


@app.get("/script.js")
def script():
    return send_from_directory(BASE_DIR, "script.js")


init_db()

if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "1").strip() == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug_mode)
