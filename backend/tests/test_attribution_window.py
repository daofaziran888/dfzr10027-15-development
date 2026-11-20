"""归因窗口口径验收：同一批成交在窗口变更前后的分配、未覆盖与对账校验。

场景（来自需求）：
- 订单 ORD-100：成交 100 元，全部触点都落在窄窗口之外（宽窗口内仍有合格触点）；
- 订单 ORD-400：成交 400 元，含两个合格触点（搜索广告、推荐流）。
两笔成交合计 500 元；每个模型在任一窗口口径下都必须满足
「已分配金额 + 未覆盖金额 == 500 元」，重复提交原批返回同一范围结果。
"""
from __future__ import annotations

from app.services.attribution_batch import attribution_batch_service
from app.services.attribution_engine import (
    MODELS,
    build_window_comparison,
    compute_attribution,
    parse_window,
)
from app.services.compare_job import CompareJobService
from app.services.pipeline_orchestrator import run_attribution_compare_pipeline
from app.store import reset_store, store


def _build_batch() -> dict:
    """构造 100 元（触点在窄窗口外）+ 400 元（两个合格触点）的同一批成交。"""
    return {
        "batch_id": "BATCH-500",
        "window": "30天",
        "orders": [
            {"order_id": "ORD-100", "amount": 100.0, "conversion_time": "2026-09-30 10:00"},
            {"order_id": "ORD-400", "amount": 400.0, "conversion_time": "2026-09-30 18:00"},
        ],
        "touches": [
            # ORD-100：触于 20 天前，30 天窗口内合格、7 天窗口外不合格
            {"order_id": "ORD-100", "channel": "社交分享", "touch_time": "2026-09-10 09:00", "touch_id": "T1"},
            # ORD-400：两个触点都在成交前 7 天内，任何窗口都合格
            {"order_id": "ORD-400", "channel": "搜索广告", "touch_time": "2026-09-28 09:00", "touch_id": "T2"},
            {"order_id": "ORD-400", "channel": "推荐流", "touch_time": "2026-09-29 15:00", "touch_id": "T3"},
        ],
    }


class TestWindowParsing:
    def test_business_window_config(self):
        from datetime import timedelta

        assert parse_window("7天") == timedelta(days=7)
        assert parse_window("30日") == timedelta(days=30)
        assert parse_window("24小时") == timedelta(hours=24)
        assert parse_window("1天12小时") == timedelta(days=1, hours=12)
        assert parse_window("不限") is None
        assert parse_window(None) is None


