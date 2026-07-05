# B2C E-Commerce Transaction & Fulfillment Engine

A B2C core transaction and multi-warehouse fulfillment backend built with FastAPI, PostgreSQL, and Redis. Focused on two hard problems: eliminating database deadlocks under concurrent checkout load, and guaranteeing zero financial loss during discount proration and partial refunds (tail-difference elimination).

---

## 🛠 Tech Stack

| Technology | Role |
| --- | --- |
| FastAPI | High-performance async Web framework (Python 3.12) |
| PostgreSQL | Primary relational DB for orders, inventory, and ledgers |
| Redis | In-memory cache for atomic idempotency keys and state control |
| SQLAlchemy (Async) | Async ORM (asyncpg) with strict `selectinload` eager loading |
| Alembic | Schema migration management |
| Celery | Async task queue for unpaid order lifecycle management |
| Vanilla JS / HTML | Zero-dependency white-box audit dashboard for real-time ledger tracking |

---

## 🏗 System Architecture & Critical Workflows

### 1. Concurrent Checkout Flow (Pessimistic Locking + Deterministic Ordering)

All inventory deductions execute within isolated database transactions using `SELECT ... FOR UPDATE`. Before acquiring any row lock, the engine **sorts all target `variant_id` values in ascending order**. This forces every concurrent transaction to request locks in the exact same sequence, natively eliminating AB-BA circular wait deadlocks at the database level without any retry loop overhead.

### 2. Financial Proration & Tail-Difference Elimination

