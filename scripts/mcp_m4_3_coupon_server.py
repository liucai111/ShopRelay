"""Isolated M4.3-A MCP server for the existing coupon catalog reader.

The probe applies Alembic and adds its explicitly marked catalog fixture before
launching this server. The server itself never creates or writes business rows.
"""
from __future__ import annotations

import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.m3.policy_tools import CouponReadRepository


engine = create_engine(os.environ["SHOPRELAY_MCP_COUPON_DB_URL"], pool_pre_ping=True)
repository = CouponReadRepository(sessionmaker(engine, expire_on_commit=False))
server = MCPServer("ShopRelay M4.3-A isolated coupon catalog reader", version="1.0.0")


class CouponRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_id: str
    version: str
    min_spend_cents: int


class CouponResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    availability: str
    coupon_code: str | None = None
    coupon: CouponRecord | None = None
    eligibility_status: str
    source_type: str | None = None
    unknown_reasons: list[str] = []
    error_code: str | None = None


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False), structured_output=True)
def get_coupon(coupon_code: str) -> CouponResult:
    """Read coupon catalog metadata, never user entitlement or transaction state."""
    return CouponResult.model_validate(repository.get_coupon(coupon_code))


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "http":
        server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[2]), streamable_http_path="/mcp")
    elif mode == "stdio":
        server.run("stdio")
    else:
        raise SystemExit("mode must be stdio or http")
