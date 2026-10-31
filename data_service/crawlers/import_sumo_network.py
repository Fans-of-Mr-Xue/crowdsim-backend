"""Explicitly publish SUMO pedestrian geometry to an existing environment."""
import argparse
import asyncio
import json
from pathlib import Path

from ..repositories.datasets import DatasetRepository
from ..repositories.environments import EnvironmentRepository
from ..services.sumo_network import export_sumo_network
from .publish_pedestrian_network import publish


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment-id', required=True)
    parser.add_argument('--net', required=True)
    parser.add_argument('--hotspots', required=True)
    parser.add_argument('--hotspot-id', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--update-linked-tasks', action='store_true')
    args = parser.parse_args()
    database = DatasetRepository()
    try:
        environment = EnvironmentRepository(database.db).get(args.environment_id)
        if not environment:
            raise ValueError('环境库区域不存在')
        network = export_sumo_network(args.net, args.hotspots, args.hotspot_id, environment['geometry'])
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(network, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'asset': str(path), 'quality': network['quality'],
                          'source': network['source']}, ensure_ascii=False), flush=True)
    finally:
        database.close()
    if args.publish:
        asyncio.run(publish(args.environment_id, args.output, args.update_linked_tasks))


if __name__ == '__main__':
    main()
