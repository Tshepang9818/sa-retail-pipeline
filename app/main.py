import os
import time
import json
import psycopg2
import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="SA Retail Inventory API")
Instrumentator().instrument(app).expose(app)

DB_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
cache = redis.from_url(REDIS_URL, decode_responses=True)

# The six SA branches this system manages
SA_BRANCHES = [
    "Soweto", "Khayelitsha", "Durban CBD",
    "Pretoria North", "Bloemfontein", "Polokwane"
]

# Minimum stock levels before a reorder alert fires
LOW_STOCK_THRESHOLD = {
    "maize_meal": 50,
    "cooking_oil": 30,
    "bread": 40,
    "milk": 25,
    "sugar": 35
}

def get_conn():
    return psycopg2.connect(DB_URL)

def init_db():
    """
    Initialises the database schema and seeds starting stock data.
    Retries 5 times with a 5 second gap — this handles the race
    condition where the app starts before PostgreSQL is ready.
    This is critical in Kubernetes where container startup order
    is not guaranteed.
    """
    retries = 5
    while retries > 0:
        try:
            conn = get_conn()
            cur = conn.cursor()

            # inventory table stores current stock per branch per product
            cur.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    id SERIAL PRIMARY KEY,
                    branch TEXT NOT NULL,
                    product TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    last_updated TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(branch, product)
                )
            """)

            # reorder_alerts stores every time stock drops below threshold
            # this is the audit trail that proves the system caught the problem
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reorder_alerts (
                    id SERIAL PRIMARY KEY,
                    branch TEXT NOT NULL,
                    product TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    threshold INTEGER NOT NULL,
                    alerted_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.commit()

            # Seed starting stock — 3x the threshold for each product
            # so there is room to decrease before alerts fire
            for branch in SA_BRANCHES:
                for product, threshold in LOW_STOCK_THRESHOLD.items():
                    cur.execute("""
                        INSERT INTO inventory (branch, product, quantity)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (branch, product) DO NOTHING
                    """, (branch, product, threshold * 3))
            conn.commit()
            conn.close()
            print("Database initialised and seeded successfully")
            break
        except Exception as e:
            print(f"DB not ready, retrying in 5s... ({e})")
            retries -= 1
            time.sleep(5)

init_db()

# --- Data models ---
# Pydantic models validate incoming JSON automatically
# if a field is missing or the wrong type FastAPI returns
# a clear 422 error instead of a cryptic server crash

class StockUpdate(BaseModel):
    branch: str
    product: str
    quantity: int

class StockDecrease(BaseModel):
    branch: str
    product: str
    amount: int

# --- API Routes ---

@app.get("/")
def root():
    """
    Root endpoint confirms the service is running and
    lists all managed branches. Useful for quick sanity checks.
    """
    return {
        "service": "SA Retail Inventory API",
        "status": "running",
        "branches": SA_BRANCHES
    }

@app.get("/health")
def health():
    """
    Health endpoint is critical for Kubernetes.
    Kubernetes hits this endpoint every 10 seconds via
    livenessProbe and readinessProbe. If it returns anything
    other than 200 Kubernetes restarts the pod automatically.
    This is how zero-downtime self-healing works.
    """
    return {"status": "healthy", "timestamp": str(datetime.utcnow())}

@app.get("/inventory")
def get_all_inventory():
    """
    Returns stock levels for all branches.
    First checks Redis cache — if data is there it returns
    immediately without touching PostgreSQL.
    If cache is empty it queries PostgreSQL and stores
    the result in Redis for 30 seconds.
    This pattern is called cache-aside and it is the most
    common caching strategy in production retail systems.
    """
    cached = cache.get("all_inventory")
    if cached:
        print("Serving inventory from Redis cache")
        return json.loads(cached)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT branch, product, quantity, last_updated
        FROM inventory ORDER BY branch, product
    """)
    rows = cur.fetchall()
    conn.close()
    result = [
        {
            "branch": r[0],
            "product": r[1],
            "quantity": r[2],
            "last_updated": str(r[3])
        } for r in rows
    ]
    cache.setex("all_inventory", 30, json.dumps(result))
    return result

