# CrowdSim Overlay Backend

This repository contains the standalone WebSocket backend used by the CrowdSim frontend.

It reads a SUMO-format road network and route files, runs an overlay crowd/vehicle simulation, and streams frames to the frontend over WebSocket.

## Contents

- `crowdsim_overlay_server.py` - WebSocket server and overlay simulator
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
