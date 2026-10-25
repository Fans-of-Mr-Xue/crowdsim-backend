"""Rule model algorithm namespace."""

from crowdsim.modeling_algorithm.rule_model.catalogue import (
    STRONG_RULES,
    default_rule_methods,
    implemented_rule_method,
    resolve_rule_method,
    rule_entry,
    strong_rule_catalogue,
)

__all__ = [
    "STRONG_RULES",
    "default_rule_methods",
    "implemented_rule_method",
    "resolve_rule_method",
    "rule_entry",
    "strong_rule_catalogue",
]
