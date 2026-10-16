"""Small PostgreSQL compatibility layer for the existing Shopy SQLite SQL.

Not a general SQL transpiler. Handles only the audited SQL statements in this
project. Does NOT initialize, drop, or seed a production database.
"""
from __future__ import annotations
import os
import re

SERIAL_TABLES = {
    'users', 'products', 'shipping', 'orders', 'order_items', 'product_media',
    'shipping_zones', 'notification_outbox', 'safka_shipping_rates',
    'safka_sync_runs', 'safka_order_outbox',
}

class PGRow(dict):
    """Existing templates use named access; a few helper functions use row[0]."""
    def __iter__(self):
        return iter(self.values())
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)

class Result:
    def __init__(self, cur, lastrowid=None, returned=None):
        self.cur = cur
        self.lastrowid = lastrowid
        self._returned = returned
        self.rowcount = cur.rowcount
    def fetchone(self):
        r = self.cur.fetchone() if self._returned is None else self._returned
        if self._returned is not None: self._returned = None
        return PGRow(r) if r is not None else None
    def fetchall(self):
        return [PGRow(row) for row in self.cur.fetchall()]
    def __iter__(self):
        return (PGRow(r) for r in self.cur)


def _translate(statement):
    q = statement.strip()
    # The old code uses SQLite's table introspection in additive migrations.
    info = re.fullmatch(r'PRAGMA\s+table_info\((\w+)\)',q,re.I)
    if info:
        return ("SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='"+info.group(1)+"'"), ()
    if q.upper().startswith('PRAGMA FOREIGN_KEYS'):
        return 'SELECT 1', ()
    if q.upper() == 'BEGIN IMMEDIATE':
        return 'SELECT 1', ()  # PostgreSQL transaction already begun.
    q = re.sub(r'\bINTEGER\s+PRIMARY\s+KEY(?:\s+AUTOINCREMENT)?\b',
               'BIGSERIAL PRIMARY KEY',q, flags=re.I)
    q = re.sub(r'^INSERT\s+OR\s+IGNORE\s+INTO\b','INSERT INTO',q,flags=re.I)
    if re.match(r'^INSERT\s+INTO\b',q,re.I) and re.search(r'^INSERT\s+OR\s+IGNORE',statement.strip(),re.I):
        q += ' ON CONFLICT DO NOTHING'
    # Escape literal '%' for psycopg parameter syntax, then translate SQLite '?'.
    q = q.replace('DEFAULT CURRENT_TIMESTAMP','DEFAULT (CURRENT_TIMESTAMP::text)')
    q = q.replace('%', '%%').replace('?', '%s')
    return q, None

class PGConnection:
    def __init__(self, url=None):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError('Install psycopg[binary] to use Supabase PostgreSQL') from exc
        url = url or os.getenv('DATABASE_URL')
        if not url: raise RuntimeError('DATABASE_URL is missing; do not commit secrets')
        # Supabase transaction pooler forbids prepared statements.
        self._conn = psycopg.connect(url, row_factory=dict_row, prepare_threshold=None,
                                     connect_timeout=10, sslmode='require')
    def __enter__(self): return self
    def __exit__(self, typ, val, tb):
        try:
            if typ: self._conn.rollback()
            else: self._conn.commit()
        finally: self._conn.close()
    def execute(self, query, args=()):
        sql, special_args = _translate(query)
        if special_args is not None: args = special_args
        match = re.match(r'^INSERT\s+INTO\s+(\w+)\s*\(',sql,re.I)
        generated = bool(match and match.group(1).lower() in SERIAL_TABLES)
        if generated and not re.search(r'\bRETURNING\b', sql, re.I):
            sql += ' RETURNING id'
        cur = self._conn.execute(sql, tuple(args))
        if generated:
            row = cur.fetchone()
            return Result(cur, lastrowid=(row['id'] if row else None))
        return Result(cur)
    def executescript(self, script):
        for item in script.split(';'):
            if item.strip(): self.execute(item)
    def commit(self): self._conn.commit()
    def rollback(self): self._conn.rollback()