class TestWindowedAttribution:
    def setup_method(self):
        reset_store()
        self.batch = _build_batch()

    def test_wide_window_covers_both_orders(self):
        result = compute_attribution(self.batch["orders"], self.batch["touches"], "30天")
        scope = result["scope"]
        # 成交范围：同一批 2 笔订单、总额 500 元，窗口变化不改变成交范围
        assert scope["order_count"] == 2
        assert scope["total_amount"] == 500.0
        assert scope["covered_order_count"] == 2
        assert scope["uncovered_amount"] == 0.0

        # ORD-400 两个合格触点：首次归搜索广告、末次归推荐流、线性各 200
        assert result["models"]["first_touch"] == {"搜索广告": 400.0, "社交分享": 100.0}
        assert result["models"]["last_touch"] == {"推荐流": 400.0, "社交分享": 100.0}
        assert result["models"]["linear"] == {
            "搜索广告": 200.0, "推荐流": 200.0, "社交分享": 100.0,
        }
        for model in MODELS:
            assert result["reconciliation"][model]["balanced"] is True
            assert result["model_summaries"][model]["allocated"] == 500.0
            assert result["model_summaries"][model]["uncovered"] == 0.0

    def test_narrow_window_loses_all_touches_for_small_order(self):
        result = compute_attribution(self.batch["orders"], self.batch["touches"], "7天")
        scope = result["scope"]
        # 成交范围仍是同一批 500 元，但 ORD-100 失去全部候选触点
        assert scope["order_count"] == 2
        assert scope["total_amount"] == 500.0
        assert scope["uncovered_order_ids"] == ["ORD-100"]
        assert scope["uncovered_amount"] == 100.0
        assert scope["covered_amount"] == 400.0

        # ORD-400 仍能分配给渠道；ORD-100 的 100 元三个模型都保留为未归因
        assert result["models"]["first_touch"] == {"搜索广告": 400.0}
        assert result["models"]["last_touch"] == {"推荐流": 400.0}
        assert result["models"]["linear"] == {"搜索广告": 200.0, "推荐流": 200.0}

        # 对账：每个模型 分配金额 + 未覆盖金额 == 500
        for model in MODELS:
            summary = result["model_summaries"][model]
            assert summary["allocated"] == 400.0
            assert summary["uncovered"] == 100.0
            assert summary["allocated"] + summary["uncovered"] == 500.0
            assert result["reconciliation"][model]["balanced"] is True

        # 订单级审计保留了未归因转化，可逐笔对账
        order_100 = next(row for row in result["orders"] if row["order_id"] == "ORD-100")
        assert order_100["candidate_count"] == 0
        for model in MODELS:
            assert order_100["models"][model]["uncovered"] == 100.0

    def test_touch_after_conversion_never_qualified(self):
        orders = [{"order_id": "O1", "amount": 50.0, "conversion_time": "2026-09-30 10:00"}]
        touches = [
            {"order_id": "O1", "channel": "直播", "touch_time": "2026-09-30 12:00", "touch_id": "T9"},
        ]
        result = compute_attribution(orders, touches, "30天")
        assert result["scope"]["uncovered_amount"] == 50.0
        for model in MODELS:
            assert result["model_summaries"][model]["allocated"] == 0.0
            assert result["model_summaries"][model]["uncovered"] == 50.0

    def test_window_comparison_keeps_scope_and_shows_coverage_change(self):
        comparison = build_window_comparison(
            self.batch["orders"], self.batch["touches"], "30天", "7天")
        baseline = comparison["baseline"]
        target = comparison["comparison"]
        # 两窗口的成交范围完全相同，份额变化可与范围变化区分
        assert baseline["scope"]["total_amount"] == target["scope"]["total_amount"] == 500.0
        assert baseline["scope"]["order_ids"] == target["scope"]["order_ids"]
        # 覆盖范围缩小：未覆盖金额 0 -> 100
        assert baseline["scope"]["uncovered_amount"] == 0.0
        assert target["scope"]["uncovered_amount"] == 100.0
        # 渠道份额差异行同时携带两边的未覆盖金额
        social_rows = [row for row in comparison["compare_rows"] if row["触点类型"] == "社交分享"]
        assert social_rows
        for row in social_rows:
            assert row["基准未覆盖金额"] == 0.0
            assert row["对比未覆盖金额"] == 100.0
        scope_rows = comparison["scope_rows"]
        assert {row["窗口"] for row in scope_rows} == {"30天", "7天"}
        for row in scope_rows:
            assert row["成交总额"] == 500.0
            assert row["覆盖金额"] + row["未覆盖金额"] == 500.0


class TestBatchSubmission:
    def setup_method(self):
        reset_store()

    def test_idempotent_resubmit_same_scope(self):
        payload = _build_batch()
        first, msg1 = attribution_batch_service.submit_batch(payload)
        assert first is not None and "已登记" in msg1
        # 重复提交原批：不产生第二份数据，返回同一范围快照
        second, msg2 = attribution_batch_service.submit_batch(payload)
        assert second is not None
        assert "同一归因快照" in msg2
        assert second["batch_id"] == first["batch_id"]
        assert len(attribution_batch_service.list_batches()) == 1
        wide = second["results"]["30天"]
        assert wide["scope"]["total_amount"] == 500.0

    def test_window_change_recomputes_same_batch(self):
        attribution_batch_service.submit_batch(_build_batch())
        wide, _ = attribution_batch_service.compute_window("BATCH-500", "30天")
        narrow, _ = attribution_batch_service.compute_window("BATCH-500", "7天")
        # 同批 500 元：宽窗口全覆盖、窄窗口留 100 元未归因，两口径各自对账平衡
        assert wide["scope"]["total_amount"] == narrow["scope"]["total_amount"] == 500.0
        assert wide["scope"]["uncovered_amount"] == 0.0
        assert narrow["scope"]["uncovered_amount"] == 100.0
        for result in (wide, narrow):
            for model in MODELS:
                summary = result["model_summaries"][model]
                assert summary["allocated"] + summary["uncovered"] == 500.0
        # 再次取同一窗口命中缓存，数值完全一致
        again, message = attribution_batch_service.compute_window("BATCH-500", "7天")
        assert "缓存" in message
        assert again["models"] == narrow["models"]
        assert again["scope"] == narrow["scope"]

    def test_legacy_touch_stream_supported(self):
        # 兼容旧版「触点+下单混合流水」提交方式
        payload = {
            "batch_id": "BATCH-LEGACY",
            "touches": [
                {"order_id": "U1", "触点类型": "搜索广告", "touch_time": "2026-09-28 09:00", "贡献金额": 0.0},
                {"order_id": "U1", "触点类型": "下单", "touch_time": "2026-09-30 10:00", "贡献金额": 320.0},
            ],
        }
        batch, message = attribution_batch_service.submit_batch(payload)
        assert batch is not None, message
        assert batch["orders"][0]["amount"] == 320.0
        result, _ = attribution_batch_service.compute_window("BATCH-LEGACY", "7天")
        assert result["scope"]["total_amount"] == 320.0
        assert result["models"]["first_touch"] == {"搜索广告": 320.0}


