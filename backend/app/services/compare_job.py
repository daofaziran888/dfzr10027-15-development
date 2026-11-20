"""对比分析业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.attribution_batch import attribution_batch_service
from app.store import store

MODULE = "compare_job"
LIST_FIELDS = ["任务编号", "基准数据", "对比数据", "比对字段", "差异阈值", "对比周期", "差异行数", "处理结论"]
REQUIRED_FIELDS = ["任务编号", "基准数据", "对比数据"]
STATUS_ORDER = ["待执行", "对比中", "已完成", "差异显著"]
ACTION_RULES = {"执行对比": "对比中", "标记完成": "已完成", "标记差异": "差异显著"}
NEGATIVE_ACTIONS = []


class CompareJobService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("任务编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"对比任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于对比分析可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"对比任务已{action}"

    def attribution_window_diff(
        self, batch_id: str, baseline_window: str, comparison_window: str
    ) -> tuple[dict[str, Any] | None, str]:
        """基于同一批归因成交同步计算两个窗口口径的渠道模型差异。

        结果同时携带计算份额时使用的成交范围与未覆盖金额，让分析人员区分
        份额变化来自归因规则调整，还是进入对比的成交覆盖范围缩小。
        """
        comparison, message = attribution_batch_service.compare_windows(
            batch_id, baseline_window, comparison_window)
        if comparison is None:
            return None, message

        baseline = comparison["baseline"]
        target = comparison["comparison"]
        scope_note = {
            "batch_id": batch_id,
            "baseline_window": baseline["window"],
            "comparison_window": target["window"],
            "conversion_scope": {
                "order_count": baseline["scope"]["order_count"],
                "order_ids": baseline["scope"]["order_ids"],
                "total_amount": baseline["scope"]["total_amount"],
            },
            "scope_changed": (
                baseline["scope"]["total_amount"] != target["scope"]["total_amount"]
                or baseline["scope"]["order_count"] != target["scope"]["order_count"]
            ),
            "coverage": {
                baseline["window"]: {
                    model: {
                        "allocated": baseline["model_summaries"][model]["allocated"],
                        "uncovered": baseline["model_summaries"][model]["uncovered"],
                    }
                    for model in baseline["model_summaries"]
                },
                target["window"]: {
                    model: {
                        "allocated": target["model_summaries"][model]["allocated"],
                        "uncovered": target["model_summaries"][model]["uncovered"],
                    }
                    for model in target["model_summaries"]
                },
            },
        }
        diff_payload = {
            "diff": comparison,
            "scope_note": scope_note,
            "compare_rows": comparison["compare_rows"],
            "scope_rows": comparison["scope_rows"],
        }

        # 差异行数按份额确实发生变化的渠道-模型组合统计。
        diff_count = sum(1 for row in comparison["compare_rows"] if row["份额差异"] != 0.0)
        for row in store.rows(MODULE):
            if row.get("任务编号") == "COMP-ATTRIBUTION":
                row["差异行数"] = str(diff_count)
                row["处理结论"] = (
                    f"同批{baseline['scope']['order_count']}笔成交，"
                    f"未覆盖金额 {baseline['scope']['uncovered_amount']} -> "
                    f"{target['scope']['uncovered_amount']}，份额差异行 {diff_count} 条"
                )
                row["attribution_diff"] = diff_payload
        return diff_payload, "归因窗口模型差异已同步计算"
