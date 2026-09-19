"""FastAPI application exposing the guillotine cutting plan endpoint."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .models import PlanRequest, PlanResponse
from .packer import UnfitItemError, build_plan

app = FastAPI(
    title="Guillotine Cutting Plan API",
    version="1.0.0",
    description=(
        "面向小型印刷店的裁纸排版服务：给定原纸尺寸、切缝宽度和成品清单，"
        "按可旋转的铡刀式（每刀贯穿当前子矩形）规则生成多张原纸的排版方案。"
    ),
)


@app.get("/")
def root() -> dict:
    return {
        "service": "guillotine-cut-planner",
        "docs": "/docs",
        "health": "/health",
        "plans": "POST /plans",
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/plans", response_model=PlanResponse)
def create_plan(request: PlanRequest) -> dict:
    items = []
    seen_ids = set()
    for position, item in enumerate(request.items, start=1):
        item_id = item.id or f"item-{position}"
        if item_id in seen_ids:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"duplicate item id '{item_id}': please assign explicit "
                    "unique ids to every item"
                ),
            )
        seen_ids.add(item_id)
        items.append((item_id, item.width, item.height, item.quantity))

    try:
        return build_plan(
            stock_width=request.stock.width,
            stock_height=request.stock.height,
            kerf=request.kerf,
            items=items,
        )
    except UnfitItemError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
