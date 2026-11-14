"""
Бот + сервер для Mini App «Рулетка» с покупкой фишек за Telegram Stars.
Один процесс: FastAPI (API и раздача index.html) + aiogram (бот, оплата).

Запуск:
    export BOT_TOKEN="123:ABC"
    export WEBAPP_URL="https://ваш-домен.com"   # HTTPS, адрес этого сервера
    export ADMIN_ID="123456789"
    uvicorn server:app --host 0.0.0.0 --port 8000
"""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    Message, PreCheckoutQuery, WebAppInfo,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
WEBAPP_URL = os.environ["WEBAPP_URL"].strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
STARS_PER_PACK = int(os.environ.get("STARS_PER_PACK", "50"))   # цена пакета в Stars
CHIPS_PER_PACK = int(os.environ.get("CHIPS_PER_PACK", "50"))   # фишек за пакет
STAKES = {5, 10, 25, 50}
RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------------- БД ----------------
db = sqlite3.connect("roulette.db", check_same_thread=False, isolation_level=None)
lock = threading.Lock()
db.executescript("""
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, chips INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY, user_id INTEGER, stars INTEGER, chips INTEGER,
    refunded INTEGER DEFAULT 0, ts INTEGER DEFAULT (strftime('%s','now')));
CREATE TABLE IF NOT EXISTS spins (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, bet TEXT, stake INTEGER,
    number INTEGER, payout INTEGER, ts INTEGER DEFAULT (strftime('%s','now')));
""")


def tx(fn):
    """Выполняет fn(db) в одной атомарной транзакции."""
    with lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            result = fn(db)
            db.execute("COMMIT")
            return result
        except Exception:
            db.execute("ROLLBACK")
            raise


def get_balance(uid: int) -> int:
    with lock:
        row = db.execute("SELECT chips FROM users WHERE id=?", (uid,)).fetchone()
    return row[0] if row else 0


def credit(charge_id: str, uid: int, stars: int, chips: int) -> bool:
    """Начисляет фишки один раз на платёж (защита от дублей)."""
    def run(c):
        cur = c.execute(
            "INSERT OR IGNORE INTO payments (charge_id,user_id,stars,chips) VALUES (?,?,?,?)",
            (charge_id, uid, stars, chips))
        if not cur.rowcount:
            return False
        c.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (uid,))
        c.execute("UPDATE users SET chips = chips + ? WHERE id=?", (chips, uid))
        return True
    return tx(run)


def do_spin(uid: int, bet: str, stake: int):
    number = secrets.randbelow(37)           # криптостойкий ГСЧ, только на сервере

    if bet == "zero":
        mult = 36 if number == 0 else 0
    elif number == 0:
        mult = 0
    elif bet == "red":
        mult = 2 if number in RED else 0
    elif bet == "black":
        mult = 2 if number not in RED else 0
    elif bet == "even":
        mult = 2 if number % 2 == 0 else 0
    else:
        mult = 2 if number % 2 == 1 else 0

    def run(c):
        row = c.execute("SELECT chips FROM users WHERE id=?", (uid,)).fetchone()
        if not row or row[0] < stake:
            return None
        payout = stake * mult
        c.execute("UPDATE users SET chips = chips - ? + ? WHERE id=?", (stake, payout, uid))
        c.execute("INSERT INTO spins (user_id,bet,stake,number,payout) VALUES (?,?,?,?,?)",
                  (uid, bet, stake, number, payout))
        return number, c.execute("SELECT chips FROM users WHERE id=?", (uid,)).fetchone()[0]
    return tx(run)


# ---------------- Проверка initData (подпись Telegram) ----------------
def verify_init_data(init_data: str) -> int:
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got_hash = pairs.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got_hash):
            raise ValueError("bad hash")
        if time.time() - int(pairs["auth_date"]) > 86400:
            raise ValueError("expired")
        return int(json.loads(pairs["user"])["id"])
    except Exception as e:
        print("initData check failed:", repr(e), "| initData length:", len(init_data or ""), flush=True)
        raise HTTPException(401, "Откройте приложение через Telegram")


# ---------------- Бот ----------------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎰 Играть", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("Рулетка. Фишки покупаются за ⭐ внутри приложения.", reply_markup=kb)


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok = q.currency == "XTR" and q.invoice_payload.startswith("chips:") \
        and q.total_amount % STARS_PER_PACK == 0
    await q.answer(ok=ok, error_message=None if ok else "Неверный платёж")


@dp.message(F.successful_payment)
async def on_paid(m: Message):
    p = m.successful_payment
    chips = p.total_amount // STARS_PER_PACK * CHIPS_PER_PACK
    credit(p.telegram_payment_charge_id, m.from_user.id, p.total_amount, chips)
    await m.answer(f"✅ Оплата прошла: +{chips} фишек. Баланс: {get_balance(m.from_user.id)}")


@dp.message(Command("refund"))
async def cmd_refund(m: Message):
    """Только админ: /refund <charge_id> — вернуть Stars и снять фишки."""
    if m.from_user.id != ADMIN_ID:
        return
    try:
        charge_id = m.text.split()[1]
        with lock:
            row = db.execute("SELECT user_id, chips, refunded FROM payments WHERE charge_id=?",
                             (charge_id,)).fetchone()
        if not row or row[2]:
            return await m.answer("Платёж не найден или уже возвращён.")
        await bot.refund_star_payment(user_id=row[0], telegram_payment_charge_id=charge_id)

        def run(c):
            c.execute("UPDATE payments SET refunded=1 WHERE charge_id=?", (charge_id,))
            c.execute("UPDATE users SET chips = MAX(0, chips - ?) WHERE id=?", (row[1], row[0]))
        tx(run)
        await m.answer("Возврат выполнен.")
    except Exception as e:
        await m.answer(f"Ошибка: {e}\nФормат: /refund <charge_id>")


# ---------------- API ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(dp.start_polling(bot))
    yield
    task.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse("index.html", headers={"Cache-Control": "no-store"})


@app.post("/api/balance")
async def api_balance(req: Request):
    uid = verify_init_data((await req.json()).get("initData", ""))
    return {"balance": get_balance(uid)}


@app.post("/api/invoice")
async def api_invoice(req: Request):
    uid = verify_init_data((await req.json()).get("initData", ""))
    try:
        link = await bot.create_invoice_link(
            title="Пакет фишек",
            description=f"{CHIPS_PER_PACK} игровых фишек для рулетки",
            payload=f"chips:{uid}",
            currency="XTR",
            prices=[LabeledPrice(label="Фишки", amount=STARS_PER_PACK)],
        )
    except Exception as e:
        print("create_invoice_link failed:", repr(e), flush=True)
        raise HTTPException(500, f"Telegram не создал счёт: {e}")
    return {"link": link}


@app.post("/api/spin")
async def api_spin(req: Request):
    body = await req.json()
    uid = verify_init_data(body.get("initData", ""))
    bet, stake = body.get("bet"), body.get("stake")
    if bet not in {"red", "black", "even", "odd", "zero"} or stake not in STAKES:
        raise HTTPException(400, "Неверная ставка")
    result = do_spin(uid, bet, stake)
    if result is None:
        raise HTTPException(400, "Недостаточно фишек")
    number, balance = result
    return {"number": number, "balance": balance}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
