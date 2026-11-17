#!/usr/bin/env python3
"""
Полуавтоматический редактор Telegram-каналов.

Цикл:
  1. По расписанию (или командой /draft) берёт следующую тему из plan.json.
  2. Ищет информацию в интернете и пишет черновик (GigaChat по умолчанию).
  3. Присылает черновик владельцу в личку с кнопками:
       ✅ Опубликовать · ✏️ Править · 🔁 Переписать · ❌ Отклонить
  4. После одобрения публикует пост в канал.

Провайдеры нейросети (переменная PROVIDER в .env):
  gigachat  : GigaChat от Сбера, бесплатный тариф для физлиц (по умолчанию)
  openai    : любой OpenAI-совместимый сервис (Groq, Cerebras, OpenRouter и т.д.)
  anthropic : Claude (платно, со встроенным веб-поиском)

Зависимости: requests, ddgs (поиск), anthropic (только для PROVIDER=anthropic).
Запуск: python bot.py
"""

import html
import json
import os
import re
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

try:
    import anthropic
except ImportError:  # нужен только для PROVIDER=anthropic
    anthropic = None

BASE = Path(__file__).parent
STATE_FILE = BASE / "state.json"
USE_IMAGES = os.environ.get("USE_IMAGES", "1") == "1"
IMG_DIR = BASE / "images"

# ───────────────────────── конфиг ─────────────────────────

