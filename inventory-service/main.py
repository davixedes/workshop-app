import json
import os
import socket
import threading
import time

import boto3
import psycopg2
import psycopg2.pool
from fastapi import FastAPI

app = FastAPI(title="inventory-service")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/workshop")
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

sqs = boto3.client(
    "sqs",
    region_name=AWS_REGION,
    endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),  # None em AWS, set para LocalStack
)

db_pool: psycopg2.pool.ThreadedConnectionPool = None

INITIAL_STOCK = [
    ("notebook", 50),
    ("teclado", 100),
    ("mouse", 150),
    ("monitor", 30),
    ("headset", 75),
]


def init_db():
    global db_pool
    db_pool = psycopg2.pool.ThreadedConnectionPool(1, 5, DATABASE_URL)
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stock (
                    product  VARCHAR(100) PRIMARY KEY,
                    quantity INTEGER      NOT NULL DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reservations (
                    id           SERIAL       PRIMARY KEY,
                    order_id     VARCHAR(50)  NOT NULL,
                    product      VARCHAR(100) NOT NULL,
                    quantity     INTEGER      NOT NULL,
                    status       VARCHAR(50)  NOT NULL,
                    processed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                )
            """)
            for product, qty in INITIAL_STOCK:
                cur.execute(
                    "INSERT INTO stock (product, quantity) VALUES (%s,%s) ON CONFLICT (product) DO NOTHING",
                    (product, qty),
                )
        conn.commit()
        print("Database ready")
    finally:
        db_pool.putconn(conn)


def process_order(order: dict):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute("SELECT quantity FROM stock WHERE product = %s FOR UPDATE", (order["product"],))
            row = cur.fetchone()
            available = row[0] if row else 0
            status = "reserved" if available >= order["quantity"] else "out_of_stock"

            if status == "reserved":
                cur.execute(
                    "UPDATE stock SET quantity = quantity - %s WHERE product = %s",
                    (order["quantity"], order["product"]),
                )
            cur.execute(
                "INSERT INTO reservations (order_id, product, quantity, status) VALUES (%s,%s,%s,%s)",
                (order["id"], order["product"], order["quantity"], status),
            )
        conn.commit()
        print(f"Order {order['id']}: {status} ({order['quantity']}x {order['product']})")
    except Exception as e:
        conn.rollback()
        print(f"Failed to process order: {e}")
    finally:
        db_pool.putconn(conn)


def consume():
    if not SQS_QUEUE_URL:
        print("SQS_QUEUE_URL not set — consumer not started")
        return

    print("Starting SQS consumer...")
    while True:
        try:
            response = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=20,  # long polling — evita requests vazios
            )
            for msg in response.get("Messages", []):
                order = json.loads(msg["Body"])
                process_order(order)
                sqs.delete_message(
                    QueueUrl=SQS_QUEUE_URL,
                    ReceiptHandle=msg["ReceiptHandle"],
                )
        except Exception as e:
            print(f"SQS error: {e}")
            time.sleep(5)


@app.on_event("startup")
def startup():
    init_db()
    threading.Thread(target=consume, daemon=True).start()


@app.get("/")
def root():
    return {
        "service": "inventory-service",
        "version": os.environ.get("APP_VERSION", "1.0.0"),
        "host": socket.gethostname(),
        "queue": SQS_QUEUE_URL or "not configured",
        "endpoints": ["GET /inventory", "GET /health"],
    }


@app.get("/inventory")
def get_inventory():
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT product, quantity FROM stock ORDER BY product")
            stock = [{"product": r[0], "quantity": r[1]} for r in cur.fetchall()]
            cur.execute(
                "SELECT id, order_id, product, quantity, status, processed_at FROM reservations ORDER BY processed_at DESC LIMIT 50"
            )
            reservations = [
                {"id": r[0], "order_id": r[1], "product": r[2],
                 "quantity": r[3], "status": r[4], "processed_at": str(r[5])}
                for r in cur.fetchall()
            ]
        return {"stock": stock, "reservations": reservations}
    finally:
        db_pool.putconn(conn)


@app.get("/health")
def health():
    return {"status": "healthy", "service": "inventory-service", "host": socket.gethostname()}
