"""
Умный бот для группы ВКонтакте танцевального зала.

Что умеет:
- Новому подписчику пишет приветствие и дарит первое занятие для ребёнка.
  (ВКонтакте разрешает писать первым только тем, кто разрешил сообщения от группы.
   Если не разрешил — бот напишет, как только человек нажмёт «Разрешить сообщения»
   или сам напишет в группу.)
- Отвечает на вопросы как живой администратор (через Claude), строго по knowledge.md.
- Собирает заявку (имя ребёнка, возраст, телефон) и пересылает её администраторам.
- Если администратор сам ответил человеку в диалоге — бот замолкает в этом диалоге на 12 часов.

Запуск: python bot.py   (настройки — в переменных окружения, см. .env.example)
"""
import os, json, time, random, re, sqlite3, logging, threading
import requests

VK_TOKEN = os.environ["VK_GROUP_TOKEN"]
GROUP_ID = int(os.environ["VK_GROUP_ID"])
PROVIDER = os.environ.get("LLM_PROVIDER", "yandex").lower()   # yandex | anthropic
YANDEX_KEY = os.environ.get("YANDEX_API_KEY", "")
YANDEX_FOLDER = os.environ.get("YANDEX_FOLDER_ID", "")
YANDEX_MODEL = os.environ.get("YANDEX_MODEL", "yandexgpt/latest")  # или yandexgpt-lite/latest — дешевле
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_VK_IDS", "").replace(" ", "").split(",") if x]
PAUSE_HOURS = float(os.environ.get("PAUSE_AFTER_ADMIN_HOURS", "12"))
V = "5.199"
HERE = os.path.dirname(os.path.abspath(__file__))
KNOWLEDGE = open(os.path.join(HERE, "knowledge.md"), encoding="utf-8").read()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.info

WELCOME = (
    "Здравствуйте! Спасибо, что подписались на студию KS 🤍\n\n"
    "Дарим вашему ребёнку первое занятие по спортивным бальным танцам — бесплатно.\n\n"
    "Группы с нуля на Лухмановской, 8:\n"
    "▫️ 3–5 лет — вт/чт 17:00\n"
    "▫️ 6–10 лет — вт/чт 18:00\n\n"
    "Напишите, пожалуйста, имя и возраст ребёнка — подберём группу и запишем на подарочное занятие."
)

SYSTEM = f"""Ты — администратор танцевальной студии KS (спортивные бальные танцы для детей и взрослых) в группе ВКонтакте.
Пиши как живой, тёплый и вежливый человек: коротко (1–4 предложения), на «вы», без канцелярита, можно 1 эмодзи.
Отвечай ТОЛЬКО по фактам из базы знаний ниже. Если ответа нет в базе — не выдумывай: скажи, что уточнишь у тренера, и добавь в конец тег [АДМИН].
Твоя цель — мягко довести родителя до записи на подарочное первое занятие. Для записи нужны: имя ребёнка, возраст, телефон родителя.
Узнавай их по одному, не всё сразу. Возраст помогает выбрать группу (3–5 или 6–10 лет).
Когда у тебя есть имя, возраст и телефон — подтверди запись (скажи, что администратор свяжется и подтвердит время) и добавь в конец строку:
[ЗАЯВКА: имя=...; возраст=...; телефон=...; группа=...]
Если человек грубит, пишет не по теме или просит позвать человека — вежливо ответь и добавь [АДМИН].
Никогда не обещай скидки, даты турниров и того, чего нет в базе. Не проси паспортные и платёжные данные.

БАЗА ЗНАНИЙ:
{KNOWLEDGE}
"""

# ---------- память ----------
db = sqlite3.connect(os.path.join(HERE, "bot.db"), check_same_thread=False)
db.execute("create table if not exists msgs(uid int, role text, text text, ts real)")
db.execute("create table if not exists users(uid int primary key, welcomed int default 0, paused_until real default 0)")
db.commit()
lock = threading.Lock()

def user(uid):
    with lock:
        r = db.execute("select welcomed, paused_until from users where uid=?", (uid,)).fetchone()
        if not r:
            db.execute("insert into users(uid) values(?)", (uid,)); db.commit(); r = (0, 0)
        return {"welcomed": r[0], "paused_until": r[1]}

def set_user(uid, **kw):
    user(uid)
    with lock:
        for k, v in kw.items():
            db.execute(f"update users set {k}=? where uid=?", (v, uid))
        db.commit()

def remember(uid, role, text):
    with lock:
        db.execute("insert into msgs values(?,?,?,?)", (uid, role, text, time.time())); db.commit()

def history(uid, n=16):
    with lock:
        rows = db.execute("select role, text from msgs where uid=? order by ts desc limit ?", (uid, n)).fetchall()
    msgs = [{"role": r, "content": t} for r, t in reversed(rows)]
    while msgs and msgs[0]["role"] != "user":   # Claude требует начинать с user
        msgs.pop(0)
    return msgs

# ---------- VK ----------
def vk(method, **params):
    params.update(access_token=VK_TOKEN, v=V)
    r = requests.post(f"https://api.vk.com/method/{method}", data=params, timeout=30).json()
    if "error" in r:
        raise RuntimeError(f"{method}: {r['error'].get('error_msg')}")
    return r["response"]