@app.get("/inventory/{branch}")
def get_branch_inventory(branch: str):
    """
    Returns stock levels for a specific branch.
    Each branch has its own cache key so Soweto's cache
    expiring does not invalidate Khayelitsha's cache.
    This is fine-grained caching — more efficient than
    clearing everything when one branch updates.
    """
    cache_key = f"inventory_{branch}"
    cached = cache.get(cache_key)
    if cached:
        print(f"Serving {branch} inventory from Redis cache")
        return json.loads(cached)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT product, quantity, last_updated
        FROM inventory WHERE branch = %s
    """, (branch,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        raise HTTPException(status_code=404, detail=f"Branch {branch} not found")
    result = [
        {
            "product": r[0],
            "quantity": r[1],
            "last_updated": str(r[2])
        } for r in rows
    ]
    cache.setex(cache_key, 30, json.dumps(result))
    return result

@app.put("/inventory/update")
def update_stock(update: StockUpdate):
    """
    Updates stock level for a product at a branch.
    After updating PostgreSQL it clears the relevant
    cache keys so the next read gets fresh data.
    This is cache invalidation — one of the hardest
    problems in computer science done simply.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO inventory (branch, product, quantity)
        VALUES (%s, %s, %s)
        ON CONFLICT (branch, product)
        DO UPDATE SET quantity = %s, last_updated = NOW()
    """, (update.branch, update.product,
          update.quantity, update.quantity))
    conn.commit()
    conn.close()
    cache.delete("all_inventory")
    cache.delete(f"inventory_{update.branch}")
    return {
        "message": f"Stock updated",
        "branch": update.branch,
        "product": update.product,
        "new_quantity": update.quantity
    }

@app.put("/inventory/decrease")
def decrease_stock(decrease: StockDecrease):
    """
    Decreases stock when items are sold.
    After decreasing it checks if the new quantity
    is at or below the threshold for that product.
    If it is, it writes a reorder alert to the database
    and prints a warning — in a production system this
    would trigger an SMS via Africa's Talking or an
    email to the store manager automatically.
    This is event-driven architecture at its simplest.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE inventory
        SET quantity = quantity - %s, last_updated = NOW()
        WHERE branch = %s AND product = %s
        RETURNING quantity
    """, (decrease.amount, decrease.branch, decrease.product))
    row = cur.fetchone()
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Product or branch not found"
        )
    new_quantity = row[0]
    alerts = []
    threshold = LOW_STOCK_THRESHOLD.get(decrease.product, 0)
    if new_quantity <= threshold:
        cur.execute("""
            INSERT INTO reorder_alerts
            (branch, product, quantity, threshold)
            VALUES (%s, %s, %s, %s)
        """, (decrease.branch, decrease.product,
              new_quantity, threshold))
        alert_msg = (
            f"LOW STOCK ALERT: {decrease.product} at "
            f"{decrease.branch} — only {new_quantity} units left"
        )
        alerts.append(alert_msg)
        print(f"ALERT: {alert_msg}")
    conn.commit()
    conn.close()
    cache.delete("all_inventory")
    cache.delete(f"inventory_{decrease.branch}")
    return {
        "message": "Stock decreased",
        "branch": decrease.branch,
        "product": decrease.product,
        "new_quantity": new_quantity,
        "threshold": threshold,
        "alerts": alerts
    }

@app.get("/low-stock")
def get_low_stock():
    """
    Returns all products across all branches that are
    at or below their reorder threshold.
    This is the dashboard endpoint — a store manager
    or automated system would poll this regularly
    to know which branches need restocking urgently.
    """
    conn = get_conn()
    cur = conn.cursor()
    results = []
    for product, threshold in LOW_STOCK_THRESHOLD.items():
        cur.execute("""
            SELECT branch, product, quantity
            FROM inventory
            WHERE product = %s AND quantity <= %s
        """, (product, threshold))
        rows = cur.fetchall()
        for r in rows:
            results.append({
                "branch": r[0],
                "product": r[1],
                "quantity": r[2],
                "threshold": threshold,
                "urgency": "CRITICAL" if r[2] <= threshold // 2 else "LOW"
            })
    conn.close()
    return sorted(results, key=lambda x: x["quantity"])

@app.get("/alerts")
def get_alerts():
    """
    Returns the full audit trail of every reorder alert
    that has ever fired. This is the POPIA-compliant
    record that proves the system detected and logged
    every stock shortage event with a timestamp.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT branch, product, quantity, threshold, alerted_at
        FROM reorder_alerts
        ORDER BY alerted_at DESC LIMIT 100
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "branch": r[0],
            "product": r[1],
            "quantity": r[2],
            "threshold": r[3],
            "alerted_at": str(r[4])
        } for r in rows
    ]
