from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import os

from database import (
    get_db, init_db, get_next_order_number,
    get_order_components_full, get_order_dict,
    calculate_raw_components, round_up, get_product_bom
)

app = FastAPI()

PINS = {
    '2481632': 'production',
    '392781': 'warehouse',
}

# ── startup ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()

# ── auth ─────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    pin: str

@app.post("/api/login")
def login(req: LoginRequest):
    role = PINS.get(req.pin.strip())
    if not role:
        raise HTTPException(status_code=401, detail="קוד שגוי")
    name = 'מנהלת תפי' if role == 'production' else 'מנהל מחסן'
    return {"role": role, "name": name}

# ── reference data ───────────────────────────────────────────────────────────

@app.get("/api/subcontractors")
def list_subcontractors():
    conn = get_db()
    rows = conn.execute("SELECT * FROM subcontractors ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/customers")
def list_customers():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT customer FROM products ORDER BY customer").fetchall()
    conn.close()
    return [r['customer'] for r in rows]


@app.get("/api/products")
def list_products(customer: str = '', q: str = ''):
    conn = get_db()
    sql = "SELECT id, name, customer FROM products WHERE 1=1"
    params = []
    if customer:
        sql += " AND customer = ?"
        params.append(customer)
    if q:
        sql += " AND (name LIKE ? OR id LIKE ?)"
        params += [f'%{q}%', f'%{q}%']
    sql += " ORDER BY customer, name"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/components")
def list_components(q: str = ''):
    conn = get_db()
    if q:
        rows = conn.execute(
            "SELECT id, name, component_type, unit FROM components WHERE id=? OR name LIKE ? LIMIT 10",
            (q, f'%{q}%')
        ).fetchall()
    else:
        rows = []
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/products/{product_id}/bom")
def get_bom(product_id: str):
    conn = get_db()
    row = conn.execute("SELECT id, name, customer FROM products WHERE id=?", (product_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "מוצר לא נמצא")
    bom = get_product_bom(conn, product_id)
    conn.close()
    return {"product": dict(row), "bom": bom}


# ── orders ────────────────────────────────────────────────────────────────────

@app.get("/api/orders")
def list_orders(subcontractor_id: int = 0, status: str = ''):
    conn = get_db()
    sql = """
        SELECT o.id, o.order_number, o.status, o.created_at, o.closed_at,
               s.name as sub_name, s.id as subcontractor_id
        FROM orders o JOIN subcontractors s ON s.id = o.subcontractor_id
        WHERE 1=1
    """
    params = []
    if subcontractor_id:
        sql += " AND o.subcontractor_id = ?"
        params.append(subcontractor_id)
    if status:
        sql += " AND o.status = ?"
        params.append(status)
    sql += " ORDER BY o.order_number DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/orders/active/{subcontractor_id}")
