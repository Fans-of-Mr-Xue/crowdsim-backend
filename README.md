# CrowdSim Overlay Backend

This repository contains the standalone WebSocket backend used by the CrowdSim frontend.

It reads a SUMO-format road network and route files, runs an overlay crowd/vehicle simulation, and streams frames to the frontend over WebSocket.

## Contents

- `crowdsim_overlay_server.py` - simulation composition and command-line entrypoint
- `crowdsim_models.py` - agent, segment, flood, route, and event domain models
- `network_adapter.py` - SUMO XML loading, route loading, geometry, and projection
- `crowd_environment.py` - local perception, event pressure, and density levels
- `agent_decision.py` - individual decision engine and LLM-compatible provider
- `websocket_server.py` - frontend WebSocket protocol and simulation loop
- `tests/` - decision, environment, and simulation-loop tests
- `scenarios/shanghai_bund/` - default SUMO network and route files
- `vendor/PedNStream/` - vendored pedestrian Link Transmission Model implementation

## Run

```bash
pip install -r requirements.txt
python crowdsim_overlay_server.py --host 127.0.0.1 --port 8765
```

The default frontend endpoint is:

```text
ws://localhost:8765
```

## Notes

The backend uses SUMO XML files as input data, but it does not launch SUMO or TraCI at runtime. The live simulation is implemented in Python as an overlay model.

## Agent decision loop

Each pedestrian has stable attributes (age group, mobility, risk tolerance,
familiarity, group size, perception radius, and an acceptable-neighbour count)
and dynamic state (nearby people, local density, density level, stress, fatigue,
and current decision).

The simulation loop is:

```text
road/event state -> local perception -> density/stress state -> decision
                 -> movement/wait/diversion -> new road/event state
```

Density levels are `free`, `busy`, `crowded`, and `critical`. They combine the
agent's personal acceptable-neighbour threshold with people per square metre in
its perception area.

An OpenAI-compatible model is optional. Without it, or when it fails, the same
decision contract uses a deterministic local fallback so simulation continues.

```powershell
$env:CROWDSIM_LLM_ENDPOINT="https://your-host/v1/chat/completions"
$env:CROWDSIM_LLM_API_KEY="your-key"
$env:CROWDSIM_LLM_MODEL="your-model"
python crowdsim_overlay_server.py
```

The model must return JSON such as:

```json
{"action":"avoid","reason":"local density is critical"}
```

Allowed actions are `continue`, `slow_down`, `avoid`, `follow_crowd`, and
`wait`. Calls are bounded per simulation step and fall back locally on errors.

## Simplified event protocol

Send an event over WebSocket; its type is descriptive and all event types use
the same density-impact mechanism:

```json
{
  "action": "trigger_event",
  "id": "demo-event",
  "eventType": "generic",
  "lng": 121.4905,
  "lat": 31.2417,
  "radius": 40,
  "intensity": 0.8,
  "densityMultiplier": 2.0,
  "growthSeconds": 5,
  "decaySeconds": 15,
  "effects": {"speed": 0.4, "stress": 0.8, "avoidance": 0.7},
  "duration": 60
}
```

Event types (`generic`, `fire`, `smoke`, `rumor`, `alarm`, `obstacle`, and
`flood`) are parameter presets over the same engine. Every event can affect
density, speed, stress, avoidance, and pedestrian-network capacity, and can
evolve through `growing`, `active`, and `decaying` phases.

Clear active events with `{"action":"set_event","mode":"clear"}`. Update
frames expose every pedestrian's `state`, active `events`, density-level counts,
and decision-engine diagnostics.
