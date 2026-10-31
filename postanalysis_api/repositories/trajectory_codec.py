"""Lossless bounded trajectory chunks for Mongo; HTTP still exposes normal frames."""
import json
import zlib

from postanalysis_api.schemas.common import ApiError

MAX_CHUNK_BYTES = 8 * 1024 * 1024


def encode(frames):
    raw = json.dumps(frames, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_CHUNK_BYTES:
        raise ApiError("TRAJECTORY_CHUNK_TOO_LARGE", "轨迹分块超过8 MiB，未丢弃或截断人员", 422)
    return {"encoding": "zlib-json-utf8/1", "uncompressedBytes": len(raw), "compressedFrames": zlib.compress(raw, 3)}


def decode(document):
    if document.get("encoding") != "zlib-json-utf8/1":
        return document  # Earlier uncompressed chunks remain readable.
    result = dict(document)
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(result.pop("compressedFrames"), MAX_CHUNK_BYTES + 1)
        if len(raw) > MAX_CHUNK_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("Invalid compressed chunk")
        result["frames"] = json.loads(raw.decode("utf-8"))
        return result
    except (ValueError, TypeError, KeyError, zlib.error) as exc:
        raise ApiError("TRAJECTORY_CORRUPT", "轨迹分块校验失败，未返回伪造帧", 500) from exc
