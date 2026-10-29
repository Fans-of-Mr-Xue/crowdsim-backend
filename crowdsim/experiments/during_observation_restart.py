"""Select the current class-one round without changing the other rounds."""


def current_observation_run(runs):
    return (next((run for run in runs if run.get("restartPending")
                  or run.get("restartCount", 0) and run["status"] in {"draft", "queued"}), None)
            or next((run for run in runs if run["status"] in {"preparing", "running", "paused"}), None)
            or next((run for run in reversed(runs) if run["status"] in {"completed", "failed"}
                     or run["status"] == "cancelled" and (run.get("started_at") or run.get("runtime_run_id") or run.get("restartCount", 0))), None))