def load_env(path: Path = BASE / ".env") -> None:
    """Мини-загрузчик .env, чтобы не тянуть лишнюю зависимость."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


load_env()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
PROVIDER = os.environ.get("PROVIDER", "gigachat").strip().lower()
USE_SEARCH = os.environ.get("USE_WEB_SEARCH", "1") == "1"

# GigaChat
GIGA_KEY = os.environ.get("GIGACHAT_AUTH_KEY", "")
GIGA_SCOPE = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGA_MODEL = os.environ.get("GIGACHAT_MODEL", "GigaChat-2-Max")
GIGA_OAUTH = os.environ.get("GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
GIGA_BASE = os.environ.get("GIGACHAT_BASE_URL", "https://api.giga.chat/v1")

# Любой OpenAI-совместимый сервис
OPENAI_BASE = os.environ.get("OPENAI_BASE_URL", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")

# Claude
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

CONFIG = read_json(BASE / "config.json", {})
TZ = ZoneInfo(CONFIG.get("timezone", "Asia/Krasnoyarsk"))
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_PROXY = os.environ.get("TELEGRAM_PROXY", "").strip()
TG_PROXIES = {"http": TG_PROXY, "https": TG_PROXY} if TG_PROXY else None


def giga_verify():
    """Проверка сертификата: файл НУЦ Минцифры, обычная проверка или (не рекомендуется) отключена."""
    ca = os.environ.get("GIGACHAT_CA_BUNDLE", "").strip()
    if ca:
        return ca
    if os.environ.get("GIGACHAT_VERIFY_SSL", "1") == "0":
        try:
            import urllib3
            urllib3.disable_warnings()
        except Exception:
            pass
        return False
    return True


# ───────────────────────── состояние ─────────────────────────
# state = {
#   "offset": int,                      # для getUpdates
#   "plan_pos": {channel_key: int},     # сколько тем плана уже взято
#   "drafts": {draft_id: {...}},        # черновики
#   "awaiting": {"draft_id": str, "mode": "edit"|"feedback"} | None,
#   "last_run": {"channel:HH:MM": "YYYY-MM-DD"},
#   "history": {channel_key: [заголовки опубликованных постов]}
# }

def load_state() -> dict:
    st = read_json(STATE_FILE, {})
    st.setdefault("offset", 0)
    st.setdefault("plan_pos", {})
    st.setdefault("drafts", {})
    st.setdefault("awaiting", None)
    st.setdefault("last_run", {})
    st.setdefault("history", {})
    return st


def save_state(st: dict) -> None:
    write_json(STATE_FILE, st)


# ───────────────────────── Telegram ─────────────────────────

def tg(method: str, **params):
    r = requests.post(f"{API}/{method}", json=params, timeout=(10, 45), proxies=TG_PROXIES)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]
def tg_photo(chat_id, path, caption=None):
    data = {"chat_id": chat_id}
    if caption:
        data.update(caption=caption, parse_mode="HTML")
    with open(path, "rb") as f:
        r = requests.post(f"{API}/sendPhoto", data=data, files={"photo": f},
                          timeout=(10, 60), proxies=TG_PROXIES)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto: {j.get('description')}")
    return j["result"]


def find_image(query: str, skip: int = 0) -> str:
    """Ищет картинку по запросу, скачивает в папку images, возвращает путь (или пустую строку)."""
    try:
        from ddgs import DDGS
        results = DDGS().images(query, region="ru-ru", max_results=12) or []
    except Exception as e:
        print(f"[warn] поиск картинок не сработал: {e}", file=sys.stderr)
        return ""
    IMG_DIR.mkdir(exist_ok=True)
    for r in results[skip:]:
        url = r.get("image")
        if not url:
            continue
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            ct = resp.headers.get("content-type", "")
            if (resp.status_code == 200 and ("jpeg" in ct or "png" in ct)
                    and 10_000 < len(resp.content) < 9_000_000):
                path = IMG_DIR / f"{uuid.uuid4().hex}.jpg"
                path.write_bytes(resp.content)
                return str(path)
        except Exception:
            continue
    return ""

def send_html(chat_id, text: str, **extra):
    """Отправка с HTML-разметкой; если разметка сломана, уходит обычным текстом."""
    try:
        return tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                  link_preview_options={"is_disabled": True}, **extra)
    except RuntimeError as e:
        if "parse entities" in str(e):
            plain = re.sub(r"<[^>]+>", "", text)
            return tg("sendMessage", chat_id=chat_id, text=html.unescape(plain),
                      link_preview_options={"is_disabled": True}, **extra)
        raise


def keyboard(draft_id: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ Опубликовать", "callback_data": f"ok:{draft_id}"}],
        [{"text": "✏️ Править", "callback_data": f"edit:{draft_id}"},
         {"text": "🔁 Переписать", "callback_data": f"redo:{draft_id}"}],
        [{"text": "🖼 Другая картинка", "callback_data": f"img:{draft_id}"},
         {"text": "🚫 Без картинки", "callback_data": f"noimg:{draft_id}"}],
        [{"text": "❌ Отклонить", "callback_data": f"no:{draft_id}"}],
    ]}


def show_draft(st: dict, draft_id: str) -> None:
    d = st["drafts"][draft_id]
    ch = CONFIG["channels"][d["channel"]]
    head = f"📝 <b>Черновик для {html.escape(ch['title'])}</b>\n" \
           f"<i>{html.escape(d['rubric'])} · {html.escape(d['topic'])}</i>\n" \
           f"{'─' * 18}"
    send_html(OWNER_ID, head)
    if d.get("image") and Path(d["image"]).exists():
        try:
            tg_photo(OWNER_ID, d["image"])
        except Exception:
            traceback.print_exc()
    send_html(OWNER_ID, d["text"], reply_markup=keyboard(draft_id))
    extra = []
    if d.get("notes"):
        extra.append("⚠️ <b>Что проверить:</b>\n" + html.escape(d["notes"]))
    if d.get("sources"):
        extra.append("🔗 <b>Источники:</b>\n" + "\n".join(html.escape(u) for u in d["sources"][:8]))
    if extra:
        send_html(OWNER_ID, "\n\n".join(extra))


# ───────────────────────── поиск в интернете (бесплатный) ─────────────────────────

def page_excerpt(url: str, limit: int = 2500) -> str:
    """Скачивает страницу и возвращает начало текста без разметки."""
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if "text/html" not in r.headers.get("content-type", "") or r.status_code != 200:
            return ""
        t = r.text
        t = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", t)
        t = re.sub(r"(?s)<[^>]+>", " ", t)
        t = html.unescape(re.sub(r"\s+", " ", t)).strip()
        return t[:limit]
    except Exception:
        return ""


def web_research(query: str, pages: int = 3):
    """
    Бесплатный поиск через DuckDuckGo (пакет ddgs, ключ не нужен).
    Возвращает (текст для модели, список ссылок, предупреждение или "").
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return "", [], "Поиск отключён: не установлен пакет ddgs (pip install ddgs)."
    try:
        results = DDGS().text(query, region="ru-ru", max_results=6) or []
    except Exception as e:
        return "", [], f"Поиск в интернете не сработал ({str(e)[:120]}). Факты в посте не подтверждены."
    if not results:
        return "", [], "Поиск ничего не нашёл. Факты в посте не подтверждены."

    chunks, urls = [], []
    for i, r in enumerate(results, 1):
        url = r.get("href") or r.get("url") or ""
        title = r.get("title", "")
        snippet = r.get("body", "")
        body = page_excerpt(url) if i <= pages and url else ""
        chunks.append(f"[{i}] {title}\n{url}\n{body or snippet}")
        if url:
            urls.append(url)
    return "\n\n".join(chunks), urls, ""


