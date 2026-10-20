#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FROGI ARENA • ALL IN ONE • Telegram bot + responsive Mini App + HTTP API + SQLite.
Python 3.10+. No pip packages, Node.js, npm, Vite, React, or other files required.

RENDER (Web Service, Language=Python):
    Build command:  echo "No dependencies"
    Start command:  python bot.py
    Environment:    BOT_TOKEN=123456:your_telegram_bot_token
    Optional:       ADMIN_IDS=123456789,987654321
                    ADMIN_TOKEN=<long_random_secret>
                    DATABASE_PATH=/var/data/frogi.sqlite (paid persistent disk only)
                    PUBLIC_URL=https://your-custom-domain.example (otherwise Render auto detects)
    Render injects PORT and RENDER_EXTERNAL_URL automatically. One process only.

LOCAL:
    BOT_TOKEN=... ALLOW_DEMO_AUTH=true python bot.py
    Open http://127.0.0.1:10000 for local, demo-only testing.
    If PUBLIC_URL is empty, the bot can poll Telegram from this same process.

SECURITY:
    - Telegram signed initData verification (HMAC, auth_date, duplicate keys rejected)
    - Private webhook URL and Telegram secret_token header verification
    - Constant-time checks, server-side sessions (hashed tokens), expiration
    - All credit mutations protected by SQLite BEGIN IMMEDIATE transaction
    - Exactly-once operations when clients provide idempotency keys
    - No real payments, cash-out, token, cryptocurrency transfers, or real gambling
    - Only user-scoped account queries; no client-provided user identifiers

DATA PERSISTENCE WARNING:
    SQLite data is a real local file, not a durable cloud database. Render Free
    deletes local files during spin-down, restart or deployment. To retain data,
    attach a persistent disk on PAID Render plan and set DATABASE_PATH to path
    inside the mounted disk. Don't promise durable accounts on Render Free.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import signal
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

LOG = logging.getLogger('frogi')
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(), format='%(asctime)s %(levelname)s %(name)s: %(message)s')
TOKEN = (os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()
PORT = int(os.getenv('PORT', '10000'))
PUBLIC_URL = (os.getenv('PUBLIC_URL') or os.getenv('WEBAPP_URL') or os.getenv('RENDER_EXTERNAL_URL') or '').strip().rstrip('/')
if PUBLIC_URL and (not PUBLIC_URL.startswith('https://') or urlsplit(PUBLIC_URL).username or urlsplit(PUBLIC_URL).password or urlsplit(PUBLIC_URL).query or urlsplit(PUBLIC_URL).fragment):
    raise RuntimeError('PUBLIC_URL must be a clean https://... URL')
DB_PATH = os.getenv('DATABASE_PATH') or os.getenv('DB_PATH') or 'frogi.sqlite3'
ADMIN_IDS = {int(x) for x in os.getenv('ADMIN_IDS', os.getenv('TELEGRAM_ADMIN_IDS', '')).split(',') if x.strip().isdigit()}
ADMIN_TOKEN = os.getenv('ADMIN_TOKEN', '')
DEMO_AUTH = os.getenv('ALLOW_DEMO_AUTH', '').lower() == 'true' and not PUBLIC_URL
SPIN_COST = 100
DAY_REWARD = 75
PROMOS = {'FROGI2026': 250, 'HELLOFROGI': 100, 'ARENASTART': 150}
PRIZE_WEIGHTS = [('frog_common', 6500), ('red_crystal', 2500), ('blue_orbit', 850), ('gold_frog', 150)]
RARITY_POINTS = {'Common': 10, 'Rare': 30, 'Epic': 80, 'Legendary': 200}
RARITY_ORDER = {'Common': 1, 'Rare': 2, 'Epic': 3, 'Legendary': 4}
SESSION_SECONDS = 60 * 60 * 24 * 7
WEBHOOK_SECRET = hmac.new(TOKEN.encode(), b'frogi-webhook-secret-v1', hashlib.sha256).hexdigest() if TOKEN else ''
WEBHOOK_PATH = '/tg/' + (hmac.new(TOKEN.encode(), b'frogi-webhook-path-v1', hashlib.sha256).hexdigest()[:30] if TOKEN else 'disabled')
LOCK = threading.RLock()

# The following catalog, missions, milestones and announcements are copied from
# the original FROGI 2.1 project so previous demo prices and probabilities remain aligned.
CATALOG = json.loads(r'''[["frog_common","Frog Pixel","Common",120,"🐸","Небольшой пиксельный герой твоей коллекции."],["red_crystal","Red Crystal","Rare",350,"🔴","Алый кристалл из глубин FROGI Arena."],["blue_orbit","Blue Orbit","Epic",700,"🔵","Сфера с сиянием далёкой галактики."],["gold_frog","Golden Frog","Legendary",1800,"👑","Корона главного коллекционера."],["neon_lily","Neon Lily","Common",145,"🌸","Светящийся цветок из пруда будущего."],["pixel_star","Pixel Star","Common",160,"⭐","Звезда для тех, кто только начинает путь."],["cosmic_frog","Cosmic Frog","Rare",460,"🪐","Космический исследователь новых миров."],["emerald_echo","Emerald Echo","Rare",480,"💚","Изумрудный сигнал из скрытой вселенной."],["cyber_crown","Cyber Crown","Epic",890,"💠","Корона цифровой королевской династии."],["thunder_core","Thunder Core","Epic",980,"⚡","Редкий источник энергии арены."],["solar_relic","Solar Relic","Legendary",2250,"☀️","Древняя реликвия цифрового солнца."],["royal_frog","Royal Frog","Legendary",3200,"🏆","Трофей для истинных легенд FROGI."],["moon_charm","Moon Charm","Rare",390,"🌙","Лунный талисман из ночной серии."],["aurora_gem","Aurora Gem","Epic",840,"💎","Самоцвет с северным свечением."],["tiny_rocket","Tiny Rocket","Common",180,"🚀","Маленький корабль больших открытий."],["nebula_gate","Nebula Gate","Legendary",2900,"🌌","Портал в редкую галактическую коллекцию."]]''')
MILESTONES = json.loads(r'''[{"id":"hello_frogi","title":"Добро пожаловать","icon":"🐸","details":"Создай профиль FROGI","category":"start","threshold":1},{"id":"first_spin","title":"Первое вращение","icon":"🎡","details":"Сыграй один демо-раунд колеса","category":"spins","threshold":1},{"id":"spin_5","title":"Удача любит смелых","icon":"⚡","details":"Сыграй пять раундов колеса","category":"spins","threshold":5},{"id":"spin_20","title":"Мастер арены","icon":"🏆","details":"Сыграй двадцать раундов колеса","category":"spins","threshold":20},{"id":"collector_1","title":"Начало коллекции","icon":"🎁","details":"Получить первый предмет","category":"collection","threshold":1},{"id":"collector_5","title":"Коллекционер","icon":"💎","details":"Собери пять предметов","category":"collection","threshold":5},{"id":"collector_15","title":"Хранитель реликвий","icon":"👑","details":"Собери пятнадцать предметов","category":"collection","threshold":15},{"id":"rare_finder","title":"Охотник за редкостями","icon":"🔮","details":"Получить предмет Rare или выше","category":"rare","threshold":1},{"id":"epic_finder","title":"Эпическая находка","icon":"🌌","details":"Получить предмет Epic или выше","category":"epic","threshold":1},{"id":"legend_finder","title":"Легенда коллекции","icon":"🌟","details":"Получить предмет Legendary","category":"legendary","threshold":1},{"id":"market_first","title":"Умная покупка","icon":"🛍️","details":"Купить предмет в маркете","category":"purchases","threshold":1},{"id":"market_10","title":"Посетитель маркета","icon":"📦","details":"Сделай десять покупок","category":"purchases","threshold":10},{"id":"daily_first","title":"Ежедневный гость","icon":"📅","details":"Забери ежедневный бонус","category":"dailies","threshold":1},{"id":"daily_7","title":"Верный игрок","icon":"🔥","details":"Забери бонус в семь разных дней","category":"dailies","threshold":7},{"id":"favorites_3","title":"Любимые предметы","icon":"❤️","details":"Добавь три предмета в избранное","category":"favorites","threshold":3},{"id":"promotional","title":"Знаешь секрет","icon":"🎟️","details":"Активируй первый промокод","category":"promos","threshold":1}]''')
MISSIONS = json.loads(r'''[{"id":"spin_1","title":"Попробуй удачу","description":"Сыграй один раунд","icon":"🎡","group":"spins","target":1},{"id":"spin_5","title":"Пять вращений","description":"Проведи пять демо-раундов","icon":"⚡","group":"spins","target":5},{"id":"collection_3","title":"Три сокровища","description":"Собери три подарка","icon":"🎁","group":"collection","target":3},{"id":"collection_10","title":"Пополнение инвентаря","description":"Собери десять подарков","icon":"💎","group":"collection","target":10},{"id":"market_1","title":"Первый заказ","description":"Купи предмет в маркете","icon":"🛍️","group":"purchases","target":1},{"id":"market_3","title":"Серия покупок","description":"Купи три предмета в маркете","icon":"🛒","group":"purchases","target":3},{"id":"daily_1","title":"Не пропусти бонус","description":"Забери ежедневные FC","icon":"📅","group":"dailies","target":1},{"id":"favorites_1","title":"Запомни любимое","description":"Отметь предмет сердцем","icon":"❤️","group":"favorites","target":1}]''')
NEWS = json.loads(r'''[{"id":"beta-01","type":"update","icon":"🚀","title":"FROGI Arena 2.0","text":"Каталог расширен, появились миссии, избранное и история активности.","date":"2026-10-20"},{"id":"beta-02","type":"fairness","icon":"🛡️","title":"Важное об игре","text":"Результат колеса выбирает сервер. В демоверсии FC не имеют денежной стоимости.","date":"2026-10-20"},{"id":"beta-03","type":"tips","icon":"💡","title":"Совет коллекционеру","text":"Фильтруй подарки по редкости и добавляй любимые в избранное.","date":"2026-10-20"}]''')

class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def fail(code: str, message: str, status: int = 400):
    raise AppError(code, message, status)


def utc_day():
    return datetime.now(timezone.utc).date().isoformat()


def now_seconds():
    return int(time.time())


def connection():
    c = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=20000')
    return c


@contextmanager
def transaction():
    # Avoid simultaneous writes from webhook, Mini App and demo HTTP requests.
    with LOCK:
        c = connection()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.execute('COMMIT')
        except BaseException:
            if c.in_transaction:
                c.execute('ROLLBACK')
            raise
        finally:
            c.close()


@contextmanager
def readonly():
    c = connection()
    try:
        yield c
    finally:
        c.close()


def row(c, sql, params=()):
    result = c.execute(sql, params).fetchone()
    return dict(result) if result else None


def rows(c, sql, params=()):
    return [dict(x) for x in c.execute(sql, params).fetchall()]


def count(c, sql, params=()):
    return c.execute(sql, params).fetchone()[0]


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    # sqlite3.executescript() commits implicitly, so run the schema DDL first
    # under the global lock; seed/maintenance use an explicit transaction below.
    with LOCK:
        c = connection()
        try:
            c.executescript('''
          CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, telegram_id TEXT UNIQUE, username TEXT,
            first_name TEXT NOT NULL DEFAULT 'FROGI User', balance INTEGER NOT NULL DEFAULT 1000 CHECK(balance>=0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
          );
          CREATE TABLE IF NOT EXISTS gifts (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, rarity TEXT NOT NULL, price INTEGER NOT NULL CHECK(price>=0),
            emoji TEXT NOT NULL, description TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
          );
          CREATE TABLE IF NOT EXISTS inventory (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
            gift_id TEXT NOT NULL REFERENCES gifts(id), acquired_at TEXT DEFAULT CURRENT_TIMESTAMP,
            source TEXT NOT NULL DEFAULT 'demo'
          );
          CREATE INDEX IF NOT EXISTS idx_inventory_user ON inventory(user_id, acquired_at DESC);
          CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(id),
            delta INTEGER NOT NULL, reason TEXT NOT NULL, metadata TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
          );
          CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, id DESC);
          CREATE TABLE IF NOT EXISTS spins (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(id),
            cost INTEGER NOT NULL, prize_gift_id TEXT NOT NULL REFERENCES gifts(id),
            result TEXT NOT NULL DEFAULT 'win', created_at TEXT DEFAULT CURRENT_TIMESTAMP
          );
          CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
            expires_at INTEGER NOT NULL, revoked_at INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP
          );
          CREATE INDEX IF NOT EXISTS idx_session_exp ON sessions(expires_at);
          CREATE TABLE IF NOT EXISTS idempotency (
            user_id TEXT NOT NULL REFERENCES users(id), operation TEXT NOT NULL,
            key TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, operation, key)
          );
          CREATE TABLE IF NOT EXISTS daily_claims (
            user_id TEXT NOT NULL REFERENCES users(id), day TEXT NOT NULL, amount INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id,day)
          );
          CREATE TABLE IF NOT EXISTS promo_redemptions (
            user_id TEXT NOT NULL REFERENCES users(id), code TEXT NOT NULL, amount INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id,code)
          );
          CREATE TABLE IF NOT EXISTS favorites (
            user_id TEXT NOT NULL REFERENCES users(id),
            inventory_id TEXT NOT NULL REFERENCES inventory(id) ON DELETE CASCADE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id,inventory_id)
          );
          CREATE TABLE IF NOT EXISTS settings (
            user_id TEXT PRIMARY KEY REFERENCES users(id), sound INTEGER NOT NULL DEFAULT 1,
            vibration INTEGER NOT NULL DEFAULT 1, reduce_motion INTEGER NOT NULL DEFAULT 0,
            show_on_leaderboard INTEGER NOT NULL DEFAULT 1, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
          );
          CREATE TABLE IF NOT EXISTS achievements (
            user_id TEXT NOT NULL REFERENCES users(id), achievement_id TEXT NOT NULL,
            unlocked_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id,achievement_id)
          );
          CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL, read_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
          );
          CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id,id DESC);
          CREATE TABLE IF NOT EXISTS support_tickets (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
            category TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
          );
          CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
            action TEXT NOT NULL, target TEXT NOT NULL, details TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
          );
          CREATE TABLE IF NOT EXISTS processed_updates (
            update_id INTEGER PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP
          );
          CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
          );
        ''')
        finally:
            c.close()
    with transaction() as c:
        c.executemany('''INSERT OR IGNORE INTO gifts(id,title,rarity,price,emoji,description)
                         VALUES(?,?,?,?,?,?)''', CATALOG)
        c.execute('DELETE FROM sessions WHERE expires_at < ?', (now_seconds(),))
        c.execute('DELETE FROM idempotency WHERE created_at < datetime(\'now\',\'-14 days\')')
        c.execute('DELETE FROM processed_updates WHERE created_at < datetime(\'now\',\'-3 days\')')
    LOG.info('SQLite initialized: %s', DB_PATH)


def public_user(u):
    return {'id': u['id'], 'username': u['username'] or '',
            'firstName': u['first_name'], 'balance': u['balance'], 'createdAt': u['created_at']}


def provision(c, telegram_id: int | str | None, username='', first_name='FROGI User'):
    if telegram_id is not None:
        tid = str(telegram_id)
        if not re.fullmatch(r'[1-9][0-9]{0,15}', tid):
            fail('BAD_USER', 'Неверный Telegram ID.', 401)
        uid = 'tg_' + tid
        c.execute('''INSERT OR IGNORE INTO users(id,telegram_id,username,first_name)
                     VALUES(?,?,?,?)''', (uid, tid, str(username or '')[:64], str(first_name or 'FROGI User')[:100]))
        c.execute('UPDATE users SET username=?,first_name=? WHERE id=?',
                  (str(username or '')[:64], str(first_name or 'FROGI User')[:100], uid))
    else:
        uid = 'demo_local_user'
        c.execute('''INSERT OR IGNORE INTO users(id,username,first_name) VALUES(?,?,?)''',
                  (uid, 'demo_frogi', 'Demo Player'))
    sync_achievements(c, uid)
    return row(c, 'SELECT * FROM users WHERE id=?', (uid,))


def verify_init_data(init_data: str):
    if not TOKEN or not isinstance(init_data, str) or not 0 < len(init_data) <= 8192:
        return None
    try:
        fields = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        values = dict(fields)
        if len(values) != len(fields):
            return None
        provided = values.pop('hash', '')
        values.pop('signature', None)
        if not re.fullmatch(r'[a-fA-F0-9]{64}', provided):
            return None
        check = '\n'.join(f'{k}={v}' for k, v in sorted(values.items()))
        key = hmac.new(b'WebAppData', TOKEN.encode('utf-8'), hashlib.sha256).digest()
        expected = hmac.new(key, check.encode('utf-8'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided.lower(), expected):
            return None
        auth_date = values.get('auth_date', '')
        if not re.fullmatch(r'[0-9]{10,12}', auth_date):
            return None
        delta = now_seconds() - int(auth_date)
        if delta < -60 or delta > 86400:
            return None
        u = json.loads(values.get('user', '{}'))
        if not isinstance(u, dict) or not isinstance(u.get('id'), int) or isinstance(u.get('id'), bool) or not (0 < u['id'] < 2**53):
            return None
        return {'id': u['id'], 'username': str(u.get('username') or '')[:64],
                'first_name': str(u.get('first_name') or 'FROGI User')[:100]}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def auth_session(c, init_data, demo=False):
    identity = verify_init_data(init_data)
    if identity:
        u = provision(c, identity['id'], identity['username'], identity['first_name'])
    elif demo is True and not init_data and DEMO_AUTH:
        u = provision(c, None)
    else:
        fail('AUTH_FAILED', 'Открой FROGI через Telegram-бота. На публичном сайте вход только через Telegram.', 401)
    token = secrets.token_urlsafe(32)
    expiry = now_seconds() + SESSION_SECONDS
    c.execute('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',
              (hashlib.sha256(token.encode()).hexdigest(), u['id'], expiry))
    return {'user': public_user(u), 'token': token, 'expiresAt': expiry, 'demoMode': identity is None}


def authenticated_user(c, authorization):
    m = re.fullmatch(r'Bearer ([A-Za-z0-9_-]{32,160})', authorization or '')
    if not m:
        fail('UNAUTHORIZED', 'Открой приложение заново через Telegram.', 401)
    session = row(c, '''SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                        WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?''',
                  (hashlib.sha256(m.group(1).encode()).hexdigest(), now_seconds()))
    if not session:
        fail('UNAUTHORIZED', 'Сессия истекла. Перезапусти Mini App.', 401)
    return session


def consume(c, uid, amount):
    if c.execute('UPDATE users SET balance=balance-? WHERE id=? AND balance>=?', (amount, uid, amount)).rowcount != 1:
        fail('INSUFFICIENT_BALANCE', 'Недостаточно тестовых FC.')


def add_credit(c, uid, amount, reason, metadata=None):
    c.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, uid))
    c.execute('INSERT INTO ledger(user_id,delta,reason,metadata) VALUES(?,?,?,?)',
              (uid, amount, reason, json.dumps(metadata or {}, ensure_ascii=False)))


def prize_pick():
    ticket = secrets.randbelow(sum(w for _, w in PRIZE_WEIGHTS))
    for gift_id, weight in PRIZE_WEIGHTS:
        if ticket < weight:
            return gift_id
        ticket -= weight
    return PRIZE_WEIGHTS[-1][0]


def once(c, uid, op, key, function):
    key = key if isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_-]{12,100}', key) else None
    if key:
        existing = row(c, 'SELECT response FROM idempotency WHERE user_id=? AND operation=? AND key=?', (uid, op, key))
        if existing:
            return {**json.loads(existing['response']), 'replayed': True}
    result = function()
    if key:
        c.execute('INSERT INTO idempotency(user_id,operation,key,response) VALUES(?,?,?,?)',
                  (uid, op, key, json.dumps(result, ensure_ascii=False)))
    return result


def gift_row(c, gid):
    return row(c, 'SELECT id,title,rarity,price,emoji,description FROM gifts WHERE id=? AND active=1', (gid,))


def spin(c, uid, key=None):
    def action():
        prize = gift_row(c, prize_pick())
        if prize is None:
            fail('PRIZE_CONFIG', 'Приз недоступен.', 500)
        consume(c, uid, SPIN_COST)
        inv_id = str(uuid.uuid4())
        add_ledger(c, uid, -SPIN_COST, 'demo_spin', {'giftId': prize['id']})
        c.execute('INSERT INTO inventory(id,user_id,gift_id,source) VALUES(?,?,?,?)',
                  (inv_id, uid, prize['id'], 'demo_spin'))
        c.execute('INSERT INTO spins(user_id,cost,prize_gift_id,result) VALUES(?,?,?,?)',
                  (uid, SPIN_COST, prize['id'], 'win'))
        return {'prize': prize, 'inventoryId': inv_id, 'user': user_balance(c, uid)}
    result = once(c, uid, 'spin', key, action)
    sync_achievements(c, uid)
    return result


def add_ledger(c, uid, delta, reason, metadata=None):
    c.execute('INSERT INTO ledger(user_id,delta,reason,metadata) VALUES(?,?,?,?)',
              (uid, delta, reason, json.dumps(metadata or {}, ensure_ascii=False)))


def user_balance(c, uid):
    return row(c, 'SELECT id,balance,username,first_name,created_at FROM users WHERE id=?', (uid,))


def buy(c, uid, gid, key=None):
    if not isinstance(gid, str) or len(gid) > 64:
        fail('BAD_REQUEST', 'Некорректный идентификатор подарка.')
    gift = gift_row(c, gid)
    if not gift:
        fail('NOT_FOUND', 'Подарок не найден.', 404)
    def action():
        consume(c, uid, gift['price'])
        inv_id = str(uuid.uuid4())
        add_ledger(c, uid, -gift['price'], 'demo_market_purchase', {'giftId': gid})
        c.execute('INSERT INTO inventory(id,user_id,gift_id,source) VALUES(?,?,?,?)',
                  (inv_id, uid, gid, 'demo_market_purchase'))
        return {'gift': gift, 'inventoryId': inv_id, 'user': user_balance(c, uid)}
    result = once(c, uid, 'market:' + gid, key, action)
    sync_achievements(c, uid)
    return result


def daily(c, uid):
    day = utc_day()
    try:
        c.execute('INSERT INTO daily_claims(user_id,day,amount) VALUES(?,?,?)', (uid, day, DAY_REWARD))
    except sqlite3.IntegrityError:
        fail('ALREADY_CLAIMED', 'Бонус сегодня уже получен.', 409)
    add_credit(c, uid, DAY_REWARD, 'daily_reward', {'day': day})
    notify(c, uid, 'reward', 'Ежедневный бонус', f'+{DAY_REWARD} тестовых FC')
    sync_achievements(c, uid)
    return {'reward': DAY_REWARD, 'day': day, 'user': user_balance(c, uid)}


def redeem(c, uid, code):
    value = str(code or '').strip().upper()
    if len(value) > 40 or value not in PROMOS:
        fail('INVALID_PROMO', 'Промокод не найден.', 404)
    try:
        c.execute('INSERT INTO promo_redemptions(user_id,code,amount) VALUES(?,?,?)', (uid, value, PROMOS[value]))
    except sqlite3.IntegrityError:
        fail('PROMO_USED', 'Промокод уже активирован.', 409)
    add_credit(c, uid, PROMOS[value], 'promo_reward', {'code': value})
    notify(c, uid, 'reward', 'Промокод принят', f'+{PROMOS[value]} тестовых FC')
    sync_achievements(c, uid)
    return {'reward': PROMOS[value], 'code': value, 'user': user_balance(c, uid)}


def notify(c, uid, kind, title, message):
    c.execute('INSERT INTO notifications(user_id,kind,title,message) VALUES(?,?,?,?)',
              (uid, kind, title, message))


def statistics(c, uid):
    stat = {}
    stat['spins'] = count(c, 'SELECT COUNT(*) FROM spins WHERE user_id=?', (uid,))
    stat['collection'] = count(c, 'SELECT COUNT(*) FROM inventory WHERE user_id=?', (uid,))
    stat['rare'] = count(c, '''SELECT COUNT(*) FROM inventory i JOIN gifts g ON g.id=i.gift_id
                                WHERE i.user_id=? AND g.rarity IN ('Rare','Epic','Legendary')''', (uid,))
    stat['epic'] = count(c, '''SELECT COUNT(*) FROM inventory i JOIN gifts g ON g.id=i.gift_id
                                WHERE i.user_id=? AND g.rarity IN ('Epic','Legendary')''', (uid,))
    stat['legendary'] = count(c, '''SELECT COUNT(*) FROM inventory i JOIN gifts g ON g.id=i.gift_id
                                     WHERE i.user_id=? AND g.rarity='Legendary' ''', (uid,))
    stat['purchases'] = count(c, "SELECT COUNT(*) FROM ledger WHERE user_id=? AND reason='demo_market_purchase'", (uid,))
    stat['dailies'] = count(c, 'SELECT COUNT(*) FROM daily_claims WHERE user_id=?', (uid,))
    stat['favorites'] = count(c, 'SELECT COUNT(*) FROM favorites WHERE user_id=?', (uid,))
    stat['promos'] = count(c, 'SELECT COUNT(*) FROM promo_redemptions WHERE user_id=?', (uid,))
    stat['collectionScore'] = count(c, '''SELECT COALESCE(SUM(CASE g.rarity WHEN 'Common' THEN 10 WHEN 'Rare' THEN 30
       WHEN 'Epic' THEN 80 WHEN 'Legendary' THEN 200 ELSE 0 END),0) FROM inventory i JOIN gifts g ON g.id=i.gift_id WHERE i.user_id=?''', (uid,))
    stat['uniqueGifts'] = count(c, 'SELECT COUNT(DISTINCT gift_id) FROM inventory WHERE user_id=?', (uid,))
    stat['start'] = 1
    stat['totalScore'] = stat['collectionScore'] + stat['spins'] * 5 + stat['purchases'] * 2 + stat['dailies'] * 10
    return stat


