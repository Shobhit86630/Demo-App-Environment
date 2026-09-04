import logging
import os
import random
import threading
import time

import psycopg2
import redis
from fastapi import FastAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger("sentinel-api")

app = FastAPI(title="Sentinel Lab API")

# Fake in-memory order table, deliberately missing most ids so /orders/{id}
# produces a real, unhandled KeyError for ids that were never seeded.
ORDERS = {
    "1001": {"item": "widget", "qty": 3},
    "1002": {"item": "gadget", "qty": 1},
}

# Background thread emits a random INFO/WARNING/ERROR line every 8-20s from
# this pool, so there is always fresh, varied noise for the agent's live
# collector to pick up even when nobody is hitting the broken endpoints by
# hand - most lines are faults, but a few are mundane on purpose, since real
# noise isn't all alarming.
SYNTHETIC_EVENTS = [
    (logging.WARNING, "disk usage on /var/lib/postgresql/data at 87% - approaching capacity threshold"),
    (logging.WARNING, "connection pool exhausted: 20/20 connections in use, request queued"),
    (logging.ERROR, "auth failure for user 'admin' from 10.0.0.14 - invalid credentials (3rd attempt)"),
    (logging.ERROR, "upstream dependency 'shipping-rates-api' returned 503 after 2 retries"),
    (logging.WARNING, "GC pause exceeded 400ms - request latency degraded"),
    (logging.ERROR, "TCP connection reset by peer while streaming response to client 10.0.0.22"),
    (logging.INFO, "scheduled cache warm-up completed for 128 keys"),
]


def _synthetic_noise_loop():
    while True:
        time.sleep(random.uniform(8, 20))
        level, message = random.choice(SYNTHETIC_EVENTS)
        logger.log(level, message)


@app.on_event("startup")
def start_background_noise():
    thread = threading.Thread(target=_synthetic_noise_loop, daemon=True)
    thread.start()


@app.get("/")
def root():
    logger.info("Root endpoint requested")
    return {
        "service": "sentinel-api",
        "status": "running",
    }


@app.get("/health")
def health():
    return {
        "status": "healthy"
    }


@app.get("/db")
def database_check():
    connection = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        database=os.getenv("DB_NAME", "sentinel"),
        user=os.getenv("DB_USER", "sentinel"),
        password=os.getenv("DB_PASSWORD", "sentinel"),
    )

    connection.close()

    logger.info("Database connection successful")

    return {
        "database": "connected"
    }


def _redis_client():
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
    )


@app.get("/redis")
def redis_check():
    client = _redis_client()

    client.ping()

    logger.info("Redis connection successful")

    return {
        "redis": "connected"
    }


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    """Deliberately unhandled KeyError for any id outside the seed data -
    an application bug, distinct from the infra faults above."""
    logger.info("Looking up order %s", order_id)
    order = ORDERS[order_id]

    logger.info("Order %s found", order_id)
    return order


@app.get("/cache/counter")
def cache_counter():
    """Seeds a string key, then tries to INCR it - a Redis WRONGTYPE error,
    a data-shape bug distinct from a connection failure."""
    client = _redis_client()

    client.set("request_counter", "not-a-number")
    logger.info("Seeded request_counter cache key")

    value = client.incr("request_counter")

    return {"request_counter": value}


PAYMENT_GATEWAY_FAILURE_RATE = 0.4


@app.get("/payments/charge")
def charge_payment():
    """Simulates a flaky external payment gateway - fails
    ~PAYMENT_GATEWAY_FAILURE_RATE of the time with a timeout, distinct from
    every other fault class here."""
    logger.info("Contacting payment gateway")

    if random.random() < PAYMENT_GATEWAY_FAILURE_RATE:
        logger.error("payment gateway timeout after 5000ms - gateway=stripe-sim endpoint=/charge")
        raise TimeoutError("payment gateway did not respond within 5000ms")

    logger.info("Payment charged successfully")
    return {"status": "charged"}