# ───────────────────────── нейросеть ─────────────────────────

FACT_RULE_SEARCH = ("Факты (инструменты, цены, вакансии, версии, новости) бери только из блока "
                    "«Материалы из интернета» в сообщении пользователя и из общеизвестных сведений. "
                    "Не выдумывай цифры, названия и ссылки. Если материалов нет или они не отвечают на тему, "
                    "пиши только общеизвестное и укажи в <notes>, что не проверено.")
FACT_RULE_NATIVE = ("Если нужны актуальные факты (инструменты, цены, вакансии, версии, новости), сначала найди "
                    "их в интернете. Не выдумывай цифры, названия и ссылки. Если чего-то не смог подтвердить, "
                    "не пиши это как факт.")

SYSTEM_TMPL = """Ты редактор Telegram-канала «{title}».
Аудитория: {audience}

{style}

Правила работы:
- {fact_rule}
- Пиши на русском. Формат разметки только Telegram HTML: <b>, <i>, <a href="...">, <code>. Никакого Markdown, никаких звёздочек.
- Длина поста: {min_len}-{max_len} символов.
- Не добавляй хештеги в конце сообщения.
- Не используй длинное тире.
- Добавляй тематические эмодзи в начало поста.
- Пиши в посте интересные факты на тему поста.
- Первая строка поста это цепляющий заголовок в <b>, без слов «Привет» и «Друзья».
- Начинай сразу с сути, без вступлений вроде «В современном мире» и «Сегодня поговорим о».
- Пиши короткими абзацами по 1-3 предложения, между абзацами пустая строка.
- Никакой воды и канцелярита: «является», «осуществлять», «данный», «в рамках».
- Если в посте есть список, делай не больше 5 пунктов.
- Обращайся к читателю на «ты» (или на «вы»).
- Не пиши от первого лица, будто у тебя есть личный опыт.
- Каждый факт с цифрой или названием должен быть из найденных материалов, иначе не пиши его.
- Не вставляй ссылки в текст поста, источники я проверю отдельно.
- Пиши без пафоса и громких обещаний вроде «гарантированно», «за один день», «секрет, о котором молчат».
- Ответ строго в таком виде:
<post>
текст поста
</post>
<notes>
что в посте стоит проверить человеку (неподтверждённые факты, спорные места); если всё подтверждено, напиши «нет»
</notes>
"""


def build_system(channel_key: str) -> str:
    ch = CONFIG["channels"][channel_key]
    style = (BASE / "style.md").read_text(encoding="utf-8") if (BASE / "style.md").exists() else ""
    return SYSTEM_TMPL.format(
        title=ch["title"], audience=ch["audience"], style=style,
        fact_rule=FACT_RULE_NATIVE if PROVIDER == "anthropic" else FACT_RULE_SEARCH,
        min_len=CONFIG.get("min_len", 500), max_len=CONFIG.get("max_len", 1200),
    )


