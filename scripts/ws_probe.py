"""Small protocol probe for a running CrowdSim WebSocket server."""

import argparse
import asyncio
import json

import websockets


async def probe(uri: str):
    async with websockets.connect(uri) as socket:
        commands = ({"action": "configure"}, {"action": "get_status", "request_id": "probe-status"}, {"action": "start", "request_id": "probe-start"}, {"action": "pause", "request_id": "probe-pause"})
        for command in commands:
            await socket.send(json.dumps(command))
            while True:
                response = json.loads(await socket.recv())
                print(json.dumps(response, ensure_ascii=False))
                if command["action"] == "configure" and response.get("type") == "init":
                    break
                if response.get("request_id") == command.get("request_id"):
                    break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="ws://127.0.0.1:8765")
    asyncio.run(probe(parser.parse_args().uri))


if __name__ == "__main__":
    main()
