"""Generate the Bund finite hotspot demand with optional background pedestrians."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crowdsim.scenarios.hotspot_demand import build_hotspot_demand


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "scenarios" / "shanghai_bund" / "bund_ped.rou.xml")
    parser.add_argument("--network", type=Path, default=ROOT / "scenarios" / "shanghai_bund" / "bund.net.xml")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "crowd_hotspots.json")
    parser.add_argument("--output", type=Path, default=ROOT / "scenarios" / "shanghai_bund" / "bund_hotspot.rou.xml")
    parser.add_argument("--hotspot-id", default="people_heroes_monument")
    parser.add_argument("--seed", type=int, default=20260908)
    parser.add_argument("--visitors", type=int)
    parser.add_argument("--background", type=int)
    args = parser.parse_args()
    report = build_hotspot_demand(args.source, args.network, args.config, args.output, hotspot_id=args.hotspot_id, seed=args.seed, visitor_count=args.visitors, background_count=args.background)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