class TestCompareJobDiff:
    def setup_method(self):
        reset_store()
        attribution_batch_service.submit_batch(_build_batch())
        self.service = CompareJobService()

    def test_diff_payload_carries_scope_and_uncovered(self):
        payload, message = self.service.attribution_window_diff("BATCH-500", "30天", "7天")
        assert payload is not None
        assert "同步计算" in message
        note = payload["scope_note"]
        # 结果上给出计算份额用到的转化范围
        assert note["conversion_scope"]["order_count"] == 2
        assert note["conversion_scope"]["total_amount"] == 500.0
        assert note["conversion_scope"]["order_ids"] == ["ORD-100", "ORD-400"]
        # 同批成交范围没有缩小，缩小的只是窗口覆盖
        assert note["scope_changed"] is False
        for window in ("30天", "7天"):
            for model in MODELS:
                coverage = note["coverage"][window][model]
                assert coverage["allocated"] + coverage["uncovered"] == 500.0
        assert note["coverage"]["30天"]["first_touch"]["uncovered"] == 0.0
        assert note["coverage"]["7天"]["first_touch"]["uncovered"] == 100.0

        # 对比任务行同步落了差异行数与未覆盖金额结论
        comp_row = next(row for row in store.rows("compare_job") if row["任务编号"] == "COMP-ATTRIBUTION")
        assert comp_row["差异行数"] != "待计算"
        assert "未覆盖金额 0.0 -> 100.0" in comp_row["处理结论"]

    def test_unknown_batch_rejected(self):
        payload, message = self.service.attribution_window_diff("MISSING", "30天", "7天")
        assert payload is None
        assert "不存在" in message


class TestPipelineIntegration:
    def setup_method(self):
        reset_store()

    def test_pipeline_uses_configured_window_and_reconciles(self):
        result = run_attribution_compare_pipeline("ATTR-001")
        # ATTR-001 业务配置窗口为 7 天，触点都在成交当天，全部覆盖
        assert result["window"] == "7天"
        assert result["scope"]["total_amount"] == 320.0
        for model in MODELS:
            assert result["totals"][model] + result["uncovered"][model] == 320.0
            assert result["reconciliation"][model]["balanced"] is True
        # 旧契约字段保留
        assert result["compare_rows"]
        assert {"触点类型", "首次归因", "末次归因", "差异"} <= set(result["compare_rows"][0])

    def test_pipeline_window_override_can_uncover_orders(self):
        # ATTR-002 触点在成交前 11.5 小时 ~ 2.5 小时：3 小时窗口只剩直播一个合格触点
        result = run_attribution_compare_pipeline("ATTR-002", "3小时")
        assert result["scope"]["total_amount"] == 580.0
        assert result["models"]["first_touch"] == {"直播": 580.0}
        assert result["models"]["last_touch"] == {"直播": 580.0}
        assert result["models"]["linear"] == {"直播": 580.0}
        for model in MODELS:
            assert result["totals"][model] + result["uncovered"][model] == 580.0