def get_active_order(subcontractor_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM orders WHERE subcontractor_id=? AND status IN ('open','locked') ORDER BY id DESC LIMIT 1",
        (subcontractor_id,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    result = get_order_dict(conn, row['id'])
    conn.close()
    return result


@app.get("/api/orders/{order_id}")
def get_order(order_id: int):
    conn = get_db()
    result = get_order_dict(conn, order_id)
    conn.close()
    if not result:
        raise HTTPException(404, "הזמנה לא נמצאה")
    return result


class CreateOrderRequest(BaseModel):
    subcontractor_id: int

@app.post("/api/orders")
def create_order(req: CreateOrderRequest):
    conn = get_db()
    # Ensure no active order exists
    existing = conn.execute(
        "SELECT id FROM orders WHERE subcontractor_id=? AND status IN ('open','locked')",
        (req.subcontractor_id,)
    ).fetchone()
    if existing:
        conn.close()
        raise HTTPException(400, "קיימת הזמנה פתוחה לקבלן זה")
    num = get_next_order_number(conn)
    conn.execute(
        "INSERT INTO orders (order_number, subcontractor_id, status) VALUES (?,?,?)",
        (num, req.subcontractor_id, 'open')
    )
    conn.commit()
    order_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()['id']
    result = get_order_dict(conn, order_id)
    conn.close()
    return result


# ── order products ────────────────────────────────────────────────────────────

class UpsertProductRequest(BaseModel):
    product_id: str
    quantity: int

@app.post("/api/orders/{order_id}/products")
def upsert_product(order_id: int, req: UpsertProductRequest):
    conn = get_db()
    _check_open(conn, order_id)
    if req.quantity <= 0:
        conn.execute("DELETE FROM order_products WHERE order_id=? AND product_id=?",
                     (order_id, req.product_id))
    else:
        conn.execute("""
            INSERT INTO order_products (order_id, product_id, quantity) VALUES (?,?,?)
            ON CONFLICT(order_id, product_id) DO UPDATE SET quantity=excluded.quantity
        """, (order_id, req.product_id, req.quantity))
    conn.commit()
    result = get_order_dict(conn, order_id)
    conn.close()
    return result


@app.delete("/api/orders/{order_id}/products/{product_id}")
def remove_product(order_id: int, product_id: str):
    conn = get_db()
    _check_open(conn, order_id)
    conn.execute("DELETE FROM order_products WHERE order_id=? AND product_id=?",
                 (order_id, product_id))
    conn.commit()
    result = get_order_dict(conn, order_id)
    conn.close()
    return result


# ── component adjustments ─────────────────────────────────────────────────────

class AdjustRequest(BaseModel):
    component_id: str
    quantity_adjusted: Optional[float] = None  # None = reset to auto
    manual_note: Optional[str] = None

@app.put("/api/orders/{order_id}/adjustments")
def adjust_component(order_id: int, req: AdjustRequest):
    conn = get_db()
    _check_open(conn, order_id)
    if req.quantity_adjusted is None and not req.manual_note:
        conn.execute("DELETE FROM order_component_adjustments WHERE order_id=? AND component_id=?",
                     (order_id, req.component_id))
    else:
        conn.execute("""
            INSERT INTO order_component_adjustments (order_id, component_id, quantity_adjusted, manual_note)
            VALUES (?,?,?,?)
            ON CONFLICT(order_id, component_id) DO UPDATE SET
                quantity_adjusted=excluded.quantity_adjusted,
                manual_note=excluded.manual_note
        """, (order_id, req.component_id, req.quantity_adjusted, req.manual_note))
    conn.commit()
    result = get_order_dict(conn, order_id)
    conn.close()
    return result


# ── status transitions ────────────────────────────────────────────────────────

@app.post("/api/orders/{order_id}/lock")
def lock_order(order_id: int):
    conn = get_db()
    _check_status(conn, order_id, 'open')
    conn.execute("UPDATE orders SET status='locked' WHERE id=?", (order_id,))
    conn.commit()
    result = get_order_dict(conn, order_id)
    conn.close()
    return result

@app.post("/api/orders/{order_id}/unlock")
def unlock_order(order_id: int):
    """Production manager can reopen a locked order (but not a closed one)."""
    conn = get_db()
    _check_status(conn, order_id, 'locked')
    conn.execute("UPDATE orders SET status='open' WHERE id=?", (order_id,))
    conn.commit()
    result = get_order_dict(conn, order_id)
    conn.close()
    return result


class CloseRequest(BaseModel):
    sent: list  # [{component_id, quantity_sent}]

@app.post("/api/orders/{order_id}/close")
def close_order(order_id: int, req: CloseRequest):
    conn = get_db()
    _check_status(conn, order_id, 'locked')
    for item in req.sent:
        conn.execute("""
            INSERT INTO order_sent (order_id, component_id, quantity_sent) VALUES (?,?,?)
            ON CONFLICT(order_id, component_id) DO UPDATE SET quantity_sent=excluded.quantity_sent
        """, (order_id, item['component_id'], item['quantity_sent']))
    conn.execute("UPDATE orders SET status='closed', closed_at=datetime('now','localtime') WHERE id=?",
                 (order_id,))
    conn.commit()
    result = get_order_dict(conn, order_id)
    conn.close()
    return result


@app.get("/api/orders/{order_id}/carryover")
def get_carryover(order_id: int):
    """Returns components where quantity_sent < quantity_final (remainder to carry over)."""
    conn = get_db()
    order = get_order_dict(conn, order_id)
    conn.close()
    if not order or order['status'] != 'closed':
        return []
    carryover = []
    for comp in order['components']:
        final = comp['quantity_final'] or 0
        sent = comp['quantity_sent'] or 0
        remaining = final - sent
        if remaining > 0 and not comp['is_manual']:
            carryover.append({**comp, 'remaining': remaining})
    return carryover


class CarryoverRequest(BaseModel):
    subcontractor_id: int

@app.post("/api/orders/{order_id}/carryover")
def create_carryover_order(order_id: int, req: CarryoverRequest):
    """Create a new order and pre-fill adjustments with remaining quantities."""
    conn = get_db()
    carryover = []
    order = get_order_dict(conn, order_id)
    if not order or order['status'] != 'closed':
        conn.close()
        raise HTTPException(400, "הזמנה לא סגורה")
    for comp in order['components']:
        final = comp['quantity_final'] or 0
        sent = comp['quantity_sent'] or 0
        remaining = final - sent
        if remaining > 0 and not comp['is_manual']:
            carryover.append({'component_id': comp['component_id'], 'remaining': remaining})

    # Create new order
    existing = conn.execute(
        "SELECT id FROM orders WHERE subcontractor_id=? AND status IN ('open','locked')",
        (req.subcontractor_id,)
    ).fetchone()
    if existing:
        conn.close()
        raise HTTPException(400, "כבר קיימת הזמנה פתוחה לקבלן זה")

    num = get_next_order_number(conn)
    conn.execute("INSERT INTO orders (order_number, subcontractor_id, status) VALUES (?,?,?)",
                 (num, req.subcontractor_id, 'open'))
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()['id']

    # Copy products from old order
    conn.execute("INSERT INTO order_products (order_id, product_id, quantity) SELECT ?,product_id,quantity FROM order_products WHERE order_id=?",
                 (new_id, order_id))

    # Set adjustments to carry-over quantities
    for item in carryover:
        conn.execute("""
            INSERT INTO order_component_adjustments (order_id, component_id, quantity_adjusted)
            VALUES (?,?,?)
            ON CONFLICT(order_id, component_id) DO UPDATE SET quantity_adjusted=excluded.quantity_adjusted
        """, (new_id, item['component_id'], item['remaining']))
    conn.commit()
    result = get_order_dict(conn, new_id)
    conn.close()
    return result


# ── free items ────────────────────────────────────────────────────────────────

class FreeItemRequest(BaseModel):
    name: str
    quantity: float
    unit: Optional[str] = ''
    category: Optional[str] = 'מוצרי אריזה'
    note: Optional[str] = ''

@app.post("/api/orders/{order_id}/free-items")
def add_free_item(order_id: int, req: FreeItemRequest):
    conn = get_db()
    _check_open(conn, order_id)
    conn.execute("""
        INSERT INTO order_free_items (order_id, name, quantity, unit, category, note)
        VALUES (?,?,?,?,?,?)
    """, (order_id, req.name, req.quantity, req.unit, req.category, req.note))
    conn.commit()
    result = get_order_dict(conn, order_id)
    conn.close()
    return result

@app.put("/api/orders/{order_id}/free-items/{item_id}")
def update_free_item(order_id: int, item_id: int, req: FreeItemRequest):
    conn = get_db()
    _check_open(conn, order_id)
    conn.execute("""
        UPDATE order_free_items SET name=?, quantity=?, unit=?, category=?, note=?
        WHERE id=? AND order_id=?
    """, (req.name, req.quantity, req.unit, req.category, req.note, item_id, order_id))
    conn.commit()
    result = get_order_dict(conn, order_id)
    conn.close()
    return result

@app.delete("/api/orders/{order_id}/free-items/{item_id}")
def delete_free_item(order_id: int, item_id: int):
    conn = get_db()
    _check_open(conn, order_id)
    conn.execute("DELETE FROM order_free_items WHERE id=? AND order_id=?", (item_id, order_id))
    conn.commit()
    result = get_order_dict(conn, order_id)
    conn.close()
    return result

@app.delete("/api/orders/{order_id}")
def delete_order(order_id: int):
    conn = get_db()
    row = conn.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404)
    conn.execute("DELETE FROM order_free_items WHERE order_id=?", (order_id,))
    conn.execute("DELETE FROM order_sent WHERE order_id=?", (order_id,))
    conn.execute("DELETE FROM order_component_adjustments WHERE order_id=?", (order_id,))
    conn.execute("DELETE FROM order_products WHERE order_id=?", (order_id,))
    conn.execute("DELETE FROM orders WHERE id=?", (order_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ── helpers ───────────────────────────────────────────────────────────────────

def _check_open(conn, order_id):
    row = conn.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row or row['status'] != 'open':
        conn.close()
        raise HTTPException(400, "ניתן לערוך רק הזמנות פתוחות")

def _check_status(conn, order_id, expected):
    row = conn.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row or row['status'] != expected:
        conn.close()
        raise HTTPException(400, f"הזמנה חייבת להיות במצב '{expected}'")


# ── static files ──────────────────────────────────────────────────────────────

static_dir = os.path.join(os.path.dirname(__file__), 'static')
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def root():
    return FileResponse(os.path.join(static_dir, "index.html"))
