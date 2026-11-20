"""归因批次服务：接收触点与成交数据批次，按窗口口径产出可对账、可复算的归因结果。

批次以 batch_id 幂等：同一批数据重复提交不会产生第二份结果，而是返回同一快照，
保证「重复提交原批也能核对同一范围」。快照只存原始成交/触点与各窗口计算结果，
窗口变更时在同一批成交上重新计算，成交范围本身不发生变化。
"""
from __future__ import annotations

from typing import Any

from app.services.attribution_engine import build_window_comparison, compute_attribution
from app.store import store

BATCH_TABLE = "_attribution_batches"


class AttributionBatchService:
    def list_batches(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(BATCH_TABLE)]

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_TABLE):
            if row.get("batch_id") == batch_id:
                return dict(row)
        return None

    def submit_batch(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """登记一批触点与成交数据；batch_id 已存在时直接回放原快照，不重复入账。"""
        batch_id = str(payload.get("batch_id") or "").strip()
        if not batch_id:
            return None, "缺少必填字段：batch_id"

        existing = self.get_batch(batch_id)
        if existing is not None:
            existing["resubmitted"] = True
            return existing, "批次已存在，返回同一归因快照"

        orders = payload.get("orders")
        touches = payload.get("touches")
        # 兼容旧管道风格：只给触点流水（含下单事件与贡献金额），由引擎自动拆分。
        if orders is None and isinstance(touches, list):
            orders, touches = _split_touch_stream(touches)
        if not isinstance(orders, list) or not isinstance(touches, list):
            return None, "缺少必填字段：orders、touches"

        default_window = payload.get("window")
        snapshot = {
            "batch_id": batch_id,
            "orders": orders,
            "touches": touches,
            "results": {},
            "comparisons": {},
        }
        if default_window is not None:
            snapshot["results"][str(default_window)] = compute_attribution(orders, touches, default_window)
        store.rows(BATCH_TABLE).append(snapshot)
        return self.get_batch(batch_id), "归因批次已登记"

    def compute_window(self, batch_id: str, window: str | None) -> tuple[dict[str, Any] | None, str]:
        """在指定窗口口径下计算批次；同一批次同一窗口只算一次并缓存结果。"""
        batch = self.get_batch(batch_id)
        if batch is None:
            return None, f"归因批次 {batch_id} 不存在"
        key = str(window) if window is not None else "不限"
        cached = batch["results"].get(key)
        if cached is not None:
            return cached, "命中批次窗口缓存，返回同一范围结果"
        result = compute_attribution(batch["orders"], batch["touches"], window)
        batch["results"][key] = result
        return dict(result), "窗口归因结果已计算"

    def compare_windows(
        self, batch_id: str, baseline_window: str, comparison_window: str
    ) -> tuple[dict[str, Any] | None, str]:
        """在同一批成交上对比两个窗口口径的渠道份额差异与未覆盖金额。"""
        batch = self.get_batch(batch_id)
        if batch is None:
            return None, f"归因批次 {batch_id} 不存在"
        key = f"{baseline_window}||{comparison_window}"
        cached = batch["comparisons"].get(key)
        if cached is not None:
            return cached, "命中批次窗口对比缓存，返回同一范围结果"
        comparison = build_window_comparison(
            batch["orders"], batch["touches"], baseline_window, comparison_window)
        # 同步缓存两个窗口的单窗口结果，保证对比与明细口径一致。
        batch["results"][str(baseline_window)] = comparison["baseline"]
        batch["results"][str(comparison_window)] = comparison["comparison"]
        batch["comparisons"][key] = comparison
        return dict(comparison), "窗口对比结果已计算"


def _split_touch_stream(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把「触点+下单」混合流水拆成订单与触点两组，下单事件携带成交金额与成交时间。"""
    orders: list[dict[str, Any]] = []
    touches: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        touch_type = str(row.get("触点类型") or row.get("channel") or "").strip()
        if touch_type in {"下单", "支付完成", "成交"}:
            orders.append({
                "order_id": row.get("order_id") or row.get("订单号") or f"ORDER-{index + 1}",
                "amount": row.get("贡献金额", row.get("amount", 0.0)),
                "conversion_time": row.get("touch_time") or row.get("成交时间"),
                "user_id": row.get("user_id") or row.get("用户ID"),
            })
        else:
            touches.append(dict(row))
    return orders, touches


attribution_batch_service = AttributionBatchService()
