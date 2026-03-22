from decimal import Decimal
from typing import Dict

from pydantic import BaseModel, ConfigDict, Field


class ProductCreate(BaseModel):
    name: str = Field(..., min_length=2)
    is_active: bool = Field(default=True)


class ProductResponse(ProductCreate):
    id: int

    model_config = ConfigDict(from_attributes=True)


class VariantCreate(BaseModel):
    product_id: int
    price: Decimal = Field(..., gt=0)
    stock: int = Field(..., ge=0)
    attributes: Dict[str, str]


class VariantResponse(VariantCreate):
    id: int

    model_config = ConfigDict(from_attributes=True)