def level_data(score):
    lvl = math.isqrt(max(0, score) // 80) + 1
    previous = 80 * (lvl - 1) ** 2
    next_ = 80 * lvl ** 2
    current, required = score - previous, next_ - previous
    return {'level': lvl, 'currentXp': current, 'requiredXp': required,
            'progressPercent': max(0, min(100, round(current / required * 100)))}


def progress(c, uid):
    st = statistics(c, uid)
    achievements = [{**item, 'current': min(st.get(item['category'], 0), item['threshold']),
                     'unlocked': st.get(item['category'], 0) >= item['threshold']} for item in MILESTONES]
    missions = [{**item, 'current': min(st.get(item['group'], 0), item['target']),
                 'completed': st.get(item['group'], 0) >= item['target']} for item in MISSIONS]
    return {'stats': st, 'level': level_data(st['totalScore']), 'achievements': achievements, 'missions': missions,
            'unlockedCount': sum(x['unlocked'] for x in achievements),
            'completedMissions': sum(x['completed'] for x in missions)}


def sync_achievements(c, uid):
    st = statistics(c, uid)
    for item in MILESTONES:
        if st.get(item['category'], 0) >= item['threshold']:
            cur = c.execute('INSERT OR IGNORE INTO achievements(user_id,achievement_id) VALUES(?,?)', (uid, item['id']))
            if cur.rowcount:
                notify(c, uid, 'achievement', item['icon'] + ' ' + item['title'], item['details'])


def player_state(c, uid):
    user = row(c, 'SELECT * FROM users WHERE id=?', (uid,))
    gifts = rows(c, 'SELECT id,title,rarity,price,emoji,description FROM gifts WHERE active=1 ORDER BY price')
    inventory = rows(c, '''SELECT i.id AS inventoryId, g.id AS giftId,g.title,g.rarity,g.price,g.emoji,g.description,
                              i.acquired_at AS acquiredAt,i.source,
                              CASE WHEN f.inventory_id IS NULL THEN 0 ELSE 1 END AS favorite
                              FROM inventory i JOIN gifts g ON g.id=i.gift_id
                              LEFT JOIN favorites f ON f.inventory_id=i.id AND f.user_id=i.user_id
                              WHERE i.user_id=? ORDER BY i.acquired_at DESC, i.rowid DESC''', (uid,))
    claims = rows(c, 'SELECT day,amount FROM daily_claims WHERE user_id=? ORDER BY day DESC LIMIT 20', (uid,))
    settings = row(c, 'SELECT sound,vibration,reduce_motion AS reduceMotion,show_on_leaderboard AS showOnLeaderboard FROM settings WHERE user_id=?', (uid,))
    notifications = rows(c, 'SELECT id,kind,title,message,read_at AS readAt,created_at AS createdAt FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 40', (uid,))
    weights = sum(w for _, w in PRIZE_WEIGHTS)
    return {
        'user': public_user(user), 'gifts': gifts, 'inventory': inventory,
        'ledger': rows(c, 'SELECT id,delta,reason,metadata,created_at AS createdAt FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 50', (uid,)),
        'spins': rows(c, '''SELECT s.id,s.cost,s.created_at AS createdAt,g.title,g.emoji,g.rarity,g.id AS giftId
                             FROM spins s JOIN gifts g ON g.id=s.prize_gift_id WHERE s.user_id=? ORDER BY s.id DESC LIMIT 30''', (uid,)),
        'progress': progress(c, uid), 'spinCost': SPIN_COST,
        'probabilities': [{'giftId': gid, 'weight': w, 'probabilityPercent': round(w / weights * 100, 2)} for gid, w in PRIZE_WEIGHTS],
        'daily': {'reward': DAY_REWARD, 'today': utc_day(), 'claimed': any(cl['day'] == utc_day() for cl in claims), 'history': claims},
        'settings': settings or {'sound': 1, 'vibration': 1, 'reduceMotion': 0, 'showOnLeaderboard': 1},
        'notifications': notifications,
        'unreadCount': sum(n['readAt'] is None for n in notifications),
        'tickets': rows(c, 'SELECT id,subject,category,status,created_at AS createdAt FROM support_tickets WHERE user_id=? ORDER BY created_at DESC LIMIT 20', (uid,))
    }


def leaders(c, size=50):
    people = rows(c, '''SELECT u.id, u.username,u.first_name AS firstName,
     (SELECT COUNT(*) FROM inventory i WHERE i.user_id=u.id) AS items,
     (SELECT COUNT(*) FROM spins s WHERE s.user_id=u.id) AS spins,
     (SELECT COALESCE(SUM(CASE g.rarity WHEN 'Common' THEN 10 WHEN 'Rare' THEN 30 WHEN 'Epic' THEN 80
                        WHEN 'Legendary' THEN 200 ELSE 0 END),0)
      FROM inventory i JOIN gifts g ON g.id=i.gift_id WHERE i.user_id=u.id)
     + 5*(SELECT COUNT(*) FROM spins p WHERE p.user_id=u.id)
     + 2*(SELECT COUNT(*) FROM ledger l WHERE l.user_id=u.id AND l.reason='demo_market_purchase')
     + 10*(SELECT COUNT(*) FROM daily_claims d WHERE d.user_id=u.id) AS points
     FROM users u LEFT JOIN settings s ON s.user_id=u.id
     WHERE COALESCE(s.show_on_leaderboard,1)=1
     ORDER BY points DESC, spins DESC, u.created_at ASC LIMIT ?''', (size,))
    return [{**p, 'rank': i + 1, 'name': '@' + p['username'] if p['username'] else p['firstName'],
             'score': p['points']} for i, p in enumerate(people)]


def require_string(v, label, low, high):
    if not isinstance(v, str) or not low <= len(v.strip()) <= high:
        fail('BAD_REQUEST', f'Проверь поле «{label}».')
    return v.strip()


def require_body(data, allowed):
    if not isinstance(data, dict) or any(k not in allowed for k in data):
        fail('BAD_REQUEST', 'Некорректные поля запроса.')


def api_dispatch(c, method, path, data, headers, query):
    if method == 'GET' and path == '/api/health':
        return {'ok': True, 'name': 'FROGI Arena', 'mode': 'DEMO', 'time': now_seconds()}
    if method == 'POST' and path == '/api/auth':
        require_body(data, {'initData', 'demo'})
        return auth_session(c, data.get('initData', ''), data.get('demo') is True)
    if path.startswith('/api/admin/'):
        secret = headers.get('x-admin-token', '')
        if len(ADMIN_TOKEN) < 16 or not hmac.compare_digest(ADMIN_TOKEN, secret):
            fail('FORBIDDEN', 'Доступ запрещён.', 403)
        if method == 'GET' and path == '/api/admin/overview':
            return {'mode': 'DEMO ONLY', 'users': count(c, 'SELECT COUNT(*) FROM users'),
                    'demoCredits': count(c, 'SELECT COALESCE(SUM(balance),0) FROM users'),
                    'spins': count(c, 'SELECT COUNT(*) FROM spins'),
                    'purchases': count(c, "SELECT COUNT(*) FROM ledger WHERE reason='demo_market_purchase'"),
                    'tickets': count(c, 'SELECT COUNT(*) FROM support_tickets'),
                    'latestUsers': rows(c, 'SELECT id,username,first_name,balance,created_at FROM users ORDER BY created_at DESC LIMIT 25'),
                    'recentTickets': rows(c, 'SELECT id,user_id,category,subject,status,created_at FROM support_tickets ORDER BY created_at DESC LIMIT 20')}
        if method == 'POST' and path == '/api/admin/adjust-balance':
            uid = require_string(data.get('userId'), 'userId', 1, 128)
            delta = data.get('delta')
            reason = require_string(data.get('reason'), 'reason', 3, 200)
            if isinstance(delta, bool) or not isinstance(delta, int) or not -1000000 <= delta <= 1000000 or delta == 0:
                fail('BAD_REQUEST', 'Укажи корректную сумму изменения.')
            if not row(c, 'SELECT id FROM users WHERE id=?', (uid,)):
                fail('NOT_FOUND', 'Игрок не найден.', 404)
            if delta < 0:
                consume(c, uid, -delta)
            else:
                c.execute('UPDATE users SET balance=balance+? WHERE id=?', (delta, uid))
            add_ledger(c, uid, delta, 'admin_adjustment: '+reason, {'actor':'admin-key'})
            c.execute('INSERT INTO audit_log(actor,action,target,details) VALUES(?,?,?,?)',
                      ('admin-key', 'balance_adjustment', uid, json.dumps({'delta':delta,'reason':reason}, ensure_ascii=False)))
            return {'user': user_balance(c, uid)}
        if method == 'PATCH' and path.startswith('/api/admin/support/'):
            ticket = path.rsplit('/', 1)[-1]
            status = data.get('status')
            if status not in ('open', 'resolved'):
                fail('BAD_REQUEST', 'Неверный статус.')
            cur = c.execute('UPDATE support_tickets SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (status, ticket))
            if not cur.rowcount:
                fail('NOT_FOUND', 'Обращение не найдено.', 404)
            return {'ok':True}
        if method == 'GET' and path == '/api/admin/audit':
            return {'entries':rows(c, 'SELECT * FROM audit_log ORDER BY id DESC LIMIT 100')}
        fail('NOT_FOUND', 'Маршрут не найден.', 404)
    if method == 'GET' and path == '/api/news':
        return {'news': NEWS}
    if method == 'GET' and path == '/api/catalog/stats':
        stats = rows(c, 'SELECT rarity,COUNT(*) AS total,MIN(price) AS minPrice,MAX(price) AS maxPrice FROM gifts WHERE active=1 GROUP BY rarity')
        return {'byRarity': stats, 'total': sum(s['total'] for s in stats)}
    user = authenticated_user(c, headers.get('authorization', ''))
    uid = user['id']
    if method == 'GET' and path == '/api/me':
        return player_state(c, uid)
    if method == 'POST' and path == '/api/logout':
        h = headers.get('authorization', '')[7:]
        c.execute('UPDATE sessions SET revoked_at=? WHERE token_hash=? AND user_id=?',
                  (now_seconds(), hashlib.sha256(h.encode()).hexdigest(), uid))
        return {'ok': True}
    if method == 'POST' and path == '/api/spin':
        return spin(c, uid, headers.get('x-idempotency-key'))
    if method == 'POST' and path == '/api/market/buy':
        require_body(data, {'giftId'})
        return buy(c, uid, data.get('giftId'), headers.get('x-idempotency-key'))
    if method == 'POST' and path == '/api/daily/claim':
        return daily(c, uid)
    if method == 'POST' and path == '/api/promo/redeem':
        require_body(data, {'code'})
        return redeem(c, uid, data.get('code'))
    if method == 'PATCH' and re.fullmatch(r'/api/inventory/[A-Za-z0-9_-]{1,100}/favorite', path):
        require_body(data, {'favorite'})
        inv_id = path.split('/')[3]
        if not isinstance(data.get('favorite'), bool):
            fail('BAD_REQUEST', 'Неверный параметр избранного.')
        if not row(c, 'SELECT id FROM inventory WHERE id=? AND user_id=?', (inv_id, uid)):
            fail('NOT_FOUND', 'Предмет не найден.', 404)
        if data['favorite']:
            c.execute('INSERT OR IGNORE INTO favorites(user_id,inventory_id) VALUES(?,?)', (uid, inv_id))
        else:
            c.execute('DELETE FROM favorites WHERE user_id=? AND inventory_id=?', (uid, inv_id))
        sync_achievements(c, uid)
        return {'inventoryId': inv_id, 'favorite': data['favorite']}
    if method == 'PATCH' and path == '/api/settings':
        required = {'sound','vibration','reduceMotion','showOnLeaderboard'}
        require_body(data, required)
        if set(data) != required or not all(isinstance(data.get(k), bool) for k in required):
            fail('BAD_REQUEST', 'Некорректные настройки.')
        vals = [int(data[k]) for k in ('sound','vibration','reduceMotion','showOnLeaderboard')]
        c.execute('''INSERT INTO settings(user_id,sound,vibration,reduce_motion,show_on_leaderboard)
                     VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET sound=excluded.sound,
                     vibration=excluded.vibration,reduce_motion=excluded.reduce_motion,
                     show_on_leaderboard=excluded.show_on_leaderboard,updated_at=CURRENT_TIMESTAMP''', (uid,*vals))
        return {'settings': row(c, 'SELECT sound,vibration,reduce_motion AS reduceMotion,show_on_leaderboard AS showOnLeaderboard FROM settings WHERE user_id=?',(uid,))}
    if method == 'GET' and path == '/api/leaderboard':
        return {'leaders':leaders(c)}
    if method == 'POST' and path == '/api/support/tickets':
        require_body(data, {'category','subject','body'})
        category = data.get('category')
        if category not in ('question','bug','idea','account','other'):
            fail('BAD_REQUEST', 'Выбери категорию обращения.')
        subject = require_string(data.get('subject'), 'Тема', 5, 100)
        body = require_string(data.get('body'), 'Описание', 10, 2000)
        if count(c, "SELECT COUNT(*) FROM support_tickets WHERE user_id=? AND status='open'", (uid,)) >= 10:
            fail('TOO_MANY_TICKETS', 'Слишком много открытых обращений.', 429)
        ticket_id = 'FR-' + secrets.token_hex(5).upper()
        c.execute('INSERT INTO support_tickets(id,user_id,category,subject,body) VALUES(?,?,?,?,?)',
                  (ticket_id, uid, category, subject, body))
        return {'ticket':{'id':ticket_id,'category':category,'subject':subject,'status':'open'},'message':'Обращение сохранено.'}
    if method == 'GET' and path == '/api/support/tickets':
        return {'tickets':rows(c, 'SELECT * FROM support_tickets WHERE user_id=? ORDER BY created_at DESC LIMIT 50', (uid,))}
    if method == 'POST' and path == '/api/notifications/read':
        ids = data.get('ids', [])
        if not isinstance(ids,list) or len(ids) > 100 or any(isinstance(i,bool) or not isinstance(i,int) or i<1 for i in ids):
            fail('BAD_REQUEST', 'Неверный список уведомлений.')
        updated = 0
        for i in set(ids):
            updated += c.execute('UPDATE notifications SET read_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=? AND read_at IS NULL', (i, uid)).rowcount
        return {'updated':updated}
    if method == 'GET' and path == '/api/activity':
        def safe_int(v, default, low, high):
            try: return max(low, min(high, int(v)))
            except (TypeError, ValueError): return default
        limit = safe_int(query.get('limit', ['30'])[0],30,1,100)
        before = safe_int(query.get('before', ['9223372036854775807'])[0],2**63-1,1,2**63-1)
        entries = rows(c, 'SELECT id,delta,reason,created_at AS createdAt,metadata FROM ledger WHERE user_id=? AND id<? ORDER BY id DESC LIMIT ?', (uid,before,limit+1))
        return {'entries': entries[:limit], 'nextCursor': entries[limit-1]['id'] if len(entries)>limit else None}
    fail('NOT_FOUND', 'Маршрут API не найден.', 404)

# ---------------------------------------------------------------------------
# Telegram Bot API: webhook on Render, optional long-polling on local machine.
# Runs within the same Python process as the Mini App and API.
# ---------------------------------------------------------------------------
BOT_COMMANDS = [
    ('start', '🏠 Открыть главное меню'), ('app', '🎮 Открыть FROGI Arena'),
    ('balance', '💰 Мой баланс'), ('daily', '📅 Ежедневный бонус'),
    ('inventory', '🎁 Мои подарки'), ('market', '🛍 Каталог подарков'),
    ('wheel', '🎡 Колесо удачи'), ('spin', '🎡 Вращать колесо'),
    ('profile', '👤 Мой профиль'), ('top', '🏆 Рейтинг'),
    ('promo', '🎟 Промокод'), ('help', '❓ Список команд')
]


def tg_api(method, args=None, timeout=15):
    if not TOKEN:
        raise RuntimeError('BOT_TOKEN is not set')
    target = 'https://api.telegram.org/bot' + TOKEN + '/' + method
    req = Request(target, data=json.dumps(args or {}, ensure_ascii=False).encode('utf-8'),
                  headers={'Content-Type': 'application/json; charset=utf-8'}, method='POST')
    try:
        with urlopen(req, timeout=timeout) as response:
            packet = json.loads(response.read(1_000_000))
    except HTTPError as ex:
        # Never write the URL (which includes Telegram token) to error logs.
        error_data = ex.read(1024).decode('utf-8', 'replace')
        raise RuntimeError(f'Telegram {method} HTTP {ex.code}: {error_data[:200]}') from None
    except (URLError, TimeoutError) as ex:
        raise RuntimeError(f'Telegram {method}: network unavailable') from ex
    if not packet.get('ok'):
        raise RuntimeError(f'Telegram {method}: {packet.get("description", "unknown error")}')
    return packet.get('result')


def button_rows():
    result = []
    if PUBLIC_URL:
        result.append([{'text': '🎮 Открыть FROGI Arena', 'web_app': {'url': PUBLIC_URL}}])
    result += [
        [{'text': '💰 Баланс', 'callback_data': 'balance'}, {'text': '📅 Бонус', 'callback_data': 'daily'}],
        [{'text': '🎁 Подарки', 'callback_data': 'inventory'}, {'text': '🛍 Маркет', 'callback_data': 'market'}],
        [{'text': '🎡 Колесо', 'callback_data': 'wheel'}, {'text': '🏆 Топ игроков', 'callback_data': 'top'}],
        [{'text': '👤 Профиль', 'callback_data': 'profile'}, {'text': '❓ Помощь', 'callback_data': 'help'}]
    ]
    return {'inline_keyboard': result}


def send_text(chat_id, message, markup=None):
    payload = {'chat_id': chat_id, 'text': str(message)[:4050], 'disable_web_page_preview': True}
    if markup:
        payload['reply_markup'] = markup
    return tg_api('sendMessage', payload)


def format_items(c, uid):
    items = rows(c, '''SELECT g.title,g.emoji,g.rarity,COUNT(*) AS quantity FROM inventory i
                        JOIN gifts g ON g.id=i.gift_id WHERE i.user_id=?
                        GROUP BY g.id ORDER BY MAX(i.acquired_at) DESC LIMIT 12''', (uid,))
    if not items:
        return 'Пока пусто. Попробуй /spin или открой /market.'
    return '\n'.join(f'{g["emoji"]} {g["title"]} ({g["rarity"]}) ×{g["quantity"]}' for g in items)


def parse_cmd(body):
    text = str(body or '').strip()
    if not text.startswith('/'):
        return None, ''
    m = re.match(r'^/([a-zA-Z][a-zA-Z0-9_]{0,31})(?:@([A-Za-z0-9_]{5,32}))?(?:\s+([\s\S]*))?$', text)
    if not m:
        return None, ''
    mention = m.group(2)
    if mention and BOT_USERNAME and mention.lower() != BOT_USERNAME.lower():
        return None, ''
    return m.group(1).lower(), (m.group(3) or '').strip()


def process_command(c, sender, cmd, argument='', op_key=''):
    uid = 'tg_' + str(sender['id'])
    user = row(c, 'SELECT * FROM users WHERE id=?', (uid,))
    if not user:
        fail('NOT_FOUND', 'Профиль не найден.', 404)
    menu = button_rows()
    notice = '\n\n🔒 Все FC и подарки виртуальные, без денежной стоимости и вывода.'
    if cmd in ('start','menu','app'):
        return ('🐸 FROGI ARENA\n\nДобро пожаловать, ' + user['first_name'] + '!\n'
                + '🎮 Играй, собирай подарки, открывай достижения и поднимайся в рейтинге.\n\n'
                + f'💰 Баланс: {user["balance"]:,} FC'.replace(',', ' ') + notice, menu)
    if cmd in ('balance','wallet'):
        return (f'💰 МОЙ БАЛАНС\n\n{user["balance"]:,} тестовых FC\n\n'.replace(',', ' ')
                + 'Заработать: /daily\nКрутить колесо: /spin\nПодарки: /inventory' + notice, menu)
    if cmd == 'daily':
        result = daily(c, uid)
        return f'📅 Ежедневный бонус: +{result["reward"]} FC!\n💰 Баланс: {result["user"]["balance"]} FC', menu
    if cmd == 'inventory':
        return '🎁 МОЯ КОЛЛЕКЦИЯ\n\n' + format_items(c, uid), menu
    if cmd == 'market':
        gifts = rows(c, 'SELECT id,title,emoji,rarity,price FROM gifts WHERE active=1 ORDER BY price')
        sample = '\n'.join(f'{g["emoji"]} {g["title"]} — {g["price"]} FC\n/buy {g["id"]}' for g in gifts[:12])
        return '🛍 FROGI MARKET\n\n' + sample + '\n\nДля покупки отправь команду /buy ID.', menu
    if cmd == 'buy':
        if not argument:
            return 'Укажи ID подарка: /buy frog_common\nСписок: /market', menu
        result = buy(c, uid, argument, op_key)
        return f'🎁 Куплено: {result["gift"]["emoji"]} {result["gift"]["title"]}\n💰 Осталось: {result["user"]["balance"]} FC', menu
    if cmd == 'wheel':
        wheel_menu = {'inline_keyboard': [[{'text': '🎡 Вращать за 100 FC', 'callback_data': 'spin'}]] + menu['inline_keyboard']}
        return '🎡 КОЛЕСО FROGI\n\nВращение — 100 тестовых FC. Результат выбирается на сервере. Каждый раунд даёт подарок.' + notice, wheel_menu
    if cmd == 'spin':
        result = spin(c, uid, op_key)
        return (f'🎉 Выпало: {result["prize"]["emoji"]} {result["prize"]["title"]}!\n'
                f'Редкость: {result["prize"]["rarity"]}\nБаланс: {result["user"]["balance"]} FC\nПодарок сохранён.'), menu
    if cmd == 'profile':
        p = progress(c, uid)
        st = p['stats']
        return (f'👤 ПРОФИЛЬ\n\n{user["first_name"]}\nУровень: {p["level"]["level"]}\n'
                f'🏆 Очки: {st["totalScore"]}\n🎁 Подарки: {st["collection"]}\n🎡 Вращения: {st["spins"]}\n'
                f'🏅 Достижения: {p["unlockedCount"]}/{len(p["achievements"])}'), menu
    if cmd == 'top':
        top = leaders(c, 10)
        return ('🏆 ТОП ИГРОКОВ\n\n' + ('\n'.join(f'{x["rank"]}. {x["name"]} — {x["score"]} PTS' for x in top) if top else 'Пока пусто.')), menu
    if cmd == 'promo':
        if not argument:
            return '🎟 Введи /promo FROGI2026\nКаждый промокод работает один раз.', menu
        result = redeem(c, uid, argument)
        return f'🎟 Промокод {result["code"]} активирован! +{result["reward"]} FC\nБаланс: {result["user"]["balance"]} FC', menu
    if cmd == 'news':
        return '📰 НОВОСТИ\n\n' + '\n\n'.join(n['icon'] + ' ' + n['title'] + '\n' + n['text'] for n in NEWS), menu
    if cmd == 'admin':
        if sender['id'] not in ADMIN_IDS:
            return '⛔ Нет доступа.', menu
        return ('🛡 Админ-статистика\n\nИгроки: ' + str(count(c, 'SELECT COUNT(*) FROM users'))
                + '\nРаунды: ' + str(count(c, 'SELECT COUNT(*) FROM spins'))
                + '\nОбращения: ' + str(count(c, 'SELECT COUNT(*) FROM support_tickets'))), menu
    if cmd == 'help':
        return ('❓ КОМАНДЫ FROGI\n\n' + '\n'.join('/'+c+' — '+d for c,d in BOT_COMMANDS)
                + '\n/buy ID — купить предмет\n/promo КОД — активировать промокод' + notice), menu
    return 'Команда не найдена. Используй /help.', menu


BOT_USERNAME = ''

def process_update(update):
    if not isinstance(update, dict) or not isinstance(update.get('update_id'), int):
        return
    update_id = update['update_id']
    callback = update.get('callback_query')
    message = update.get('message')
    obj = callback or message
    if not isinstance(obj, dict):
        return
    sender = obj.get('from')
    if not isinstance(sender, dict) or not isinstance(sender.get('id'), int) or sender.get('is_bot'):
        return
    # Telegram Web App keyboard works only in private conversations.
    chat = (obj.get('message') or {}).get('chat') if callback else obj.get('chat')
    if not isinstance(chat, dict) or chat.get('type') != 'private':
        return
    chat_id = chat.get('id')
    if not isinstance(chat_id, int):
        return
    raw_cmd = callback.get('data') if callback else message.get('text')
    if callback:
        cmd, arg = str(raw_cmd or '')[:64], ''
        try:
            tg_api('answerCallbackQuery', {'callback_query_id': callback['id']}, 8)
        except Exception as err:
            LOG.warning('AnswerCallbackQuery unavailable: %s', type(err).__name__)
    else:
        cmd, arg = parse_cmd(raw_cmd)
        if not cmd:
            cmd = 'menu' if raw_cmd else ''
    if not cmd:
        return
    try:
        with transaction() as c:
            mark = c.execute('INSERT OR IGNORE INTO processed_updates(update_id) VALUES(?)', (update_id,))
            if mark.rowcount != 1:
                return
            provision(c, sender['id'], sender.get('username',''), sender.get('first_name','FROGI User'))
            # Unique Telegram update_id is also an idempotency key for bot commands.
            text, markup = process_command(c, sender, cmd, arg, 'tgupdate_' + str(update_id))
    except AppError as err:
        text, markup = '⚠️ ' + str(err), button_rows()
    try:
        send_text(chat_id, text, markup)
    except Exception as err:
        LOG.error('Failed to reply to Telegram update %s: %s', update_id, type(err).__name__)


def setup_telegram():
    global BOT_USERNAME
    if not TOKEN:
        LOG.warning('BOT_TOKEN not configured. Public Telegram auth and bot are disabled.')
        return
    try:
        BOT_USERNAME = (tg_api('getMe') or {}).get('username', '')
        LOG.info('Telegram bot connected: @%s', BOT_USERNAME)
        tg_api('setMyCommands', {'commands': [{'command': c, 'description': desc} for c,desc in BOT_COMMANDS]})
        if PUBLIC_URL:
            tg_api('setWebhook', {'url': PUBLIC_URL + WEBHOOK_PATH,
                                  'secret_token': WEBHOOK_SECRET, 'drop_pending_updates': False,
                                  'allowed_updates': ['message', 'callback_query'], 'max_connections': 10})
            tg_api('setChatMenuButton', {'menu_button': {'type': 'web_app', 'text': 'FROGI Arena',
                                                      'web_app': {'url': PUBLIC_URL}}})
            LOG.info('Telegram webhook configured on public HTTPS URL (path hidden).')
        else:
            tg_api('deleteWebhook', {'drop_pending_updates': False})
            LOG.info('No PUBLIC_URL; starting Telegram long polling in same Python process.')
            threading.Thread(target=poll_telegram, name='frogi-poll', daemon=True).start()
    except Exception as err:
        LOG.error('Telegram initialization failed: %s', type(err).__name__)
        # A restart/redeploy retries webhook setup. HTTP service still stays available.


def poll_telegram():
    # Local-only fallback: Render always injects RENDER_EXTERNAL_URL and uses webhook.
    with readonly() as c:
        state = row(c, "SELECT value FROM bot_state WHERE key='poll_offset'")
    offset = int(state['value']) if state else 0
    while True:
        try:
            updates = tg_api('getUpdates', {'offset': offset, 'timeout': 22,
                                            'allowed_updates': ['message','callback_query']}, timeout=30)
            for u in updates:
                process_update(u)
                offset = max(offset, int(u['update_id']) + 1)
                with transaction() as c:
                    c.execute("INSERT INTO bot_state(key,value) VALUES('poll_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(offset),))
        except Exception as ex:
            LOG.warning('Polling failed (%s). Retrying.', type(ex).__name__)
            time.sleep(4)


# ---------------------------------------------------------------------------
# Single-file HTTP server, static UI, JSON API, Render health, Telegram webhook.
# ---------------------------------------------------------------------------
HTML = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#090a0f"><meta name="description" content="FROGI Arena - Telegram Mini App"><title>FROGI Arena</title><script src="https://telegram.org/js/telegram-web-app.js"></script><style>@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap');
:root{font-family:Manrope,Inter,system-ui,-apple-system,sans-serif;color:#f5f5f8;background:#09090d;font-synthesis:none;text-rendering:optimizeLegibility;-webkit-font-smoothing:antialiased;--bg:#09090d;--panel:#121218;--panel2:#17171f;--muted:#898996;--line:#25252e;--red:#ff3559;--blue:#3988ff;--green:#62e5b2}
*{box-sizing:border-box}body{margin:0;min-width:320px;background:radial-gradient(ellipse at 50% -10%,#1a1118 0%,#09090d 42%);min-height:100vh}button,input{font:inherit}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.45}.app-shell{max-width:600px;margin:0 auto;min-height:100vh;padding:0 17px 95px;position:relative;overflow:hidden}.topbar{height:76px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #ffffff0c}.brand{display:flex;align-items:center;gap:11px}.brand-mark{width:42px;height:42px;border-radius:13px;background:linear-gradient(145deg,#ff4c69,#9e102b);display:grid;place-items:center;font-weight:900;font-size:21px;box-shadow:0 5px 25px #ff355928;letter-spacing:-3px}.brand-mark span{color:#111}.brand-name{font-weight:900;letter-spacing:2px;font-size:16px}.brand-name span{font-size:8px;color:var(--red);vertical-align:top}.brand-sub{font-size:8px;letter-spacing:1.2px;color:#858590;margin-top:3px}.icon-button{width:38px;height:38px;border:1px solid var(--line);border-radius:12px;background:#15151b;color:#d8d8df;display:grid;place-items:center}.demo-banner{display:flex;align-items:center;gap:7px;margin-top:12px;padding:9px 11px;background:#17171f;border:1px solid #34303c;border-radius:10px;color:#c6c6d1;font-size:9px;font-weight:800;letter-spacing:.4px}.demo-banner svg{color:#f4bb57}.error-banner{display:flex;align-items:center;gap:9px;padding:11px;margin-top:10px;border:1px solid #6d2836;background:#29131a;border-radius:10px;color:#ffb5c2;font-size:12px}.error-banner span{flex:1}.error-banner button{border:0;background:transparent;color:inherit;font-size:20px}.main-content{padding-top:18px}.page{display:flex;flex-direction:column;gap:18px}.enter{animation:enter .3s ease both}@keyframes enter{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}.hero-card{position:relative;isolation:isolate;overflow:hidden;min-height:284px;border-radius:24px;border:1px solid #ffffff12;background:linear-gradient(120deg,#1d1119 0%,#11131e 65%,#0f1422 100%);padding:22px 20px}.hero-glow{position:absolute;width:180px;height:180px;border-radius:50%;filter:blur(55px);opacity:.24;z-index:-1}.glow-red{background:#ff174a;right:5px;top:45px}.glow-blue{background:#1c6cff;right:-50px;bottom:-90px}.eyebrow{display:flex;align-items:center;gap:7px;color:#d2d2db;font-size:9px;font-weight:800;letter-spacing:1.7px}.live-dot{width:6px;height:6px;background:#ff3559;border-radius:50%;box-shadow:0 0 10px #ff3559}.hero-pill{border:1px solid #ff355954;border-radius:5px;color:#ff6e87;padding:3px 5px;font-size:8px;letter-spacing:.5px}.hero-card h1{font-size:29px;line-height:1.15;letter-spacing:-1.2px;margin:20px 0 10px;font-weight:800;position:relative;z-index:2}.hero-card h1 span{color:#ff4a68}.hero-card p{font-size:11px;line-height:1.7;color:#b3b2c0;max-width:245px;margin:0 0 17px;position:relative;z-index:2}.primary-button{display:inline-flex;align-items:center;justify-content:center;gap:9px;border:0;border-radius:12px;padding:12px 16px;color:white;font-weight:800;font-size:12px;background:linear-gradient(100deg,#ff3658,#db1741);box-shadow:0 7px 22px #ff174a26;transition:transform .2s,filter .2s}.primary-button:active{transform:scale(.98)}.hero-art{position:absolute;width:160px;height:160px;right:-3px;bottom:9px;display:grid;place-items:center}.frog-emoji{font-size:78px;filter:drop-shadow(0 8px 15px #0008);z-index:2;animation:bob 3s ease-in-out infinite}@keyframes bob{50%{transform:translateY(-7px) rotate(3deg)}}.orbit{position:absolute;border:1px solid #ffffff25;border-radius:50%;transform:rotate(-24deg)}.orbit-a{width:146px;height:72px}.orbit-b{width:122px;height:140px;transform:rotate(55deg);border-color:#ff35594a}.art-chip{position:absolute;background:#171722;border:1px solid #ffffff20;border-radius:7px;padding:6px 8px;font-size:8px;font-weight:900;letter-spacing:1px;box-shadow:0 5px 15px #0008}.chip-one{top:13px;right:16px;color:#ff6980}.chip-two{bottom:18px;left:4px;color:#7bb2ff}.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.stat-card{background:linear-gradient(145deg,#15151d,#101015);border:1px solid var(--line);border-radius:17px;padding:14px;display:flex;flex-direction:column;align-items:flex-start;min-width:0}.stat-icon{width:31px;height:31px;border-radius:10px;display:grid;place-items:center;margin-bottom:13px}.stat-icon.red{color:#ff5470;background:#ff35591a}.stat-icon.blue{color:#6eabff;background:#3988ff1c}.stat-label{font-size:10px;color:#9d9daa}.stat-card strong{font-size:23px;letter-spacing:-.7px;margin:6px 0 8px}.stat-card strong small{font-size:10px;color:#a4a4b2;letter-spacing:0}.stat-card button{display:flex;align-items:center;gap:3px;background:transparent;border:0;padding:0;color:#ff6980;font-size:10px;font-weight:700}.section-title{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-top:5px}.section-kicker{font-size:8px;letter-spacing:1.5px;color:#72727f;font-weight:800}.section-title h2{font-size:21px;letter-spacing:-.6px;margin:4px 0 0;font-weight:800}.section-note{font-size:10px;color:#8d8d9a;white-space:nowrap;margin-bottom:3px}.quick-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.quick-card{display:flex;justify-content:space-between;align-items:flex-start;text-align:left;border-radius:16px;padding:14px;border:1px solid #ffffff12;color:#fff;min-height:105px}.quick-card>div{display:flex;flex-direction:column;align-items:flex-start;gap:9px}.quick-card span{font-size:11px;font-weight:800}.quick-card small{font-size:9px;color:#a7a7b4}.quick-card.quick-red{background:linear-gradient(135deg,#30121c,#171117)}.quick-red>div>svg{color:#ff5270}.quick-card.quick-blue{background:linear-gradient(135deg,#111e36,#12151f)}.quick-blue>div>svg{color:#69a6ff}.quick-card>svg{color:#b4b4c0}.gift-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:11px}.gift-card{position:relative;overflow:hidden;min-width:0;background:linear-gradient(145deg,#1a1a24,#111117);border:1px solid #ffffff12;border-radius:16px;padding:12px}.rarity-rare{background:linear-gradient(145deg,#20141b,#121117)}.rarity-epic{background:linear-gradient(145deg,#17172b,#101017)}.rarity-legendary{background:linear-gradient(145deg,#282116,#131218)}.gift-card-top{display:flex;justify-content:space-between;align-items:center;position:relative;z-index:1}.rarity-tag{font-size:8px;font-weight:800;text-transform:uppercase;letter-spacing:.8px;color:#ff7188;background:#ff355918;padding:5px 7px;border-radius:5px}.rarity-epic .rarity-tag{color:#a49bff;background:#8173ff18}.rarity-legendary .rarity-tag{color:#f0cb77;background:#f0cb7718}.gift-dots{font-size:10px;letter-spacing:2px;color:#656572}.gift-art{height:105px;display:grid;place-items:center;position:relative}.gift-art>span{font-size:52px;z-index:1;filter:drop-shadow(0 8px 13px #0008)}.gift-orbit{position:absolute;width:76px;height:42px;border:1px solid #ff35592c;border-radius:50%;transform:rotate(-25deg)}.rarity-epic .gift-orbit{border-color:#8173ff45}.rarity-legendary .gift-orbit{border-color:#f0cb7740}.gift-details strong{font-size:12px;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.gift-details p{font-size:9px;color:#9999a7;line-height:1.55;margin:6px 0 1px;min-height:28px}.section-foot{display:flex;justify-content:space-between;align-items:center;color:#8e8e9b;font-size:10px}.section-foot button{display:flex;align-items:center;gap:4px;border:0;background:transparent;color:#ff617b;font-size:10px;font-weight:800}.bottom-nav{position:fixed;z-index:20;bottom:0;left:50%;transform:translateX(-50%);width:min(600px,100%);padding:10px 8px calc(10px + env(safe-area-inset-bottom));background:#101015ed;backdrop-filter:blur(18px);border-top:1px solid #ffffff10;display:flex;justify-content:space-around;gap:2px}.nav-item{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;min-width:0;background:transparent;border:0;color:#777784;padding:5px 1px;font-size:8px;font-weight:700}.nav-item.active{color:#ff4c69}.nav-item.active svg{filter:drop-shadow(0 0 7px #ff35594d)}.admin-nav{opacity:.8}.footer-note{text-align:center;color:#53535f;font-size:7px;letter-spacing:1px;padding:18px 0 0}.wheel-intro{color:#a6a6b3;font-size:11px;line-height:1.7;background:#14141b;border:1px solid var(--line);border-radius:12px;padding:12px}.wheel-intro b{color:#ff617b}.wheel-stage{height:310px;display:grid;place-items:center;position:relative;overflow:hidden}.wheel-ring{width:226px;height:226px;border-radius:50%;border:9px solid #2b2b36;box-shadow:0 0 0 3px #ff355926,0 0 40px #ff355916,inset 0 0 30px #000a;background:conic-gradient(from -45deg,#4d1b2c 0deg 90deg,#172e4d 90deg 180deg,#322052 180deg 270deg,#4b3920 270deg 360deg);position:relative;display:grid;place-items:center;transition:transform 1.6s cubic-bezier(.12,.76,.13,1)}.wheel-running .wheel-ring{transform:rotate(1080deg)}.wheel-core{width:76px;height:76px;border-radius:50%;border:4px solid #33333f;background:linear-gradient(145deg,#20202b,#0d0d12);display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:27px;box-shadow:0 5px 18px #000a;z-index:2}.wheel-core small{font-size:7px;font-weight:900;letter-spacing:1px;color:#ff5470;margin-top:2px}.wheel-pointer{position:absolute;top:23px;z-index:4;width:0;height:0;border-left:12px solid transparent;border-right:12px solid transparent;border-top:25px solid #ff4766;filter:drop-shadow(0 2px 5px #ff35596b)}.wheel-prize{position:absolute;top:50%;left:50%;margin:-22px 0 0 -22px;transform-origin:22px 22px;width:44px;height:44px;display:grid;place-items:center}.wheel-prize span{display:grid;place-items:center;width:40px;height:40px;border-radius:12px;font-size:22px;box-shadow:0 4px 15px #0005}.wheel-shadow{position:absolute;bottom:22px;width:190px;height:20px;border-radius:50%;background:#0008;filter:blur(14px)}.spin-button{width:100%;min-height:48px}.win-card{display:flex;align-items:center;gap:12px;padding:14px;border:1px solid #f0cb7745;background:linear-gradient(110deg,#302315,#17141a);border-radius:14px;animation:enter .3s ease}.win-spark{width:42px;height:42px;border-radius:13px;background:#f0cb771b;color:#f0cb77;display:grid;place-items:center}.win-card>div:last-child{display:flex;flex-direction:column;gap:4px}.win-card span{font-size:8px;letter-spacing:1.2px;color:#f0cb77;font-weight:900}.win-card strong{font-size:14px}.win-card small{font-size:9px;color:#a8a5a1}.probability-panel{margin-top:5px}.prob-list{display:flex;flex-direction:column;gap:12px}.prob-row{display:flex;align-items:center;gap:9px;font-size:10px}.prob-emoji{font-size:18px;width:26px;text-align:center}.prob-name{width:88px;color:#c8c8d2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.prob-bar{height:5px;border-radius:9px;background:#262630;flex:1;overflow:hidden}.prob-bar i{display:block;height:100%;background:linear-gradient(90deg,#ff3559,#438dff);border-radius:9px;min-width:2px}.prob-row>b{width:42px;text-align:right;font-size:10px}.tiny-note{color:#747482;font-size:9px;line-height:1.65;margin:0}.center{text-align:center}.muted{color:#888895;font-size:11px}.market-banner{display:flex;gap:12px;align-items:center;padding:14px;border-radius:14px;background:linear-gradient(100deg,#25121a,#131521);border:1px solid #ffffff12;color:#ff617b}.market-banner div{display:flex;flex-direction:column;gap:4px}.market-banner b{font-size:12px;color:#fff}.market-banner span{font-size:9px;color:#aaaab7}.market-card{min-width:0}.market-card-bottom{display:flex;align-items:center;justify-content:space-between;gap:5px;padding:9px 2px}.market-card-bottom>div{display:flex;flex-direction:column;gap:3px}.market-card-bottom strong{font-size:13px}.market-card-bottom strong small{font-size:9px;color:#9999a7}.market-card-bottom span{font-size:9px;color:#8b8b99}.market-card-bottom button,.admin-form button{background:#ff3559;color:#fff;border:0;border-radius:9px;padding:9px 12px;font-size:10px;font-weight:800}.inventory-wrap{min-width:0}.acquired{font-size:8px;color:#777784;padding:7px 2px}.empty-state{padding:36px 18px;text-align:center;border:1px dashed #33333f;border-radius:17px;background:#111117}.empty-state>div{color:#ff5772;margin-bottom:12px}.empty-state strong{font-size:13px}.empty-state p{font-size:10px;line-height:1.6;color:#9696a4;max-width:240px;margin:8px auto 0}.wallet-card{padding:19px;border:1px solid #ffffff16;border-radius:20px;background:radial-gradient(circle at 100% 0%,#34202b 0%,transparent 48%),linear-gradient(140deg,#191920,#101016);box-shadow:0 12px 32px #0003}.wallet-card-top{display:flex;align-items:center;justify-content:space-between;color:#a5a5b2;font-size:9px;font-weight:800;letter-spacing:.8px}.wallet-card-top span:first-child{display:flex;align-items:center;gap:8px}.wallet-card-top svg{color:#ff5772}.demo-chip{border-radius:5px;padding:4px 6px;background:#ff35591c;color:#ff6b83}.wallet-card>strong{display:block;font-size:34px;letter-spacing:-1px;margin:20px 0 7px}.wallet-card>strong small{font-size:13px;color:#aaaab7}.wallet-card p{color:#9999a7;font-size:10px;line-height:1.7;margin:0 0 17px;max-width:360px}.wallet-actions{display:flex;gap:9px}.wallet-actions button{flex:1;display:flex;align-items:center;justify-content:center;gap:7px;padding:11px 5px;border-radius:10px;border:1px solid #ffffff14;background:#202028;color:#aaaab7;font-size:10px;font-weight:800}.network-note{display:flex;align-items:flex-start;gap:10px;padding:13px;border:1px solid #26354d;background:#101722;border-radius:13px;color:#70aaff}.network-note div{display:flex;flex-direction:column;gap:5px}.network-note b{font-size:11px;color:#d9e5ff}.network-note span{font-size:9px;line-height:1.6;color:#a0aabb}.ledger-list{display:flex;flex-direction:column;gap:1px}.ledger-row{display:flex;align-items:center;gap:10px;padding:12px 0;border-bottom:1px solid #ffffff0b}.ledger-icon{width:34px;height:34px;display:grid;place-items:center;border-radius:10px;background:#1a1a24;color:#a0a0ae}.ledger-main{flex:1;display:flex;flex-direction:column;gap:4px;min-width:0}.ledger-main b{font-size:10px;text-transform:capitalize}.ledger-main span{font-size:8px;color:#777784}.ledger-row>strong{font-size:11px;white-space:nowrap}.positive{color:#62e5b2!important}.negative{color:#ff7188!important}.admin-warning{display:flex;gap:9px;align-items:center;padding:12px;border:1px solid #634b25;background:#211b12;color:#e6c37d;border-radius:11px;font-size:10px;line-height:1.5}.admin-login,.admin-form{display:flex;flex-direction:column;gap:12px;padding:14px;background:#131319;border:1px solid var(--line);border-radius:14px}.admin-login label,.admin-form label{display:flex;flex-direction:column;gap:6px;font-size:9px;color:#9b9ba8;font-weight:800;letter-spacing:.6px}.admin-login input,.admin-form input{width:100%;background:#09090e;border:1px solid #30303b;border-radius:9px;padding:11px;color:#f5f5f8;outline:none;font-size:12px}.admin-login input:focus,.admin-form input:focus{border-color:#ff4c69}.admin-stats{display:grid;grid-template-columns:1fr 1fr;gap:9px}.admin-stats>div{display:flex;flex-direction:column;gap:8px;background:#14141b;border:1px solid var(--line);padding:13px;border-radius:12px}.admin-stats span{font-size:9px;color:#9999a7}.admin-stats b{font-size:21px}.subheading{font-size:13px;margin:4px 0 -7px}.admin-table{border:1px solid var(--line);border-radius:12px;overflow:hidden}.admin-table>button,.admin-table>div{width:100%;display:flex;justify-content:space-between;align-items:center;gap:8px;text-align:left;padding:11px;background:#131319;border:0;border-bottom:1px solid #ffffff0b;color:#eee}.admin-table>button:last-child,.admin-table>div:last-child{border-bottom:0}.admin-table span{display:flex;flex-direction:column;gap:4px;min-width:0}.admin-table b{font-size:10px}.admin-table small{font-size:8px;color:#7f7f8d;overflow:hidden;text-overflow:ellipsis;max-width:220px}.admin-table strong{font-size:10px;white-space:nowrap}.loading{min-height:55vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:#9a9aa7;font-size:12px}.spin{animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}@media(min-width:500px){.app-shell{padding-left:22px;padding-right:22px}.hero-card{min-height:310px;padding:26px}.hero-card h1{font-size:34px}.hero-art{right:10px;transform:scale(1.1)}.gift-art{height:120px}}@media(max-width:370px){.app-shell{padding-left:12px;padding-right:12px}.hero-card{padding:17px}.hero-card h1{font-size:25px}.hero-art{right:-27px;transform:scale(.86)}.gift-card{padding:9px}.gift-art{height:90px}.nav-item{font-size:7px}.section-title h2{font-size:19px}}

/* FROGI feature expansion: profile, PvP, Mines and referral/support panels */
.profile-strip{display:flex;align-items:center;gap:11px;padding:12px;border:1px solid var(--line);background:#121219;border-radius:15px}.avatar-circle,.avatar-large{display:grid;place-items:center;border-radius:50%;background:linear-gradient(145deg,#ff3658,#252c51);box-shadow:0 0 20px #ff355920}.avatar-circle{width:42px;height:42px;font-size:23px}.profile-strip>div:nth-child(2){display:flex;flex-direction:column;gap:4px;flex:1}.profile-strip strong{font-size:12px}.profile-strip span{font-size:9px;color:#9292a0}.level-pill{display:flex;align-items:center;gap:4px;color:#f0cb77;background:#f0cb7716;border:1px solid #f0cb7733;border-radius:8px;padding:6px;font-size:9px;font-weight:800}.feature-hero{padding:20px;border-radius:18px;border:1px solid #ffffff12;display:flex;flex-direction:column;align-items:flex-start;gap:9px}.feature-hero h3{margin:0;font-size:19px}.feature-hero p,.plain-copy{margin:0;color:#a2a2b0;font-size:11px;line-height:1.7}.pvp-hero{background:radial-gradient(circle at 90% 0%,#382034,transparent 45%),#14131c;color:#ff6c86}.mines-hero{background:radial-gradient(circle at 90% 0%,#19365a,transparent 45%),#12151d;color:#75b2ff}.feature-hero p{color:#aaaab7}.panel-card{padding:15px;display:flex;flex-direction:column;gap:13px;background:#131319;border:1px solid var(--line);border-radius:15px}.field-label{display:flex;flex-direction:column;gap:7px;color:#9999a7;font-size:10px}.field-label input,.inline-form input,.panel-card textarea{width:100%;border:1px solid #30303b;background:#09090e;color:#f4f4f8;border-radius:9px;padding:11px;outline:none;font:inherit;font-size:12px}.panel-card textarea{resize:vertical;line-height:1.6}.full{width:100%}.result-note{padding:11px;border:1px solid #2d614f;background:#10241f;border-radius:10px;color:#8ce8c1;font-size:10px;line-height:1.7}.activity-list{display:flex;flex-direction:column;gap:8px}.activity-row{display:flex;align-items:center;gap:10px;padding:11px;background:#14141b;border:1px solid #ffffff0d;border-radius:12px}.activity-avatar{width:35px;height:35px;border-radius:10px;background:#20202b;display:grid;place-items:center;font-size:18px}.activity-copy{display:flex;flex-direction:column;gap:4px;flex:1;min-width:0}.activity-copy b{font-size:10px}.activity-copy span{font-size:9px;color:#8e8e9b}.activity-row>strong{font-size:10px;color:#8ce8c1}.mines-top{display:flex;justify-content:space-between;font-size:10px;color:#9292a0}.mines-top b{color:#f5f5f8}.mine-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.mine-cell{aspect-ratio:1;border-radius:12px;border:1px solid #ffffff12;background:linear-gradient(145deg,#20202c,#12121a);color:#b7b7c5;font-size:22px;font-weight:900;box-shadow:inset 0 1px #ffffff09}.mine-cell:disabled{opacity:1}.mine-cell.mine-safe{background:#112b28;border-color:#2d7461}.mine-cell.mine-hit{background:#35151d;border-color:#96354b}.profile-card{padding:20px;text-align:center;border:1px solid #ffffff14;border-radius:20px;background:radial-gradient(circle at 50% 0%,#34202b,transparent 55%),#121219}.avatar-large{width:76px;height:76px;margin:0 auto 12px;font-size:42px}.profile-name{font-size:17px;font-weight:900}.profile-id{font-size:9px;color:#858593;margin-top:5px}.profile-metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:18px}.profile-metrics>div{display:flex;flex-direction:column;gap:5px;padding:10px 4px;background:#ffffff06;border-radius:10px}.profile-metrics b{font-size:16px}.profile-metrics span{font-size:9px;color:#8f8f9c}.inline-form{display:flex;gap:7px}.inline-form input{min-width:0}.inline-form button,.secondary-button{border:1px solid #ff35594a;background:#ff355916;color:#ff7188;border-radius:9px;padding:10px;font-size:10px;font-weight:800;white-space:nowrap}.form-message{font-size:10px;color:#8ce8c1;line-height:1.6;margin:0}.ref-code{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px;background:#09090e;border:1px dashed #41414e;border-radius:10px;font-size:12px;font-weight:900;letter-spacing:1px}.ref-code button{border:0;background:transparent;color:#ff7188;font-size:10px;font-weight:800}.ref-summary{display:flex;align-items:center;gap:8px;color:#a8a8b5;font-size:10px}.ref-summary b{margin-left:auto;color:#fff;font-size:15px}.fee-note{display:flex;align-items:flex-start;gap:10px;padding:13px;border:1px solid #53452b;background:#201b13;border-radius:12px;color:#e7c77e}.fee-note span{display:flex;flex-direction:column;gap:5px;font-size:10px;line-height:1.65;color:#b8ad96}.fee-note b{font-size:11px;color:#f2d99b}.bottom-nav{overflow-x:auto}.nav-item{min-width:48px}

/*
 * FROGI 2.0 — additional visual language.
 * The original dark neon appearance is preserved. These styles add consistent
 * control sizing, better touch targets, accessible focus states, resilient mobile
 * layouts, and responsive screens for the new interaction flows.
 */
:root {
  --frogi-radius-xs: 8px;
  --frogi-radius-sm: 12px;
  --frogi-radius-md: 16px;
  --frogi-radius-lg: 22px;
  --frogi-surface: #15151d;
  --frogi-surface-elevated: #1b1b24;
  --frogi-stroke: rgba(255, 255, 255, .085);
  --frogi-stroke-strong: rgba(255, 255, 255, .16);
  --frogi-red-soft: rgba(255, 53, 89, .14);
  --frogi-green-soft: rgba(98, 229, 178, .11);
  --frogi-blue-soft: rgba(57, 136, 255, .14);
  --frogi-yellow-soft: rgba(247, 196, 92, .13);
  --frogi-ease: cubic-bezier(.22, 1, .36, 1);
}
html { scroll-behavior: smooth; }
body { overscroll-behavior-y: contain; }
button, input, textarea, select { -webkit-tap-highlight-color: transparent; }
button { touch-action: manipulation; }
button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible {
  outline: 2px solid #82b6ff;
  outline-offset: 3px;
}
button:disabled { filter: saturate(.55); }
input, select, textarea { accent-color: #ff3559; }
button svg { flex-shrink: 0; }
img, svg { max-width: 100%; }
.sr-only { position: absolute !important; height: 1px !important; width: 1px !important; padding: 0 !important; overflow: hidden !important; clip: rect(0,0,0,0) !important; white-space: nowrap !important; border: 0 !important; }
.full { width: 100%; }
.muted { color: #9999a7; }
.center { text-align: center; }
.app-shell { overflow: visible; }
.brand-button { padding: 0; appearance: none; border: 0; color: inherit; background: transparent; text-align: left; }
.topbar { position: relative; }
.header-actions { display: flex; align-items: center; gap: 7px; }
.header-menu { display: none; }
.balance-pill { border: 1px solid #ff35592e; background: #27131d; color: #fff; border-radius: 12px; padding: 10px 11px; display: inline-flex; align-items: center; gap: 7px; font-size: 12px; font-weight: 800; }
.balance-pill svg { color: #ff6880; }
.balance-pill strong { font-size: 12px; font-variant-numeric: tabular-nums; }
.balance-pill span { color: #ff798f; font-size: 9px; }
.notification-shortcut { position: relative; }
.notification-shortcut i { position: absolute; right: -4px; top: -5px; min-width: 16px; height: 16px; line-height: 14px; display: grid; place-items: center; padding: 1px 4px; color: #fff; background: #ff3559; border: 2px solid #09090d; border-radius: 9px; font-size: 8px; font-style: normal; font-weight: 800; }
.success-toast { position: fixed; top: max(88px, env(safe-area-inset-top)); left: 50%; transform: translateX(-50%); z-index: 100; width: min(570px, calc(100vw - 26px)); display: flex; align-items: center; justify-content: space-between; gap: 10px; background: #15362e; color: #c2ffea; border: 1px solid #397e65; padding: 13px 15px; border-radius: 13px; box-shadow: 0 16px 48px #0009; font-size: 12px; font-weight: 700; animation: toast-in .25s var(--frogi-ease); }
.success-toast button { background: transparent; color: inherit; font-size: 20px; border: 0; }
@keyframes toast-in { from { opacity: 0; transform: translate(-50%, -8px); } to { opacity: 1; transform: translate(-50%, 0); } }
.startup-view { display: flex; flex-direction: column; justify-content: center; align-items: center; min-height: 60vh; gap: 13px; text-align: center; }
.startup-view h1 { font-size: clamp(23px, 6vw, 36px); letter-spacing: -.04em; margin: 0; }
.startup-view p { color: #aaaab7; line-height: 1.7; max-width: 330px; font-size: 12px; }
.startup-orbit { width: 108px; height: 108px; font-size: 65px; display: grid; place-items: center; border-radius: 50%; border: 1px solid #ff36584c; box-shadow: inset 0 0 34px #ff35591a, 0 0 45px #ff355921; animation: bob 2.8s ease-in-out infinite; }
.demo-banner { letter-spacing: .05em; }
.main-content { min-height: 62vh; }
.nav-item { min-width: 0; border-radius: 10px; transition: background .2s, color .2s; }
.nav-item:hover { background: #ffffff08; }
.nav-item:active { background: #ff35591a; }
.nav-item.active { background: #ff355913; }
.nav-item span { white-space: nowrap; }
.bottom-nav { overflow-x: visible; padding-left: max(8px, env(safe-area-inset-left)); padding-right: max(8px, env(safe-area-inset-right)); box-shadow: 0 -9px 30px #0008; }
.section-action { color: #ff7188; font-size: 11px; font-weight: 800; display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; border: 0; background: none; padding: 3px; }
.section-action:hover { color: #ffb6c2; }
.primary-button { min-height: 42px; }
.primary-button:hover:not(:disabled) { filter: brightness(1.10); transform: translateY(-1px); }
.secondary-button { display: inline-flex; justify-content: center; align-items: center; gap: 8px; min-height: 40px; }
.secondary-button:hover:not(:disabled) { background: #ff355926; }
.quick-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.quick-card { transition: transform .2s, border-color .2s, box-shadow .2s; }
.quick-card:hover { transform: translateY(-2px); border-color: #ffffff28; box-shadow: 0 8px 30px #0004; }
.quick-violet { background: linear-gradient(135deg, #25203e, #16131e); }
.quick-violet > div > svg { color: #b9a1ff; }
.quick-green { background: linear-gradient(135deg, #17362e, #111b1b); }
.quick-green > div > svg { color: #76ecc4; }
.hero-cta { position: relative; z-index: 3; }
.hero-card { box-shadow: inset 0 1px #ffffff0c, 0 15px 40px #0004; }
.hero-card h1 { max-width: 340px; }
.hero-art { pointer-events: none; }
.profile-strip .level-pill { border: 1px solid #f0cb7733; cursor: pointer; }
.profile-strip .level-pill:hover { background: #f0cb7728; }
.home-insights { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; }
.home-insights > div { display: flex; flex-direction: column; align-items: flex-start; gap: 5px; border: 1px solid #ffffff0d; background: linear-gradient(160deg, #181820, #111117); border-radius: 14px; padding: 13px; }
.home-insights .insight-icon { font-size: 19px; margin-bottom: 4px; }
.home-insights strong { font-size: 20px; letter-spacing: -.04em; font-variant-numeric: tabular-nums; }
.home-insights small { font-size: 9px; color: #9797a6; line-height: 1.3; }
.daily-card { display: flex; align-items: center; gap: 13px; position: relative; overflow: hidden; background: linear-gradient(115deg, #292017, #1a1620 60%, #21152a); border: 1px solid #f1ba5533; border-radius: 17px; padding: 17px; }
.daily-card::before { content: ''; position: absolute; inset: -75% auto auto 82%; width: 150px; height: 150px; border-radius: 50%; background: #ebbb3d1a; filter: blur(27px); pointer-events: none; }
.daily-symbol { display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 5px; flex: 0 0 57px; height: 61px; border-radius: 14px; background: #f7bf4516; color: #ffd681; }
.daily-symbol span { font-size: 12px; font-weight: 900; }
.daily-copy { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.daily-copy .micro-label { color: #ffca72; }
.daily-copy strong { font-size: 13px; line-height: 1.25; }
.daily-copy p { margin: 0; color: #b6a7a1; line-height: 1.45; font-size: 10px; }
.daily-claim { width: 36px; height: 36px; flex: 0 0 36px; border: 0; border-radius: 11px; display: grid; place-items: center; color: #201606; background: #f0bf60; box-shadow: 0 5px 15px #f0bf602e; }
.daily-claim.claimed { background: #273b31; color: #71dbaa; box-shadow: none; }
.micro-label { font-size: 9px; font-weight: 800; letter-spacing: 1px; display: inline-flex; align-items: center; gap: 6px; }
.gift-card { height: 100%; }
.gift-preview { display: flex; flex-direction: column; width: 100%; min-height: 138px; padding: 0; border: 0; color: inherit; text-align: left; background: transparent; }
.gift-preview:disabled { cursor: default; opacity: 1; }
.gift-preview .gift-art { width: 100%; flex-shrink: 0; }
.gift-preview .gift-details { width: 100%; }
.gift-preview:not(:disabled):hover .gift-art > span { transform: translateY(-4px) scale(1.06); }
.gift-preview .gift-art > span { transition: transform .22s var(--frogi-ease); }
.gift-card-top { gap: 4px; }
.gift-sparkle { color: #ffffff55; }
.rarity-common .rarity-tag { color: #c1c2d5; background: #9293a423; }
.rarity-rare .rarity-tag { color: #6fadff; background: #3988ff23; }
.favorite-button { color: #888895; border: 0; border-radius: 7px; background: #ffffff06; width: 30px; height: 30px; display: grid; place-items: center; }
.favorite-button:hover, .favorite-button.is-active { color: #ff5274; background: #ff355917; }
.search-box { display: flex; align-items: center; gap: 10px; padding: 11px 13px; min-height: 43px; background: #14141b; border: 1px solid #ffffff18; border-radius: 12px; color: #9292a0; }
.search-box:focus-within { border-color: #ff5673; box-shadow: 0 0 0 2px #ff355918; }
.search-box input { background: transparent; border: none; outline: none; padding: 0; min-width: 0; flex: 1; color: #fff; font-size: 12px; }
.search-box button { border: 0; background: transparent; color: #9999a7; display: grid; place-items: center; width: 24px; height: 24px; }
.horizontal-filter { max-width: 100%; overflow-x: auto; scrollbar-width: none; margin: -2px -4px; padding: 2px 4px 6px; }
.horizontal-filter::-webkit-scrollbar { display: none; }
.filter-tabs { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.horizontal-filter .filter-tabs { flex-wrap: nowrap; width: max-content; }
.filter-tabs button { border-radius: 9px; border: 1px solid #ffffff13; background: #16161e; color: #a4a4b3; font-size: 10px; font-weight: 700; padding: 9px 13px; white-space: nowrap; transition: background .2s, color .2s, border-color .2s; }
.filter-tabs button:hover { color: #fff; border-color: #ffffff29; }
.filter-tabs button.selected { color: #ff8195; background: #ff35591b; border-color: #ff35595a; }
.catalog-toolbar { display: flex; justify-content: space-between; align-items: center; gap: 8px; color: #8f8f9e; font-size: 10px; }
.catalog-toolbar b { color: #fff; }
.catalog-toolbar label { display: inline-flex; align-items: center; gap: 4px; color: #9494a6; }
.catalog-toolbar select { border: 0; background: #17171e; color: #c5c5d2; font: inherit; font-weight: 700; border-radius: 8px; padding: 9px 7px; max-width: 160px; }
.market-grid .market-card { min-width: 0; display: flex; flex-direction: column; }
.market-card > .gift-card { border-radius: 15px 15px 0 0; border-bottom-color: transparent; }
.market-card-bottom { display: flex; align-items: center; justify-content: space-between; gap: 7px; background: #191920; border: 1px solid #ffffff12; border-top: 0; border-radius: 0 0 15px 15px; padding: 10px; }
.market-card-bottom > div { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.market-card-bottom strong { font-size: 13px; white-space: nowrap; }
.market-card-bottom strong small { font-size: 9px; color: #aaaab5; }
.market-card-bottom span { font-size: 9px; color: #828291; }
.market-card-bottom button { flex: 0 0 auto; padding: 10px 9px; min-width: 67px; border: 0; border-radius: 9px; background: #ff3559; color: #fff; font-size: 10px; font-weight: 800; }
.market-card-bottom button:disabled { background: #35313a; color: #aba2ab; font-size: 9px; min-width: 0; }
.market-card-bottom button:hover:not(:disabled) { background: #ff5b78; }
.market-banner { display: flex; align-items: center; gap: 12px; border-radius: 15px; padding: 15px; background: linear-gradient(105deg, #21131b, #171721); border: 1px solid #ff35592c; color: #ff6682; }
.market-banner > div { display: flex; flex-direction: column; gap: 6px; }
.market-banner b { font-size: 12px; color: #fff; }
.market-banner span { color: #a9a0af; font-size: 10px; line-height: 1.5; }
.collection-banner { position: relative; display: flex; align-items: center; gap: 12px; padding: 16px; border-radius: 17px; overflow: hidden; border: 1px solid #5695ed30; background: linear-gradient(105deg, #121e36, #15151e 72%); }
.collection-art { width: 56px; height: 56px; display: grid; place-items: center; border: 1px solid #448bff37; border-radius: 13px; background: #3988ff19; font-size: 32px; }
.collection-banner > div:nth-child(2) { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 6px; }
.collection-banner strong { font-size: 13px; }
.collection-banner span { color: #a5b7d1; font-size: 10px; }
.collection-banner .collection-count { color: #91bfff; font-size: 25px; font-weight: 900; }
.favorites-toggle { display: inline-flex; align-items: center; gap: 5px; border: 1px solid #ffffff16; color: #aaaab8; background: #16161c; padding: 9px; border-radius: 9px; font-size: 10px; }
.favorites-toggle.is-active { border-color: #ff35594a; background: #ff35591c; color: #ff839a; }
.inventory-wrap { min-width: 0; }
.inventory-wrap .gift-card { height: auto; }
.acquired { color: #77778a; font-size: 9px; text-align: center; margin: 8px 0 2px; }
.notice { display: flex; align-items: flex-start; gap: 10px; padding: 12px 13px; background: #141b27; border: 1px solid #2b4360; border-radius: 11px; color: #a8c8ea; font-size: 10px; line-height: 1.65; }
.notice svg { flex-shrink: 0; margin-top: 1px; color: #6aa9ff; }
.notice-warning { background: #292015; border-color: #755027; color: #edc28f; }
.notice-warning svg { color: #edb65e; }
.notice-error { background: #28141d; border-color: #792e45; color: #ffc0cc; }
.notice-error svg { color: #ff6a87; }
.empty-state { display: flex; align-items: center; flex-direction: column; gap: 10px; padding: 32px 19px; text-align: center; border: 1px dashed #353541; border-radius: 18px; background: #14141a; }
.empty-state > div:first-child { width: 55px; height: 55px; border-radius: 16px; color: #ff7188; background: #ff355914; display: grid; place-items: center; }
.empty-state strong { font-size: 14px; }
.empty-state p { margin: 0; line-height: 1.7; font-size: 11px; color: #9898a7; max-width: 290px; }
.empty-action { margin-top: 7px; }
.modal-backdrop { position: fixed; inset: 0; z-index: 70; background: rgba(3, 3, 8, .82); backdrop-filter: blur(6px); display: flex; align-items: center; justify-content: center; padding: 20px; animation: modal-appear .18s ease both; }
@keyframes modal-appear { from { opacity: 0; } to { opacity: 1; } }
.item-modal, .confirm-modal { max-height: calc(100dvh - 36px); overflow-y: auto; width: min(400px, 100%); color: #f6f6fa; border-radius: 22px; border: 1px solid #ffffff2b; background: linear-gradient(150deg, #21212a, #111116 70%); box-shadow: 0 25px 80px #000d; padding: 21px; position: relative; text-align: center; }
.modal-close { position: absolute; right: 12px; top: 12px; border: 1px solid #ffffff1b; color: #c9c9d1; background: #22222b; width: 32px; height: 32px; display: grid; place-items: center; border-radius: 9px; z-index: 2; }
.modal-art { height: 152px; position: relative; display: grid; place-items: center; border: 1px solid #ffffff1a; border-radius: 17px; margin-bottom: 12px; overflow: hidden; }
.modal-art > span { z-index: 1; font-size: 88px; filter: drop-shadow(0 10px 15px #0008); }
.modal-orbit { position: absolute; inset: 40px 27%; border: 1px solid #ff35595a; border-radius: 50%; transform: rotate(-35deg); }
.rarity-chip { color: #d0d0e0; font-size: 10px; font-weight: 900; text-transform: uppercase; letter-spacing: 1px; }
.rarity-text-rare { color: #7bb7ff; }
.rarity-text-epic { color: #b09bff; }
.rarity-text-legendary { color: #ffd386; }
.item-modal > h2 { font-size: 21px; letter-spacing: -.04em; margin: 9px 0; }
.item-modal > p { color: #aaaab6; font-size: 12px; line-height: 1.65; margin: 0 0 18px; }
.modal-information { display: flex; justify-content: space-between; align-items: center; gap: 12px; text-align: left; padding: 11px 0; border-bottom: 1px solid #ffffff11; }
.modal-information span { color: #9696a4; font-size: 11px; }
.modal-information b { font-size: 11px; font-weight: 700; text-align: right; }
.item-modal > .primary-button { margin-top: 17px; }
.item-modal > .secondary-button { margin-top: 9px; }
.item-modal > .tiny-note { display: block; margin-top: 14px; }
.confirm-modal { width: min(355px, 100%); }
.confirm-emoji { font-size: 64px; padding: 10px; }
.confirm-modal > h3 { margin: 7px 0 9px; font-size: 19px; }
.confirm-modal > p { font-size: 12px; color: #aaaab5; line-height: 1.7; margin-bottom: 20px; }
.confirm-modal > p b { color: #fff; }
.confirm-modal > .secondary-button { margin-top: 10px; }
.wheel-intro { display: flex; align-items: center; gap: 8px; }
.wheel-intro > svg { flex: 0 0 auto; color: #79e4b5; }
.wheel-possible { display: flex; align-items: center; justify-content: center; gap: 8px; color: #b7b2c0; font-size: 10px; }
.wheel-possible svg { color: #ffd386; }
.wheel-balance { display: flex; justify-content: space-between; align-items: center; padding: 12px 15px; border-radius: 11px; border: 1px solid #ffffff12; background: #14141a; }
.wheel-balance span { color: #9292a0; font-size: 11px; }
.wheel-balance b { color: #ff8195; font-size: 12px; }
.wheel-ring { will-change: transform; }
.prob-name { display: flex; flex-direction: column; gap: 3px; }
.prob-name small { color: #777785; font-size: 8px; }
.result-lost { background: #32181e; color: #ffc1cf; border-color: #743142; }
.pvp-versus { display: flex; align-items: center; justify-content: space-around; gap: 8px; padding: 14px; background: #0d0d14; border-radius: 13px; }
.pvp-versus > span { display: flex; flex-direction: column; align-items: center; gap: 7px; font-size: 38px; }
.pvp-versus > span small { color: #9999a7; font-size: 9px; font-weight: 900; }
.pvp-versus > strong { font-size: 20px; font-style: italic; letter-spacing: -.08em; color: #ff7188; }
.pvp-subtitle { text-align: center; font-size: 14px; margin: 5px; }
.pvp-choices { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; }
.pvp-choices button { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; min-height: 99px; background: #1a1a25; border: 1px solid #ffffff14; border-radius: 13px; color: #eee; }
.pvp-choices button[aria-pressed="true"] { background: #32202c; border-color: #ff35596b; }
.pvp-choices button > span { font-size: 37px; }
.pvp-choices button > small { color: #c3c3d0; font-size: 11px; }
.pvp-result { border: 1px solid #4b7e63; background: #15271f; border-radius: 13px; padding: 16px; text-align: center; }
.pvp-result > div { display: flex; justify-content: center; gap: 16px; align-items: center; font-size: 35px; }
.pvp-result > div > span { color: #90c4a5; font-size: 13px; font-weight: 900; }
.pvp-result > strong { display: block; margin-top: 9px; font-size: 17px; }
.pvp-result > p { color: #aac5b7; font-size: 11px; margin: 7px 0 0; }
.profile-cover { position: absolute; inset: 0 0 auto; height: 95px; background: radial-gradient(ellipse at top, #ff355934, transparent 76%); pointer-events: none; }
.profile-card { position: relative; overflow: hidden; }
.profile-card > .avatar-large { position: relative; border: 2px solid #ffffff2b; }
.profile-rank { display: inline-flex; align-items: center; gap: 5px; background: #e8bc5820; color: #f3d07d; border-radius: 8px; padding: 6px 9px; font-size: 10px; font-weight: 800; margin-top: 12px; }
.panel-heading { display: inline-flex; align-items: center; gap: 8px; color: #fafafa; font-size: 13px; }
.panel-heading svg { color: #ff6c88; }
.panel-card .inline-form input { flex: 1; }
.inline-form button:disabled { opacity: .45; }
.settings-group { overflow: hidden; border: 1px solid #ffffff15; border-radius: 14px; background: #14141c; }
.settings-group > h3 { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #9b9ba7; padding: 16px 15px 12px; margin: 0; }
.settings-group .link-row, .settings-group .setting-row { border-top: 1px solid #ffffff0e; }
.link-row { width: 100%; border: 0; text-align: left; background: transparent; display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 15px; color: #d4d4e0; font-size: 11px; }
.link-row:hover { background: #ffffff06; }
.link-row > span { display: inline-flex; align-items: center; gap: 11px; }
.link-row > span > svg { color: #ff7188; }
.link-row > svg { color: #747485; }
.profile-id-panel { display: flex; justify-content: space-between; align-items: center; gap: 9px; padding: 12px; background: #14141c; border: 1px solid #ffffff15; border-radius: 12px; }
.profile-id-panel > div { min-width: 0; display: flex; flex-direction: column; gap: 7px; }
.profile-id-panel span { color: #9898a4; font-size: 9px; }
.profile-id-panel strong { overflow: hidden; text-overflow: ellipsis; font-size: 11px; }
.copy-button { flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; gap: 6px; background: #ff35591b; color: #ff8499; border-radius: 8px; padding: 9px; border: 1px solid #ff35593f; font-size: 10px; font-weight: 800; }
/* Control stability for dynamic Russian strings, large font accessibility & small screens. */
@media (max-width: 410px) {
  .header-actions { gap: 4px; }
  .brand-name { font-size: 14px; }
  .balance-pill { padding: 9px 8px; gap: 4px; font-size: 10px; }
  .balance-pill strong { font-size: 10px; }
  .hero-card h1 { font-size: 27px; }
  .home-insights > div { padding: 10px; }
  .home-insights small { font-size: 8px; }
  .market-card-bottom { padding: 8px; gap: 5px; }
  .market-card-bottom button { min-width: 58px; padding: 9px 5px; font-size: 9px; }
  .market-card-bottom strong { font-size: 11px; }
  .daily-card { padding: 12px; gap: 9px; }
  .daily-symbol { flex-basis: 48px; width: 48px; }
  .daily-copy strong { font-size: 11px; }
  .daily-copy p { font-size: 9px; }
}
@media (min-width: 650px) {
  .header-menu { display: grid; }
  .app-shell { max-width: 700px; }
  .bottom-nav { width: min(700px, 100%); }
  .gift-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .market-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 350px) {
  .brand-sub { font-size: 7px; }
  .brand-mark { width: 35px; height: 35px; }
  .hero-art { opacity: .68; }
  .hero-card h1 { font-size: 24px; }
  .gift-details strong { font-size: 11px; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .01ms !important; transition-duration: .01ms !important; scroll-behavior: auto !important; }
}
html.reduce-motion *, html.reduce-motion *::before, html.reduce-motion *::after { animation-duration: .01ms !important; transition-duration: .01ms !important; scroll-behavior: auto !important; }

/* Feature pages: achievements, missions, history, notifications, support, settings.
 * All blocks are fluid and usable in 320px-wide Telegram WebViews.
 */
.level-banner { display: flex; align-items: center; gap: 18px; padding: 21px; border: 1px solid #7d6de63c; border-radius: 18px; background: linear-gradient(120deg, #251c3c, #181725 65%, #1b172b); }
.level-orb { width: 62px; height: 62px; flex: 0 0 62px; background: linear-gradient(135deg, #9c70ef, #5c3ab5); color: #efdaff; box-shadow: 0 0 25px #936de547, inset 0 0 10px #ffffff14; border-radius: 19px; display: grid; place-items: center; }
.level-banner-copy { flex: 1; min-width: 0; }
.level-banner-copy .micro-label { color: #baaff7; }
.level-banner-copy h3 { margin: 7px 0 13px; font-size: 19px; letter-spacing: -.02em; }
.level-row { display: flex; justify-content: space-between; gap: 8px; margin-bottom: 7px; color: #bbb2d8; font-size: 10px; font-weight: 700; }
.progress-track { overflow: hidden; background: #333341; width: 100%; height: 7px; border-radius: 99px; }
.progress-fill { height: 100%; border-radius: inherit; background: linear-gradient(90deg, #ff3860, #ffa0ae); transition: width .4s ease; }
.level-banner .progress-fill { background: linear-gradient(90deg, #9267f4, #cb98ff); }
.mission-list { display: flex; flex-direction: column; gap: 9px; }
.mission-card { display: flex; align-items: center; gap: 12px; background: #15151d; border: 1px solid #ffffff13; border-radius: 14px; padding: 14px; }
.mission-complete { background: linear-gradient(95deg, #15271f, #15191b); border-color: #3b865c45; }
.mission-icon { flex: 0 0 42px; width: 42px; height: 42px; border-radius: 12px; display: grid; place-items: center; background: #ff355917; font-size: 23px; }
.mission-complete .mission-icon { background: #49ac6b22; }
.mission-copy { flex: 1; display: flex; flex-direction: column; gap: 7px; min-width: 0; }
.mission-copy strong { font-size: 12px; }
.mission-copy > span { font-size: 10px; color: #9898a6; line-height: 1.35; }
.mission-copy > small { color: #9c9cab; font-size: 9px; }
.mission-card .progress-track { height: 5px; }
.mission-check { width: 35px; text-align: center; flex: 0 0 35px; color: #a3a3b4; font-size: 10px; font-weight: 800; }
.mission-complete .mission-check { color: #6ee1a8; }
.achievement-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.achievement-item { position: relative; min-width: 0; border-radius: 14px; border: 1px solid #ffffff16; background: linear-gradient(135deg, #191921, #101015); padding: 14px; display: flex; flex-direction: column; gap: 7px; min-height: 146px; }
.achievement-item.unlocked { border-color: #dbb05852; background: linear-gradient(140deg, #292219, #15141a); }
.achievement-emoji { display: flex; align-items: center; justify-content: center; width: 37px; height: 37px; font-size: 24px; background: #ffffff0b; border-radius: 10px; }
.achievement-item.unlocked .achievement-emoji { background: #ebae4c20; }
.achievement-item b { font-size: 11px; line-height: 1.35; }
.achievement-item small { color: #898997; font-size: 9px; line-height: 1.35; flex: 1; }
.achievement-state { font-size: 9px; color: #858594; display: inline-flex; align-items: center; gap: 5px; font-weight: 800; }
.achievement-item.unlocked .achievement-state { color: #f0c56e; }
.hint-card { display: flex; align-items: flex-start; gap: 10px; background: #251d14; border: 1px solid #c3954c35; border-radius: 13px; padding: 13px; }
.hint-card svg { flex: 0 0 auto; color: #e9ba60; }
.hint-card p { margin: 0; font-size: 10px; line-height: 1.7; color: #d0bfa6; }
.leaderboard-hero { background: radial-gradient(circle at 85% 15%, #63482e55, transparent 50%), linear-gradient(145deg, #222018, #17171c); color: #f1c778; }
.leaderboard-hero h3 { color: #fff; }
.podium { display: flex; justify-content: center; align-items: flex-end; gap: 8px; padding: 13px 4px 0; }
.podium-user { min-width: 0; flex: 1; display: flex; flex-direction: column; align-items: center; text-align: center; border-radius: 13px 13px 0 0; border: 1px solid #ffffff1b; background: linear-gradient(160deg, #29232d, #181821); padding: 13px 6px 0; }
.podium-1 { order: 2; min-height: 193px; border-color: #edc46d6b; background: linear-gradient(150deg, #35271d, #19161b); }
.podium-2 { order: 1; min-height: 163px; }
.podium-3 { order: 3; min-height: 147px; }
.podium-medal { font-size: 27px; }
.podium-avatar { display: grid; place-items: center; border-radius: 50%; background: #ffffff0e; border: 1px solid #ffffff2b; width: 49px; height: 49px; margin: 8px 0; font-size: 30px; }
.podium-user strong { font-size: 11px; width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.podium-user > span { font-size: 9px; color: #b7b2bb; margin: 5px 0 14px; }
.podium-base { font-weight: 900; font-size: 14px; color: #f1c778; width: 100%; padding: 10px 0; background: #ffffff0d; }
.ranking-list { display: flex; flex-direction: column; gap: 7px; }
.ranking-row { display: flex; align-items: center; gap: 10px; min-width: 0; padding: 11px; border-radius: 12px; background: #15151d; border: 1px solid #ffffff13; }
.ranking-row.my-rank { border-color: #ff35594c; background: #27181f; }
.rank-number { flex: 0 0 26px; width: 26px; text-align: center; font-size: 13px; color: #c4c4d1; font-weight: 900; }
.rank-avatar { flex: 0 0 33px; width: 33px; height: 33px; border-radius: 10px; display: grid; place-items: center; background: #ffffff10; font-size: 20px; }
.rank-name { flex: 1; display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.rank-name strong { font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rank-name em { font-style: normal; font-size: 9px; color: #ff8198; padding: 2px 5px; background: #ff355924; margin-left: 5px; border-radius: 5px; }
.rank-name small { font-size: 9px; color: #898995; }
.ranking-row > b { white-space: nowrap; font-size: 12px; }
.ranking-row > b > small { font-size: 8px; color: #898995; }
.my-rank-banner { display: flex; align-items: center; gap: 12px; border: 1px solid #ff355934; background: #211720; border-radius: 14px; padding: 13px; }
.my-rank-banner > svg { flex-shrink: 0; color: #ff6e8a; }
.my-rank-banner > div { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 5px; }
.my-rank-banner strong { font-size: 11px; }
.my-rank-banner span { font-size: 9px; color: #a9a0aa; }
.my-rank-banner button { display: inline-flex; align-items: center; gap: 4px; border: 0; color: #ff8196; background: none; font-size: 10px; }
.news-feature { position: relative; overflow: hidden; border-radius: 20px; border: 1px solid #ffffff20; background: linear-gradient(145deg, #391720, #191829 70%); padding: 24px 19px 25px; min-height: 222px; }
.news-feature .micro-label { color: #ff8ba4; }
.news-feature h3 { font-size: 23px; line-height: 1.2; margin: 20px 0 9px; max-width: 235px; }
.news-feature p { font-size: 11px; line-height: 1.7; color: #b6a7b5; margin: 0; max-width: 245px; }
.news-feature-art { position: absolute; right: 11px; bottom: 5px; font-size: 75px; transform: rotate(-13deg); filter: drop-shadow(0 13px 15px #000a); }
.news-feature-art svg { position: absolute; top: -8px; right: -9px; color: #ff9caf; }
.news-feed { display: flex; flex-direction: column; gap: 10px; }
.news-card { display: flex; gap: 13px; background: #14141b; border: 1px solid #ffffff13; border-radius: 14px; padding: 15px; }
.news-card-icon { flex: 0 0 42px; width: 42px; height: 42px; background: #ff355913; display: grid; place-items: center; border-radius: 11px; font-size: 22px; }
.news-card > div:nth-child(2) { min-width: 0; }
.news-meta { color: #898997; font-size: 9px; }
.news-card h3 { margin: 6px 0 7px; font-size: 13px; }
.news-card p { font-size: 10px; line-height: 1.7; margin: 0; color: #adadba; }
.notification-list { display: flex; flex-direction: column; gap: 9px; }
.notification-item { display: flex; align-items: flex-start; gap: 12px; background: #16161e; border: 1px solid #ffffff12; border-radius: 14px; padding: 14px; }
.notification-item.unread { background: #211823; border-color: #ff35592d; }
.notification-symbol { flex: 0 0 38px; width: 38px; height: 38px; display: grid; place-items: center; border-radius: 12px; color: #ff7c94; background: #ff355917; }
.notification-copy { flex: 1; min-width: 0; }
.notification-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.notification-heading strong { font-size: 11px; }
.unread-dot { width: 7px; height: 7px; flex-shrink: 0; border-radius: 100%; background: #ff4b6a; box-shadow: 0 0 7px #ff3559; }
.notification-copy p { font-size: 10px; line-height: 1.65; color: #b0aab5; margin: 6px 0; }
.notification-copy > span { color: #7d7d8e; font-size: 9px; }
.support-hero { color: #74afff; background: radial-gradient(circle at 85% 15%, #2c4a7545, transparent 60%), #141821; }
.support-hero h3 { color: #fff; }
.support-form { display: flex; flex-direction: column; gap: 13px; }
.field-label { font-weight: 700; color: #adaab8; font-size: 10px; }
.field-label input, .field-label textarea, .field-label select { width: 100%; color: #f6f6fa; background: #0e0e14; border: 1px solid #ffffff25; border-radius: 10px; padding: 11px 12px; font-size: 12px; line-height: 1.5; }
.field-label input:focus, .field-label textarea:focus, .field-label select:focus { outline: none; border-color: #ff5877; box-shadow: 0 0 0 2px #ff355917; }
.field-label select { appearance: auto; }
.field-label option { background: #17171f; }
.field-label textarea { min-height: 112px; resize: vertical; }
.form-counter { text-align: right; font-size: 9px; color: #858594; }
.ticket-list { display: flex; flex-direction: column; gap: 9px; }
.ticket-row { display: flex; align-items: flex-start; gap: 10px; justify-content: space-between; background: #14141c; border: 1px solid #ffffff16; padding: 13px; border-radius: 12px; }
.ticket-row > div { display: flex; min-width: 0; flex-direction: column; gap: 6px; }
.ticket-id { font-size: 9px; color: #8d8d99; }
.ticket-row strong { font-size: 11px; overflow-wrap: anywhere; }
.ticket-row small { color: #858593; font-size: 9px; }
.ticket-status { white-space: nowrap; padding: 6px 8px; border-radius: 7px; font-size: 9px; font-weight: 800; }
.ticket-open { background: #dca94a21; color: #f1cb85; }
.ticket-resolved { background: #55ae7e25; color: #93e9b1; }
.setting-row { display: flex; align-items: center; gap: 12px; padding: 15px; position: relative; cursor: pointer; }
.setting-icon { color: #ff6e87; display: grid; place-items: center; flex: 0 0 28px; }
.setting-text { display: flex; flex-direction: column; gap: 5px; flex: 1; min-width: 0; }
.setting-text b { font-size: 11px; }
.setting-text small { font-size: 9px; color: #9494a5; line-height: 1.45; }
.switch-input { position: absolute; right: 14px; width: 47px; height: 29px; opacity: 0; cursor: pointer; z-index: 1; }
.switch-track { flex: 0 0 42px; width: 42px; height: 25px; border-radius: 16px; border: 1px solid #ffffff20; background: #32323d; display: flex; align-items: center; padding: 3px; transition: background .2s; }
.switch-track span { display: block; width: 17px; height: 17px; border-radius: 100%; background: #dedee8; transition: transform .2s; }
.switch-input:checked + .switch-track { background: #fa355d; border-color: #ff5676; }
.switch-input:checked + .switch-track span { background: #fff; transform: translateX(17px); }
.switch-input:focus-visible + .switch-track { outline: 2px solid #82b6ff; outline-offset: 3px; }
.about-card { display: flex; align-items: flex-start; gap: 13px; border: 1px solid #2c644d; background: #11251d; border-radius: 13px; padding: 15px; }
.about-card > svg { color: #73e0ae; flex: 0 0 auto; }
.about-card strong { font-size: 12px; }
.about-card p { font-size: 10px; line-height: 1.7; color: #a7c8b4; margin: 6px 0 0; }
.wallet-card-large { padding: 21px; }
.wallet-summary { border-top: 1px solid #ffffff17; display: grid; grid-template-columns: 1fr 1fr; gap: 14px; padding-top: 17px; margin-top: 15px; }
.wallet-summary > div { display: flex; flex-direction: column; gap: 8px; }
.wallet-summary span { font-size: 10px; color: #9999a6; }
.wallet-summary b { font-size: 14px; }
.spin-history { display: flex; flex-direction: column; gap: 7px; }
.spin-history-row { display: flex; align-items: center; gap: 10px; border: 1px solid #ffffff12; border-radius: 11px; background: #15151c; padding: 10px; }
.spin-history-row > span { width: 34px; height: 34px; flex: 0 0 34px; border-radius: 10px; background: #ffffff0c; display: grid; place-items: center; font-size: 21px; }
.spin-history-row > div { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.spin-history-row b { font-size: 11px; }
.spin-history-row small { font-size: 9px; color: #848493; }
.spin-history-row > strong { font-size: 11px; color: #ff8194; white-space: nowrap; }
.ledger-list { background: #111116; border: 1px solid #ffffff12; border-radius: 14px; padding: 0 12px; }
.ledger-row:last-child { border-bottom: none; }
.ledger-icon.credit { background: #1a382d; color: #70e0aa; }
.ledger-icon.debit { background: #361922; color: #ff8195; }
.ledger-list--compact .ledger-row { padding: 9px 0; }
.directory-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.directory-item { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; position: relative; min-height: 121px; padding: 15px; text-align: left; background: linear-gradient(145deg, #1a1a24, #121218); border: 1px solid #ffffff16; border-radius: 14px; color: #f7f7fb; transition: background .2s, border-color .2s, transform .2s; }
.directory-item:hover { background: #22222c; border-color: #ff35594f; transform: translateY(-1px); }
.directory-icon { border: 1px solid #ffffff16; background: #ffffff0a; border-radius: 11px; display: grid; place-items: center; font-size: 23px; width: 42px; height: 42px; }
.directory-item > div { display: flex; flex-direction: column; gap: 5px; max-width: calc(100% - 12px); }
.directory-item strong { font-size: 11px; }
.directory-item small { font-size: 9px; color: #9494a4; line-height: 1.4; }
.directory-item > svg { position: absolute; right: 12px; top: 25px; color: #797988; }
.retry-block { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 10px; padding: 30px 14px; border: 1px solid #b2536045; background: #241820; border-radius: 14px; }
.retry-block > svg { color: #ff7f93; }
.retry-block b { font-size: 12px; }
.retry-block p { font-size: 11px; line-height: 1.6; color: #c5a0ac; margin: 0; }
.inline-spinner { display: inline-flex; align-items: center; gap: 7px; color: #aaaaba; font-size: 11px; }
.table-action { background: #22503a !important; color: #8af5bb !important; border-radius: 6px !important; padding: 7px 8px !important; font-size: 9px !important; border: 1px solid #45875c !important; width: auto !important; }
@media (max-width: 375px) {
  .mission-icon { width: 34px; height: 34px; flex-basis: 34px; }
  .mission-card { gap: 8px; padding: 11px; }
  .mission-check { flex-basis: 29px; width: 29px; }
  .podium { gap: 5px; }
  .podium-user { padding: 9px 4px 0; }
  .podium-user strong { font-size: 9px; }
  .podium-user > span { font-size: 8px; }
  .podium-avatar { width: 42px; height: 42px; font-size: 24px; }
  .ranking-row { gap: 7px; padding: 9px; }
  .rank-number { flex-basis: 20px; }
  .rank-avatar { flex-basis: 28px; width: 28px; height: 28px; }
  .news-feature-art { opacity: .7; }
}

/* Private collection dashboards: clean, responsive charts without chart dependencies. */
.analytics-hero { isolation: isolate; overflow: hidden; position: relative; padding: 21px; border: 1px solid #4151a34d; border-radius: 17px; background: linear-gradient(135deg, #262146, #171a31 55%, #191523); }
.analytics-hero .micro-label { color: #bca6ff; }
.analytics-hero h2 { max-width: 300px; margin: 17px 0 10px; letter-spacing: -.04em; font-size: 23px; line-height: 1.22; }
.analytics-hero p { max-width: 315px; position: relative; z-index: 1; color: #b5b1c8; margin: 0; font-size: 11px; line-height: 1.7; }
.analytics-hero-art { color: #b6a2ff36; position: absolute; right: -12px; top: 16px; transform: scale(2.2) rotate(-8deg); }
.analytics-metrics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.analytics-metric { padding: 16px; background: #16161f; border: 1px solid #ffffff15; border-radius: 14px; display: flex; flex-direction: column; gap: 6px; }
.analytics-metric-icon { display: grid; place-items: center; width: 36px; height: 36px; background: #ff355916; color: #ff6d86; border-radius: 11px; margin-bottom: 6px; }
.analytics-metric--blue .analytics-metric-icon { background: #3988ff1d; color: #78b2ff; }
.analytics-metric--pink .analytics-metric-icon { background: #ef5c901d; color: #ffa1c3; }
.analytics-metric--gold .analytics-metric-icon { background: #dfb66c1d; color: #efc680; }
.analytics-metric > span { font-size: 10px; color: #a4a4b1; }
.analytics-metric > strong { font-size: 24px; line-height: 1.2; letter-spacing: -.03em; }
.analytics-metric > small { color: #777787; font-size: 9px; }
.analytics-breakdown { display: flex; flex-direction: column; align-items: center; gap: 23px; border: 1px solid #ffffff14; border-radius: 16px; background: #15151c; padding: 19px; }
.analytics-donut { display: grid; place-items: center; width: 168px; height: 168px; border-radius: 100%; flex: 0 0 168px; box-shadow: 0 0 25px #0004; }
.analytics-donut-center { border-radius: 100%; background: #16161c; height: 119px; width: 119px; display: flex; flex-direction: column; justify-content: center; align-items: center; gap: 4px; }
.analytics-donut-center strong { font-size: 33px; letter-spacing: -.05em; }
.analytics-donut-center small { font-size: 9px; color: #9999a6; letter-spacing: 1px; }
.analytics-breakdown-bars { display: flex; width: 100%; flex-direction: column; gap: 14px; }
.analytics-category-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 11px; margin-bottom: 8px; }
.analytics-category-head > div { display: flex; align-items: center; gap: 7px; }
.analytics-category-head i { width: 8px; height: 8px; border-radius: 3px; }
.analytics-category-head strong { font-size: 11px; font-variant-numeric: tabular-nums; }
.analytics-category-head small { font-weight: normal; font-size: 9px; color: #858594; }
.analytics-category-track { overflow: hidden; background: #30303b; height: 7px; border-radius: 99px; }
.analytics-category-track > span { display: block; height: 100%; border-radius: inherit; transition: width .4s ease; }
.analytics-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.analytics-summary > div { padding: 12px 8px; display: flex; flex-direction: column; gap: 9px; background: #17171f; border: 1px solid #ffffff13; border-radius: 11px; }
.analytics-summary span { font-size: 9px; color: #898999; }
.analytics-summary strong { font-size: 11px; overflow-wrap: anywhere; }
.analytics-coverage { background: #171721; border: 1px solid #ffffff13; border-radius: 14px; padding: 16px; }
.analytics-coverage > div:first-child { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 12px; font-size: 10px; color: #b2b2c0; }
.analytics-coverage strong { color: #ff8196; }
.analytics-coverage-track { width: 100%; height: 10px; overflow: hidden; background: #37303b; border-radius: 99px; }
.analytics-coverage-track > span { height: 100%; display: block; border-radius: inherit; background: linear-gradient(90deg, #ff345d, #faadbd); }
.analytics-coverage p { margin: 10px 0 0; font-size: 9px; line-height: 1.65; color: #9393a3; }
.analytics-sources { display: flex; flex-direction: column; gap: 8px; }
.analytics-sources > div { display: flex; align-items: center; gap: 12px; padding: 13px; border-radius: 12px; border: 1px solid #ffffff12; background: #15151d; }
.analytics-source-icon { flex: 0 0 38px; height: 38px; width: 38px; border-radius: 11px; display: grid; place-items: center; }
.analytics-source-icon.red { background: #ff355919; color: #ff718a; }
.analytics-source-icon.blue { background: #3988ff1a; color: #88b8ff; }
.analytics-sources > div > div { flex: 1; display: flex; flex-direction: column; gap: 5px; }
.analytics-sources strong { font-size: 11px; }
.analytics-sources small { font-size: 9px; color: #9999a7; }
.analytics-sources b { font-size: 17px; }
.analytics-top-gift { display: flex; align-items: center; gap: 12px; padding: 16px; border: 1px solid #e0b85c45; background: linear-gradient(105deg, #312417, #17151c); border-radius: 14px; }
.analytics-top-gift > span { width: 54px; height: 54px; display: grid; place-items: center; border-radius: 12px; background: #f0c36e16; font-size: 34px; }
.analytics-top-gift > div { display: flex; flex: 1; flex-direction: column; gap: 6px; min-width: 0; }
.analytics-top-gift strong { font-size: 12px; }
.analytics-top-gift small { font-size: 10px; color: #dabd8b; }
.analytics-top-gift b { font-size: 12px; color: #f9d38c; white-space: nowrap; }
.analytics-items { display: flex; flex-direction: column; gap: 8px; }
.analytics-item { display: flex; align-items: center; gap: 10px; background: #16161d; border: 1px solid #ffffff11; border-radius: 12px; padding: 10px; }
.analytics-item > span { flex: 0 0 36px; width: 36px; height: 36px; background: #ffffff09; border-radius: 11px; display: grid; place-items: center; font-size: 23px; }
.analytics-item > div { flex: 1; display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.analytics-item strong { font-size: 11px; }
.analytics-item small { font-size: 9px; color: #9292a0; }
.analytics-item b { color: #ff879c; font-size: 13px; }
@media (min-width: 500px) { .analytics-breakdown { flex-direction: row; gap: 27px; } .analytics-breakdown-bars { flex: 1; } }
@media (max-width: 380px) { .analytics-summary > div { padding: 10px 7px; } .analytics-summary span { font-size: 8px; } .analytics-summary strong { font-size: 10px; } .analytics-top-gift > span { width: 42px; height: 42px; font-size: 26px; } }

/* FROGI ARENA standalone site overrides and additional sections. */
:root{--bg:#090a0f;--surface:#11131b;--line:rgba(255,255,255,.095);--green:#bcfb46;--muted:#9295a3;--white:#f8f9f6;--radius:18px;color-scheme:dark}
*,*::before,*::after{box-sizing:border-box}html{scroll-behavior:smooth;min-height:100%;background:var(--bg)}body{background:var(--bg);color:var(--white);margin:0;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}button,input,select,textarea{font:inherit}button{cursor:pointer}button:disabled{opacity:.46;cursor:not-allowed}a{color:inherit}.app-shell{min-height:100vh;padding-bottom:100px;background:radial-gradient(ellipse 60% 35% at 90% -10%,rgba(132,210,44,.08),transparent),#090a0f}.main-content{max-width:1150px;margin:0 auto;padding:18px clamp(15px,4vw,38px) 34px;min-height:65vh}.topbar{position:sticky;top:0;z-index:22;max-width:none;padding:12px max(14px,calc((100vw - 1100px)/2));background:rgba(9,10,15,.91);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}.brand-button{border:0;background:transparent;color:inherit;display:flex;align-items:center;gap:9px;text-align:left}.brand-mark{display:grid;place-content:center;background:var(--green);color:#17210b;width:42px;height:42px;border-radius:13px;font-size:19px;font-weight:1000;letter-spacing:-3px}.brand-mark span{color:#639018}.brand-name{font-weight:1000;font-size:20px;letter-spacing:-1px}.brand-name>span{font-size:9px;vertical-align:top;padding-left:3px;color:var(--green)}.brand-sub{color:#979ca2;font-size:8px;letter-spacing:1.7px}.balance-pill{display:flex;align-items:center;gap:4px;border-radius:16px;padding:10px 12px;border:1px solid #353e23;background:#1a2215;color:var(--green);font-size:13px}.balance-pill span{font-size:10px}.header-actions{display:flex;align-items:center;gap:9px}.icon-button{border:1px solid var(--line);background:#181b23;color:white;border-radius:14px;width:39px;height:39px;display:grid;place-content:center;font-size:18px;position:relative}.notification-shortcut i{position:absolute;top:-3px;right:-4px;border-radius:50%;background:#e74f68;color:white;padding:2px 5px;font-size:9px;font-style:normal}.demo-banner{padding:6px 15px;text-align:center;color:#c8b681;font-size:10px;letter-spacing:.08em;background:#211b0e;border-bottom:1px solid #493a1a}.page{display:flex;flex-direction:column;gap:22px}.page-heading{padding:20px 0 3px}.page-heading h1{font-size:clamp(28px,4.5vw,45px);font-weight:1000;letter-spacing:-1.5px;line-height:1.06;margin:12px 0 10px}.page-heading p{font-size:14px;color:var(--muted);margin:0}.micro-label{display:inline-flex;align-items:center;gap:6px;font-size:10px;letter-spacing:.15em;color:var(--green);font-weight:800}.section-title{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin:3px 0 -8px}.section-title h2{font-size:clamp(18px,2.7vw,24px);margin:0;font-weight:900;letter-spacing:-.5px}.section-title span{font-size:10px;letter-spacing:.1em;color:var(--muted)}.f-btn{display:inline-flex;align-items:center;justify-content:center;text-align:center;border-radius:13px;padding:11px 19px;font-size:13px;font-weight:900;min-height:42px;transition:transform .17s,background .17s;border:1px solid transparent;white-space:nowrap}.f-btn:not(:disabled):active{transform:scale(.97)}.f-btn.primary{color:#11190c;background:var(--green);box-shadow:0 7px 25px rgba(166,255,66,.13)}.f-btn.outline{background:#1a1d24;color:#f7f8f4;border-color:#32343d}.f-btn.tiny{font-size:12px;padding:8px 12px;min-height:34px;background:#c3fb52;color:#131b0e}.f-btn.huge{width:100%;padding:16px 24px;font-size:16px}.f-btn.daily-btn{font-size:21px;min-width:50px;height:50px;padding:0;background:var(--green);color:#121909;border-radius:15px}.f-hero{min-height:350px;position:relative;overflow:hidden;border-radius:25px;background:radial-gradient(circle at 77% 38%,rgba(161,255,58,.17),transparent 32%),linear-gradient(140deg,#1f291b,#131c13 55%,#101716);border:1px solid rgba(196,255,119,.22);display:flex;align-items:center;padding:clamp(24px,5vw,57px)}.f-hero-content{position:relative;z-index:1;max-width:65%}.f-hero-content h1{font-size:clamp(29px,5.3vw,63px);line-height:1.03;font-weight:1000;letter-spacing:-2px;margin:22px 0 13px}.f-hero-content h1 span{color:var(--green)}.f-hero-content p{max-width:400px;color:#aab9a8;font-size:14px;line-height:1.6}.f-hero-deco{position:absolute;right:7%;top:16%;font-size:clamp(110px,19vw,205px);filter:drop-shadow(0 20px 40px rgba(118,255,40,.22));transform:rotate(-11deg)}.hero-actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:23px}.f-stat-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.f-stat{border:1px solid var(--line);background:#161820;border-radius:17px;padding:17px;display:flex;flex-direction:column;gap:6px}.f-stat small{color:#8d9297;font-size:10px;font-weight:700;letter-spacing:.08em}.f-stat strong{font-size:clamp(22px,3.4vw,33px);letter-spacing:-.8px;font-weight:950}.f-stat strong small{font-size:12px;color:#bdf74a}.f-stat>span{font-size:11px;color:var(--muted)}.f-daily{display:flex;align-items:center;gap:16px;background:linear-gradient(100deg,#1d2619,#20221d);padding:16px 19px;border-radius:19px;border:1px solid #36452b}.daily-pic{width:57px;height:57px;flex-shrink:0;background:#344b21;border-radius:17px;display:grid;place-content:center;font-size:22px;position:relative}.daily-pic small{font-size:11px;color:#d4ff7e;text-align:center;font-weight:900}.daily-info{flex:1}.daily-info h3{font-size:17px;margin:5px 0 4px}.daily-info p{font-size:12px;margin:0;color:#adb6a7}.quick-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.quick-tile{position:relative;text-align:left;display:flex;flex-direction:column;gap:11px;background:#161820;border:1px solid var(--line);padding:19px;border-radius:16px;min-height:118px;color:white}.quick-tile>span:first-child{font-size:27px}.quick-tile strong{font-size:14px}.quick-tile .arrow{position:absolute;top:16px;right:17px;color:#8f9b7f}.f-gift-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.f-gift{min-width:0;border:1px solid var(--line);background:#16171e;border-radius:18px;overflow:hidden;transition:transform .2s,border .2s}.f-gift:hover{transform:translateY(-3px);border-color:#758950}.gift-visual{height:150px;position:relative;display:grid;place-items:center;background:radial-gradient(circle at 50% 50%,rgba(177,243,56,.17),transparent 63%),#1b211d;overflow:hidden}.gift-visual>span{font-size:65px;z-index:1;filter:drop-shadow(0 12px 16px rgba(0,0,0,.35))}.rarity-rare .gift-visual{background:radial-gradient(circle,rgba(68,141,245,.23),transparent 62%),#1b1e2c}.rarity-epic .gift-visual{background:radial-gradient(circle,rgba(168,99,247,.29),transparent 62%),#231c2c}.rarity-legendary .gift-visual{background:radial-gradient(circle,rgba(240,185,61,.33),transparent 62%),#2c251c}.gift-body{padding:14px;display:flex;flex-direction:column;gap:9px}.badge-tag{display:inline-block;background:#242a23;border-radius:20px;padding:5px 9px;font-size:9px;font-weight:800;letter-spacing:.03em;color:#a2c67b;white-space:nowrap}.badge-tag.r-rare{background:#15243b;color:#84b8ff}.badge-tag.r-epic{background:#2c203d;color:#d1a3ff}.badge-tag.r-legendary{background:#382b16;color:#fbd17c}.badge-tag.good{color:#9aff72}.gift-body h3{font-size:15px;letter-spacing:-.3px;margin:0;min-height:20px}.gift-body p{font-size:11px;color:#95989d;line-height:1.45;margin:0;min-height:33px}.gift-footer{border-top:1px solid var(--line);padding-top:10px;display:flex;align-items:center;justify-content:space-between;gap:5px}.gift-footer strong{font-size:14px}.gift-footer strong small{font-size:10px;color:#8f9394}.f-banner{display:flex;align-items:center;gap:18px;background:linear-gradient(107deg,#22251f,#171a1c);border:1px solid var(--line);border-radius:19px;padding:24px}.f-banner>span{font-size:49px;flex-shrink:0}.f-banner h3{font-size:19px;margin:0 0 5px}.f-banner p{margin:0;font-size:12px;line-height:1.55;color:#989d9a}.f-banner>div{flex:1}.f-list{background:#16171e;border:1px solid var(--line);border-radius:18px;overflow:hidden}.list-item{display:flex;align-items:center;gap:12px;padding:13px 16px;border-bottom:1px solid var(--line)}.list-item:last-child{border-bottom:0}.list-item>div:not(.list-emoji){flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}.list-item b{font-size:13px}.list-item small{font-size:11px;color:var(--muted)}.list-item strong{font-size:12px;white-space:nowrap}.list-emoji{display:grid;place-items:center;background:#242632;border-radius:12px;width:42px;height:42px;font-size:22px;flex-shrink:0}.positive{color:#a5ec4c}.negative{color:#fd7f94}.rank{width:32px;text-align:center}.my-rank{background:rgba(190,255,79,.08)}.unread{background:rgba(190,255,79,.035)}.unread-dot{width:7px;height:7px;background:var(--green);border-radius:50%}.f-controls{display:flex;align-items:center;gap:10px}.f-input{min-width:0;width:100%;border:1px solid #343640;border-radius:12px;padding:12px 13px;background:#181a23;color:#f1f3f4;outline:0;font-size:13px}.f-input:focus{border-color:#b5f34a;box-shadow:0 0 0 2px rgba(175,243,74,.1)}.f-controls select{max-width:215px}.filter-row{display:flex;gap:8px;flex-wrap:wrap}.filter-chip{padding:9px 12px;border:1px solid #33363b;border-radius:12px;background:#181a21;color:#aeb2b8;font-size:12px;font-weight:700}.filter-chip.selected{color:#1c260b;border-color:var(--green);background:var(--green)}.profile-hero{display:flex;gap:19px;align-items:center;padding:24px;background:linear-gradient(130deg,#22311a,#161a1c);border:1px solid #34472c;border-radius:20px}.profile-avatar{width:100px;height:100px;border-radius:24px;display:grid;place-content:center;font-size:55px;background:#324b22}.profile-hero h2{font-size:25px;margin:8px 0 4px}.profile-hero p{margin:0 0 10px;color:#99ab99;font-size:12px}.f-box{padding:22px;border:1px solid var(--line);background:#181920;border-radius:18px}.f-box h3{margin:0 0 10px}.f-muted{color:var(--muted);font-size:12px;line-height:1.7}.f-between{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:12px;font-size:12px}.progress-track{height:8px;background:#2b2f32;border-radius:10px;overflow:hidden}.progress-track i{display:block;height:100%;background:linear-gradient(90deg,#7ea836,#c4f955);border-radius:10px}.tile-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.f-tile{border:1px solid var(--line);border-radius:15px;padding:16px;background:#171920;color:white;display:flex;gap:12px;align-items:center;text-align:left}.f-tile span{font-size:25px}.f-tile b{font-size:12px;flex:1}.f-tile small{font-size:15px;color:var(--green)}.f-mission{display:flex;gap:12px;align-items:center;padding:15px;border-radius:15px;background:#161920;border:1px solid var(--line)}.f-mission>div{flex:1;min-width:0}.f-mission b,.f-mission small{display:block}.f-mission b{font-size:13px}.f-mission small{font-size:11px;color:var(--muted);margin:5px 0}.mission-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.mission-pic{font-size:26px}.mission-check{font-size:13px;color:var(--green);font-weight:bold}.ach-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.ach{padding:14px;background:#161920;border:1px solid var(--line);border-radius:14px;display:flex;gap:8px;flex-direction:column;opacity:.75}.ach.complete{border-color:#3a4d28;opacity:1}.ach span{font-size:27px}.ach b{font-size:12px}.ach small{font-size:10px;color:var(--muted);line-height:1.4;flex:1}.ach em{font-size:9px;color:#b7f65a;font-style:normal}.wallet-hero{padding:35px;border:1px solid #4b6430;border-radius:24px;background:radial-gradient(circle at 95% 0,rgba(192,255,103,.19),transparent 50%),#20291d}.wallet-hero small{font-size:11px;color:#bdcfac;font-weight:800;letter-spacing:.1em}.wallet-hero h1{font-size:clamp(44px,7vw,70px);font-weight:1000;letter-spacing:-2px;margin:14px 0}.wallet-hero h1 span{font-size:22px;color:var(--green)}.wallet-hero p{color:#a1b196;font-size:12px}.chart-row{margin-bottom:20px}.chart-row>div:first-child{display:flex;justify-content:space-between;font-size:12px;margin-bottom:8px}.chart-row span{color:var(--muted)}.rarity-bar.rare i{background:#71a7fa}.rarity-bar.epic i{background:#ae79ed}.rarity-bar.legendary i{background:#e8b451}.f-news{display:flex;gap:17px;padding:20px;background:#171920;border:1px solid var(--line);border-radius:16px}.f-news>span{font-size:29px}.f-news h3{margin:0 0 8px;font-size:16px}.f-news p{font-size:13px;line-height:1.6;color:var(--muted);margin:0}.f-form{display:flex;flex-direction:column;gap:17px}.f-form label{display:flex;flex-direction:column;gap:8px;font-size:12px;font-weight:700}.f-form textarea{resize:vertical}.settings-list{margin-bottom:20px}.setting-row{display:flex;align-items:center;gap:13px;border-bottom:1px solid var(--line);padding:15px 0}.setting-row:first-child{padding-top:0}.setting-row>span{font-size:25px}.setting-row>div{flex:1;display:flex;flex-direction:column;gap:5px}.setting-row b{font-size:13px}.setting-row small{font-size:11px;color:var(--muted)}.f-toggle{appearance:none;flex-shrink:0;width:43px;height:24px;border-radius:15px;border:1px solid #505052;background:#35383d;position:relative;cursor:pointer}.f-toggle:before{content:'';position:absolute;width:18px;height:18px;border-radius:50%;background:#d9d9d9;left:2px;top:2px;transition:.2s}.f-toggle:checked{background:var(--green);border-color:var(--green)}.f-toggle:checked:before{left:21px;background:#202a0b}.f-empty{text-align:center;padding:38px 20px;color:var(--muted);background:#161820;border:1px solid var(--line);border-radius:16px;grid-column:1/-1}.f-empty>div{font-size:40px}.f-empty h3{color:white;margin:12px 0 6px}.f-empty p{font-size:12px;line-height:1.6}.f-error,.f-toast{z-index:30;max-width:1100px;margin:10px auto;padding:13px 18px;border-radius:12px;border:1px solid #a64855;background:#371921;color:#ffd2d2;font-size:12px;display:flex;align-items:center;justify-content:space-between;gap:10px}.f-error button,.f-toast button{border:none;background:transparent;color:inherit;font-size:20px}.f-toast{position:fixed;bottom:105px;left:50%;transform:translateX(-50%);border-color:#55753b;background:#1e311e;color:#ddffd0;max-width:min(95vw,470px);width:max-content;box-shadow:0 10px 50px #000a}.bottom-nav{position:fixed;bottom:0;left:0;right:0;z-index:21;display:flex;justify-content:space-around;background:rgba(18,19,24,.96);border-top:1px solid #313337;backdrop-filter:blur(18px);padding:7px max(5px,calc((100vw - 900px)/2)) calc(6px + env(safe-area-inset-bottom))}.nav-link{flex:1;border:0;background:transparent;color:#7f8589;display:flex;align-items:center;flex-direction:column;gap:3px;padding:4px 2px;max-width:130px}.nav-link span{font-size:24px;line-height:27px}.nav-link small{font-size:10px;font-weight:700}.nav-link.active{color:var(--green)}.wheel-view{padding:22px 24px 28px;text-align:center;background:radial-gradient(circle at 50% 34%,rgba(175,255,67,.13),transparent 47%),#171a1f;border:1px solid #35482e;border-radius:24px}.wheel-decor{font-size:11px;letter-spacing:.2em;color:#c0f46e}.wheel-arena{position:relative;width:min(320px,76vw);height:min(320px,76vw);margin:26px auto}.wheel-face{height:100%;width:100%;border:9px solid #30342c;border-radius:50%;background:conic-gradient(#e2ff73 0 25%,#a672f6 25% 50%,#66b5fb 50% 75%,#f4c86d 75% 100%);box-shadow:0 0 0 8px #222722,0 17px 60px #000a;transition:transform 1.85s cubic-bezier(.13,.75,.13,1)}.wheel-labels{position:absolute;inset:0}.wheel-labels span{font-size:40px;position:absolute;left:calc(50% - 22px);top:calc(50% - 22px)}.wheel-pointer{position:absolute;top:-21px;left:50%;transform:translateX(-50%);z-index:4;font-size:32px;color:var(--green);filter:drop-shadow(0 2px 3px #000)}.wheel-center{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);background:#1b2519;color:var(--green);border:5px solid #283922;border-radius:50%;width:70px;height:70px;display:grid;place-content:center;font-size:25px;font-weight:950;box-shadow:0 0 18px #0008}.wheel-bottom h2{font-size:36px;margin:9px 0 12px}.wheel-bottom h2 small{font-size:16px;color:var(--green)}.wheel-bottom{max-width:410px;margin:0 auto}.wheel-bottom p{color:var(--muted);font-size:11px;line-height:1.5}.prob-list{display:flex;flex-direction:column;background:#161820;border-radius:17px;border:1px solid var(--line)}.prob-list>div{padding:13px 17px;display:flex;align-items:center;justify-content:space-between;gap:6px;border-bottom:1px solid var(--line);font-size:12px}.prob-list>div:last-child{border-bottom:0}.prob-list>div>span:last-child{display:flex;align-items:center;gap:10px}.mines-head{display:flex;justify-content:space-between;align-items:center}.mines-grid{max-width:440px;margin:0 auto;display:grid;grid-template-columns:repeat(4,1fr);gap:9px;width:100%}.mine-cell{border:1px solid #435838;background:#202c1e;border-radius:15px;font-size:33px;aspect-ratio:1;color:#a4c57d;font-weight:900}.mine-cell.open{background:#2b4923}.mine-cell.bomb{background:#4d2025}.game-result{margin:14px 0;text-align:center;font-size:18px;font-weight:800}.duel{display:flex;align-items:center;gap:12px;justify-content:center;background:#171920;border:1px solid var(--line);border-radius:20px;padding:30px}.duel-team{display:flex;align-items:center;flex-direction:column;gap:15px;font-size:16px}.duel-team span{font-size:77px}.duel-vs{font-size:29px;font-weight:1000;color:var(--green);padding:20px}.startup-view{min-height:60vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:12px}.startup-orbit{font-size:75px;filter:drop-shadow(0 0 25px rgba(185,250,85,.24))}.startup-view h1{font-size:34px;margin:0;font-weight:1000}.startup-view p{font-size:13px;color:var(--muted);max-width:340px;line-height:1.6}
@media(max-width:850px){.f-gift-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.ach-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:600px){.topbar{padding:8px 14px}.main-content{padding:15px 14px 26px}.page{gap:18px}.f-hero{min-height:290px;padding:25px}.f-hero-content{max-width:85%}.f-hero-content h1{font-size:34px}.f-hero-content p{font-size:12px}.f-hero-deco{font-size:106px;opacity:.45;right:-15px;top:15%}.f-stat-grid{gap:7px}.f-stat{padding:11px;border-radius:12px}.f-stat small{font-size:8px}.f-stat strong{font-size:20px}.f-stat>span{font-size:9px}.quick-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.quick-tile{min-height:100px;padding:12px}.quick-tile strong{font-size:11px}.quick-tile>span:first-child{font-size:22px}.f-gift-grid{gap:9px}.gift-visual{height:113px}.gift-visual>span{font-size:49px}.gift-body{padding:11px;gap:7px}.gift-body h3{font-size:13px}.gift-body p{font-size:10px}.gift-footer strong{font-size:12px}.f-btn.tiny{padding:7px 9px}.f-daily{gap:10px;padding:12px}.daily-pic{width:46px;height:49px}.daily-info h3{font-size:13px}.daily-info p{font-size:10px}.f-btn.daily-btn{width:39px;min-width:39px;height:40px;font-size:19px}.f-controls{flex-wrap:wrap}.f-controls select{max-width:none}.mission-list{grid-template-columns:1fr}.ach-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.tile-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.f-banner{padding:17px;gap:11px}.f-banner>span{font-size:36px}.f-banner h3{font-size:15px}.f-banner p{font-size:11px}.f-banner>.f-btn{padding:8px}.f-mission{padding:10px}.profile-avatar{width:70px;height:70px;font-size:39px}.profile-hero{padding:16px}.profile-hero h2{font-size:18px}.bottom-nav{padding-bottom:calc(6px + env(safe-area-inset-bottom))}.nav-link span{font-size:22px}.nav-link small{font-size:9px}.brand-mark{width:37px;height:37px}.brand-name{font-size:18px}.brand-sub{font-size:7px}.balance-pill{padding:9px;font-size:11px}.icon-button{width:34px;height:34px}.section-title span{font-size:9px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition-duration:.01ms!important;animation-duration:.01ms!important}.wheel-face{transition-duration:.01ms!important}}html.reduce-motion *{scroll-behavior:auto!important;animation-duration:.01ms!important;transition-duration:.01ms!important}
</style></head><body><div id="app"></div><script>const NEWS_DATA=[{"id": "beta-01", "type": "update", "icon": "🚀", "title": "FROGI Arena 2.0", "text": "Каталог расширен, появились миссии, избранное и история активности.", "date": "2026-10-20"}, {"id": "beta-02", "type": "fairness", "icon": "🛡️", "title": "Важное об игре", "text": "Результат колеса выбирает сервер. В демоверсии FC не имеют денежной стоимости.", "date": "2026-10-20"}, {"id": "beta-03", "type": "tips", "icon": "💡", "title": "Совет коллекционеру", "text": "Фильтруй подарки по редкости и добавляй любимые в избранное.", "date": "2026-10-20"}];
/* FROGI Arena • self-contained Vanilla JS application. No React / build required. */
'use strict';
const T = window.Telegram?.WebApp;
try { T?.ready(); T?.expand(); T?.setHeaderColor?.('#0a0b10'); T?.setBackgroundColor?.('#090a0f'); } catch (_) {}
let D = null, SESSION = null, PAGE = 'home', BUSY = false, SPINNING = false;
let ERROR = '', TOAST = '', TIMER = null, WHEEL_ANGLE = 0, LEADERS = [], FILTER = 'all', SORT = 'price', QUERY = '', ONLY_FAV = false;
let MINES = null, PVP = null;
const NAV = [['home','⌂','Главная'],['wheel','✦','Колесо'],['market','▣','Маркет'],['inventory','🎁','Подарки'],['profile','◉','Профиль']];
const EXTRA = [['wallet','💰','Кошелёк'],['missions','🏆','Задания'],['leaderboard','🥇','Рейтинг'],['analytics','📈','Аналитика'],['mines','💣','Mines'],['pvp','⚔️','PvP'],['notifications','🔔','Уведомления'],['news','📰','Новости'],['support','💬','Поддержка'],['settings','⚙️','Настройки']];
const LABELS = Object.fromEntries([...NAV,...EXTRA].map(x=>[x[0],x[2]]));
const ROOT = document.getElementById('app');
const e = (str='') => String(str ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n=>Number(n||0).toLocaleString('ru-RU');
const date = s=>{ if(!s) return '—'; const d=new Date(String(s).replace(' ','T')+'Z');return Number.isFinite(d.getTime())?d.toLocaleDateString('ru-RU',{day:'numeric',month:'short'}):String(s).slice(0,10); };
const icon = (text,cls='')=>`<span class="i ${cls}">${text}</span>`;
const money = n=>`${fmt(n)} <small>FC</small>`;
const tag = (txt,kind='')=>`<span class="badge-tag ${kind}">${e(txt)}</span>`;
const section = (title,small='')=>`<div class="section-title"><h2>${e(title)}</h2><span>${e(small)}</span></div>`;
const btn = (text,action,css='primary',disabled=false,attrs='')=>`<button type="button" class="f-btn ${css}" data-action="${action}" ${disabled?'disabled':''} ${attrs}>${text}</button>`;
const empty = (pic,title,sub,action='',text='')=>`<div class="f-empty"><div>${pic}</div><h3>${e(title)}</h3><p>${e(sub)}</p>${action?btn(e(text),action,'outline'):''}</div>`;
const crumb = (title,sub)=>`<div class="page-heading"><span class="micro-label">FROGI ARENA / ${e(PAGE).toUpperCase()}</span><h1>${e(title)}</h1><p>${e(sub||'')}</p></div>`;
function haptic(){ if(D?.settings?.vibration)try{T?.HapticFeedback?.notificationOccurred('success')}catch(_){} }
function snack(message){TOAST=String(message);clearTimeout(TIMER);TIMER=setTimeout(()=>{TOAST=''; render();},3500);render();haptic();}
async function api(path, method='GET', body=undefined, once=false){
  const headers={'Accept':'application/json'};
  if(SESSION) headers['Authorization']='Bearer '+SESSION;
  if(body!==undefined)headers['Content-Type']='application/json';
  if(once)headers['X-Idempotency-Key']=(crypto?.randomUUID?.() || (Date.now()+Math.random()).toString().replace('.',''));
  const res=await fetch(path,{method,headers,body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});
  let obj;try{obj=await res.json()}catch(_){throw Error('Сервер временно недоступен ('+res.status+').')}
  if(!res.ok)throw Error(obj.error||'Ошибка ('+res.status+')');
  return obj;
}
async function boot(){
  ERROR=''; render();
  try{
    const initData=T?.initData||'';
    const auth=await api('/api/auth','POST',{initData,demo:!initData});
    SESSION=auth.token;D=await api('/api/me');
    PAGE=(location.hash.slice(1) in LABELS)?location.hash.slice(1):'home';
    render();
  }catch(ex){ERROR=ex.message;render();}
}
async function refresh(){ D=await api('/api/me');render(); }
async function command(path,method='POST',body,success='',once=false){
 if(BUSY)return;
 BUSY=true;ERROR='';render();
 try{let response=await api(path,method,body,once); await refresh(); if(success)snack(typeof success==='function'?success(response):success);return response;}
 catch(ex){ERROR=ex.message;render();}
 finally{BUSY=false;render();}
}
function go(page){ PAGE=LABELS[page]?page:'home'; if(PAGE==='leaderboard')loadLeaders(); location.hash=PAGE;render();scrollTo({top:0,behavior:'instant'}); }
async function loadLeaders(){try{LEADERS=(await api('/api/leaderboard')).leaders||[];render()}catch(ex){ERROR=ex.message;render()}}
function navButton([id,emoji,label]){return `<button class="nav-link ${PAGE===id?'active':''}" data-page="${id}"><span>${emoji}</span><small>${e(label)}</small></button>`;}
function shell(inner){
 const u=D?.user||{};
 return `<div class="app-shell"><header class="topbar"><button type="button" class="brand brand-button" data-page="home"><div class="brand-mark">F<span>R</span></div><div><div class="brand-name">FROGI<span>®</span></div><div class="brand-sub">COLLECT · PLAY · EXPLORE</div></div></button><div class="header-actions">${D?`<button class="balance-pill" data-page="wallet">◈ <b>${fmt(u.balance)}</b><span> FC</span></button>`:''}<button class="icon-button notification-shortcut" data-page="notifications">♧${D?.unreadCount?`<i>${D.unreadCount>9?'9+':D.unreadCount}</i>`:''}</button><button class="icon-button header-menu" data-page="menu">☰</button></div></header>
 <div class="demo-banner">◉ DEMO MODE · Виртуальные FC и подарки, без реальных денег и вывода</div>
 ${ERROR?`<div class="f-error" role="alert">⚠ ${e(ERROR)} <button data-action="clear-error">×</button></div>`:''}
 ${TOAST?`<div class="f-toast" role="status">✓ ${e(TOAST)} <button data-action="clear-toast">×</button></div>`:''}
 <main class="main-content" id="main-content">${inner}</main>
 <nav class="bottom-nav" aria-label="Основная навигация">${NAV.map(navButton).join('')}</nav></div>`;
}
function giftCard(g,buyMode=true){return `<article class="f-gift rarity-${e(g.rarity).toLowerCase()}"><div class="gift-visual"><span>${e(g.emoji)}</span><div class="gift-glow"></div></div><div class="gift-body">${tag(g.rarity,'r-'+g.rarity.toLowerCase())}<h3>${e(g.title)}</h3><p>${e(g.description)}</p><div class="gift-footer"><strong>${money(g.price)}</strong>${buyMode?btn('Купить','buy','tiny',BUSY||D.user.balance<g.price,`data-id="${e(g.id)}"`):btn(g.favorite?'♥':'♡','favorite','tiny',BUSY,`data-id="${e(g.inventoryId)}" data-favorite="${g.favorite?'1':'0'}"`)}</div></div></article>`;}
function quickActions(){let items=[['wheel','🎡','Колесо'],['market','🛍️','Маркет'],['inventory','🎁','Коллекция'],['missions','🏆','Миссии'],['leaderboard','🥇','Рейтинг'],['analytics','📈','Аналитика']];return `<div class="quick-grid">${items.map(([id,emoji,label])=>`<button class="quick-tile" data-page="${id}"><span>${emoji}</span><strong>${label}</strong><span class="arrow">↗</span></button>`).join('')}</div>`;}
function dailyCard(){let claimed=D.daily.claimed;return `<section class="f-daily"><div class="daily-pic">📅<small>+${D.daily.reward}</small></div><div class="daily-info"><span class="micro-label">DAILY DROP · ${e(date(D.daily.today))}</span><h3>${claimed?'Награда получена':'Ежедневный бонус'}</h3><p>${claimed?'Возвращайся завтра за новым бонусом.':'Каждый день тестовые FC — бесплатно.'}</p></div>${btn(claimed?'✓':'→','daily','daily-btn',claimed||BUSY)}</section>`;}
function home(){let s=D.progress.stats; let collection=D.inventory;return `<div class="page enter">${`<section class="f-hero"><div class="f-hero-deco">🐸</div><div class="f-hero-content"><span class="micro-label"><i class="live-dot"></i> FROGI ARENA / SEASON 01</span><h1>ТВОЯ АРЕНА.<br><span>ТВОИ ПРАВИЛА.</span></h1><p>Собирай цифровые подарки, открывай достижения и поднимайся в рейтинге.</p><div class="hero-actions">${btn('✦ Играть','go-wheel','primary')} ${btn('Маркет ↗','go-market','outline')}</div></div></section>`}
 <div class="f-stat-grid"><div class="f-stat"><small>БАЛАНС</small><strong>${money(D.user.balance)}</strong><span>Виртуальные кредиты</span></div><div class="f-stat"><small>КОЛЛЕКЦИЯ</small><strong>${fmt(collection.length)}</strong><span>Цифровых подарков</span></div><div class="f-stat"><small>УРОВЕНЬ</small><strong>${fmt(D.progress.level.level)}</strong><span>${D.progress.level.progressPercent}% до следующего</span></div></div>
 ${dailyCard()} ${section('Открой мир FROGI','РАЗДЕЛЫ')} ${quickActions()}
 ${section('Популярные предметы','МАРКЕТ · ДЕМО')}<div class="f-gift-grid">${D.gifts.slice(0,4).map(g=>giftCard(g)).join('')}</div>
 <div class="f-banner"><span>🎡</span><div><h3>Испытай удачу</h3><p>Сервер случайно выбирает приз. В каждом раунде — виртуальный подарок.</p></div>${btn('Вращать','go-wheel','tiny')}</div></div>`;}
function wheel(){let total=D.probabilities.reduce((a,v)=>a+v.weight,0);let entries=D.probabilities.map((p,i)=>({p,g:D.gifts.find(g=>g.id===p.giftId),i}));return `<div class="page enter">${crumb('Колесо удачи','Каждое вращение приносит один коллекционный предмет.')}
 <div class="wheel-view"><div class="wheel-decor">✦ FAIR PLAY ✦</div><div class="wheel-arena"><div class="wheel-pointer">▼</div><div class="wheel-face ${SPINNING?'wheel-spinning':''}" id="wheel-face" style="transform:rotate(${WHEEL_ANGLE}deg)"><div class="wheel-labels">${entries.map(({g,i})=>`<span style="transform:rotate(${i*90+45}deg) translateY(-105px) rotate(${-i*90-45}deg)">${e(g?.emoji||'🎁')}</span>`).join('')}</div></div><div class="wheel-center">FR</div></div>
 <div class="wheel-bottom"><span class="micro-label">ЦЕНА РАУНДА</span><h2>${money(D.spinCost)}</h2>${btn(SPINNING?'Крутим колесо...':'🎡 Вращать колесо','spin','primary huge',BUSY||SPINNING||D.user.balance<D.spinCost)}<p>Только демо-валюта. В каждом раунде выдаётся виртуальный подарок.</p></div></div>
 ${section('Шансы выпадения','ПРОВЕРЯЕМЫЕ ВЕРОЯТНОСТИ')}<div class="prob-list">${entries.map(({p,g})=>`<div><span>${e(g?.emoji)} <b>${e(g?.title)}</b></span><span>${tag(g?.rarity||'','r-'+String(g?.rarity||'').toLowerCase())} <strong>${p.probabilityPercent}%</strong></span></div>`).join('')}</div>
 ${section('Последние вращения',`${D.spins.length} ЗАПИСЕЙ`)}<div class="f-list">${D.spins.slice(0,6).map(s=>`<div class="list-item"><span class="list-emoji">${e(s.emoji)}</span><div><b>${e(s.title)}</b><small>${e(date(s.createdAt))}</small></div><span class="negative">-${fmt(s.cost)} FC</span></div>`).join('')||empty('🎡','Ещё нет вращений','Попробуй колесо!')}</div></div>`;}
function market(){let all=D.gifts.filter(g=>(FILTER==='all'||g.rarity===FILTER)&&(g.title+' '+g.description).toLowerCase().includes(QUERY.toLowerCase()));all.sort((a,b)=>SORT==='expensive'?b.price-a.price:SORT==='name'?a.title.localeCompare(b.title):a.price-b.price);return `<div class="page enter">${crumb('Маркет подарков','Виртуальные предметы для твоей коллекции.')}
 <div class="f-banner market-banner"><span>🛍️</span><div><h3>FROGI MARKET</h3><p>${D.gifts.length} уникальных предметов · реальные платежи не используются</p></div></div>
 <div class="f-controls"><input id="search" class="f-input" type="search" placeholder="Поиск предметов..." value="${e(QUERY)}" maxlength="60"><select id="sort" class="f-input"><option value="price" ${SORT==='price'?'selected':''}>Сначала доступные</option><option value="expensive" ${SORT==='expensive'?'selected':''}>Сначала дорогие</option><option value="name" ${SORT==='name'?'selected':''}>По названию</option></select></div>
 <div class="filter-row">${[['all','Все'],['Common','Common'],['Rare','Rare'],['Epic','Epic'],['Legendary','Legendary']].map(([id,label])=>`<button class="filter-chip ${FILTER===id?'selected':''}" data-filter="${id}">${label}</button>`).join('')}</div>
 ${section('Каталог',`${all.length} ПРЕДМЕТОВ`)}${all.length?`<div class="f-gift-grid">${all.map(g=>giftCard(g)).join('')}</div>`:empty('🔎','Ничего не найдено','Измени фильтры или строку поиска.','reset-filters','Сбросить фильтры')}</div>`;}
function inventory(){let items=D.inventory.filter(g=>(FILTER==='all'||g.rarity===FILTER)&&(!ONLY_FAV||g.favorite)&&(g.title.toLowerCase().includes(QUERY.toLowerCase())));return `<div class="page enter">${crumb('Моя коллекция','Каждая находка хранится в твоём инвентаре.')}
 <div class="f-banner"><span>🎁</span><div><h3>Твои сокровища</h3><p>${D.inventory.length} предметов · ${D.inventory.filter(g=>g.favorite).length} в избранном</p></div></div>
 <div class="f-controls"><input id="search" class="f-input" type="search" placeholder="Поиск в коллекции..." value="${e(QUERY)}"><button class="filter-chip ${ONLY_FAV?'selected':''}" data-action="toggle-favs">♥ Избранное</button></div>
 <div class="filter-row">${[['all','Все'],['Common','Common'],['Rare','Rare'],['Epic','Epic'],['Legendary','Legendary']].map(([id,label])=>`<button class="filter-chip ${FILTER===id?'selected':''}" data-filter="${id}">${label}</button>`).join('')}</div>
 ${items.length?`<div class="f-gift-grid">${items.map(g=>giftCard(g,false)).join('')}</div>`:empty('🎁','Здесь пока пусто','Получи подарок в колесе или купи в маркете.','go-market','В маркет')}</div>`;}
function profile(){let u=D.user,p=D.progress,stats=p.stats;return `<div class="page enter">${crumb('Личный кабинет','Твои достижения, прогресс и настройки.')}
 <div class="profile-hero"><div class="profile-avatar">🐸</div><div><span class="micro-label">COLLECTOR PROFILE</span><h2>${e(u.username?'@'+u.username:u.firstName)}</h2><p>Игрок FROGI с ${e(date(u.createdAt))}</p>${tag(`Уровень ${p.level.level}`,'level')}</div></div>
 <div class="f-stat-grid"><div class="f-stat"><small>ОЧКИ</small><strong>${fmt(stats.totalScore)}</strong><span>PTS</span></div><div class="f-stat"><small>ВРАЩЕНИЯ</small><strong>${fmt(stats.spins)}</strong><span>Всего</span></div><div class="f-stat"><small>ДОСТИЖЕНИЯ</small><strong>${p.unlockedCount}/${p.achievements.length}</strong><span>Открыто</span></div></div>
 <div class="f-box"><div class="f-between"><b>✨ Опыт коллекционера</b><span>${p.level.currentXp} / ${p.level.requiredXp} XP</span></div><div class="progress-track"><i style="width:${p.level.progressPercent}%"></i></div></div>
 ${section('Активировать промокод')}<div class="f-box"><div class="f-controls"><input id="promo" class="f-input" placeholder="Введите промокод" maxlength="40">${btn('Активировать','promo','primary',BUSY)}</div><p class="f-muted">Промокоды используются один раз на аккаунт.</p></div>
 ${section('Разделы аккаунта')}<div class="tile-grid">${EXTRA.filter(x=>!['mines','pvp','news'].includes(x[0])).map(([id,pic,t])=>`<button class="f-tile" data-page="${id}"><span>${pic}</span><b>${t}</b><small>↗</small></button>`).join('')}</div></div>`;}
function missions(){let p=D.progress;return `<div class="page enter">${crumb('Задания и награды','Развивай свой аккаунт и собирай достижения.')}${dailyCard()}
 <div class="f-box"><div class="f-between"><h3>⭐ Уровень ${p.level.level}</h3><strong>${p.level.progressPercent}%</strong></div><div class="progress-track"><i style="width:${p.level.progressPercent}%"></i></div><p class="f-muted">${p.level.currentXp} / ${p.level.requiredXp} XP</p></div>
 ${section('Миссии',`${p.completedMissions} / ${p.missions.length}`)}<div class="mission-list">${p.missions.map(m=>`<div class="f-mission"><span class="mission-pic">${e(m.icon)}</span><div><b>${e(m.title)}</b><small>${e(m.description)}</small><div class="progress-track"><i style="width:${m.current/m.target*100}%"></i></div><small>${m.current} / ${m.target}</small></div><span class="mission-check">${m.completed?'✓':Math.round(m.current/m.target*100)+'%'}</span></div>`).join('')}</div>
 ${section('Достижения',`${p.unlockedCount} / ${p.achievements.length}`)}<div class="ach-grid">${p.achievements.map(a=>`<div class="ach ${a.unlocked?'complete':''}"><span>${a.unlocked?e(a.icon):'🔒'}</span><b>${e(a.title)}</b><small>${e(a.details)}</small><em>${a.unlocked?'✓ Получено':a.current+'/'+a.threshold}</em></div>`).join('')}</div></div>`;}
function leaderboard(){return `<div class="page enter">${crumb('Рейтинг игроков','Лучшие коллекционеры арены.')}
 <div class="f-banner"><span>🏆</span><div><h3>Зал славы FROGI</h3><p>Баллы начисляются за коллекцию, вращения, покупки и ежедневные бонусы.</p></div></div>
 ${section('Топ игроков',`${LEADERS.length} УЧАСТНИКОВ`)}<div class="f-list">${LEADERS.map(l=>`<div class="list-item ${l.id===D.user.id?'my-rank':''}"><b class="rank">${l.rank<=3?['🥇','🥈','🥉'][l.rank-1]:'#'+l.rank}</b><span class="list-emoji">🐸</span><div><b>${e(l.name)}${l.id===D.user.id?' (ты)':''}</b><small>${l.items} предметов · ${l.spins} вращений</small></div><strong>${fmt(l.score)} PTS</strong></div>`).join('')||empty('🏆','Пока никого','Стань первым в рейтинге!')}</div></div>`;}
function wallet(){return `<div class="page enter">${crumb('Баланс и история','Вся активность тестовых FC.')}
 <div class="wallet-hero"><small>ТЕКУЩИЙ БАЛАНС</small><h1>${fmt(D.user.balance)} <span>FC</span></h1><p>Только демо-кредиты · не обмениваются на деньги</p></div>${dailyCard()}
 ${section('История операций',`${D.ledger.length} ПОСЛЕДНИХ`)}<div class="f-list">${D.ledger.map(l=>`<div class="list-item"><span class="list-emoji">${l.delta>=0?'💚':'💳'}</span><div><b>${e(({demo_spin:'Вращение колеса',demo_market_purchase:'Покупка подарка',daily_reward:'Ежедневная награда',promo_reward:'Промокод'})[l.reason]||l.reason)}</b><small>${e(date(l.createdAt))}</small></div><strong class="${l.delta>=0?'positive':'negative'}">${l.delta>=0?'+':''}${fmt(l.delta)} FC</strong></div>`).join('')||empty('🧾','История пуста','Первые операции появятся после игры.')}</div></div>`;}
function analytics(){let n=D.inventory.length,tot=n?D.inventory.reduce((a,g)=>a+g.price,0):0;let groups=['Common','Rare','Epic','Legendary'];return `<div class="page enter">${crumb('Аналитика коллекции','Твои находки в цифрах и графиках.')}
 <div class="f-stat-grid"><div class="f-stat"><small>ПРЕДМЕТЫ</small><strong>${n}</strong><span>Всего</span></div><div class="f-stat"><small>УНИКАЛЬНЫХ</small><strong>${D.progress.stats.uniqueGifts}</strong><span>Разных видов</span></div><div class="f-stat"><small>КАТАЛОЖНАЯ СТОИМОСТЬ</small><strong>${fmt(tot)}</strong><span>Тестовых FC</span></div></div>
 ${section('Распределение редкостей')}<div class="f-box">${groups.map(r=>{let c=D.inventory.filter(g=>g.rarity===r).length;return `<div class="chart-row"><div><b>${r}</b><span>${c} (${n?Math.round(c/n*100):0}%)</span></div><div class="progress-track rarity-bar ${r.toLowerCase()}"><i style="width:${n?c/n*100:0}%"></i></div></div>`;}).join('')}</div>
 ${section('Твои самые редкие находки')}<div class="f-gift-grid">${[...D.inventory].sort((a,b)=>(['Common','Rare','Epic','Legendary'].indexOf(b.rarity)-['Common','Rare','Epic','Legendary'].indexOf(a.rarity))).slice(0,4).map(g=>giftCard(g,false)).join('')||empty('📈','Нет данных','Собери подарки для аналитики.')}</div></div>`;}
function notes(){return `<div class="page enter">${crumb('Уведомления','Новости аккаунта, награды и достижения.')}
 ${D.notifications.filter(n=>!n.readAt).length?btn('Отметить всё прочитанным','read-all','outline',BUSY):''}
 <div class="f-list">${D.notifications.map(n=>`<div class="list-item ${!n.readAt?'unread':''}"><span class="list-emoji">${n.kind==='achievement'?'🏅':'🎁'}</span><div><b>${e(n.title)}</b><small>${e(n.message)}</small><small>${e(date(n.createdAt))}</small></div>${!n.readAt?'<span class="unread-dot"></span>':''}</div>`).join('')||empty('🔔','Пока нет уведомлений','Все события появятся здесь.')}</div></div>`;}
function news(){return `<div class="page enter">${crumb('Новости FROGI','Обновления и полезные рекомендации.')}
 <div class="f-list">${[{title:'FROGI Arena в одном bot.py',text:'Теперь бот, Mini App и API работают из одного Python-файла, без Node.js.',icon:'🚀'}, ...NEWS_DATA].map(n=>`<div class="f-news"><span>${e(n.icon)}</span><div><h3>${e(n.title)}</h3><p>${e(n.text)}</p></div></div>`).join('')}</div></div>`;}
function support(){return `<div class="page enter">${crumb('Служба поддержки','Напиши нам об ошибке, идее или вопросе.')}
 <form id="support-form" class="f-box f-form"><label>Категория<select class="f-input" name="category"><option value="question">Вопрос</option><option value="bug">Ошибка</option><option value="idea">Идея</option><option value="account">Аккаунт</option><option value="other">Другое</option></select></label><label>Тема<input class="f-input" name="subject" minlength="5" maxlength="100" placeholder="Краткая тема" required></label><label>Сообщение<textarea class="f-input" name="body" rows="5" minlength="10" maxlength="2000" placeholder="Опиши ситуацию подробнее" required></textarea></label><button class="f-btn primary" type="submit" ${BUSY?'disabled':''}>Отправить обращение →</button></form>
 ${section('Мои обращения',`${D.tickets.length}`)}<div class="f-list">${D.tickets.map(t=>`<div class="list-item"><span class="list-emoji">💬</span><div><b>${e(t.subject)}</b><small>${e(t.id)} · ${date(t.createdAt)}</small></div>${tag(t.status==='resolved'?'Решено':'Открыто',t.status==='resolved'?'good':'')}</div>`).join('')||empty('💬','Обращений нет','Создай первое обращение выше.')}</div></div>`;}
function settings(){let s=D.settings;return `<div class="page enter">${crumb('Настройки','Персонализируй свой интерфейс и профиль.')}
 <div class="f-box"><div class="settings-list">${[['sound','🔊','Звуки','Звуковые подсказки'],['vibration','📳','Вибрация','Отклик через Telegram'],['reduceMotion','🌙','Меньше анимации','Более спокойный интерфейс'],['showOnLeaderboard','🏆','Показывать в рейтинге','Имя и очки видны игрокам']].map(([k,p,t,d])=>`<label class="setting-row"><span>${p}</span><div><b>${t}</b><small>${d}</small></div><input class="f-toggle" data-setting="${k}" type="checkbox" ${s[k]?'checked':''}></label>`).join('')}</div>${btn('Сохранить настройки','save-settings','primary',BUSY)}</div><p class="f-muted">Настройки сохраняются на сервере для твоего Telegram-аккаунта.</p></div>`;}
function initMines(){const all=Array.from({length:16},(_,i)=>i);for(let i=all.length-1;i>0;i--){let j=Math.floor(Math.random()*(i+1));[all[i],all[j]]=[all[j],all[i]]} MINES={bombs:new Set(all.slice(0,3)),open:new Set(),over:false,win:false};}
function mines(){if(!MINES)initMines();return `<div class="page enter">${crumb('Mines','Демо-головоломка — без ставок и призов.')}
 <div class="f-banner"><span>💣</span><div><h3>Найди безопасные клетки</h3><p>На поле 4×4 спрятаны 3 мины. Угадай остальные 13 клеток.</p></div></div>
 <div class="mines-head"><strong>Открыто: ${MINES.open.size}/13</strong>${btn('Новая игра','new-mines','outline')}</div><div class="mines-grid">${Array.from({length:16},(_,i)=>`<button class="mine-cell ${MINES.open.has(i)?'open':''} ${MINES.over&&MINES.bombs.has(i)?'bomb':''}" data-mine="${i}" ${MINES.open.has(i)||MINES.over?'disabled':''}>${MINES.open.has(i)?'💎':MINES.over&&MINES.bombs.has(i)?'💣':'?'}</button>`).join('')}</div>${MINES.over?`<div class="f-box game-result">${MINES.win?'🏆 Победа!':'💥 Мина!'} Попробуй ещё раз.</div>`:''}</div>`;}
function pvp(){return `<div class="page enter">${crumb('PvP — тренировочный режим','Пример дуэли. Реальный мультиплеер не подключён.')}
 <div class="f-banner"><span>⚔️</span><div><h3>FROGI DUEL</h3><p>Выбери сторону и сыграй против случайного виртуального соперника.</p></div></div>
 <div class="duel"><div class="duel-team"><span>🐸</span><b>ТЫ</b></div><div class="duel-vs">VS</div><div class="duel-team"><span>🤖</span><b>БОТ</b></div></div><div class="f-box"><h3>Выбери сторону монеты</h3><div class="hero-actions">${btn('☀ Орёл','pvp-head','primary')} ${btn('🌙 Решка','pvp-tail','outline')}</div>${PVP?`<div class="game-result">${e(PVP)}</div>`:''}<p class="f-muted">Это демонстрация интерфейса, ставки FC не принимаются.</p></div></div>`;}
function directory(){return `<div class="page enter">${crumb('Все разделы','Выбирай занятие в мире FROGI.')}
 <div class="tile-grid">${[...NAV,...EXTRA].map(([id,pic,t])=>`<button class="f-tile" data-page="${id}"><span>${pic}</span><b>${t}</b><small>↗</small></button>`).join('')}</div><p class="f-muted">FROGI Arena 2.2 · Python all-in-one · демоверсия</p></div>`;}
function render(){
 document.documentElement.classList.toggle('reduce-motion',Boolean(D?.settings?.reduceMotion));
 let body='';
 if(!D){body=`<div class="startup-view"><div class="startup-orbit">🐸</div><h1>FROGI ARENA</h1><p>${ERROR?e(ERROR):'Вход через Telegram Mini App...'}</p>${btn('↻ Повторить вход','retry','primary')}</div>`;}
 else {let page={home,wheel,market,inventory,profile,missions,leaderboard,wallet,analytics,notifications:notes,news,support,settings,mines,pvp,menu:directory}[PAGE]||home;body=page();}
 ROOT.innerHTML=shell(body);
}
function sound(){if(!D?.settings?.sound)return;try{const ctx=new(window.AudioContext||window.webkitAudioContext)();let o=ctx.createOscillator(),g=ctx.createGain();o.type='sine';o.frequency.value=740;g.gain.value=.028;o.connect(g);g.connect(ctx.destination);o.start();o.stop(ctx.currentTime+.085);o.onended=()=>ctx.close()}catch(_){}}
async function performSpin(){
 if(BUSY||SPINNING)return;SPINNING=true;BUSY=true;ERROR='';render();
 try{
  let data=await api('/api/spin','POST',{},true);
  let i=D.probabilities.findIndex(x=>x.giftId===data.prize.id);
  WHEEL_ANGLE+=1800 + (360-((i+0.5)*90)%360) - (WHEEL_ANGLE%360);
  let face=document.getElementById('wheel-face');if(face)face.style.transform=`rotate(${WHEEL_ANGLE}deg)`;
  await new Promise(resolve=>setTimeout(resolve,D.settings.reduceMotion?120:1850));
  D=await api('/api/me');sound();snack(`🎉 ${data.prize.emoji} ${data.prize.title} добавлен в коллекцию!`);
 }catch(ex){ERROR=ex.message;}finally{SPINNING=false;BUSY=false;render();}
}
async function actions(action,el){
 if(action.startsWith('go-')){go(action.slice(3));return;}
 if(action==='clear-error'){ERROR='';render();return;}
 if(action==='clear-toast'){TOAST='';render();return;}
 if(action==='retry'){await boot();return;}
 if(action==='spin'){await performSpin();return;}
 if(action==='daily'){await command('/api/daily/claim','POST',{},r=>`+${r.reward} FC · ежедневный бонус!`);return;}
 if(action==='buy'){let gift=D.gifts.find(g=>g.id===el.dataset.id);if(!gift)return;if(!confirm(`Купить ${gift.title} за ${gift.price} тестовых FC?`))return;await command('/api/market/buy','POST',{giftId:gift.id},r=>r.gift.emoji+' '+r.gift.title+' куплен!',true);return;}
 if(action==='favorite'){let fav=el.dataset.favorite==='1';await command(`/api/inventory/${encodeURIComponent(el.dataset.id)}/favorite`,'PATCH',{favorite:!fav},fav?'Удалено из избранного':'Добавлено в избранное');return;}
 if(action==='promo'){let code=document.getElementById('promo')?.value||'';await command('/api/promo/redeem','POST',{code},r=>`Промокод: +${r.reward} FC`);return;}
 if(action==='toggle-favs'){ONLY_FAV=!ONLY_FAV;render();return;}
 if(action==='reset-filters'){FILTER='all';QUERY='';ONLY_FAV=false;render();return;}
 if(action==='read-all'){await command('/api/notifications/read','POST',{ids:D.notifications.filter(n=>!n.readAt).map(n=>n.id)},'Уведомления прочитаны');return;}
 if(action==='save-settings'){let s={};for(let key of ['sound','vibration','reduceMotion','showOnLeaderboard'])s[key]=Boolean(document.querySelector(`[data-setting="${key}"]`)?.checked);await command('/api/settings','PATCH',s,'Настройки сохранены');return;}
 if(action==='new-mines'){initMines();render();return;}
 if(action.startsWith('pvp-')){let me=action==='pvp-head'?'Орёл':'Решка';let result=Math.random()<.5?'Орёл':'Решка';PVP=`${result===me?'🏆 Победа!':'🤖 Победил соперник.'} Выпало: ${result}`;render();return;}
}
ROOT.addEventListener('click',async ev=>{
 let page=ev.target.closest('[data-page]');if(page){go(page.dataset.page);return;}
 let filter=ev.target.closest('[data-filter]');if(filter){FILTER=filter.dataset.filter;render();return;}
 let mine=ev.target.closest('[data-mine]');if(mine&&MINES&&!MINES.over){let id=Number(mine.dataset.mine);if(MINES.bombs.has(id)){MINES.over=true;MINES.win=false;}
 else{MINES.open.add(id);if(MINES.open.size>=13){MINES.over=true;MINES.win=true;}}render();return;}
 let target=ev.target.closest('[data-action]');if(target)await actions(target.dataset.action,target);
});
ROOT.addEventListener('input',ev=>{if(ev.target.id==='search'){QUERY=ev.target.value;let start=ev.target.selectionStart;let shouldFocus=true;render();let input=document.getElementById('search');if(shouldFocus&&input){input.focus();input.setSelectionRange(start,start);}}});
ROOT.addEventListener('change',ev=>{if(ev.target.id==='sort'){SORT=ev.target.value;render();}});
ROOT.addEventListener('submit',async ev=>{if(ev.target.id!=='support-form')return;ev.preventDefault();const f=new FormData(ev.target);await command('/api/support/tickets','POST',{category:f.get('category'),subject:f.get('subject'),body:f.get('body')},'Обращение отправлено');});
window.addEventListener('hashchange',()=>{let page=location.hash.slice(1);if(page!==PAGE&&LABELS[page]){PAGE=page;if(page==='leaderboard')loadLeaders();render();}});
render();boot();
</script></body></html>'''

_REQUEST_BUCKETS = {}
_BUCKET_LOCK = threading.Lock()


def allow_request(key, rate):
    clock = time.monotonic()
    with _BUCKET_LOCK:
        hits, expiry = _REQUEST_BUCKETS.get(key, (0, 0))
        if expiry < clock:
            hits, expiry = 0, clock + 60
        hits += 1
        _REQUEST_BUCKETS[key] = (hits, expiry)
        if len(_REQUEST_BUCKETS) > 4096:
            for key2, (_, t) in list(_REQUEST_BUCKETS.items()):
                if t < clock: del _REQUEST_BUCKETS[key2]
        return hits <= rate


class FrogiHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'FROGI/2.2'
    sys_version = ''
    def log_message(self, fmt, *args):
        # Do not leak private hashed Telegram webhook URL into logs.
        if self.path.startswith('/tg/'):
            LOG.info('Telegram webhook request handled')
        else:
            LOG.info('%s %s', self.command, self.path[:140])

    def common_headers(self, mime, count, code=200):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(count))
        self.send_header('Cache-Control', 'no-store' if ('application/json' in mime) else 'public, max-age=120')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        self.send_header('Content-Security-Policy', "default-src 'none'; img-src 'self' data:; connect-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' data: https://fonts.gstatic.com; base-uri 'none'; form-action 'self'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org")
        self.end_headers()

    def reply(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.common_headers('application/json; charset=utf-8', len(data), code)
        self.wfile.write(data)

    def read_json(self):
        content_type = self.headers.get('Content-Type', '').split(';')[0].lower()
        if content_type != 'application/json':
            fail('CONTENT_TYPE', 'Ожидается application/json.', 415)
        size = self.headers.get('Content-Length')
        if not size or not size.isdigit() or int(size) > 32768:
            fail('BAD_REQUEST', 'Слишком большой или пустой запрос.', 413)
        content = self.rfile.read(int(size))
        try:
            obj = json.loads(content)
            if not isinstance(obj, dict):
                fail('BAD_REQUEST', 'Ожидается JSON-объект.')
            return obj
        except (ValueError, UnicodeError):
            fail('BAD_JSON', 'Некорректный JSON.')

    def handle_any(self):
        try:
            url = urlsplit(self.path)
            path = url.path
            if self.command == 'GET' and path in ('/health', '/healthz'):
                return self.reply({'ok': True, 'service': 'FROGI Arena', 'botConfigured': bool(TOKEN),
                                   'webhookMode': bool(PUBLIC_URL)})
            if self.command == 'GET' and path in ('/', '/app'):
                doc = HTML.encode('utf-8')
                self.common_headers('text/html; charset=utf-8', len(doc))
                self.wfile.write(doc)
                return
            if self.command == 'GET' and path == '/favicon.ico':
                self.send_response(204)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.command == 'POST' and path == WEBHOOK_PATH and TOKEN and PUBLIC_URL:
                provided = self.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
                if not hmac.compare_digest(provided, WEBHOOK_SECRET):
                    fail('FORBIDDEN', 'Forbidden', 403)
                data = self.read_json()
                process_update(data)
                return self.reply({'ok': True})
            if path.startswith('/api/'):
                origin = self.headers.get('Origin')
                # CORS not required: Mini App and its API share same origin.
                if origin:
                    expected = PUBLIC_URL if PUBLIC_URL else 'http://localhost:'+str(PORT)
                    local_alt = 'http://127.0.0.1:'+str(PORT)
                    if origin not in (expected,local_alt):
                        fail('FORBIDDEN', 'Чужой источник запроса.', 403)
                key = self.client_address[0] + (':login' if path=='/api/auth' else ':api')
                if not allow_request(key, 30 if path=='/api/auth' else 350):
                    fail('RATE_LIMIT', 'Слишком много запросов. Повтори позже.', 429)
                data = self.read_json() if self.command in ('POST','PATCH') else {}
                headers = {k.lower():v for k,v in self.headers.items()}
                q = dict()
                for k,v in parse_qsl(url.query,keep_blank_values=True): q.setdefault(k,[]).append(v)
                with transaction() as c:
                    result = api_dispatch(c,self.command,path,data,headers,q)
                return self.reply(result, 201 if path=='/api/support/tickets' and self.command=='POST' else 200)
            return self.reply({'error':'Не найдено.','code':'NOT_FOUND'},404)
        except AppError as ex:
            return self.reply({'error':str(ex),'code':ex.code},ex.status)
        except (sqlite3.Error, OSError) as ex:
            LOG.exception('Storage or server error')
            return self.reply({'error':'Временная ошибка сервера.','code':'SERVER_ERROR'},503)
        except Exception:
            LOG.exception('Unexpected request error')
            return self.reply({'error':'Внутренняя ошибка сервера.','code':'INTERNAL_ERROR'},500)

    def do_GET(self): self.handle_any()
    def do_POST(self): self.handle_any()
    def do_PATCH(self): self.handle_any()
    def do_HEAD(self):
        if self.path in ('/health','/'):
            self.common_headers('text/plain; charset=utf-8', 0)
        else: self.send_error(404)


def main():
    if os.getenv('RENDER') == 'true' and not TOKEN:
        LOG.error('Set BOT_TOKEN in Render Environment: Telegram bot will not work without it.')
    init_db()
    server = ThreadingHTTPServer(('0.0.0.0', PORT), FrogiHandler)
    server.daemon_threads = True
    LOG.info('FROGI ready: http://0.0.0.0:%d • Mini App + API + Telegram', PORT)
    if os.getenv('RENDER') == 'true' and not DB_PATH.startswith('/var/data/'):
        LOG.warning('Render local SQLite database is EPHEMERAL! Set persistent disk and DATABASE_PATH for durable data.')
    threading.Thread(target=setup_telegram, name='frogi-telegram-init', daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        LOG.info('FROGI server stopped')


# ---------------------------------------------------------------------------
# Built-in deployment guide. Print with: python bot.py --guide
# Everything lives in this one source file: no README/requirements files needed.
# ---------------------------------------------------------------------------
FROGI_GUIDE = r"""
══════════════════════════════════════════════════════════════════════════
                 FROGI ARENA — ONE-FILE DEPLOYMENT GUIDE
══════════════════════════════════════════════════════════════════════════

VERSION: 2.2 Python All-in-One • Required files: exactly one, bot.py.
Runtime: Python 3.10 or newer (3.11/3.12/3.13 recommended).
External Python dependencies: ZERO.

1. CREATE TELEGRAM BOT
----------------------
  1) Open Telegram and search for the official @BotFather account.
  2) Send /newbot and follow the instructions.
  3) Keep the token private. It is a password for the Telegram bot.
  4) Do not paste the token in GitHub, code comments, or screenshots.
  5) At deployment you will paste it in Render's secret environment setting.

2. CREATE GITHUB REPOSITORY
----------------------------
  1) Sign in to github.com and select New repository.
  2) Give it a name such as frogi-arena-bot.
  3) Create repository (private is fine).
  4) Upload bot.py as the ONLY code file in the root directory.
  5) Commit the change to the default branch (main).

3. DEPLOY ON RENDER
-------------------
  1) Navigate to dashboard.render.com.
  2) New -> Web Service -> Build and deploy from a Git repository.
  3) Connect the repository containing bot.py.
  4) Configure:

      Language:       Python 3
      Root directory: (leave empty)
      Build command:  echo "No dependencies to install"
      Start command:  python bot.py
      Health Check:   /health
      Instances:      1 (do not scale SQLite bot horizontally)

  5) Environment -> Add Environment Variable:

      BOT_TOKEN      = 123456789:YOUR_ACTUAL_SECRET_BOT_TOKEN

  6) OPTIONAL additional environment values:

      ADMIN_IDS      = 111111111,222222222
      ADMIN_TOKEN    = a_secret_of_at_least_16_characters
      DATABASE_PATH  = /var/data/frogi.sqlite3
      PUBLIC_URL     = https://custom-domain.example
      PORT           = 10000 (Render supplies this automatically)
      LOG_LEVEL      = INFO

  7) Click Deploy. Watch runtime logs for:

      "FROGI ready: ..."
      "Telegram bot connected: @..."
      "Telegram webhook configured on public HTTPS URL"

  8) Open your Telegram bot, send /start and tap "Открыть FROGI Arena".

4. WHY ONLY ONE FILE?
---------------------
  - The Python standard library includes an HTTP server and SQLite.
  - Server, database schema, Telegram Bot API, web pages and JavaScript
    are all in this file. No npm, Flask, FastAPI, or pip requirements.
  - The file builds database tables on first launch automatically.
  - The database itself is a runtime state file. It is not source code and
    should NOT be uploaded to a public GitHub repository.
  - Render automatically provides RENDER_EXTERNAL_URL and PORT. The code uses
    those to configure Telegram's webhook without an extra worker.

5. EXTREMELY IMPORTANT: SQLITE AND RENDER FREE
----------------------------------------------
  This is a demonstration project. Render Free web services have ephemeral
  filesystems. All local SQLite balances, settings and collections may be
  irreversibly lost during idle spin-down, deploy or restart.

  For persistent SQLite:
    a) Upgrade the Render Web Service to a PAID plan with persistent disks.
    b) Attach a persistent disk mounted at /var/data.
    c) Set DATABASE_PATH=/var/data/frogi.sqlite3.
    d) Keep service at ONE instance (no horizontal scaling).
    e) Take regular independent backups.

  Other production options:
    - Move database operations to a managed Postgres database (would require
      changing this one-file implementation and adding a DB driver), or
    - Use a hosted service offering persistent files.

  Paid Render is NOT required to test the bot's interface, but it is needed
  for durable local SQLite there. Free is for testing only.

6. BOT COMMANDS
---------------
  /start           Get the main menu.
  /app             Get the Mini App launch button.
  /balance         View demo balance.
  /daily           Claim daily demo reward once per UTC day.
  /inventory       List saved gifts.
  /market          Show catalog with IDs and prices.
  /buy gift_id     Buy a gift (e.g. /buy frog_common).
  /wheel           Read wheel rules, open a spin button.
  /spin            Pay 100 demo FC and receive one gift.
  /profile         Stats, level, achievements.
  /top             Leaderboard.
  /promo CODE      Activate a promotional code.
  /help            Print available bot commands.
  /admin           Limited stats for authorized Telegram ADMIN_IDS.

7. MINI APP PAGES
-----------------
  Home             Hero section, balance, daily claims, quick navigation.
  Wheel            Weighted random gifts with published chances.
  Market           16 items, rarity filters, search and sorting.
  Inventory        Owned gifts, favorites, rarity filters.
  Profile          Level, achievements, profile navigation, promo codes.
  Wallet           Balance and ledger of changes.
  Missions         Tracked mission progress, unlocked achievements.
  Leaderboard      Public opted-in players, sorted by score.
  Analytics        Rarity distribution and collection breakdown.
  Mines            No-stakes 4×4 puzzle (3 mines, no FC awards).
  PvP              UI training mode against a simulated opponent only.
  Notifications    Reward and achievement events, read/unread tracking.
  Support          Support forms persisted in SQLite.
  Settings         Sound, haptics, reduced-motion and leaderboard privacy.
  News             Product tips and change notices.

8. IMPORTANT SECURITY PROPERTIES
--------------------------------
  - Never grant credits based on browser-supplied user ID. All mutations
    use an authenticated server session obtained from Telegram signed data.
  - Check Telegram initData HMAC and age on login. Refresh expired Mini App
    sessions by reopening the app via the bot.
  - Bot updates are authenticated by Telegram webhook secret token header.
  - Raw browser tokens are never persisted, only SHA-256 digests.
  - Balance mutations and reward uniqueness run inside SQLite transactions.
  - Idempotency keys prevent duplicate purchases when retrying the same
    request with the same key. Different keys represent different actions.
  - Telegram update IDs are deduplicated before side effects.
  - The API's admin endpoints require ADMIN_TOKEN (stored only on the server).
  - Use TLS (provided by Render); never use production HTTP for Mini Apps.
  - Do not hardcode credentials, and rotate tokens if they leak.
  - Test all modifications before allowing real users to play.

9. DEMO CURRENCY AND PRIZES
----------------------------
  - Initial balance on first sign-in: 1000 FC.
  - Daily reward: 75 FC per UTC day.
  - Wheel cost: 100 FC per spin.
  - Wheel probabilities:
       Frog Pixel    65.0%
       Red Crystal   25.0%
       Blue Orbit     8.5%
       Golden Frog    1.5%
  - Promotional codes for testing (once per user):
       FROGI2026     250 FC
       HELLOFROGI    100 FC
       ARENASTART    150 FC
  - This is not real-money gambling. Gifts are not on-chain NFTs.
  - No cryptocurrency wallet, cash-out, real payments or deposits included.

10. LOCAL DEBUGGING
-------------------
  Start without a Telegram bot token, to test the Mini App design:

      ALLOW_DEMO_AUTH=true python bot.py

  By default the local site opens at http://127.0.0.1:10000.
  For another port:

      PORT=8080 ALLOW_DEMO_AUTH=true python bot.py

  For a real Telegram bot using local long polling (without HTTPS Mini App):

      BOT_TOKEN='your_token' python bot.py

  For complete Mini App integration, deploy on Render and open through
  Telegram. Telegram initData is available in its WebApp client, not in an
  ordinary desktop browser URL.

  Run isolated built-in tests without installing dependencies:

      python bot.py --selftest

  The tests use temporary files. They do not alter your normal SQLite DB.

11. MAINTENANCE / LIMITATIONS
-----------------------------
  - This project uses one process, one SQLite file, an in-memory rate
    limiter and the Python ThreadingHTTPServer. Suitable for prototypes
    and small hobby apps; do not assume it is production-hardened.
  - Default Render Free services can sleep; Telegram sometimes retries
    webhook requests while an instance is waking up.
  - Admin actions should be reviewed and audited before exposing to staff.
  - Mines and PvP are mini games, NOT paid wagering or live multiplayer.
  - No guarantee is made that a deployment is error-free on all Telegram
    versions and host configurations. Test on your chosen platform.
  - Do not increase service instances while using one SQLite database.
  - User data is deleted when the underlying storage is deleted.

12. QUICK TROUBLESHOOTING
-------------------------
  "Cannot start": check Python 3.10+ and the file location at repository root.
  "Login failed": open through Telegram bot, not just a normal website.
  "BOT_TOKEN is not set": add BOT_TOKEN in Render -> Environment.
  "No app button": wait for webhook setup, redeploy and send /start.
  "Unsupported URL": keep PUBLIC_URL valid HTTPS with no URL query string.
  "Database is locked": use only one service instance, check disk and writes.
  "Lost balances": local SQLite on Render Free is not persistent.
  "Bot doesn't answer": check Render logs and Telegram token in BotFather.
  "Health check fails": ensure start command exactly `python bot.py`.
  "Unexpected HTML": API paths must start with /api/ and use JSON.

Enjoy building FROGI Arena 🐸
"""


def selftest():
    """Comprehensive offline smoke/unit suite. No network, pip or Telegram token."""
    import io
    import tempfile
    import unittest
    from concurrent.futures import ThreadPoolExecutor

    class FrogiTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            global DB_PATH, TOKEN, DEMO_AUTH
            cls.temp = tempfile.TemporaryDirectory(prefix='frogi_selftest_')
            cls.old_db, cls.old_token, cls.old_demo = DB_PATH, TOKEN, DEMO_AUTH
            DB_PATH = os.path.join(cls.temp.name, 'test.sqlite3')
            TOKEN = '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
            DEMO_AUTH = True
            init_db()

        @classmethod
        def tearDownClass(cls):
            global DB_PATH, TOKEN, DEMO_AUTH
            DB_PATH, TOKEN, DEMO_AUTH = cls.old_db, cls.old_token, cls.old_demo
            cls.temp.cleanup()

        def test_01_database_schema_and_seed(self):
            with readonly() as c:
                self.assertEqual(count(c, 'SELECT COUNT(*) FROM gifts'), 16)
                self.assertTrue(row(c, 'SELECT * FROM gifts WHERE id=?', ('frog_common',)))

        def test_02_catalog_gift_prices(self):
            with readonly() as c:
                self.assertEqual(gift_row(c, 'frog_common')['price'], 120)
                self.assertEqual(gift_row(c, 'gold_frog')['price'], 1800)

        def test_03_user_created_once(self):
            with transaction() as c:
                first = provision(c, 101, 'first_username', 'First User')
                second = provision(c, 101, 'new_username', 'Second User')
            self.assertEqual(first['id'], second['id'])
            self.assertEqual(second['username'], 'new_username')
            self.assertEqual(first['balance'], 1000)

        def test_04_cannot_impersonate_user(self):
            with transaction() as c:
                with self.assertRaises(AppError) as error:
                    auth_session(c, '', False)
            self.assertEqual(error.exception.status, 401)

        def test_05_local_demo_mode(self):
            with transaction() as c:
                result = auth_session(c, '', True)
            self.assertEqual(result['user']['id'], 'demo_local_user')
            self.assertTrue(result['demoMode'])
            self.assertTrue(result['token'])

        def test_06_session_accept_reject(self):
            with transaction() as c:
                result = auth_session(c, '', True)
                self.assertEqual(authenticated_user(c, 'Bearer '+result['token'])['id'], 'demo_local_user')
                with self.assertRaises(AppError):
                    authenticated_user(c, 'Bearer invalid')

        def test_07_logout_revokes_session(self):
            with transaction() as c:
                result = auth_session(c, '', True)
                api_dispatch(c, 'POST', '/api/logout', {}, {'authorization':'Bearer '+result['token']}, {})
                with self.assertRaises(AppError):
                    authenticated_user(c, 'Bearer '+result['token'])

        def test_08_initdata_hmac_verification(self):
            from urllib.parse import urlencode
            identity={'id':999,'first_name':'Valid','username':'signed'}
            fields={'auth_date':str(now_seconds()),'query_id':'AAEAgAAA','user':json.dumps(identity,separators=(',',':'))}
            dcs='\n'.join(k+'='+v for k,v in sorted(fields.items()))
            key=hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
            fields['hash']=hmac.new(key,dcs.encode(),hashlib.sha256).hexdigest()
            signed=urlencode(fields)
            self.assertEqual(verify_init_data(signed)['id'],999)
            self.assertIsNone(verify_init_data(signed+'&hash=00'))
            self.assertIsNone(verify_init_data(signed.replace('Valid','Hacked')))

        def test_09_old_initdata_rejected(self):
            from urllib.parse import urlencode
            fields={'auth_date':str(now_seconds()-90000),'user':'{"id":999}'}
            data='\n'.join(k+'='+v for k,v in sorted(fields.items()))
            key=hmac.new(b'WebAppData', TOKEN.encode(),hashlib.sha256).digest()
            fields['hash']=hmac.new(key,data.encode(),hashlib.sha256).hexdigest()
            self.assertIsNone(verify_init_data(urlencode(fields)))

        def test_10_prize_probabilities(self):
            self.assertEqual(sum(w for _,w in PRIZE_WEIGHTS),10000)
            self.assertEqual({p for p,w in PRIZE_WEIGHTS}, {'frog_common','red_crystal','blue_orbit','gold_frog'})

        def test_11_spin_deducts_once(self):
            with transaction() as c:
                user=provision(c,201,'wheel','Wheel User')
                first=spin(c,user['id'],'test-wheel-key-201')
                second=spin(c,user['id'],'test-wheel-key-201')
                self.assertTrue(second['replayed'])
                self.assertEqual(first['inventoryId'],second['inventoryId'])
                self.assertEqual(user_balance(c,user['id'])['balance'],900)
                self.assertEqual(count(c,'SELECT COUNT(*) FROM spins WHERE user_id=?',(user['id'],)),1)

        def test_12_market_purchase_once(self):
            with transaction() as c:
                user=provision(c,202,'market','Market User')
                first=buy(c,user['id'],'frog_common','test-buy-key-202')
                second=buy(c,user['id'],'frog_common','test-buy-key-202')
                self.assertEqual(first['inventoryId'],second['inventoryId'])
                self.assertEqual(user_balance(c,user['id'])['balance'],880)

        def test_13_insufficient_balance_rolls_back(self):
            with transaction() as c:
                user=provision(c,203,'poor','Poor User')
                with self.assertRaises(AppError):
                    buy(c,user['id'],'royal_frog')
                self.assertEqual(user_balance(c,user['id'])['balance'],1000)
                self.assertEqual(count(c,'SELECT COUNT(*) FROM inventory WHERE user_id=?',(user['id'],)),0)

        def test_14_daily_claim_once(self):
            with transaction() as c:
                user=provision(c,204)
                daily(c,user['id'])
                with self.assertRaises(AppError):
                    daily(c,user['id'])
                self.assertEqual(user_balance(c,user['id'])['balance'],1075)

        def test_15_promo_claim_once(self):
            with transaction() as c:
                user=provision(c,205)
                first=redeem(c,user['id'],'  frogI2026  ')
                self.assertEqual(first['reward'],250)
                with self.assertRaises(AppError):
                    redeem(c,user['id'],'FROGI2026')
                self.assertEqual(user_balance(c,user['id'])['balance'],1250)

        def test_16_unknown_promo_rejected(self):
            with transaction() as c:
                user=provision(c,206)
                with self.assertRaises(AppError):
                    redeem(c,user['id'],'MAGIC_DOES_NOT_EXIST')

        def test_17_progress_achievements(self):
            with readonly() as c:
                p=progress(c,'tg_201')
                self.assertTrue(any(i['id']=='first_spin' and i['unlocked'] for i in p['achievements']))
                self.assertGreaterEqual(p['stats']['collection'],1)

        def test_18_no_direct_inventory_access(self):
            with transaction() as c:
                user=provision(c,207)
                item=row(c,'SELECT id FROM inventory WHERE user_id=?',('tg_201',))['id']
                with self.assertRaises(AppError) as err:
                    api_dispatch(c,'PATCH',f'/api/inventory/{item}/favorite',{'favorite':True},
                                 {'authorization':'Bearer '+auth_session_for_test(c,user['id'])},{})
                self.assertEqual(err.exception.status,404)

        def test_19_settings_all_boolean(self):
            with transaction() as c:
                user=provision(c,208)
                token=auth_session_for_test(c,user['id'])
                with self.assertRaises(AppError):
                    api_dispatch(c,'PATCH','/api/settings',{'sound':'false','vibration':False,'reduceMotion':False,'showOnLeaderboard':True},
                                 {'authorization':'Bearer '+token},{})
                result=api_dispatch(c,'PATCH','/api/settings',{'sound':False,'vibration':True,'reduceMotion':True,'showOnLeaderboard':False},
                                 {'authorization':'Bearer '+token},{})
                self.assertEqual(result['settings']['reduceMotion'],1)

        def test_20_leaderboard_optout(self):
            with readonly() as c:
                self.assertNotIn('tg_208', [x['id'] for x in leaders(c)])

        def test_21_support_validation(self):
            with transaction() as c:
                user=provision(c,209)
                token=auth_session_for_test(c,user['id'])
                with self.assertRaises(AppError):
                    api_dispatch(c,'POST','/api/support/tickets',{'category':'bug','subject':'x','body':'short'},
                                 {'authorization':'Bearer '+token},{})
                res=api_dispatch(c,'POST','/api/support/tickets',{'category':'bug','subject':'Page broken','body':'This page does not render correctly.'},
                                 {'authorization':'Bearer '+token},{})
                self.assertTrue(res['ticket']['id'].startswith('FR-'))

        def test_22_db_transactions_concurrent_spin(self):
            with transaction() as c:
                user=provision(c,210)
                c.execute('UPDATE users SET balance=2000 WHERE id=?',(user['id'],))
            def worker(i):
                with transaction() as c:
                    return spin(c,'tg_210',f'concurrent-spin-{i:03d}')
            with ThreadPoolExecutor(max_workers=5) as pool:
                result=list(pool.map(worker,range(8)))
            self.assertEqual(len({x['inventoryId'] for x in result}),8)
            with readonly() as c:
                self.assertEqual(user_balance(c,'tg_210')['balance'],1200)

        def test_23_api_not_found(self):
            with transaction() as c:
                t=auth_session(c,'',True)['token']
                with self.assertRaises(AppError) as er:
                    api_dispatch(c,'GET','/api/wrong',{}, {'authorization':'Bearer '+t},{})
                self.assertEqual(er.exception.status,404)

        def test_24_mission_threshold_math(self):
            level=level_data(79)
            self.assertEqual(level['level'],1)
            self.assertEqual(level_data(80)['level'],2)
            self.assertEqual(level_data(320)['level'],3)

        def test_25_html_app_embedded(self):
            for label in ['FROGI ARENA','wheel-face','support-form','/api/auth','/api/me','Telegram']:
                self.assertIn(label,HTML)
            self.assertNotIn('<script src="/assets/',HTML)
            self.assertGreater(len(HTML),60000)

        def test_26_webhook_secret_not_plain_token(self):
            self.assertNotIn('BOT_TOKEN=', HTML)
            self.assertNotIn('WEBHOOK_SECRET=', HTML)
            self.assertTrue(WEBHOOK_PATH.startswith('/tg/'))

    def auth_session_for_test(c, uid):
        t=secrets.token_urlsafe(32)
        c.execute('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',
                  (hashlib.sha256(t.encode()).hexdigest(),uid,now_seconds()+3600))
        return t

    suite=unittest.TestLoader().loadTestsFromTestCase(FrogiTests)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ('--help', '-h', '--guide', 'guide'):
        print(FROGI_GUIDE)
    elif len(sys.argv) > 1 and sys.argv[1] == '--selftest':
        raise SystemExit(0 if selftest() else 1)
    else:
        main()