def send(peer_id, text):
    vk("messages.send", peer_id=peer_id, message=text, random_id=random.randint(1, 2**31))

def allowed(uid):
    try:
        return vk("messages.isMessagesFromGroupAllowed", group_id=GROUP_ID, user_id=uid)["is_allowed"] == 1
    except Exception as e:
        log(f"isAllowed err {e}"); return False

def user_link(uid):
    try:
        u = vk("users.get", user_ids=uid)[0]
        return f"{u['first_name']} {u['last_name']} (vk.com/id{uid})"
    except Exception:
        return f"vk.com/id{uid}"

def notify_admins(text):
    for a in ADMIN_IDS:
        try: send(a, text)
        except Exception as e: log(f"notify {a} err {e}")

# ---------- нейросеть ----------
def ask_yandex(uid):
    msgs = [{"role": "system", "text": SYSTEM}] + [{"role": m["role"], "text": m["content"]} for m in history(uid)]
    body = {"modelUri": f"gpt://{YANDEX_FOLDER}/{YANDEX_MODEL}",
            "completionOptions": {"stream": False, "temperature": 0.3, "maxTokens": "500"},
            "messages": msgs}
    r = requests.post("https://llm.api.cloud.yandex.net/foundationModels/v1/completion", json=body, timeout=60,
                      headers={"Authorization": f"Api-Key {YANDEX_KEY}", "x-folder-id": YANDEX_FOLDER})
    r.raise_for_status()
    return r.json()["result"]["alternatives"][0]["message"]["text"].strip()

def ask_anthropic(uid):
    body = {"model": MODEL, "max_tokens": 400, "system": SYSTEM, "messages": history(uid)}
    r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=60, headers={
        "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"]).strip()

def ask_claude(uid):
    return ask_anthropic(uid) if PROVIDER == "anthropic" else ask_yandex(uid)

# ---------- логика ----------
def welcome(uid, reason):
    u = user(uid)
    if u["welcomed"]:
        return
    if not allowed(uid):
        log(f"{uid}: {reason}, но сообщения от группы не разрешены — ждём"); return
    send(uid, WELCOME); remember(uid, "assistant", WELCOME); set_user(uid, welcomed=1)
    log(f"{uid}: приветствие отправлено ({reason})")

def on_message(msg):
    uid = msg["from_id"]; text = (msg.get("text") or "").strip()
    if uid <= 0 or msg.get("peer_id") != uid:
        return  # только личные сообщения от людей
    u = user(uid)
    if not text:
        text = "(прислал вложение без текста)"
    remember(uid, "user", text)
    if time.time() < u["paused_until"]:
        log(f"{uid}: пауза, отвечает администратор"); return
    if not u["welcomed"]:
        set_user(uid, welcomed=1)  # человек сам написал — подарок упомянет Claude по базе
    try:
        answer = ask_claude(uid)
    except Exception as e:
        log(f"claude err {e}")
        answer = "Спасибо за сообщение! Администратор ответит вам совсем скоро 🤍 [АДМИН]"
    lead = re.search(r"\[ЗАЯВКА:(.*?)\]", answer)
    need_admin = "[АДМИН]" in answer
    clean = re.sub(r"\[ЗАЯВКА:.*?\]|\[АДМИН\]", "", answer).strip()
    send(uid, clean); remember(uid, "assistant", clean)
    if lead:
        notify_admins(f"🎉 Новая заявка на подарочное занятие\n{lead.group(1).strip()}\nОт: {user_link(uid)}")
    elif need_admin:
        notify_admins(f"❓ Нужен ответ человека\nВопрос: {text}\nОт: {user_link(uid)}\nОткройте диалог в сообщениях группы.")

def on_reply(msg):
    # исходящее сообщение группы; если его написал админ руками — бот замолкает в диалоге
    if msg.get("admin_author_id"):
        set_user(msg["peer_id"], paused_until=time.time() + PAUSE_HOURS * 3600, welcomed=1)
        log(f"{msg['peer_id']}: админ ответил сам, пауза {PAUSE_HOURS} ч")

def handle(ev):
    t, o = ev["type"], ev["object"]
    if t == "message_new":
        on_message(o["message"])
    elif t == "message_reply":
        on_reply(o)
    elif t == "group_join":
        welcome(o["user_id"], "подписка")
    elif t == "message_allow":
        welcome(o["user_id"], "разрешил сообщения")

def main():
    log("бот запущен")
    while True:
        try:
            s = vk("groups.getLongPollServer", group_id=GROUP_ID)
            server, key, ts = s["server"], s["key"], s["ts"]
            while True:
                r = requests.get(server, params={"act": "a_check", "key": key, "ts": ts, "wait": 25}, timeout=40).json()
                if "failed" in r:
                    if r["failed"] == 1: ts = r["ts"]; continue
                    break  # 2/3 — получить новый ключ
                ts = r["ts"]
                for ev in r.get("updates", []):
                    threading.Thread(target=lambda e=ev: _safe(e), daemon=True).start()
        except Exception as e:
            log(f"longpoll err {e}"); time.sleep(5)

def _safe(ev):
    try: handle(ev)
    except Exception as e: log(f"event err {e}")

if __name__ == "__main__":
    main()
