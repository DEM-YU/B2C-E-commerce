import enum
from decimal import Decimal

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class OrderStatus(enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    SHIPPED = "SHIPPED"
    REFUNDING = "REFUNDING"
    COMPLETED = "COMPLETED"
    REFUNDED = "REFUNDED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    SPLIT = "SPLIT"
    CANCELLED = "CANCELLED"


class Order(Base):
    __tablename__ = "order"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"),
        nullable=False,
        default=OrderStatus.PENDING,
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )

    parent_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("order.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
        default=None,
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    children: Mapped[list["Order"]] = relationship(
        "Order",
        foreign_keys=[parent_id],
        back_populates="parent",
        lazy="select",
    )
    parent: Mapped["Order | None"] = relationship(
        "Order",
        foreign_keys=[parent_id],
        back_populates="children",
        remote_side="Order.id",
        lazy="select",
    )


class OrderItem(Base):
    __tablename__ = "order_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("order.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("product_variant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    prorated_discount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    actual_paid_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )

    refunded_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    refunded_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    is_fully_refunded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    order: Mapped["Order"] = relationship(back_populates="items")
    variant: Mapped["ProductVariant"] = relationship(  # type: ignore[name-defined]
        "ProductVariant",
        foreign_keys=[variant_id],
        lazy="select",
    )

    @property
    def warehouse_code(self) -> str | None:
        return self.variant.warehouse_code if self.variant else None
