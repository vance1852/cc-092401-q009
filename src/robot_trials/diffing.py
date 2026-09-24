"""协议草案与已发布版本之间的规范化、机器可读差异。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Sequence


def _number_text(value: object) -> str:
    """把 JSON 数值（Decimal、int、float）规范化为确定性的十进制文本。"""

    if isinstance(value, bool):
        raise ValueError("布尔值不是数值")
    return format(Decimal(str(value)), "f")


def _scalar(value: object) -> object:
    if isinstance(value, (dict, list)):
        raise ValueError("差异字段必须是标量")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return _number_text(value)
    return value


def _normalized(value: object) -> object:
    """递归把数值转成十进制文本，得到跨入口一致的 JSON 结构。"""

    if isinstance(value, Mapping):
        return {key: _normalized(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalized(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, (Decimal, float)):
        return _number_text(value)
    return value


def _index(items: Sequence[Mapping[str, Any]], key: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for item in items:
        result[str(item[key])] = item
    return result


def _field_changes(old: Mapping[str, Any], new: Mapping[str, Any], fields: Sequence[str]) -> dict[str, dict[str, object]]:
    changes: dict[str, dict[str, object]] = {}
    for field in fields:
        old_value = _scalar(old[field])
        new_value = _scalar(new[field])
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}
    return changes


def _strata(raw: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    return raw["strata"]


def _metrics(raw: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    return raw["metrics"]


def diff_protocols(old_raw: Mapping[str, Any], new_raw: Mapping[str, Any]) -> dict[str, Any]:
    """按分层、指标、权重、规则及顶层元数据输出结构化差异。

    每个分区都给出 added/removed/changed，字段变化统一为 ``{from,to}`` 形式，
    数值一律转成十进制文本，保证不同 JSON 解析入口下结果一致。
    """

    old_strata = _index(_strata(old_raw), "key")
    new_strata = _index(_strata(new_raw), "key")
    old_metrics = _index(_metrics(old_raw), "key")
    new_metrics = _index(_metrics(new_raw), "key")

    strata_changed: list[dict[str, Any]] = []
    for key in sorted(set(old_strata) & set(new_strata)):
        fields = _field_changes(old_strata[key], new_strata[key], ("label", "required_trials"))
        if fields:
            strata_changed.append({"key": key, "fields": fields})

    metrics_changed: list[dict[str, Any]] = []
    for key in sorted(set(old_metrics) & set(new_metrics)):
        fields = _field_changes(old_metrics[key], new_metrics[key], ("label", "kind", "unit", "direction"))
        if fields:
            metrics_changed.append({"key": key, "fields": fields})

    old_weights = old_raw["stratum_weights"]
    new_weights = new_raw["stratum_weights"]
    weights_added = {
        key: _number_text(new_weights[key]) for key in sorted(set(new_weights) - set(old_weights))
    }
    weights_removed = {
        key: _number_text(old_weights[key]) for key in sorted(set(old_weights) - set(new_weights))
    }
    weights_changed: list[dict[str, Any]] = []
    for key in sorted(set(old_weights) & set(new_weights)):
        old_value = _number_text(old_weights[key])
        new_value = _number_text(new_weights[key])
        if old_value != new_value:
            weights_changed.append({"stratum": key, "from": old_value, "to": new_value})

    def rule_key(rule: Mapping[str, Any]) -> tuple[str, str]:
        return str(rule["metric"]), str(rule.get("statistic", "weighted_mean"))

    old_rules = {rule_key(rule): rule for rule in old_raw["admission_rules"]}
    new_rules = {rule_key(rule): rule for rule in new_raw["admission_rules"]}
    rules_changed: list[dict[str, Any]] = []
    for key in sorted(set(old_rules) & set(new_rules)):
        fields = _field_changes(old_rules[key], new_rules[key], ("operator", "threshold"))
        if fields:
            rules_changed.append(
                {"metric": key[0], "statistic": key[1], "fields": fields}
            )

    meta_changes = [
        {"field": field, "from": _scalar(old_raw[field]), "to": _scalar(new_raw[field])}
        for field in ("title", "task_family", "seed", "bootstrap_samples")
        if _scalar(old_raw[field]) != _scalar(new_raw[field])
    ]

    sections = {
        "meta": meta_changes,
        "strata": {
            "added": [_normalized(dict(new_strata[key])) for key in sorted(set(new_strata) - set(old_strata))],
            "removed": [_normalized(dict(old_strata[key])) for key in sorted(set(old_strata) - set(new_strata))],
            "changed": strata_changed,
        },
        "metrics": {
            "added": [_normalized(dict(new_metrics[key])) for key in sorted(set(new_metrics) - set(old_metrics))],
            "removed": [_normalized(dict(old_metrics[key])) for key in sorted(set(old_metrics) - set(new_metrics))],
            "changed": metrics_changed,
        },
        "weights": {
            "added": weights_added,
            "removed": weights_removed,
            "changed": weights_changed,
        },
        "rules": {
            "added": [_normalized(dict(new_rules[key])) for key in sorted(set(new_rules) - set(old_rules))],
            "removed": [_normalized(dict(old_rules[key])) for key in sorted(set(old_rules) - set(new_rules))],
            "changed": rules_changed,
        },
    }
    summary = {
        "meta": len(meta_changes),
        "strata": sum(len(sections["strata"][bucket]) for bucket in ("added", "removed", "changed")),
        "metrics": sum(len(sections["metrics"][bucket]) for bucket in ("added", "removed", "changed")),
        "weights": (
            len(weights_added) + len(weights_removed) + len(weights_changed)
        ),
        "rules": sum(len(sections["rules"][bucket]) for bucket in ("added", "removed", "changed")),
    }
    return {"sections": sections, "summary": summary}


def is_identical(diff: Mapping[str, Any]) -> bool:
    return all(value == 0 for value in diff["summary"].values())