When a lump-sum discount is distributed across multiple order items, standard division leaves indivisible fractional cents. `float` arithmetic is strictly prohibited. The engine uses Python `Decimal` with `ROUND_HALF_EVEN` (Banker's Rounding). Proration runs as follows: the first N-1 items receive their calculated share; the final item absorbs the exact remainder (`Total Amount - Sum of N-1 items`). The ledger balance is always exact down to the cent.

### 3. Idempotency Layer + Unidirectional State Machine

All state-mutating endpoints require a UUID `idempotency_key`. The engine performs a Redis `SETNX` with a 24-hour TTL before touching the database. Duplicate submissions from network retries are blocked before reaching the state machine or ORM layer.

Order state transitions are strictly unidirectional:

```
PENDING → PAID → PARTIALLY_REFUNDED → REFUNDED
                ↘ SPLIT (multi-warehouse sub-orders)
```

Any attempt to jump states illegally (e.g., refunding a `PENDING` order, or transitioning from `COMPLETED`) is rejected with `HTTP 409 Conflict` at the domain boundary.

---

## 🚀 Key Technical Highlights & Design Decisions

### 1. Deterministic Lock Ordering for Deadlock Prevention

**The Problem:** Under concurrent checkout load, two transactions buying overlapping product variants can acquire row locks in opposite orders, triggering AB-BA deadlocks. Standard retries add latency and don't eliminate the root cause.

**The Solution:** Before executing `SELECT ... FOR UPDATE`, all target variant IDs are sorted:

```python
# services.py — lock ordering before SELECT ... FOR UPDATE
variant_ids = sorted([item.variant_id for item in order_items])

result = await db.execute(
    select(ProductVariant)
    .where(ProductVariant.id.in_(variant_ids))
    .order_by(ProductVariant.id)   # deterministic lock order
    .with_for_update()
)
```

Every concurrent transaction acquires row locks in the same ascending order. Circular waits become structurally impossible.

---

### 2. Zero-Loss Proration & Tail-Difference Elimination

**The Problem:** Splitting a $10.00 discount across 3 items: `10.00 / 3 = 3.333...` — indivisible. Standard rounding distributes `3.33 + 3.33 + 3.33 = 9.99`, leaking $0.01 from the ledger.

**The Solution:**

```python
# services.py — tail-difference elimination
def prorate_order_discounts(items, total_discount):
    total_amount = sum(item["amount"] for item in items)
    accumulated = Decimal("0.00")

    for i, item in enumerate(items):
        if i < len(items) - 1:
            share = (item["amount"] / total_amount * total_discount).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_EVEN
            )
            accumulated += share
        else:
            # Last item absorbs the exact remainder — zero leakage
            share = total_discount - accumulated

        item["prorated_discount"] = share
        item["actual_paid_price"] = item["amount"] - share

    return items
```

`Σ(prorated_discount) == total_discount` is enforced exactly. No cent escapes.

---

### 3. Redis-Backed Idempotency & State Machine Guard

**The Problem:** Network instability causes clients to retry POST requests. A refund endpoint hit twice with the same payload would execute two database writes and refund twice the amount.

**The Solution:** The application layer performs an atomic `SETNX` before any database mutation:

```python
# router.py — idempotency guard
key = f"refund:{order_id}:{request.idempotency_key}"
is_new = await redis.set(key, "1", nx=True, ex=86400)  # 24h TTL

if not is_new:
    raise HTTPException(status_code=409, detail="Duplicate request: idempotency key already processed.")
```

Duplicate requests are blocked in the caching layer. The database and state machine never see them.

---

## 📁 Directory Structure

```
B2C/
├── app/
│   ├── core/
│   │   ├── database.py       # Async SQLAlchemy engine & session factory
│   │   ├── redis.py          # Redis client & AsyncRedis protocol stub
│   │   └── celery_app.py     # Celery worker configuration
│   ├── transaction/
│   │   ├── models.py         # Order, OrderItem ORM models
│   │   ├── schemas.py        # Pydantic request/response schemas
│   │   ├── router.py         # Checkout, refund, status endpoints
│   │   ├── services.py       # Core business logic (locking, proration)
│   │   └── tasks.py          # Celery tasks (unpaid order expiry)
│   ├── cart/
│   │   ├── router.py
│   │   └── services.py       # Redis Lua script cart mutations
│   └── catalog/              # Product variant data access
├── tests/
│   ├── conftest.py           # Async fixtures: test DB, FakeRedis, client
│   ├── test_cart.py
│   └── test_transaction_flow.py
├── alembic/                  # DB migration history
├── dashboard.html            # Vanilla JS audit dashboard
└── docker-compose.yml
```

---

## 📡 API Reference

| Method | Endpoint | Description |
| --- | --- | --- |
| `POST` | `/api/v1/transaction/orders/` | Create order. Requires `idempotency_key`. Triggers pessimistic lock + proration. |
| `PATCH` | `/api/v1/transaction/orders/{id}/status` | Advance order state. Illegal transitions return `409`. |
| `POST` | `/api/v1/transaction/orders/{id}/refund` | Partial or full refund. Runs tail-difference recalculation. Requires `idempotency_key`. |
| `GET` | `/api/v1/transaction/orders/` | Fetch all orders with full line-item financial snapshots. |

---

## 🖥 Audit Dashboard — Live Execution Log

The `dashboard.html` is a zero-dependency Vanilla JS console. No React, no Vue, no build step. It hits the backend APIs directly and renders raw ledger snapshots — primarily used to verify proration math and state machine transitions during development.

Sample session log (partial refund triggering tail-difference elimination):

```
[10:31:37 AM] [Seed] Seeding test data...
[10:31:37 AM] [SUCCESS] Seeded orders: 4, 5, 6
[10:31:37 AM] [Ledger] Syncing database...
[10:31:37 AM] [Ledger] Loaded 6 orders.
[10:33:35 AM] [Refund] Processing refund for order 6, item/variant: 8, qty: 2...
[10:33:35 AM] [Refund] Refunded: 66.66, status: PARTIALLY_REFUNDED
[10:33:35 AM] [Ledger] Syncing database...
[10:33:35 AM] [Ledger] Loaded 6 orders.
```

---

## 🧪 Running Tests

```bash
# Start dependencies
docker-compose up -d

# Run full test suite (14 integration tests)
pytest

# Expected output:
# tests/test_cart.py ..          [ 14%]
# tests/test_transaction_flow.py ............  [100%]
# 14 passed in 0.80s
```
