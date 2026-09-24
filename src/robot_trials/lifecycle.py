"""协议草案的规范化内容、摘要与机器可读差异。

所有进入生命周期（草案、发布、退役）的协议内容都先经过 :func:`normalize_protocol`
转换为只含基本类型的规范字典，保证同一份协议无论从 HTTP JSON、文件还是已发布版本
派生，得到的内容摘要都一致，差异结果也按分层、指标、权重和准入规则分区呈现。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .contracts import Protocol, ValidationError
from .jsonio import canonical_json, content_digest

# 协议顶层的标量字段，差异按此顺序输出。
SCALAR_FIELDS: tuple[str, ...] = (
    "protocol_id",
    "version",
    "title",
    "task_family",
    "seed",
    "bootstrap_samples",
)

STRATUM_FIELDS: tuple[str, ...] = ("label", "required_trials")
METRIC_FIELDS: tuple[str, ...] = ("label", "kind", "unit", "direction")


def _decimal_text(value: Any, path: str) -> str:
    if isinstance(value, bool):
        raise ValidationError(f"{path} 必须是十进制数值")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{path} 必须是十进制数值") from exc
    if not number.is_finite():
        raise ValidationError(f"{path} 必须是有限数值")
    return format(number, "f")


def normalize_protocol(raw: Mapping[str, Any]) -> dict[str, Any]:
    """完整校验协议并返回规范化字典；失败时抛出 ValidationError。"""

    protocol = Protocol.from_dict(raw)
    normalized: dict[str, Any] = {
        "protocol_id": protocol.protocol_id,
        "version": protocol.version,
        "title": protocol.title,
        "task_family": protocol.task_family,
        "seed": protocol.seed,
        "bootstrap_samples": protocol.bootstrap_samples,
        "strata": [
            {
                "key": item.key,
                "label": item.label,
                "required_trials": item.required_trials,
            }
            for item in protocol.strata
        ],
        "metrics": [
            {
                "key": item.key,
                "label": item.label,
                "kind": item.kind,
                "unit": item.unit,
                "direction": item.direction,
            }
            for item in protocol.metrics
        ],
        "stratum_weights": {
            key: format(value, "f") for key, value in sorted(protocol.stratum_weights.items())
        },
        "admission_rules": [
            {
                "metric": str(rule["metric"]),
                "statistic": str(rule.get("statistic", "weighted_mean")),
                "operator": str(rule["operator"]),
                "threshold": _decimal_text(
                    rule["threshold"], f"protocol.admission_rules.threshold"
                ),
            }
            for rule in protocol.admission_rules
        ],
    }
    return normalized


def normalized_digest(normalized: Mapping[str, Any]) -> str:
    """计算规范化协议内容的 SHA-256 摘要。"""

    return content_digest([normalized])


def canonical_text(normalized: Mapping[str, Any]) -> str:
    """规范化协议的存储文本。"""

    return canonical_json(normalized)


def _field_change(field: str, old_value: Any, new_value: Any) -> dict[str, Any]:
    return {
        "field": field,
        "from": old_value,
        "to": new_value,
        "changed": old_value != new_value,
    }


def _index(items: Any, key: str) -> dict[str, dict[str, Any]]:
    return {item[key]: item for item in items}


def _entity_diff(
    old_index: Mapping[str, dict[str, Any]],
    new_index: Mapping[str, dict[str, Any]],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    added = [new_index[key] for key in sorted(new_index.keys() - old_index.keys())]
    removed = [old_index[key] for key in sorted(old_index.keys() - new_index.keys())]
    changed: list[dict[str, Any]] = []
    unchanged: list[str] = []
    for key in sorted(old_index.keys() & new_index.keys()):
        field_changes = [
            _field_change(field, old_index[key].get(field), new_index[key].get(field))
            for field in fields
        ]
        touched = [item for item in field_changes if item["changed"]]
        if touched:
            changed.append({"key": key, "fields": touched})
        else:
            unchanged.append(key)
    return {"added": added, "removed": removed, "changed": changed, "unchanged": unchanged}


def _rule_key(rule: Mapping[str, Any]) -> str:
    return f"{rule['metric']}|{rule['statistic']}|{rule['operator']}"


def _rules_diff(
    old_rules: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
    new_rules: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> dict[str, Any]:
    old_index = {_rule_key(rule): dict(rule) for rule in old_rules}
    new_index = {_rule_key(rule): dict(rule) for rule in new_rules}
    added = [new_index[key] for key in sorted(new_index.keys() - old_index.keys())]
    removed = [old_index[key] for key in sorted(old_index.keys() - new_index.keys())]
    changed: list[dict[str, Any]] = []
    unchanged: list[str] = []
    for key in sorted(old_index.keys() & new_index.keys()):
        old_rule = old_index[key]
        new_rule = new_index[key]
        if old_rule["threshold"] != new_rule["threshold"]:
            metric, statistic, operator = key.split("|")
            changed.append(
                {
                    "key": key,
                    "metric": metric,
                    "statistic": statistic,
                    "operator": operator,
                    "field": "threshold",
                    "from": old_rule["threshold"],
                    "to": new_rule["threshold"],
                    "changed": True,
                }
            )
        else:
            unchanged.append(key)
    return {"added": added, "removed": removed, "changed": changed, "unchanged": unchanged}


def _weights_diff(
    old: Mapping[str, str] | None, new: Mapping[str, str] | None
) -> list[dict[str, Any]]:
    old = old or {}
    new = new or {}
    return [
        {
            "stratum_key": key,
            "from": old.get(key),
            "to": new.get(key),
            "changed": old.get(key) != new.get(key),
        }
        for key in sorted(old.keys() | new.keys())
    ]


def diff_protocols(
    old: Mapping[str, Any] | None, new: Mapping[str, Any]
) -> dict[str, Any]:
    """返回两份规范化协议之间按区域划分的机器可读差异。

    ``old`` 为 None 时表示新协议族的首版草案，全部内容记为新增。
    """

    fields = [
        _field_change(field, None if old is None else old.get(field), new.get(field))
        for field in SCALAR_FIELDS
    ]
    strata = _entity_diff(
        {} if old is None else _index(old["strata"], "key"),
        _index(new["strata"], "key"),
        STRATUM_FIELDS,
    )
    metrics = _entity_diff(
        {} if old is None else _index(old["metrics"], "key"),
        _index(new["metrics"], "key"),
        METRIC_FIELDS,
    )
    weights = _weights_diff(None if old is None else old["stratum_weights"], new["stratum_weights"])
    rules = _rules_diff(
        () if old is None else old["admission_rules"], new["admission_rules"]
    )
    changed_any = any(item["changed"] for item in fields) or any(
        section[key]
        for section in (strata, metrics, rules)
        for key in ("added", "removed", "changed")
    ) or any(item["changed"] for item in weights)
    return {
        "identical": not changed_any,
        "fields": fields,
        "strata": strata,
        "metrics": metrics,
        "weights": weights,
        "rules": rules,
    }
