import sqlite3
import os
import math
import json

DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'orders.db'))
DATA_PATH = os.path.join(os.path.dirname(__file__), 'data', 'products.json')

SCHEMA = """
CREATE TABLE IF NOT EXISTS subcontractors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    address TEXT
);
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    customer TEXT
);
CREATE TABLE IF NOT EXISTS components (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    component_type TEXT,
    unit TEXT,
    rounding INTEGER
);
CREATE TABLE IF NOT EXISTS product_components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT REFERENCES products(id),
    component_id TEXT REFERENCES components(id),
    quantity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number INTEGER UNIQUE NOT NULL,
    subcontractor_id INTEGER REFERENCES subcontractors(id),
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    closed_at TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS order_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    product_id TEXT REFERENCES products(id),
    quantity INTEGER NOT NULL,
    UNIQUE(order_id, product_id)
);
CREATE TABLE IF NOT EXISTS order_component_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    component_id TEXT REFERENCES components(id),
    quantity_adjusted REAL,
    manual_note TEXT,
    UNIQUE(order_id, component_id)
);
CREATE TABLE IF NOT EXISTS order_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    component_id TEXT REFERENCES components(id),
    quantity_sent REAL,
    UNIQUE(order_id, component_id)
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS order_free_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit TEXT,
    category TEXT DEFAULT 'מוצרי אריזה',
    note TEXT
);
"""

TYPE_ORDER = ['קרטון', 'מדבקה', 'קרטוניות', 'שקית', 'ספוג', 'מייקרופייבר', 'כרית', 'גלילים', 'בד', 'אחר']


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def round_up(quantity: float, rounding) -> float:
    if rounding is None or rounding == 0:
        return quantity
    return math.ceil(quantity / rounding) * rounding


def get_next_order_number(conn) -> int:
    c = conn.cursor()
    c.execute("SELECT value FROM app_settings WHERE key='next_order_number'")
    row = c.fetchone()
    num = int(row['value'])
    c.execute("UPDATE app_settings SET value=? WHERE key='next_order_number'", (str(num + 1),))
    return num


def calculate_raw_components(conn, order_id: int) -> dict:
    """Returns {component_id: raw_total_quantity} from all order products."""
    c = conn.cursor()
    c.execute("""
        SELECT pc.component_id, SUM(op.quantity * pc.quantity) as total
        FROM order_products op
        JOIN product_components pc ON pc.product_id = op.product_id
        WHERE op.order_id = ?
        GROUP BY pc.component_id
    """, (order_id,))
    return {row['component_id']: row['total'] for row in c.fetchall()}


def get_order_components_full(conn, order_id: int) -> list:
    calculated = calculate_raw_components(conn, order_id)
    if not calculated:
        return []

    c = conn.cursor()
    c.execute("SELECT component_id, quantity_adjusted, manual_note FROM order_component_adjustments WHERE order_id=?", (order_id,))
    adjustments = {r['component_id']: dict(r) for r in c.fetchall()}

    c.execute("SELECT component_id, quantity_sent FROM order_sent WHERE order_id=?", (order_id,))
    sent = {r['component_id']: r['quantity_sent'] for r in c.fetchall()}

    placeholders = ','.join('?' * len(calculated))
    c.execute(f"SELECT * FROM components WHERE id IN ({placeholders})", list(calculated.keys()))
    comp_info = {r['id']: dict(r) for r in c.fetchall()}

    result = []
    for cid, raw_qty in calculated.items():
        comp = comp_info.get(cid, {})
        rounding = comp.get('rounding')
        ctype = comp.get('component_type', 'אחר')
        qty_rounded = round_up(raw_qty, rounding)
        adj = adjustments.get(cid, {})
        qty_adjusted = adj.get('quantity_adjusted')
        qty_final = qty_adjusted if qty_adjusted is not None else qty_rounded

        result.append({
            'component_id': cid,
            'component_name': comp.get('name', cid),
            'component_type': ctype,
            'unit': comp.get('unit') or '',
            'rounding': rounding,
            'quantity_calculated': round(raw_qty, 4),
            'quantity_rounded': qty_rounded,
            'quantity_adjusted': qty_adjusted,
            'quantity_final': qty_final,
            'quantity_sent': sent.get(cid),
            'manual_note': adj.get('manual_note') or '',
            'is_manual': (ctype == 'בד'),
        })

    type_idx = {t: i for i, t in enumerate(TYPE_ORDER)}
    result.sort(key=lambda x: (type_idx.get(x['component_type'], 99), x['component_id']))
    return result


def get_product_bom(conn, product_id: str) -> list:
    """Returns BOM for a product with component type info."""
    c = conn.cursor()
    c.execute("""
        SELECT pc.component_id, pc.quantity as base_quantity,
               comp.name as component_name, comp.component_type, comp.unit, comp.rounding
        FROM product_components pc
        JOIN components comp ON comp.id = pc.component_id
        WHERE pc.product_id = ?
        ORDER BY comp.component_type, pc.component_id
    """, (product_id,))
    return [dict(r) for r in c.fetchall()]


def get_order_dict(conn, order_id: int) -> dict:
    c = conn.cursor()
    c.execute("""
        SELECT o.*, s.name as sub_name, s.address as sub_address
        FROM orders o JOIN subcontractors s ON s.id = o.subcontractor_id
        WHERE o.id = ?
    """, (order_id,))
    row = c.fetchone()
    if not row:
        return None
    o = dict(row)

    c.execute("""
        SELECT op.product_id, op.quantity, p.name as product_name, p.customer
        FROM order_products op JOIN products p ON p.id = op.product_id
        WHERE op.order_id = ?
        ORDER BY p.customer, p.name
    """, (order_id,))
    o['products'] = [dict(r) for r in c.fetchall()]
    o['components'] = get_order_components_full(conn, order_id)
    c.execute("SELECT * FROM order_free_items WHERE order_id=? ORDER BY category, id",
              (order_id,))
    o['free_items'] = [dict(r) for r in c.fetchall()]
    return o


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript(SCHEMA)

    c.execute("SELECT COUNT(*) as cnt FROM subcontractors")
    if c.fetchone()['cnt'] == 0:
        c.executemany("INSERT INTO subcontractors (name, address) VALUES (?,?)", [
            ('מלכי', 'רחוב רמז, אשקלון'),
            ('כלא רימונים', 'כלא רימונים, נתניה'),
            ('פינטו', 'קריית גת'),
        ])

    c.execute("INSERT OR IGNORE INTO app_settings VALUES ('next_order_number','25001')")

    c.execute("SELECT COUNT(*) as cnt FROM products")
    if c.fetchone()['cnt'] == 0 and os.path.exists(DATA_PATH):
        with open(DATA_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        c.executemany("INSERT OR IGNORE INTO products (id,name,customer) VALUES (?,?,?)",
                      [(p['id'], p['name'], p['customer']) for p in data['products']])
        c.executemany("INSERT OR IGNORE INTO components (id,name,component_type,unit,rounding) VALUES (?,?,?,?,?)",
                      [(comp['id'], comp['name'], comp['type'], comp['unit'], comp['rounding']) for comp in data['components']])
        c.executemany("INSERT OR IGNORE INTO product_components (product_id,component_id,quantity) VALUES (?,?,?)",
                      [(pc['product_id'], pc['component_id'], pc['quantity']) for pc in data['product_components']])

    conn.commit()
    conn.close()
