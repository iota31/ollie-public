"""Regression for model-to-MCP collection serialization."""

from pydantic import TypeAdapter

from ollie_hands.actscript import PlanStepInput


def test_plan_step_schema_exposes_nested_preconditions_array():
    schema = TypeAdapter(list[PlanStepInput]).json_schema()
    assert schema["type"] == "array"
    step = schema["$defs"]["PlanStepInput"]
    assert step["properties"]["preconditions"]["type"] == "array"


def test_item_wrapper_is_rejected_before_executor():
    try:
        TypeAdapter(PlanStepInput).validate_python({
            "id": "s1",
            "kind": "browser",
            "args": {"op": "status"},
            "preconditions": {"item": {"type": "window_exists"}},
        })
    except Exception as exc:
        assert "preconditions" in str(exc)
    else:
        raise AssertionError("object-wrapped array must not validate")
