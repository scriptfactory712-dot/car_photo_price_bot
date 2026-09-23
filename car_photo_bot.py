#!/usr/bin/env python3
"""
Car Photo Helper — бот-оценщик авто по фото.
Кидаешь фото машины с OLX -> бот говорит что за машина и сколько стоит.
Зрение: Google Gemini (ключ в env GEMINI_KEY). Только stdlib + requests.
"""
import os, sys, json, time, base64, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import requests
except ImportError:
    print("Нужно: pip install requests")
    sys.exit(1)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()

API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

START_TEXT = (
    "👋 Привет! Я помощник по авто.\n\n"
    "📸 Пришли мне ФОТО машины (можно несколько штук, лучше снаружи + салон).\n"
    "А в подписи к фото напиши цену из объявления, например: `4500$` или `180000 грн`.\n\n"
    "В ответ скажу:\n"
    "• что за машина (марка, модель, годы)\n"
    "• сколько такая реально стоит в Украине\n"
    "• норм ли цена продавца или дорого\n"
    "• на что смотреть при осмотре\n\n"
    "Погнали — кидай фото! 🚗"
)

PROMPT = (
    "Ты эксперт по авторынку Украины. По фото определи автомобиль и ответь по-русски, коротко и по делу, структурой:\n"
    "1. 🚗 Что за авто: марка, модель, поколение, годы выпуска. Если не уверен — так и скажи и дай 2 варианта.\n"
    "2. 👀 Что видно по фото: состояние кузова/салона, комплектация (если видно).\n"
    "3. 💰 Сколько такая стоит в Украине сейчас: вилка в $ и грн для среднего состояния.\n"
    "4. 🔍 На что смотреть при осмотре именно этой модели (3-5 болячек).\n"
    "{price_line}\n"
    "Важно: год по фото точно не определить — укажи диапазон и предупреди, что оценка примерная."
)


def tg(method, **kwargs):
    r = requests.post(API + "/" + method, data=kwargs, timeout=20)
    return r.json()


def send_text(chat_id, text, reply_to=None):
    # режем длинные ответы под лимит TG 4096
    for i in range(0, len(text), 3900):
        chunk = text[i:i + 3900]
        data = {"chat_id": chat_id, "text": chunk}
        if reply_to:
            data["reply_to_message_id"] = reply_to
        try:
            tg("sendMessage", **data)
        except Exception as e:
            print("send err:", e, flush=True)
        time.sleep(0.3)


def get_file_bytes(file_id):
    info = tg("getFile", file_id=file_id)
    path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.content


def ask_gemini(image_bytes, seller_price=""):
    """Фото -> текст оценки. Возвращает строку."""
    if seller_price:
        price_line = f"5. ⚖️ Вердикт: продавец просит {seller_price} — скажи честно: норм, торговаться или дорого и почему."
    else:
        price_line = "5. ⚖️ Вердикт: цену продавца не знаю — попроси прислать цену для вердикта."
    prompt = PROMPT.format(price_line=price_line)
    b64 = base64.b64encode(image_bytes).decode()
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            ]
        }],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1200},
    }
    r = requests.post(url, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError("Gemini пустой ответ: " + json.dumps(data)[:300])


def handle_photo(msg):
    chat_id = msg["chat"]["id"]
    mid = msg.get("message_id")
    caption = (msg.get("caption") or "").strip()
    photos = msg.get("photo") or []
    if not photos:
        return
    if not GEMINI_KEY:
        send_text(chat_id,
                  "⚠️ Я еще не подключен к ИИ-зрению: нет ключа GEMINI_KEY.\n"
                  "Вставь ключ в Render → Environment → GEMINI_KEY и перезапусти.",
                  reply_to=mid)
        return
    file_id = photos[-1]["file_id"]  # самое большое фото
    send_text(chat_id, "🔍 Смотрю фото, секунд 10-20...", reply_to=mid)
    try:
        img = get_file_bytes(file_id)
        answer = ask_gemini(img, seller_price=caption)
        send_text(chat_id, answer, reply_to=mid)
    except Exception as e:
        print("photo err:", e, flush=True)
        send_text(chat_id, f"😕 Не смог разобрать фото ({e}). Попробуй другое фото, лучше днем снаружи.",
                  reply_to=mid)


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK - car bot alive")

        def log_message(self, *a):
            pass

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"Health server on :{port}", flush=True)
    except Exception as e:
        print("Health server err:", e, flush=True)


def main():
    if not BOT_TOKEN:
        print("ВНИМАНИЕ: задай BOT_TOKEN через env!", flush=True)
        sys.exit(1)
    me = tg("getMe")
    print("Bot:", me.get("result", {}).get("username"), flush=True)
    print("Gemini key:", "OK" if GEMINI_KEY else "MISSING", flush=True)
    start_health_server()
    offset = 0
    print("Polling started", flush=True)
    while True:
        try:
            r = requests.post(API + "/getUpdates",
                              data={"offset": offset, "timeout": 25},
                              timeout=35).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = msg.get("chat", {}).get("id")
                if not chat_id:
                    continue
                text = (msg.get("text") or "").strip()
                if text.startswith("/start"):
                    send_text(chat_id, START_TEXT)
                elif msg.get("photo"):
                    handle_photo(msg)
                elif text:
                    send_text(chat_id, "📸 Пришли фото машины, а цену напиши в подписи к фото.")
        except Exception as e:
            print("poll err:", e, flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
