"""Standalone remote JSONL reader; executed through SSH with system Python 3."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import BinaryIO


ANCHOR_BYTES = 256
COMPRESSED_SUFFIXES = (".gz", ".bz2", ".xz", ".zst", ".zip")


def modified(path: Path) -> int:
    return path.stat().st_mtime_ns


def file_identity(stat: os.stat_result) -> str:
    return f"{stat.st_dev}:{stat.st_ino}"


def retained_generations(path: Path, identity: str) -> list[Path]:
    """Find the previous generation and all later uncompressed rotations."""
    candidates = sorted(
        (p for p in path.parent.iterdir()
         if p.name.startswith(path.name) and p != path and p.is_file()
         and p.suffix not in COMPRESSED_SUFFIXES),
        key=modified,
    )
    for index, candidate in enumerate(candidates):
        if file_identity(candidate.stat()) == identity:
            return candidates[index:]
    raise RuntimeError("Previous log generation unavailable (removed, compressed, or outside filename prefix); cursor retained")


def anchor_at(stream: BinaryIO, position: int) -> str:
    stream.seek(max(0, position - ANCHOR_BYTES))
    return hashlib.sha256(stream.read(min(position, ANCHOR_BYTES))).hexdigest()


def read_generation(
    stream: BinaryIO, identity: str, offset: int, anchor: str, *, active: bool,
) -> tuple[str, int, str, bytes, list[str]]:
    stat = os.fstat(stream.fileno())
    current_id = file_identity(stat)
    start = offset if current_id == identity else 0
    warnings: list[str] = []
    if start:
        actual = anchor_at(stream, start)
        if stat.st_size < start or actual != anchor:
            warnings.append("Log truncated or rewritten; restarting generation from byte zero")
            start = 0
    stream.seek(start)
    data = stream.read(max(0, stat.st_size - start))
    end = data.rfind(b"\n") + 1
    if not active and end != len(data):
        raise RuntimeError("Rotated log has an incomplete final line; cursor retained")
    position = start + end
    return current_id, position, anchor_at(stream, position), data[:end], warnings


def read_increment(path: Path, identity: str, offset: int, anchor: str) -> tuple[str, int, str, bytes, list[str]]:
    warnings: list[str] = []
    with path.open("rb") as active:
        active_id = file_identity(os.fstat(active.fileno()))
        candidates: list[Path] = []
        if identity and identity != active_id:
            candidates = retained_generations(path, identity)
        chunks: list[bytes] = []
        for candidate in [*candidates, path]:
            # Keep the active descriptor stable even if rotation happens during reading.
            stream = active if candidate == path else candidate.open("rb")
            try:
                current_id, position, digest, data, notices = read_generation(
                    stream, identity, offset, anchor, active=candidate == path,
                )
                chunks.append(data)
                warnings.extend(notices)
                if candidate == path:
                    return current_id, position, digest, b"".join(chunks), warnings
            finally:
                if candidate != path:
                    stream.close()
    raise RuntimeError("No active log")


def main() -> None:
    identity, offset, anchor, data, warnings = read_increment(
        Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4],
    )
    response: dict[str, object] = {
        "identity": identity, "offset": offset, "anchor": anchor,
        "data": base64.b64encode(data).decode("ascii"), "warnings": warnings,
    }
    print(json.dumps(response))


if __name__ == "__main__":
    main()
