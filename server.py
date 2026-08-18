#!/usr/bin/env python3
"""Sprout backend: accounts, sessions, and budget data over a small JSON API.

Stdlib only (sqlite3 + http.server) so it runs with no installs.
Run:  python3 server.py
Open: http://127.0.0.1:8765
"""
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'sprout.db'))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
SESSION_COOKIE = 'sprout_session'
SESSION_DAYS = 30
PORT = int(os.environ.get('PORT', 8765))
# 0.0.0.0 so it's reachable from your phone over the LAN or a Tailscale
# network, not just this Mac. Set HOST=127.0.0.1 to go back to local-only.
HOST = os.environ.get('HOST', '0.0.0.0')
# Set COOKIE_SECURE=1 only when served over real HTTPS (e.g. behind a
# hosting provider's TLS proxy) — browsers drop Secure cookies over plain
# HTTP, which would break local/Tailscale use, so it defaults off.
COOKIE_SECURE = os.environ.get('COOKIE_SECURE', '') == '1'
MAX_BODY = 1_000_000

USERNAME_RE = re.compile(r'^[A-Za-z0-9_.-]{3,32}$')
GROUPS = ('needs', 'wants', 'savings')
DEFAULT_CATEGORIES = {
    'needs': ['Housing', 'Groceries', 'Utilities', 'Transport', 'Insurance',
              'Debt Payments', 'Healthcare', 'Childcare', 'Phone & Internet'],
    'wants': ['Dining', 'Shopping', 'Subscriptions', 'Entertainment', 'Travel',
              'Hobbies', 'Gifts', 'Personal Care', 'Fitness'],
    'savings': ['Emergency Fund', 'Investing', 'Extra Debt Payment', 'Retirement', 'Education Fund'],
}


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL,
        pw_salt TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        onboarding_done INTEGER NOT NULL DEFAULT 0,
        theme TEXT,
        income REAL NOT NULL DEFAULT 4000,
        needs_limit REAL NOT NULL DEFAULT 2000,
        wants_limit REAL NOT NULL DEFAULT 1200,
        savings_limit REAL NOT NULL DEFAULT 800
    );
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        cat TEXT NOT NULL,
        grp TEXT NOT NULL,
        amt REAL NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        y INTEGER NOT NULL, m INTEGER NOT NULL, d INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        emoji TEXT NOT NULL,
        name TEXT NOT NULL,
        saved REAL NOT NULL DEFAULT 0,
        target REAL NOT NULL,
        date TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS custom_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        grp TEXT NOT NULL,
        name TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        grp TEXT NOT NULL,
        amt REAL NOT NULL,
        day INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    );
    ''')
    # Lightweight migration for databases created before this column existed.
    try:
        conn.execute('ALTER TABLE transactions ADD COLUMN source_subscription_id INTEGER')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def hash_password(password, salt_hex=None):
    if salt_hex is None:
        salt_hex = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt_hex), 200_000)
    return digest.hex(), salt_hex


def verify_password(password, salt_hex, expected_hash):
    digest, _ = hash_password(password, salt_hex)
    return hmac.compare_digest(digest, expected_hash)


class ApiError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message


def require_str(body, key, min_len=1, max_len=200, default=None):
    v = body.get(key, default)
    if v is None:
        raise ApiError(400, f'{key} is required')
    if not isinstance(v, str):
        raise ApiError(400, f'{key} must be a string')
    v = v.strip()
    if len(v) < min_len or len(v) > max_len:
        raise ApiError(400, f'{key} must be {min_len}-{max_len} characters')
    return v


def require_num(body, key, min_val=None):
    v = body.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ApiError(400, f'{key} must be a number')
    v = float(v)
    if min_val is not None and v < min_val:
        raise ApiError(400, f'{key} must be >= {min_val}')
    return v


class Handler(BaseHTTPRequestHandler):
    server_version = 'Sprout/1.0'

    # ---------- low-level helpers ----------
    def _send_json(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        for h, v in (extra_headers or []):
            self.send_header(h, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError(413, 'request body too large')
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode('utf-8'))
        except Exception:
            raise ApiError(400, 'invalid JSON body')
        if not isinstance(data, dict):
            raise ApiError(400, 'expected a JSON object')
        return data

    def _cookie(self, name):
        raw = self.headers.get('Cookie')
        if not raw:
            return None
        c = SimpleCookie()
        try:
            c.load(raw)
        except Exception:
            return None
        return c[name].value if name in c else None

    def _session_headers(self, token):
        secure = '; Secure' if COOKIE_SECURE else ''
        return [('Set-Cookie', f'{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax{secure}; Max-Age={SESSION_DAYS * 86400}')]

    def _clear_session_headers(self):
        secure = '; Secure' if COOKIE_SECURE else ''
        return [('Set-Cookie', f'{SESSION_COOKIE}=deleted; Path=/; HttpOnly; SameSite=Lax{secure}; Max-Age=0')]

    def _current_user(self, conn):
        token = self._cookie(SESSION_COOKIE)
        if not token:
            return None
        row = conn.execute(
            'SELECT s.user_id AS id, s.expires_at AS expires_at, u.username AS username '
            'FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?',
            (token,)
        ).fetchone()
        if not row:
            return None
        if row['expires_at'] < iso(now_utc()):
            conn.execute('DELETE FROM sessions WHERE token = ?', (token,))
            conn.commit()
            return None
        return {'id': row['id'], 'username': row['username']}

    def log_message(self, fmt, *args):
        print('[sprout]', self.address_string(), fmt % args)

    # ---------- routing ----------
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == '/healthz':
                return self._send_json(200, {'ok': True})
            if path == '/api/me':
                return self.api_me()
            if path == '/api/export':
                return self.api_export()
            return self.serve_static(path)
        except ApiError as e:
            self._send_json(e.code, {'error': e.message})
        except Exception as e:
            self._send_json(500, {'error': f'server error: {e}'})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            m = re.match(r'^/api/goal/(\d+)/contribute$', path)
            if m:
                return self.api_goal_contribute(int(m.group(1)))
            routes = {
                '/api/register': self.api_register,
                '/api/login': self.api_login,
                '/api/logout': self.api_logout,
                '/api/expense': self.api_add_expense,
                '/api/income': self.api_add_income,
                '/api/goal': self.api_add_goal,
                '/api/category': self.api_add_category,
                '/api/subscription': self.api_add_subscription,
                '/api/password': self.api_change_password,
                '/api/import': self.api_import,
            }
            fn = routes.get(path)
            if not fn:
                return self._send_json(404, {'error': 'not found'})
            fn()
        except ApiError as e:
            self._send_json(e.code, {'error': e.message})
        except Exception as e:
            self._send_json(500, {'error': f'server error: {e}'})

    def do_PUT(self):
        path = urlparse(self.path).path
        try:
            if path == '/api/settings':
                return self.api_update_settings()
            self._send_json(404, {'error': 'not found'})
        except ApiError as e:
            self._send_json(e.code, {'error': e.message})
        except Exception as e:
            self._send_json(500, {'error': f'server error: {e}'})

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            m = re.match(r'^/api/goal/(\d+)$', path)
            if m:
                return self.api_delete_goal(int(m.group(1)))
            m = re.match(r'^/api/category/(\d+)$', path)
            if m:
                return self.api_delete_category(int(m.group(1)))
            m = re.match(r'^/api/subscription/(\d+)$', path)
            if m:
                return self.api_delete_subscription(int(m.group(1)))
            if path == '/api/account':
                return self.api_delete_account()
            self._send_json(404, {'error': 'not found'})
        except ApiError as e:
            self._send_json(e.code, {'error': e.message})
        except Exception as e:
            self._send_json(500, {'error': f'server error: {e}'})

    # ---------- static files ----------
    def serve_static(self, path):
        if path == '/':
            path = '/index.html'
        safe = os.path.normpath(unquote(path)).lstrip('/')
        full = os.path.join(STATIC_DIR, safe)
        if not os.path.abspath(full).startswith(os.path.abspath(STATIC_DIR)):
            return self._send_json(403, {'error': 'forbidden'})
        if not os.path.isfile(full):
            return self._send_json(404, {'error': 'not found'})
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        with open(full, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- auth endpoints ----------
    def api_register(self):
        body = self._read_json()
        username = require_str(body, 'username', 3, 32)
        password = require_str(body, 'password', 8, 200)
        if not USERNAME_RE.match(username):
            raise ApiError(400, 'username may only use letters, numbers, "_", "." and "-"')
        conn = get_db()
        try:
            existing = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
            if existing:
                raise ApiError(409, 'that username is taken')
            pw_hash, pw_salt = hash_password(password)
            cur = conn.execute(
                'INSERT INTO users (username, pw_hash, pw_salt, created_at) VALUES (?, ?, ?, ?)',
                (username, pw_hash, pw_salt, iso(now_utc()))
            )
            user_id = cur.lastrowid
            conn.execute('INSERT INTO settings (user_id) VALUES (?)', (user_id,))
            token = secrets.token_urlsafe(32)
            expires = iso(now_utc() + timedelta(days=SESSION_DAYS))
            conn.execute('INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)',
                         (token, user_id, iso(now_utc()), expires))
            conn.commit()
            self._send_json(200, {'ok': True, 'username': username}, self._session_headers(token))
        finally:
            conn.close()

    def api_login(self):
        body = self._read_json()
        username = require_str(body, 'username', 1, 32)
        password = require_str(body, 'password', 1, 200)
        conn = get_db()
        try:
            row = conn.execute('SELECT id, pw_hash, pw_salt FROM users WHERE username = ?', (username,)).fetchone()
            if not row or not verify_password(password, row['pw_salt'], row['pw_hash']):
                raise ApiError(401, 'incorrect username or password')
            token = secrets.token_urlsafe(32)
            expires = iso(now_utc() + timedelta(days=SESSION_DAYS))
            conn.execute('INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)',
                         (token, row['id'], iso(now_utc()), expires))
            conn.commit()
            self._send_json(200, {'ok': True, 'username': username}, self._session_headers(token))
        finally:
            conn.close()

    def api_logout(self):
        token = self._cookie(SESSION_COOKIE)
        conn = get_db()
        try:
            if token:
                conn.execute('DELETE FROM sessions WHERE token = ?', (token,))
                conn.commit()
            self._send_json(200, {'ok': True}, self._clear_session_headers())
        finally:
            conn.close()

    def api_change_password(self):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            body = self._read_json()
            current = require_str(body, 'current_password', 1, 200)
            new = require_str(body, 'new_password', 8, 200)
            row = conn.execute('SELECT pw_hash, pw_salt FROM users WHERE id = ?', (user['id'],)).fetchone()
            if not verify_password(current, row['pw_salt'], row['pw_hash']):
                raise ApiError(401, 'current password is incorrect')
            pw_hash, pw_salt = hash_password(new)
            conn.execute('UPDATE users SET pw_hash = ?, pw_salt = ? WHERE id = ?', (pw_hash, pw_salt, user['id']))
            conn.commit()
            self._send_json(200, {'ok': True})
        finally:
            conn.close()

    def api_delete_account(self):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            body = self._read_json()
            password = require_str(body, 'password', 1, 200)
            row = conn.execute('SELECT pw_hash, pw_salt FROM users WHERE id = ?', (user['id'],)).fetchone()
            if not verify_password(password, row['pw_salt'], row['pw_hash']):
                raise ApiError(401, 'password is incorrect')
            conn.execute('DELETE FROM users WHERE id = ?', (user['id'],))
            conn.commit()
            self._send_json(200, {'ok': True}, self._clear_session_headers())
        finally:
            conn.close()

    # ---------- data endpoints ----------
    def api_me(self):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            self._send_json(200, self._full_state(conn, user))
        finally:
            conn.close()

    def api_export(self):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            state = self._full_state(conn, user)
            state['exported_at'] = iso(now_utc())
            self._send_json(200, state)
        finally:
            conn.close()

    def _ensure_subscriptions_billed(self, conn, user_id):
        today = datetime.now()
        y, m = today.year, today.month - 1
        last_day = (datetime(today.year + (1 if today.month == 12 else 0),
                              1 if today.month == 12 else today.month + 1, 1) - timedelta(days=1)).day
        subs = conn.execute('SELECT * FROM subscriptions WHERE user_id = ? AND active = 1', (user_id,)).fetchall()
        for sub in subs:
            already = conn.execute(
                'SELECT 1 FROM transactions WHERE user_id = ? AND source_subscription_id = ? AND y = ? AND m = ?',
                (user_id, sub['id'], y, m)
            ).fetchone()
            if already:
                continue
            day = min(sub['day'], last_day)
            conn.execute(
                'INSERT INTO transactions (user_id, cat, grp, amt, note, y, m, d, source_subscription_id) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (user_id, sub['name'], sub['grp'], sub['amt'], 'Recurring subscription', y, m, day, sub['id'])
            )
        if subs:
            conn.commit()

    def _full_state(self, conn, user):
        self._ensure_subscriptions_billed(conn, user['id'])
        s = conn.execute('SELECT * FROM settings WHERE user_id = ?', (user['id'],)).fetchone()
        txs = conn.execute('SELECT id, cat, grp AS "group", amt, note, y, m, d FROM transactions '
                            'WHERE user_id = ? ORDER BY id', (user['id'],)).fetchall()
        goals = conn.execute('SELECT id, emoji, name, saved, target, date FROM goals '
                              'WHERE user_id = ? ORDER BY id', (user['id'],)).fetchall()
        cats = conn.execute('SELECT id, grp AS "group", name FROM custom_categories '
                             'WHERE user_id = ? ORDER BY id', (user['id'],)).fetchall()
        subs = conn.execute('SELECT id, name, grp AS "group", amt, day FROM subscriptions '
                             'WHERE user_id = ? AND active = 1 ORDER BY day, id', (user['id'],)).fetchall()
        return {
            'username': user['username'],
            'settings': {
                'onboarding_done': bool(s['onboarding_done']),
                'theme': s['theme'],
                'income': s['income'],
                'needs_limit': s['needs_limit'],
                'wants_limit': s['wants_limit'],
                'savings_limit': s['savings_limit'],
            },
            'transactions': [dict(r) for r in txs],
            'goals': [dict(r) for r in goals],
            'default_categories': DEFAULT_CATEGORIES,
            'custom_categories': [dict(r) for r in cats],
            'subscriptions': [dict(r) for r in subs],
        }

    def api_update_settings(self):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            body = self._read_json()
            fields, params = [], []
            if 'onboarding_done' in body:
                fields.append('onboarding_done = ?')
                params.append(1 if body['onboarding_done'] else 0)
            if 'theme' in body:
                theme = body['theme']
                if theme not in (None, 'light', 'dark'):
                    raise ApiError(400, 'theme must be light, dark, or null')
                fields.append('theme = ?')
                params.append(theme)
            if 'income' in body:
                fields.append('income = ?')
                params.append(require_num(body, 'income', 0))
            if 'needs_limit' in body:
                fields.append('needs_limit = ?')
                params.append(require_num(body, 'needs_limit', 0))
            if 'wants_limit' in body:
                fields.append('wants_limit = ?')
                params.append(require_num(body, 'wants_limit', 0))
            if 'savings_limit' in body:
                fields.append('savings_limit = ?')
                params.append(require_num(body, 'savings_limit', 0))
            if fields:
                params.append(user['id'])
                conn.execute(f'UPDATE settings SET {", ".join(fields)} WHERE user_id = ?', params)
                conn.commit()
            self._send_json(200, self._full_state(conn, user))
        finally:
            conn.close()

    def api_import(self):
        """One-time helper: seed a brand-new account with previously-known Sprout data."""
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            s = conn.execute('SELECT onboarding_done FROM settings WHERE user_id = ?', (user['id'],)).fetchone()
            if s['onboarding_done']:
                raise ApiError(409, 'account already has data; import is only for brand-new accounts')
            body = self._read_json()
            income = require_num(body, 'income', 0)
            theme = body.get('theme')
            if theme not in (None, 'light', 'dark'):
                theme = None
            groups = body.get('groups') or {}
            needs = float(groups.get('needs', {}).get('limit', income * 0.5))
            wants = float(groups.get('wants', {}).get('limit', income * 0.3))
            savings = float(groups.get('savings', {}).get('limit', income * 0.2))
            conn.execute(
                'UPDATE settings SET onboarding_done = 1, theme = ?, income = ?, needs_limit = ?, '
                'wants_limit = ?, savings_limit = ? WHERE user_id = ?',
                (theme, income, needs, wants, savings, user['id'])
            )
            for g in (body.get('goals') or [])[:20]:
                name = str(g.get('name', 'Goal'))[:60]
                emoji = str(g.get('emoji', '🎯'))[:8]
                target = float(g.get('target', 0) or 0)
                saved = float(g.get('saved', 0) or 0)
                date = str(g.get('date', 'Ongoing'))[:40]
                if target > 0:
                    conn.execute('INSERT INTO goals (user_id, emoji, name, saved, target, date) VALUES (?,?,?,?,?,?)',
                                 (user['id'], emoji, name, saved, target, date))
            for t in (body.get('transactions') or [])[:500]:
                grp = t.get('group')
                if grp not in GROUPS:
                    continue
                amt = float(t.get('amt', 0) or 0)
                if amt <= 0:
                    continue
                conn.execute(
                    'INSERT INTO transactions (user_id, cat, grp, amt, note, y, m, d) VALUES (?,?,?,?,?,?,?,?)',
                    (user['id'], str(t.get('cat', 'Other'))[:60], grp, amt, str(t.get('note', ''))[:200],
                     int(t.get('y')), int(t.get('m')), int(t.get('d')))
                )
            conn.commit()
            self._send_json(200, self._full_state(conn, user))
        finally:
            conn.close()

    def api_add_expense(self):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            body = self._read_json()
            cat = require_str(body, 'cat', 1, 60)
            grp = require_str(body, 'group', 1, 20)
            if grp not in GROUPS:
                raise ApiError(400, 'group must be needs, wants, or savings')
            amt = require_num(body, 'amt', 0.01)
            note = body.get('note') or ''
            if not isinstance(note, str):
                note = ''
            note = note.strip()[:200]
            today = datetime.now()
            conn.execute(
                'INSERT INTO transactions (user_id, cat, grp, amt, note, y, m, d) VALUES (?,?,?,?,?,?,?,?)',
                (user['id'], cat, grp, amt, note, today.year, today.month - 1, today.day)
            )
            conn.commit()
            self._send_json(200, self._full_state(conn, user))
        finally:
            conn.close()

    def api_add_income(self):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            body = self._read_json()
            cat = require_str(body, 'cat', 1, 60)
            amt = require_num(body, 'amt', 0.01)
            note = body.get('note') or ''
            if not isinstance(note, str):
                note = ''
            note = note.strip()[:200]
            today = datetime.now()
            conn.execute(
                'INSERT INTO transactions (user_id, cat, grp, amt, note, y, m, d) VALUES (?,?,?,?,?,?,?,?)',
                (user['id'], cat, 'income', amt, note, today.year, today.month - 1, today.day)
            )
            conn.commit()
            self._send_json(200, self._full_state(conn, user))
        finally:
            conn.close()

    def api_add_goal(self):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            body = self._read_json()
            name = require_str(body, 'name', 1, 60)
            target = require_num(body, 'target', 0.01)
            emoji = body.get('emoji') or '🎯'
            if not isinstance(emoji, str) or not emoji.strip():
                emoji = '🎯'
            emoji = emoji.strip()[:8]
            date = body.get('date') or 'No date set'
            if not isinstance(date, str) or not date.strip():
                date = 'No date set'
            date = date.strip()[:40]
            saved = body.get('saved', 0)
            if not isinstance(saved, (int, float)) or isinstance(saved, bool) or saved < 0:
                saved = 0
            saved = min(float(saved), target)
            conn.execute('INSERT INTO goals (user_id, emoji, name, saved, target, date) VALUES (?,?,?,?,?,?)',
                         (user['id'], emoji, name, saved, target, date))
            conn.commit()
            self._send_json(200, self._full_state(conn, user))
        finally:
            conn.close()

    def api_goal_contribute(self, goal_id):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            body = self._read_json()
            amount = body.get('amount', 50)
            if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
                amount = 50
            goal = conn.execute('SELECT * FROM goals WHERE id = ? AND user_id = ?', (goal_id, user['id'])).fetchone()
            if not goal:
                raise ApiError(404, 'goal not found')
            added = min(float(amount), goal['target'] - goal['saved'])
            if added <= 0:
                raise ApiError(400, 'goal is already reached')
            conn.execute('UPDATE goals SET saved = saved + ? WHERE id = ?', (added, goal_id))
            today = datetime.now()
            conn.execute(
                'INSERT INTO transactions (user_id, cat, grp, amt, note, y, m, d) VALUES (?,?,?,?,?,?,?,?)',
                (user['id'], goal['name'], 'savings', added, '', today.year, today.month - 1, today.day)
            )
            conn.commit()
            self._send_json(200, self._full_state(conn, user))
        finally:
            conn.close()

    def api_add_subscription(self):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            body = self._read_json()
            name = require_str(body, 'name', 1, 60)
            grp = require_str(body, 'group', 1, 20)
            if grp not in GROUPS:
                raise ApiError(400, 'group must be needs, wants, or savings')
            amt = require_num(body, 'amt', 0.01)
            day = body.get('day', 1)
            if not isinstance(day, (int, float)) or isinstance(day, bool):
                raise ApiError(400, 'day must be a number')
            day = max(1, min(31, int(day)))
            conn.execute('INSERT INTO subscriptions (user_id, name, grp, amt, day, active) VALUES (?,?,?,?,?,1)',
                         (user['id'], name, grp, amt, day))
            conn.commit()
            self._send_json(200, self._full_state(conn, user))
        finally:
            conn.close()

    def api_delete_subscription(self, sub_id):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            row = conn.execute('SELECT id FROM subscriptions WHERE id = ? AND user_id = ?',
                                (sub_id, user['id'])).fetchone()
            if not row:
                raise ApiError(404, 'subscription not found')
            conn.execute('UPDATE subscriptions SET active = 0 WHERE id = ?', (sub_id,))
            conn.commit()
            self._send_json(200, self._full_state(conn, user))
        finally:
            conn.close()

    def api_add_category(self):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            body = self._read_json()
            grp = require_str(body, 'group', 1, 20)
            if grp not in GROUPS:
                raise ApiError(400, 'group must be needs, wants, or savings')
            name = require_str(body, 'name', 1, 40)
            existing_default = [c.lower() for c in DEFAULT_CATEGORIES[grp]]
            existing_custom = [r['name'].lower() for r in conn.execute(
                'SELECT name FROM custom_categories WHERE user_id = ? AND grp = ?', (user['id'], grp)).fetchall()]
            if name.lower() in existing_default or name.lower() in existing_custom:
                raise ApiError(409, f'"{name}" already exists in that group')
            conn.execute('INSERT INTO custom_categories (user_id, grp, name) VALUES (?, ?, ?)',
                         (user['id'], grp, name))
            conn.commit()
            self._send_json(200, self._full_state(conn, user))
        finally:
            conn.close()

    def api_delete_category(self, cat_id):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            row = conn.execute('SELECT id FROM custom_categories WHERE id = ? AND user_id = ?',
                                (cat_id, user['id'])).fetchone()
            if not row:
                raise ApiError(404, 'category not found')
            conn.execute('DELETE FROM custom_categories WHERE id = ?', (cat_id,))
            conn.commit()
            self._send_json(200, self._full_state(conn, user))
        finally:
            conn.close()

    def api_delete_goal(self, goal_id):
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                raise ApiError(401, 'not signed in')
            goal = conn.execute('SELECT id FROM goals WHERE id = ? AND user_id = ?', (goal_id, user['id'])).fetchone()
            if not goal:
                raise ApiError(404, 'goal not found')
            conn.execute('DELETE FROM goals WHERE id = ?', (goal_id,))
            conn.commit()
            self._send_json(200, self._full_state(conn, user))
        finally:
            conn.close()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def main():
    init_db()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'Sprout running (Ctrl+C to stop)')
    print(f'  On this Mac:      http://127.0.0.1:{PORT}')
    if HOST == '0.0.0.0':
        ip = lan_ip()
        if ip:
            print(f'  On your network:  http://{ip}:{PORT}   (iPhone must be on the same WiFi)')
        print(f'  Over Tailscale:   http://<this-machine-tailscale-name>:{PORT}  (reachable from anywhere)')
        print('  The first incoming connection may trigger a macOS Firewall prompt — click Allow.')
    print(f'Database: {DB_PATH}')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nStopping Sprout.')
        httpd.shutdown()


if __name__ == '__main__':
    main()
