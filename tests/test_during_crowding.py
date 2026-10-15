from copy import deepcopy
import json
from pathlib import Path

import pytest

from crowdsim.domain.during_crowding import summarize_crowding
from crowdsim.domain.requirement_spec import RequirementSpec


ROOT = Path(__file__).resolve().parents[1]
PRESET = json.loads((ROOT / "config/during_presets/memorial_tower_v1.json").read_text())
POLICY = {**PRESET["phenomenon"], "observation_start_seconds": 0.0}


def sample(t, *, total=50, crowded=8, high_risk=2, unknown=0):
    return {"time_seconds": t, "core_count": total, "crowded_count": crowded,
            "high_risk_count": high_risk, "unknown_count": unknown}


def sequence(start, end, **kwargs):
    return [sample(i / 2, **kwargs) for i in range(int(start * 2), int(end * 2) + 1)]


def test_ten_people_twenty_percent_must_hold_for_ten_simulation_seconds():
    assert summarize_crowding(sequence(0, 9.5), POLICY)["occurred"] is False
    result = summarize_crowding(sequence(0, 10), POLICY)
    assert result["occurred"] is True
    assert result["first_confirmed_at_seconds"] == 10
    assert result["confirmed_duration_seconds"] == 10
    assert result["episodes"][0]["right_censored"] is True


@pytest.mark.parametrize("values", [
    {"total": 100},  # Enough people, insufficient fraction.
    {"total": 9, "crowded": 7, "high_risk": 2},  # Enough fraction, insufficient people.
    {"total": 700, "crowded": 0, "high_risk": 0},  # Planned stops / busy states alone.
    {"total": 0, "crowded": 0, "high_risk": 0},
])
def test_non_events(values):
    assert summarize_crowding(sequence(0, 30, **values), POLICY)["occurred"] is False


def test_false_interval_resets_confirmation_and_episodes_end_at_first_false_sample():
    rows = sequence(0, 6) + [sample(6.5, crowded=0, high_risk=0)] + sequence(7, 17)
    rows += [sample(17.5, crowded=0, high_risk=0)]
    result = summarize_crowding(rows, POLICY)
    assert result["first_confirmed_at_seconds"] == 17
    assert result["episodes"] == [{"onset_seconds": 7, "confirmed_at_seconds": 17,
        "end_seconds": 17.5, "duration_seconds": 10.5,
        "end_reason": "condition_cleared", "right_censored": False}]


def test_unknown_density_does_not_count_as_safe_or_bridge_confirmation():
    rows = sequence(0, 6) + [sample(6.5, unknown=1)] + sequence(7, 16)
    result = summarize_crowding(rows, POLICY)
    assert result["occurred"] is None
    assert result["coverage"] == "incomplete"
    assert result["unknown_sample_count"] == 1


def test_sampling_gap_does_not_manufacture_continuous_crowding():
    result = summarize_crowding([sample(0), sample(30)], POLICY)
    assert result["occurred"] is None
    assert result["gap_count"] == 1
    assert result["episodes"] == []


def test_confirmed_event_survives_later_unknown_data_without_extending_duration():
    result = summarize_crowding(sequence(0, 10) + [sample(10.5, unknown=1)], POLICY)
    assert result["occurred"] is True
    assert result["coverage"] == "incomplete"
    assert result["episodes"][0]["end_seconds"] == 10
    assert result["episodes"][0]["right_censored"] is True


def test_duplicate_frames_do_not_advance_time_and_backwards_time_is_rejected():
    assert summarize_crowding([sample(0)] * 100, POLICY)["occurred"] is False
    with pytest.raises(ValueError, match="ordered"):
        summarize_crowding([sample(1), sample(.5)], POLICY)
    with pytest.raises(ValueError, match="conflicting"):
        summarize_crowding([sample(1), sample(1, total=51)], POLICY)


@pytest.mark.parametrize("row", [sample(float("nan")), sample(1, total=-1),
                                 sample(1, total=5), sample(1, crowded=True)])
def test_malformed_samples_are_rejected(row):
    with pytest.raises(ValueError):
        summarize_crowding([row], POLICY)


def test_empty_evidence_is_unknown():
    assert summarize_crowding([], POLICY)["occurred"] is None


def test_initialization_is_excluded_only_before_explicit_observation_start():
    policy = {**POLICY, "observation_start_seconds": 1.0}
    rows = [sample(.5, unknown=20)] + sequence(1, 15, crowded=0, high_risk=0)
    result = summarize_crowding(rows, policy)
    assert result["occurred"] is False
    assert result["excluded_initialization_samples"] == 1
    assert result["coverage"] == "complete"
    rows[2] = sample(1.5, unknown=20)
    assert summarize_crowding(rows, policy)["coverage"] == "incomplete"


def test_multiple_episodes_keep_their_own_start_and_duration():
    rows = sequence(0, 10) + [sample(10.5, crowded=0, high_risk=0)] + sequence(11, 22)
    result = summarize_crowding(rows, POLICY)
    assert len(result["episodes"]) == 2
    assert result["confirmed_duration_seconds"] == 21.5
    assert result["episodes"][1]["confirmed_at_seconds"] == 21


def test_preset_uses_valid_requirement_and_real_hotspot_targets():
    spec = RequirementSpec.parse(PRESET["requirement"])
    assert spec.location_id == "memorial-tower"
    hotspots = json.loads((ROOT / PRESET["hotspot_config"]).read_text())["hotspots"]
    hotspot = next(item for item in hotspots if item["id"] == PRESET["hotspot_id"])
    assert POLICY["scope_edges"] == hotspot["measurement_edges"]
    assert set(PRESET["control_scope"]["entry_ids"]) == set(hotspot["entry_edges"])
    assert PRESET["control_scope"]["smoke_check"]["entry_id"] in hotspot["entry_edges"]
    assert POLICY["maximum_sample_gap_seconds"] == PRESET["experiment"]["step_seconds"]


@pytest.mark.parametrize("key,value", [("minimum_count", 0), ("minimum_fraction", 1.1),
                                      ("confirmation_seconds", float("inf"))])
def test_invalid_rules_are_rejected(key, value):
    policy = deepcopy(POLICY)
    policy[key] = value
    with pytest.raises(ValueError):
        summarize_crowding([], policy)
