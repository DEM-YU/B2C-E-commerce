"""
tests/test_transaction_flow.py
end-to-end tests for pricing, state machine, and refunds
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.catalog.models import ProductVariant
from app.transaction.models import Order, OrderItem, OrderStatus
from tests.conftest import create_paid_order_in_db


# ===========================================================================
# 1. order pricing and tail difference elimination
# ===========================================================================

class TestFinancialPrecisionOrder:
    """test pricing accuracy and tail difference rounding."""

    @pytest.mark.asyncio
    async def test_proration_tail_diff_elimination(
        self,
        client: AsyncClient,
        test_db: AsyncSession,
        setup_test_data: dict,
    ) -> None:
        """check that prorated discounts sum up exactly to total discount."""
        data = setup_test_data
        variant_yeg: ProductVariant = data["variant_yeg"]
        variant_yyc: ProductVariant = data["variant_yyc"]

        stock_before_yeg = variant_yeg.stock   # 100
        stock_before_yyc = variant_yyc.stock   # 50

        payload = {
            "user_id": 1001,
            "idempotency_key": str(uuid.uuid4()),
            "items": [
                {"variant_id": variant_yeg.id, "quantity": 1},
                {"variant_id": variant_yyc.id, "quantity": 1},
            ],
        }

        # note: create_order_transaction currently does not trigger discounts.
        # we test the pricing algorithm directly in unit test below,
        # and verify the full order flow via http here.
        response = await client.post("/api/v1/transaction/orders/", json=payload)

        assert response.status_code == 201, f"order creation failed: {response.text}"

        body = response.json()

        assert "items" in body
        assert len(body["items"]) == 2
        assert body["status"] == "PENDING"

        # check that sum(prorated_discount) equals total_discount
        total_discount_in_response = sum(
            Decimal(str(item["prorated_discount"])) for item in body["items"]
        )
        total_amount = Decimal(str(body["total_amount"]))
        raw_total = sum(
            Decimal(str(item["unit_price"])) * item["quantity"]
            for item in body["items"]
        )
        expected_total_discount = (raw_total - total_amount).quantize(
            Decimal("0.01")
        )

        assert total_discount_in_response == expected_total_discount, (
            f"tail diff mismatch: sum={total_discount_in_response} "
            f"!= total={expected_total_discount}"
        )

        # verify actual_paid_price = unit_price * qty - prorated_discount
        for item in body["items"]:
            unit_price = Decimal(str(item["unit_price"]))
            qty = item["quantity"]
            prorated = Decimal(str(item["prorated_discount"]))
            actual = Decimal(str(item["actual_paid_price"]))
            expected_actual = (unit_price * qty - prorated).quantize(
                Decimal("0.01")
            )
            assert actual == expected_actual, (
                f"variant_id={item['variant_id']} actual_paid_price mismatch: "
                f"{actual} != {expected_actual}"
            )

        # check db to make sure stock decreased by 1
        result_yeg = await test_db.execute(
            select(ProductVariant).where(ProductVariant.id == variant_yeg.id)
        )
        result_yyc = await test_db.execute(
            select(ProductVariant).where(ProductVariant.id == variant_yyc.id)
        )
        db_variant_yeg = result_yeg.scalar_one()
        db_variant_yyc = result_yyc.scalar_one()

        await test_db.refresh(db_variant_yeg)
        await test_db.refresh(db_variant_yyc)

        assert db_variant_yeg.stock == stock_before_yeg - 1, (
            f"yeg stock should be {stock_before_yeg - 1}, got {db_variant_yeg.stock}"
        )
        assert db_variant_yyc.stock == stock_before_yyc - 1, (
            f"yyc stock should be {stock_before_yyc - 1}, got {db_variant_yyc.stock}"
        )

    @pytest.mark.asyncio
    async def test_proration_algorithm_unit(self) -> None:
        """unit test for discount proration and tail difference rounding."""
        from app.transaction.services import prorate_order_discounts

        items = [
            {"amount": Decimal("33.33")},
            {"amount": Decimal("66.67")},
        ]
        total_discount = Decimal("10.00")

        result = prorate_order_discounts(items, total_discount)

        # first item: 33.33 / 100.00 * 10.00 = 3.33
        assert result[0]["prorated_discount"] == Decimal("3.33"), (
            f"first item discount error: {result[0]['prorated_discount']}"
        )
        # last item gets the remainder: 10.00 - 3.33 = 6.67
        assert result[1]["prorated_discount"] == Decimal("6.67"), (
            f"last item discount error: {result[1]['prorated_discount']}"
        )
        # check sum
        sigma = sum(item["prorated_discount"] for item in result)
        assert sigma == total_discount, (
            f"tail diff error: sum={sigma} != total={total_discount}"
        )
        # check actual paid price
        assert result[0]["actual_paid_price"] == Decimal("30.00")
        assert result[1]["actual_paid_price"] == Decimal("60.00")


# ===========================================================================
# 2. state machine transition checks
# ===========================================================================

class TestStateMachineDefense:
    """test order status transitions."""

    @pytest.mark.asyncio
    async def test_illegal_transition_pending_to_shipped_returns_409(
        self,
        client: AsyncClient,
        test_db: AsyncSession,
        setup_test_data: dict,
    ) -> None:
        """pending -> shipped is not allowed directly, should return 409."""
        data = setup_test_data
        variant_yeg = data["variant_yeg"]

        # step 1: create pending order
        create_payload = {
            "user_id": 2001,
            "idempotency_key": str(uuid.uuid4()),
            "items": [{"variant_id": variant_yeg.id, "quantity": 1}],
        }
        create_resp = await client.post(
            "/api/v1/transaction/orders/", json=create_payload
        )
        assert create_resp.status_code == 201
        order_id = create_resp.json()["id"]
        assert create_resp.json()["status"] == "PENDING"

        # step 2: try illegal transition pending -> shipped
        patch_resp = await client.patch(
            f"/api/v1/transaction/orders/{order_id}/status",
            params={"target_status": "SHIPPED"},
        )

        assert patch_resp.status_code == 409, (
            f"illegal transition not blocked, got {patch_resp.status_code}: {patch_resp.text}"
        )

        error_detail = patch_resp.json().get("detail", "")
        assert "PENDING" in error_detail or "Illegal" in error_detail or "illegal" in error_detail, (
            f"missing error detail: {error_detail}"
        )

        # verify order status is still pending in db
        result = await test_db.execute(
            select(Order).where(Order.id == order_id)
        )
        order_in_db = result.scalar_one()
        await test_db.refresh(order_in_db)
        assert order_in_db.status == OrderStatus.PENDING, (
            f"status changed in db unexpectedly: {order_in_db.status}"
        )

    @pytest.mark.asyncio
    async def test_legal_transition_pending_to_paid_succeeds(
        self,
        client: AsyncClient,
        test_db: AsyncSession,
        setup_test_data: dict,
    ) -> None:
        """pending -> paid should succeed (http 200)."""
        data = setup_test_data
        variant_yeg = data["variant_yeg"]

        create_payload = {
            "user_id": 2002,
            "idempotency_key": str(uuid.uuid4()),
            "items": [{"variant_id": variant_yeg.id, "quantity": 1}],
        }
        create_resp = await client.post(
            "/api/v1/transaction/orders/", json=create_payload
        )
        assert create_resp.status_code == 201
        order_id = create_resp.json()["id"]

        # transition pending -> paid
        patch_resp = await client.patch(
            f"/api/v1/transaction/orders/{order_id}/status",
            params={"target_status": "PAID"},
        )

        assert patch_resp.status_code == 200, f"transition failed: {patch_resp.text}"
        assert patch_resp.json()["status"] == "PAID"

    @pytest.mark.asyncio
    async def test_illegal_transition_completed_to_any_returns_409(
        self,
        client: AsyncClient,
        test_db: AsyncSession,
        setup_test_data: dict,
    ) -> None:
        """completed orders cannot transition to any other status."""
        data = setup_test_data
        variant_yeg = data["variant_yeg"]

        # create completed order directly in db
        order = Order(
            user_id=2003,
            status=OrderStatus.COMPLETED,
            total_amount=Decimal("33.33"),
            idempotency_key=str(uuid.uuid4()),
        )
        test_db.add(order)
        await test_db.commit()
        await test_db.refresh(order)

        # try transitioning to refunding
        patch_resp = await client.patch(
            f"/api/v1/transaction/orders/{order.id}/status",
            params={"target_status": "REFUNDING"},
        )

        assert patch_resp.status_code == 409, (
            f"transition from completed should fail: {patch_resp.text}"
        )


# ===========================================================================
# 3. refund logic tests
# ===========================================================================

class TestPartialRefundFinancialDefense:
    """test partial refunds and stock recovery."""

    @pytest.mark.asyncio
    async def test_refund_amount_equals_actual_paid_price_snapshot(
        self,
        client: AsyncClient,
        test_db: AsyncSession,
        setup_test_data: dict,
        fake_redis,
    ) -> None:
        """full refund should match actual_paid_price and restore stock."""
        data = setup_test_data
        variant_yeg: ProductVariant = data["variant_yeg"]
        stock_before = variant_yeg.stock   # 100

        # create paid order directly in db
        order, item = await create_paid_order_in_db(
            test_db, user_id=3001, variant=variant_yeg, quantity=1
        )

        expected_refund = item.actual_paid_price   # 33.33
        assert expected_refund == Decimal("33.33"), (
            f"bad test setup: actual_paid_price={expected_refund}"
        )

        refund_payload = {
            "idempotency_key": str(uuid.uuid4()),
            "items": [
                {
                    "order_item_id": item.id,
                    "refund_qty": 1,
                }
            ],
        }

        response = await client.post(
            f"/api/v1/transaction/orders/{order.id}/refund",
            json=refund_payload,
        )

        assert response.status_code == 200, f"refund failed: {response.text}"

        body = response.json()

        # check refund amount
        actual_refunded = Decimal(str(body["refunded_amount"]))
        assert actual_refunded == expected_refund, (
            f"refund mismatch: got {actual_refunded}, expected {expected_refund}"
        )

        # check status changed to refunded
        assert body["new_status"] == "REFUNDED", (
            f"status should be REFUNDED, got {body['new_status']}"
        )

        # check stock recovered in db
        result = await test_db.execute(
            select(ProductVariant).where(ProductVariant.id == variant_yeg.id)
        )
        db_variant = result.scalar_one()
        await test_db.refresh(db_variant)

        assert db_variant.stock == stock_before + 1, (
            f"stock recovery failed: expected {stock_before + 1}, got {db_variant.stock}"
        )

    @pytest.mark.asyncio
    async def test_partial_refund_does_not_exceed_actual_paid_price(
        self,
        client: AsyncClient,
        test_db: AsyncSession,
        setup_test_data: dict,
    ) -> None:
        """refunding 1 out of 2 items should refund half the price."""
        data = setup_test_data
        variant_yyc: ProductVariant = data["variant_yyc"]

        # buy 2 items: actual_paid_price = 66.67 * 2 = 133.34
        order, item = await create_paid_order_in_db(
            test_db, user_id=3002, variant=variant_yyc, quantity=2
        )

        assert item.actual_paid_price == Decimal("133.34"), (
            f"bad test setup: actual_paid_price={item.actual_paid_price}"
        )

        # refund 1 item: expected = 133.34 / 2 = 66.67
        expected_refund_per_item = (
            Decimal("1") / Decimal("2") * item.actual_paid_price
        ).quantize(Decimal("0.01"))  # 66.67

        refund_payload = {
            "idempotency_key": str(uuid.uuid4()),
            "items": [{"order_item_id": item.id, "refund_qty": 1}],
        }

        response = await client.post(
            f"/api/v1/transaction/orders/{order.id}/refund",
            json=refund_payload,
        )

        assert response.status_code == 200, f"refund failed: {response.text}"

        body = response.json()
        actual_refunded = Decimal(str(body["refunded_amount"]))

        assert actual_refunded == expected_refund_per_item, (
            f"partial refund error: {actual_refunded} != {expected_refund_per_item}"
        )
        assert body["new_status"] == "PARTIALLY_REFUNDED", (
            f"status should be PARTIALLY_REFUNDED, got {body['new_status']}"
        )

    @pytest.mark.asyncio
    async def test_over_refund_is_blocked_422(
        self,
        client: AsyncClient,
        test_db: AsyncSession,
        setup_test_data: dict,
    ) -> None:
        """refunding more items than bought should return 422."""
        data = setup_test_data
        variant_yeg = data["variant_yeg"]

        order, item = await create_paid_order_in_db(
            test_db, user_id=3003, variant=variant_yeg, quantity=1
        )

        # try to refund 2 items when only 1 was bought
        refund_payload = {
            "idempotency_key": str(uuid.uuid4()),
            "items": [{"order_item_id": item.id, "refund_qty": 2}],
        }

        response = await client.post(
            f"/api/v1/transaction/orders/{order.id}/refund",
            json=refund_payload,
        )

        assert response.status_code == 422, (
            f"over refund not blocked, got {response.status_code}: {response.text}"
        )

    @pytest.mark.asyncio
    async def test_refund_idempotency_blocks_duplicate_409(
        self,
        client: AsyncClient,
        test_db: AsyncSession,
        setup_test_data: dict,
    ) -> None:
        """duplicate refund request with same idempotency key returns 409."""
        data = setup_test_data
        variant_yeg = data["variant_yeg"]

        order, item = await create_paid_order_in_db(
            test_db, user_id=3004, variant=variant_yeg, quantity=2
        )

        idem_key = str(uuid.uuid4())
        refund_payload = {
            "idempotency_key": idem_key,
            "items": [{"order_item_id": item.id, "refund_qty": 1}],
        }

        # first refund should succeed
        resp1 = await client.post(
            f"/api/v1/transaction/orders/{order.id}/refund",
            json=refund_payload,
        )
        assert resp1.status_code == 200, f"first refund failed: {resp1.text}"

        # second refund with same idempotency key should fail
        resp2 = await client.post(
            f"/api/v1/transaction/orders/{order.id}/refund",
            json=refund_payload,
        )

        assert resp2.status_code == 409, (
            f"idempotency check failed, got {resp2.status_code}: {resp2.text}"
        )
        assert "idempotency" in resp2.json().get("detail", "").lower() or \
               "already" in resp2.json().get("detail", "").lower(), (
            f"missing idempotency error detail: {resp2.json()}"
        )

    @pytest.mark.asyncio
    async def test_refund_non_refundable_status_blocked_409(
        self,
        client: AsyncClient,
        test_db: AsyncSession,
        setup_test_data: dict,
    ) -> None:
        """cannot refund an order that is in PENDING status."""
        data = setup_test_data
        variant_yeg = data["variant_yeg"]

        order = Order(
            user_id=3005,
            status=OrderStatus.PENDING,
            total_amount=Decimal("33.33"),
            idempotency_key=str(uuid.uuid4()),
        )
        test_db.add(order)
        await test_db.flush()

        item = OrderItem(
            order_id=order.id,
            variant_id=variant_yeg.id,
            unit_price=Decimal("33.33"),
            quantity=1,
            prorated_discount=Decimal("0.00"),
            actual_paid_price=Decimal("33.33"),
            refunded_quantity=0,
            refunded_amount=Decimal("0.00"),
            is_fully_refunded=False,
        )
        test_db.add(item)
        await test_db.commit()
        await test_db.refresh(order)
        await test_db.refresh(item)

        refund_payload = {
            "idempotency_key": str(uuid.uuid4()),
            "items": [{"order_item_id": item.id, "refund_qty": 1}],
        }

        response = await client.post(
            f"/api/v1/transaction/orders/{order.id}/refund",
            json=refund_payload,
        )

        assert response.status_code == 409, (
            f"refund on PENDING order not blocked, got {response.status_code}: {response.text}"
        )


# ===========================================================================
# 4. order creation idempotency
# ===========================================================================

class TestOrderIdempotency:
    """test order creation idempotency key checks."""

    @pytest.mark.asyncio
    async def test_duplicate_order_returns_409(
        self,
        client: AsyncClient,
        setup_test_data: dict,
    ) -> None:
        """submitting same order twice with same idempotency key returns 409."""
        data = setup_test_data
        variant_yeg = data["variant_yeg"]
        idem_key = str(uuid.uuid4())

        payload = {
            "user_id": 4001,
            "idempotency_key": idem_key,
            "items": [{"variant_id": variant_yeg.id, "quantity": 1}],
        }

        # first order: success
        resp1 = await client.post("/api/v1/transaction/orders/", json=payload)
        assert resp1.status_code == 201, f"first order failed: {resp1.text}"

        # duplicate order: blocked
        resp2 = await client.post("/api/v1/transaction/orders/", json=payload)
        assert resp2.status_code == 409, (
            f"duplicate order not blocked, got {resp2.status_code}: {resp2.text}"
        )

    @pytest.mark.asyncio
    async def test_out_of_stock_returns_error(
        self,
        client: AsyncClient,
        test_db: AsyncSession,
        setup_test_data: dict,
    ) -> None:
        """ordering an out of stock item should return 400 or 409."""
        data = setup_test_data
        variant_yeg: ProductVariant = data["variant_yeg"]

        # set stock to 0
        await test_db.execute(
            text("UPDATE product_variant SET stock = 0 WHERE id = :vid"),
            {"vid": variant_yeg.id},
        )
        await test_db.commit()

        payload = {
            "user_id": 4002,
            "idempotency_key": str(uuid.uuid4()),
            "items": [{"variant_id": variant_yeg.id, "quantity": 1}],
        }

        response = await client.post("/api/v1/transaction/orders/", json=payload)

        assert response.status_code in (400, 409), (
            f"out of stock order not blocked, got {response.status_code}: {response.text}"
        )
