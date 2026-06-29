from __future__ import annotations

from threshly.coordinator.scheduling import prefix_group


def test_shared_system_prompt_groups_together():
    a = {"messages": [{"role": "system", "content": "SAME"}, {"role": "user", "content": "x"}]}
    b = {"messages": [{"role": "system", "content": "SAME"}, {"role": "user", "content": "y"}]}
    assert prefix_group(a, "m") == prefix_group(b, "m")


def test_different_system_prompts_separate():
    a = {"messages": [{"role": "system", "content": "A"}, {"role": "user", "content": "x"}]}
    b = {"messages": [{"role": "system", "content": "B"}, {"role": "user", "content": "x"}]}
    assert prefix_group(a, "m") != prefix_group(b, "m")


def test_model_separates_groups():
    body = {"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "x"}]}
    assert prefix_group(body, "m1") != prefix_group(body, "m2")


def test_final_user_turn_does_not_affect_group():
    # Only the leading context defines the group; the varying final turn must not.
    a = {"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "alpha"}]}
    b = {"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "omega!!"}]}
    assert prefix_group(a, "m") == prefix_group(b, "m")
