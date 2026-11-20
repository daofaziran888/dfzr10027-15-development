"""归因计算引擎：按业务配置的归因窗口对触点与成交数据做首次/末次/线性归因。

口径约定：
- 每笔订单只把贡献分给「成交之前、且落在归因窗口内」的有效触点；
  成交动作本身（如下单事件）不作为可分配触点。
- 窗口内没有任何合格触点的订单不丢弃，金额原样保留为「未归因转化」，
  保证 各模型已分配金额 + 未覆盖金额 == 成交总额，可与账务对账。
- 同一批成交用不同窗口口径重复计算时，成交范围（订单与金额）保持不变，
  变化只体现在覆盖范围与渠道份额上，便于区分「规则变化」与「范围缩小」。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

# 字段别名：接口入参允许业务字段名或通用英文字段名。
_ORDER_ID_KEYS = ("订单号", "order_id", "订单编号")
_AMOUNT_KEYS = ("金额", "amount", "贡献金额", "order_amount")
_ORDER_TIME_KEYS = ("成交时间", "支付时间", "conversion_time", "order_time", "touch_time")
_TOUCH_ID_KEYS = ("触点ID", "touch_id", "触点序号")
_TOUCH_TYPE_KEYS = ("触点类型", "channel", "渠道")
_TOUCH_TIME_KEYS = ("touch_time", "触点时间", "发生时间")
_TOUCH_ORDER_KEYS = ("订单号", "order_id", "订单编号")
_USER_KEYS = ("user_id", "用户ID", "用户编号")
_CONVERSION_MARKERS = {"下单", "支付完成", "成交", "conversion", "order"}

MODEL_FIRST = "first_touch"
MODEL_LAST = "last_touch"
MODEL_LINEAR = "linear"
MODELS = (MODEL_FIRST, MODEL_LAST, MODEL_LINEAR)
MODEL_LABELS = {MODEL_FIRST: "首次归因", MODEL_LAST: "末次归因", MODEL_LINEAR: "线性归因"}

_WINDOW_PATTERN = re.compile(r"(\d+)\s*(天|日|小时|分钟)")
_DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


def _round2(value: float) -> float:
    """金额统一保留两位小数，避免浮点尾差破坏对账等式。"""
    return round(float(value) + 1e-9, 2)


def parse_window(window: str | int | timedelta | None) -> timedelta | None:
    """把业务配置的窗口口径解析为 timedelta。

    支持 "7天"、"30日"、"24小时"、"30分钟" 及它们的组合（如 "1天12小时"）；
    空值、0 或 "不限/无限制" 表示不限制时间，只保留「成交之前」这一约束。
    """
    if window is None:
        return None
    if isinstance(window, timedelta):
        return window if window > timedelta(0) else None
    if isinstance(window, int):
        return timedelta(days=window) if window > 0 else None
    text = str(window).strip()
    if not text or text in {"不限", "无限制", "无", "none", "None"}:
        return None
    total = timedelta(0)
    for number, unit in _WINDOW_PATTERN.findall(text):
        value = int(number)
        if unit in {"天", "日"}:
            total += timedelta(days=value)
        elif unit == "小时":
            total += timedelta(hours=value)
        else:
            total += timedelta(minutes=value)
    return total if total > timedelta(0) else None


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_conversion_marker(row: dict[str, Any]) -> bool:
    touch_type = str(_pick(row, _TOUCH_TYPE_KEYS) or "").strip()
    return touch_type in _CONVERSION_MARKERS


def normalize_orders(raw_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """标准化成交订单，解析失败的时间与非正金额会被显式剔除并计数。"""
    orders: list[dict[str, Any]] = []
    for index, row in enumerate(raw_orders):
        order_id = _pick(row, _ORDER_ID_KEYS) or f"ORDER-{index + 1}"
        amount = _to_float(_pick(row, _AMOUNT_KEYS))
        conv_time = _parse_dt(_pick(row, _ORDER_TIME_KEYS))
        if amount <= 0 or conv_time is None:
            continue
        orders.append({
            "order_id": str(order_id),
            "amount": _round2(amount),
            "conversion_time": conv_time,
            "user_id": str(_pick(row, _USER_KEYS) or ""),
        })
    return orders


def normalize_touches(raw_touches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """标准化触点；成交动作本身不是可分配触点，解析失败的时间直接剔除。"""
    touches: list[dict[str, Any]] = []
    for index, row in enumerate(raw_touches):
        if _is_conversion_marker(row):
            continue
        touch_time = _parse_dt(_pick(row, _TOUCH_TIME_KEYS))
        touch_type = _pick(row, _TOUCH_TYPE_KEYS)
        if touch_time is None or touch_type in (None, ""):
            continue
        touches.append({
            "touch_id": str(_pick(row, _TOUCH_ID_KEYS) or f"TOUCH-{index + 1}"),
            "order_id": str(_pick(row, _TOUCH_ORDER_KEYS) or ""),
            "channel": str(touch_type).strip(),
            "touch_time": touch_time,
            "user_id": str(_pick(row, _USER_KEYS) or ""),
        })
    return touches


def _candidate_touches(
    order: dict[str, Any], touches: list[dict[str, Any]], window: timedelta | None
) -> list[dict[str, Any]]:
    """窗口口径：成交时间之前（含成交时刻）且不早于 成交时间-窗口 的触点才合格。"""
    conv_time: datetime = order["conversion_time"]
    lower_bound = conv_time - window if window is not None else None
    candidates = []
    for touch in touches:
        if touch["order_id"] != order["order_id"]:
            continue
        touch_time = touch["touch_time"]
        if touch_time > conv_time:
            continue
        if lower_bound is not None and touch_time < lower_bound:
            continue
        candidates.append(touch)
    return sorted(candidates, key=lambda item: (item["touch_time"], item["touch_id"]))


def _empty_channel_map() -> dict[str, float]:
    return {}


def compute_attribution(
    raw_orders: list[dict[str, Any]],
    raw_touches: list[dict[str, Any]],
    window: str | int | timedelta | None = None,
) -> dict[str, Any]:
    """对同一批成交按指定窗口计算首次、末次、线性三种归因结果。

    返回结构自带成交范围（scope）与对账信息（reconciliation）：
    每个模型都满足 已分配金额 + 未覆盖金额 == 成交总额。
    """
    window_delta = parse_window(window)
    orders = normalize_orders(raw_orders)
    touches = normalize_touches(raw_touches)

    allocated = {model: _empty_channel_map() for model in MODELS}
    uncovered = {model: 0.0 for model in MODELS}
    covered_orders = {model: set() for model in MODELS}
    order_audits: list[dict[str, Any]] = []

    total_amount = 0.0
    uncovered_order_ids: set[str] = set()

    for order in orders:
        total_amount += order["amount"]
        candidates = _candidate_touches(order, touches, window_delta)
        audit: dict[str, Any] = {
            "order_id": order["order_id"],
            "amount": order["amount"],
            "conversion_time": order["conversion_time"].isoformat(),
            "candidate_count": len(candidates),
            "candidate_channels": [touch["channel"] for touch in candidates],
            "models": {},
        }
        if not candidates:
            # 没有合格触点：金额保留为可对账的未归因转化，三个模型都不分配。
            uncovered_order_ids.add(order["order_id"])
            for model in MODELS:
                uncovered[model] += order["amount"]
                audit["models"][model] = {
                    "allocations": {},
                    "allocated": 0.0,
                    "uncovered": _round2(order["amount"]),
                }
            order_audits.append(audit)
            continue

        channels_first = candidates[0]["channel"]
        channels_last = candidates[-1]["channel"]
        allocated[MODEL_FIRST].setdefault(channels_first, 0.0)
        allocated[MODEL_FIRST][channels_first] += order["amount"]
        allocated[MODEL_LAST].setdefault(channels_last, 0.0)
        allocated[MODEL_LAST][channels_last] += order["amount"]

        # 线性均分：尾差并入最后一个触点，保证单笔订单分出去的钱恰好等于成交金额。
        share = order["amount"] / len(candidates)
        per_order_linear: dict[str, float] = {}
        running = 0.0
        for position, touch in enumerate(candidates):
            piece = _round2(share) if position < len(candidates) - 1 else _round2(order["amount"] - running)
            running += piece
            per_order_linear[touch["channel"]] = per_order_linear.get(touch["channel"], 0.0) + piece
            allocated[MODEL_LINEAR].setdefault(touch["channel"], 0.0)
            allocated[MODEL_LINEAR][touch["channel"]] += piece

        for model in MODELS:
            covered_orders[model].add(order["order_id"])
        audit["models"][MODEL_FIRST] = {
            "allocations": {channels_first: _round2(order["amount"])},
            "allocated": _round2(order["amount"]),
            "uncovered": 0.0,
        }
        audit["models"][MODEL_LAST] = {
            "allocations": {channels_last: _round2(order["amount"])},
            "allocated": _round2(order["amount"]),
            "uncovered": 0.0,
        }
        audit["models"][MODEL_LINEAR] = {
            "allocations": {key: _round2(value) for key, value in per_order_linear.items()},
            "allocated": _round2(sum(per_order_linear.values())),
            "uncovered": 0.0,
        }
        order_audits.append(audit)

    models_out: dict[str, dict[str, float]] = {}
    model_summaries: dict[str, dict[str, Any]] = {}
    reconciliation: dict[str, dict[str, Any]] = {}
    for model in MODELS:
        channels_out = {channel: _round2(amount) for channel, amount in sorted(allocated[model].items())}
        models_out[model] = channels_out
        allocated_total = _round2(sum(channels_out.values()))
        # 未覆盖金额按总额倒挤，保证 rounded 口径下对账等式仍然严格成立。
        uncovered_total = _round2(total_amount - allocated_total)
        model_summaries[model] = {
            "label": MODEL_LABELS[model],
            "allocated": allocated_total,
            "uncovered": uncovered_total,
            "total": _round2(total_amount),
            "covered_order_count": len(covered_orders[model]),
            "uncovered_order_count": len(orders) - len(covered_orders[model]),
        }
        reconciliation[model] = {
            "allocated": allocated_total,
            "uncovered": uncovered_total,
            "total": _round2(total_amount),
            "balanced": _round2(allocated_total + uncovered_total) == _round2(total_amount),
        }

    scope = {
        "order_count": len(orders),
        "order_ids": [order["order_id"] for order in orders],
        "total_amount": _round2(total_amount),
        "covered_order_count": len(orders) - len(uncovered_order_ids),
        "uncovered_order_count": len(uncovered_order_ids),
        "covered_amount": _round2(total_amount - sum(
            order["amount"] for order in orders if order["order_id"] in uncovered_order_ids)),
        "uncovered_amount": _round2(sum(
            order["amount"] for order in orders if order["order_id"] in uncovered_order_ids)),
        "uncovered_order_ids": sorted(uncovered_order_ids),
    }

    return {
        "window": str(window) if window is not None else "不限",
        "window_limited": window_delta is not None,
        "window_days": (window_delta.total_seconds() / 86400) if window_delta is not None else None,
        "scope": scope,
        "models": models_out,
        "model_summaries": model_summaries,
        "reconciliation": reconciliation,
        "orders": order_audits,
    }


def build_window_comparison(
    raw_orders: list[dict[str, Any]],
    raw_touches: list[dict[str, Any]],
    baseline_window: str | int | timedelta | None,
    comparison_window: str | int | timedelta | None,
) -> dict[str, Any]:
    """用同一批成交在两个窗口口径下同步计算，输出渠道模型差异与范围/未覆盖对照。

    份额差异（份额变化）与未覆盖金额（范围变化）并排给出，分析人员可以判断
    渠道份额变化究竟来自归因规则改变，还是进入对比的成交覆盖范围缩小。
    """
    baseline = compute_attribution(raw_orders, raw_touches, baseline_window)
    comparison = compute_attribution(raw_orders, raw_touches, comparison_window)

    channels = sorted(
        set().union(*(set(result["models"][model]) for result in (baseline, comparison) for model in MODELS))
    )
    compare_rows: list[dict[str, Any]] = []
    for model in MODELS:
        for channel in channels:
            base_share = baseline["models"][model].get(channel, 0.0)
            compare_share = comparison["models"][model].get(channel, 0.0)
            compare_rows.append({
                "模型": MODEL_LABELS[model],
                "model": model,
                "触点类型": channel,
                "基准窗口": baseline["window"],
                "对比窗口": comparison["window"],
                "基准份额": base_share,
                "对比份额": compare_share,
                "份额差异": _round2(compare_share - base_share),
                "基准未覆盖金额": baseline["model_summaries"][model]["uncovered"],
                "对比未覆盖金额": comparison["model_summaries"][model]["uncovered"],
                "成交总额": baseline["scope"]["total_amount"],
            })

    scope_rows: list[dict[str, Any]] = []
    for result in (baseline, comparison):
        for model in MODELS:
            summary = result["model_summaries"][model]
            scope_rows.append({
                "窗口": result["window"],
                "模型": MODEL_LABELS[model],
                "model": model,
                "成交总额": result["scope"]["total_amount"],
                "成交订单数": result["scope"]["order_count"],
                "覆盖金额": summary["allocated"],
                "未覆盖金额": summary["uncovered"],
                "覆盖订单数": summary["covered_order_count"],
                "未覆盖订单数": summary["uncovered_order_count"],
                "未覆盖订单": result["scope"]["uncovered_order_ids"],
            })

    return {
        "baseline": baseline,
        "comparison": comparison,
        "models": [MODEL_LABELS[model] for model in MODELS],
        "channels": channels,
        "compare_rows": compare_rows,
        "scope_rows": scope_rows,
    }