def md_to_tg_html(text: str) -> str:
    """Нейросети нет-нет да и пишут Markdown. Превращаем самое частое в HTML для Telegram."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"(?m)^#{1,3}\s+(.+)$", r"<b>\1</b>", text)
    return text


def extract_post(raw: str):
    """Достаёт <post> и <notes> из ответа модели."""
    m = re.search(r"<post>\s*(.*?)\s*</post>", raw, re.S)
    post = m.group(1).strip() if m else raw.strip()
    post = md_to_tg_html(post)
    n = re.search(r"<notes>\s*(.*?)\s*</notes>", raw, re.S)
    notes = n.group(1).strip() if n else ""
    if notes.lower().strip(" .!") in ("нет", "no", ""):
        notes = ""
    return post, notes


def chat_completions(base: str, headers: dict, model: str, system: str, user: str, verify=True) -> str:
    """Запрос в формате OpenAI chat/completions (так устроены GigaChat, Groq, Cerebras, OpenRouter и др.)."""
    r = requests.post(
        f"{base.rstrip('/')}/chat/completions",
        headers=headers, timeout=180, verify=verify,
        json={"model": model, "temperature": 0.8, "max_tokens": 2000,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]},
    )
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


_giga = {"token": None, "exp": 0.0}


def giga_token(force: bool = False) -> str:
    if not force and _giga["token"] and _giga["exp"] - time.time() > 120:
        return _giga["token"]
    if not GIGA_KEY:
        raise RuntimeError("Не задан GIGACHAT_AUTH_KEY в .env")
    r = requests.post(
        GIGA_OAUTH, timeout=30, verify=giga_verify(),
        headers={"Authorization": f"Basic {GIGA_KEY}", "RqUID": str(uuid.uuid4()),
                 "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"scope": GIGA_SCOPE},
    )
    if r.status_code != 200:
        raise RuntimeError(f"GigaChat OAuth {r.status_code}: {r.text[:300]}")
    j = r.json()
    exp = float(j.get("expires_at", 0))
    if exp > 1e12:  # в миллисекундах
        exp /= 1000
    _giga["token"], _giga["exp"] = j["access_token"], exp or time.time() + 25 * 60
    return _giga["token"]


def ask_gigachat(system: str, user: str) -> str:
    for attempt in (1, 2):
        try:
            return chat_completions(GIGA_BASE, {"Authorization": f"Bearer {giga_token(force=attempt == 2)}"},
                                    GIGA_MODEL, system, user, verify=giga_verify())
        except RuntimeError as e:
            if attempt == 1 and str(e).startswith("401"):
                continue  # токен мог протухнуть, берём новый
            raise


def ask_openai_compatible(system: str, user: str) -> str:
    if not (OPENAI_BASE and OPENAI_KEY and OPENAI_MODEL):
        raise RuntimeError("Для PROVIDER=openai заполните OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL")
    return chat_completions(OPENAI_BASE, {"Authorization": f"Bearer {OPENAI_KEY}"}, OPENAI_MODEL, system, user)


def ask_claude(system: str, user: str):
    """Claude со встроенным веб-поиском. Возвращает (текст, ссылки)."""
    if anthropic is None:
        raise RuntimeError("Не установлен пакет anthropic: pip install anthropic")
    client = anthropic.Anthropic()
    kwargs = dict(model=CLAUDE_MODEL, max_tokens=4000, system=system,
                  messages=[{"role": "user", "content": user}])
    if USE_SEARCH:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    try:
        resp = client.messages.create(**kwargs)
    except Exception as e:
        if "tools" in kwargs:
            print(f"[warn] веб-поиск Claude недоступен ({e}); пробую без него", file=sys.stderr)
            kwargs.pop("tools")
            resp = client.messages.create(**kwargs)
        else:
            raise
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    urls, seen = [], set()
    for block in resp.content:
        for c in (getattr(block, "citations", None) or []):
            u = getattr(c, "url", None)
            if u and u not in seen:
                seen.add(u)
                urls.append(u)
    return text, urls


def generate(channel_key: str, topic_item: dict, feedback: str = "", previous: str = ""):
    """Один проход: (при необходимости) поиск, запрос к нейросети, разбор. Возвращает (пост, заметки, ссылки)."""
    ch = CONFIG["channels"][channel_key]
    system = build_system(channel_key)

    prompt = f"Рубрика: {topic_item['rubric']}\nТема: {topic_item['topic']}\n"
    if previous:
        prompt += f"\nПредыдущая версия поста:\n{previous}\n"
    if feedback:
        prompt += f"\nПравки владельца канала (учти обязательно): {feedback}\n"

    if PROVIDER == "anthropic":
        raw, urls = ask_claude(system, prompt + "\nНапиши пост.")
        post, notes = extract_post(raw)
        return post, notes, urls

    warn, urls = "", []
    if USE_SEARCH:
        topic = topic_item["topic"]
        query = ch.get("search_hint", ch["title"]) if topic.startswith("ВЫБЕРИ САМ") else topic
        research, urls, warn = web_research(f"{query} {datetime.now(TZ).year}")
        if research:
            prompt += f"\nМатериалы из интернета:\n{research}\n"
        else:
            prompt += "\nМатериалов из интернета нет.\n"
    prompt += "\nНапиши пост."

    raw = ask_gigachat(system, prompt) if PROVIDER == "gigachat" else ask_openai_compatible(system, prompt)
    post, notes = extract_post(raw)
    if warn:
        notes = (warn + "\n" + notes).strip()
    return post, notes, urls


def next_topic(st: dict, channel_key: str):
    """Следующая тема из plan.json; если план кончился, None (тогда модель предложит тему сама)."""
    plan = [p for p in read_json(BASE / "plan.json", []) if p["channel"] == channel_key]
    pos = st["plan_pos"].get(channel_key, 0)
    if pos < len(plan):
        st["plan_pos"][channel_key] = pos + 1
        return plan[pos]
    return None


def make_draft(st: dict, channel_key: str, topic_item=None, feedback: str = "",
               previous: str = "", draft_id: str = None) -> str:
    if topic_item is None:
        topic_item = next_topic(st, channel_key)
    if topic_item is None:
        recent = "; ".join(st["history"].get(channel_key, [])[-15:]) or "пока ничего"
        topic_item = {"channel": channel_key, "rubric": "свободная рубрика",
                      "topic": "ВЫБЕРИ САМ: полезная тема по профилю канала, которой нет среди недавних постов: " + recent}

    post, notes, sources = generate(channel_key, topic_item, feedback, previous)

    if topic_item["topic"].startswith("ВЫБЕРИ САМ"):
        img_query = re.sub(r"<[^>]+>", "", post).strip().split("\n")[0][:100]
    else:
        img_query = topic_item["topic"]
    image = find_image(img_query) if USE_IMAGES else ""

    draft_id = draft_id or str(int(time.time()))
    st["drafts"][draft_id] = {
        "channel": channel_key, "rubric": topic_item["rubric"], "topic": topic_item["topic"],
        "text": post, "notes": notes, "sources": sources, "status": "pending",
        "image": image, "image_query": img_query, "image_skip": 0,
    }
    save_state(st)
    return draft_id


# ───────────────────────── обработка событий ─────────────────────────

def publish(st: dict, draft_id: str) -> None:
    d = st["drafts"][draft_id]
    ch = CONFIG["channels"][d["channel"]]
    img, sent = d.get("image"), False
    if img and Path(img).exists():
        try:
            if len(d["text"]) <= 1024:  # лимит подписи к фото в Telegram
                tg_photo(ch["chat_id"], img, caption=d["text"])
                sent = True
            else:
                tg_photo(ch["chat_id"], img)  # длинный пост пойдёт отдельным сообщением
        except Exception:
            traceback.print_exc()
    if not sent:
        send_html(ch["chat_id"], d["text"])
    d["status"] = "published"
    first_line = re.sub(r"<[^>]+>", "", d["text"]).strip().split("\n")[0][:80]
    st["history"].setdefault(d["channel"], []).append(first_line)
    save_state(st)


def safe_generate(st, channel_key, **kw):
    try:
        tg("sendChatAction", chat_id=OWNER_ID, action="typing")
        did = make_draft(st, channel_key, **kw)
        show_draft(st, did)
    except Exception as e:  # не роняем бота из-за одной ошибки
        traceback.print_exc()
        send_html(OWNER_ID, f"⚠️ Не получилось сделать черновик: {html.escape(str(e))[:500]}")


def on_callback(st: dict, cb: dict) -> None:
    if cb["from"]["id"] != OWNER_ID:
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Нет доступа")
        return
    action, _, draft_id = cb["data"].partition(":")
    d = st["drafts"].get(draft_id)
    if not d or d["status"] != "pending":
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Этот черновик уже обработан")
        return

    if action == "ok":
        publish(st, draft_id)
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Опубликовано")
        send_html(OWNER_ID, f"✅ Опубликовано в {html.escape(CONFIG['channels'][d['channel']]['title'])}")
    elif action == "no":
        d["status"] = "rejected"
        save_state(st)
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Отклонено")
        send_html(OWNER_ID, "❌ Черновик отклонён. Новый можно запросить: /draft")
    elif action == "edit":
        st["awaiting"] = {"draft_id": draft_id, "mode": "edit"}
        save_state(st)
        tg("answerCallbackQuery", callback_query_id=cb["id"])
        send_html(OWNER_ID, "✏️ Пришлите следующим сообщением <b>готовый текст поста</b> целиком. "
                            "Разметка: <code>&lt;b&gt;жирный&lt;/b&gt;</code>, <code>&lt;i&gt;курсив&lt;/i&gt;</code>.")
    elif action == "redo":
        st["awaiting"] = {"draft_id": draft_id, "mode": "feedback"}
        save_state(st)
        tg("answerCallbackQuery", callback_query_id=cb["id"])
        send_html(OWNER_ID, "🔁 Напишите, что поменять (например: «короче, добавь пример, без вступления»). "
                            "Или пришлите «-», чтобы переписать без пожеланий.")
    elif action == "img":
        d["image_skip"] = d.get("image_skip", 0) + 1
        d["image"] = find_image(d.get("image_query") or d["topic"], d["image_skip"])
        save_state(st)
        tg("answerCallbackQuery", callback_query_id=cb["id"],
           text="" if d["image"] else "Другой картинки не нашлось")
        show_draft(st, draft_id)
    elif action == "noimg":
        d["image"] = ""
        save_state(st)
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Картинка убрана")
        show_draft(st, draft_id)         


def on_message(st: dict, msg: dict) -> None:
    if msg["from"]["id"] != OWNER_ID:
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/start") or text.startswith("/help"):
        keys = ", ".join(CONFIG["channels"])
        send_html(OWNER_ID, "Команды:\n/draft — черновик для следующего канала по плану\n"
                            f"/draft КЛЮЧ — для конкретного канала ({html.escape(keys)})\n"
                            "/draft КЛЮЧ тема — черновик на вашу тему\n/status — состояние плана")
        return

    if text.startswith("/status"):
        plan = read_json(BASE / "plan.json", [])
        lines = [f"Нейросеть: {PROVIDER}"]
        for key in CONFIG["channels"]:
            total = sum(1 for p in plan if p["channel"] == key)
            lines.append(f"{key}: тем взято {st['plan_pos'].get(key, 0)} из {total}")
        pend = sum(1 for d in st["drafts"].values() if d["status"] == "pending")
        send_html(OWNER_ID, "\n".join(lines) + f"\nЧерновиков ждёт решения: {pend}")
        return

    if text.startswith("/draft"):
        parts = text.split(maxsplit=2)
        key = parts[1] if len(parts) > 1 else None
        if key and key not in CONFIG["channels"]:
            send_html(OWNER_ID, f"Нет такого канала: {html.escape(key)}")
            return
        keys = [key] if key else list(CONFIG["channels"])
        custom = parts[2] if len(parts) > 2 else None
        for k in keys:
            item = {"channel": k, "rubric": "по запросу", "topic": custom} if custom else None
            safe_generate(st, k, topic_item=item)
        return

    aw = st.get("awaiting")
    if aw and aw["draft_id"] in st["drafts"]:
        d = st["drafts"][aw["draft_id"]]
        st["awaiting"] = None
        if aw["mode"] == "edit":
            d["text"] = text
            d["notes"], d["sources"] = "", d.get("sources", [])
            save_state(st)
            show_draft(st, aw["draft_id"])
        else:
            d["status"] = "superseded"
            save_state(st)
            fb = "" if text == "-" else text
            item = {"channel": d["channel"], "rubric": d["rubric"], "topic": d["topic"]}
            safe_generate(st, d["channel"], topic_item=item, feedback=fb, previous=d["text"])
        return

    send_html(OWNER_ID, "Не понял. Команды: /draft, /status, /help")


def check_schedule(st: dict) -> None:
    """Раз в минуту проверяет, не пора ли делать черновики по расписанию."""
    now = datetime.now(TZ)
    today, hhmm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    minutes = now.hour * 60 + now.minute
    for key, ch in CONFIG["channels"].items():
        for t in ch.get("draft_times", []):
            mark = f"{key}:{t}"
            since = minutes - (int(t[:2]) * 60 + int(t[3:]))
            # запуск в своё время или позже (до 2 часов), один раз в день
            if hhmm >= t and since < 120 and st["last_run"].get(mark) != today:
                st["last_run"][mark] = today
                save_state(st)
                safe_generate(st, key)


def main() -> None:
    if not BOT_TOKEN or not OWNER_ID:
        sys.exit("Заполните TELEGRAM_BOT_TOKEN и OWNER_ID в файле .env (см. README.md)")
    if PROVIDER not in ("gigachat", "openai", "anthropic"):
        sys.exit("PROVIDER должен быть gigachat, openai или anthropic")
    st = load_state()
    print(f"Бот запущен (нейросеть: {PROVIDER}). Остановить: Ctrl+C")
    last_sched = 0.0
    while True:
        try:
            if time.time() - last_sched > 60:
                last_sched = time.time()
                check_schedule(st)
            updates = tg("getUpdates", offset=st["offset"], timeout=30,
                         allowed_updates=["message", "callback_query"])
            for u in updates:
                st["offset"] = u["update_id"] + 1
                if "callback_query" in u:
                    on_callback(st, u["callback_query"])
                elif "message" in u:
                    on_message(st, u["message"])
                save_state(st)
        except KeyboardInterrupt:
            print("Остановлено")
            return
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
