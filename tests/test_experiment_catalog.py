from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.serve import ControlRequestHandler
from types import SimpleNamespace


def test_summary_catalog_paginates_within_workspace_and_keeps_draft_and_running_rows():
    repo = CrowdSimRepository()
    first = repo.create_experiment("u", "during-one", {"name": "saved", "clientRequestId": "saved-key", "scenario": {"presetId": "memorial-tower-during-v1", "large": "not-in-summary"}, "methods": ["C0"]})
    second = repo.create_experiment("u", "during-one", {"name": "running", "duringObservation": {"conversationId": "one", "snapshotId": "frozen"}})
    repo.create_experiment("other", "during-one", {"name": "other user"})
    repo.create_experiment("u", "during-two", {"name": "other workspace"})
    run = repo.create_run("u", "during-one", second["experiment_id"], "C0", 1, {})
    repo.update_run("u", "during-one", run["run_id"], {"status": "running"})
    page = repo.list_experiments("u", "during-one", 1, 0, summary=True)
    assert page[0]["experiment_id"] == second["experiment_id"]
    assert page[0]["runs"] == [{"run_id": run["run_id"], "status": "running", "error": None}]
    assert page[0]["duringObservation"]["snapshotId"] == "frozen"
    older = repo.list_experiments("u", "during-one", 1, 1, summary=True)
    assert older[0]["experiment_id"] == first["experiment_id"]
    assert older[0]["status"] == "draft"
    assert older[0]["runs"] == []
    assert older[0]["clientRequestId"] == "saved-key"
    assert "large" not in older[0]["scenario"]
    assert repo.list_experiments("u", "during-one", 1, 2, summary=True) == []
    # The existing full-detail list keeps its default shape and order.
    assert [item["experiment_id"] for item in repo.list_experiments("u", "during-one")] == [second["experiment_id"], first["experiment_id"]]
    assert repo.get_experiment("u", "during-one", first["experiment_id"])["scenario"]["large"] == "not-in-summary"


def test_catalog_http_query_uses_scoped_summary_pagination_without_opening_a_server():
    repo = CrowdSimRepository()
    first = repo.create_experiment("u", "during-one", {"name": "first"})
    repo.create_experiment("u", "during-one", {"name": "second"})
    repo.create_experiment("u", "during-other", {"name": "private to another workspace"})
    handler = object.__new__(ControlRequestHandler)
    handler.server = SimpleNamespace(bundle=SimpleNamespace(repository=repo))
    handler.headers = {"X-CrowdSim-User": "u", "X-CrowdSim-Workspace": "during-one"}
    handler.path = "/crowdSim/control/experiments?limit=1&offset=1&summary=1"
    result = {}
    handler._send_json = lambda status, data: result.update(status=status, data=data)
    handler.do_GET()
    assert result["status"] == 200
    assert result["data"][0]["experiment_id"] == first["experiment_id"]
    assert result["data"][0]["runs"] == []
