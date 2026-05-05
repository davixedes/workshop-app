import json
import os
import socket
import threading
from datetime import datetime, timezone

import boto3
import psycopg2
import psycopg2.pool
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="orders-service")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/workshop")
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

sqs = boto3.client(
    "sqs",
    region_name=AWS_REGION,
    endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),  # None em AWS, set para LocalStack
)

db_pool: psycopg2.pool.ThreadedConnectionPool = None


class OrderRequest(BaseModel):
    product: str
    quantity: int
    customer: str


def init_db():
    global db_pool
    db_pool = psycopg2.pool.ThreadedConnectionPool(1, 5, DATABASE_URL)
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id         VARCHAR(50)  PRIMARY KEY,
                    product    VARCHAR(100) NOT NULL,
                    quantity   INTEGER      NOT NULL,
                    customer   VARCHAR(255) NOT NULL,
                    status     VARCHAR(50)  NOT NULL DEFAULT 'created',
                    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                )
            """)
        conn.commit()
        print("Database ready")
    finally:
        db_pool.putconn(conn)


def send_to_queue(order: dict):
    if not SQS_QUEUE_URL:
        print("SQS_QUEUE_URL not set — skipping queue publish")
        return
    try:
        sqs.send_message(QueueUrl=SQS_QUEUE_URL, MessageBody=json.dumps(order))
        print(f"Published order {order['id']} to SQS")
    except Exception as e:
        print(f"Failed to send to SQS: {e}")


@app.on_event("startup")
def startup():
    init_db()


@app.get("/")
def root():
    return {
        "service": "orders-service",
        "version": os.environ.get("APP_VERSION", "2.0.0"),
        "host": socket.gethostname(),
        "queue": SQS_QUEUE_URL or "not configured",
        "endpoints": ["POST /orders", "GET /orders", "GET /orders/{id}", "GET /health"],
    }


@app.post("/orders", status_code=201)
def create_order(req: OrderRequest):
    order_id = f"ORD-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO orders (id, product, quantity, customer, status) VALUES (%s,%s,%s,%s,%s)",
                (order_id, req.product, req.quantity, req.customer, "created"),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise HTTPException(status_code=500, detail="Failed to create order")
    finally:
        db_pool.putconn(conn)

    order = {"id": order_id, "product": req.product,
             "quantity": req.quantity, "customer": req.customer, "status": "created"}

    threading.Thread(target=send_to_queue, args=(order,), daemon=True).start()
    return order


@app.get("/orders")
def list_orders():
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, product, quantity, customer, status, created_at FROM orders ORDER BY created_at DESC")
            rows = cur.fetchall()
        return [{"id": r[0], "product": r[1], "quantity": r[2],
                 "customer": r[3], "status": r[4], "created_at": str(r[5])} for r in rows]
    finally:
        db_pool.putconn(conn)


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, product, quantity, customer, status, created_at FROM orders WHERE id = %s",
                (order_id,),
            )
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
        return {"id": row[0], "product": row[1], "quantity": row[2],
                "customer": row[3], "status": row[4], "created_at": str(row[5])}
    finally:
        db_pool.putconn(conn)


@app.get("/health")
def health():
    return {"status": "healthy", "service": "orders-service", "host": socket.gethostname()}
