"""Stage-2 browser contract checks; no live service or simulation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from crowdsim.experiments.contracts import ContractError, validate_experiment_config
from crowdsim.experiments.repository import CrowdSimRepository


def config(request_id="confirmed-draft-1"):
    return validate_experiment_config({
        "name": "纪念塔持续拥挤观察", "researchQuestion": "观察持续拥挤及行程完成情况",
        "clientRequestId": request_id, "methods": ["C0"], "mode": "formal",
        "scenario": {"presetId": "memorial-tower-during-v1", "population": 700,
                     "durationSeconds": 1800, "seedSet": [20260908, 20260909, 20260910]},
    })


def test_confirmed_browser_payload_binds_full_preset():
    payload = config()
    assert payload["scenario"]["duringPreset"]["requirement"]["population"]["total"] == 700
    assert payload["scenario"]["presetFingerprint"].startswith("sha256:")
    assert len(payload["scenario"]["seedSet"]) == 3
    assert payload["researchQuestion"] == "观察持续拥挤及行程完成情况"


def test_duplicate_create_is_atomic_and_survives_reload(tmp_path):
    repository = CrowdSimRepository(tmp_path)
    payload = config()
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: repository.create_experiment("u", "during-chat", payload), range(8)))
    assert len({r["experiment_id"] for r in responses}) == 1
    assert len(repository.list_experiments("u", "during-chat")) == 1
    repository.update_experiment("u", "during-chat", responses[0]["experiment_id"], {"status": "completed"})
    restarted = CrowdSimRepository(tmp_path)
    recovered = restarted.create_experiment("u", "during-chat", payload)
    assert recovered["experiment_id"] == responses[0]["experiment_id"]
    assert recovered["status"] == "completed"


def test_request_key_conflict_and_scope_isolation():
    repository = CrowdSimRepository()
    payload = config()
    original = repository.create_experiment("u", "during-chat", payload)
    changed = deepcopy(payload)
    changed["scenario"]["seedSet"] = [1]
    with pytest.raises(ValueError, match="CLIENT_REQUEST_CONFLICT"):
        repository.create_experiment("u", "during-chat", changed)
    assert repository.create_experiment("u", "during-other", payload)["experiment_id"] != original["experiment_id"]
    assert repository.create_experiment("other", "during-chat", payload)["experiment_id"] != original["experiment_id"]
    assert len(repository.list_experiments("u", "during-chat")) == 1


@pytest.mark.parametrize("invalid", ["", "spaces here", "x" * 129, "中文", 123, True, {}, []])
def test_bad_request_keys_are_rejected(invalid):
    with pytest.raises(ContractError, match="clientRequestId"):
        config(invalid)


def test_legacy_callers_without_request_key_still_create_new_experiments():
    repository = CrowdSimRepository()
    payload = config()
    del payload["clientRequestId"]
    assert repository.create_experiment("u", "w", payload)["experiment_id"] != repository.create_experiment("u", "w", payload)["experiment_id"]
