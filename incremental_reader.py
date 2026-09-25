"""Standalone remote JSONL reader; executed through SSH with system Python 3."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys


def modified(path: Path) -> int:
    return path.stat().st_mtime_ns


def read_increment(path: Path, identity: str, offset: int, anchor: str) -> tuple[str, int, str, bytes, list[str]]:
    warnings: list[str] = []
    with path.open("rb") as active:
        active_stat = os.fstat(active.fileno())
        active_id = f"{active_stat.st_dev}:{active_stat.st_ino}"
        candidates: list[Path] = []
        if identity and identity != active_id:
            # Cowrie dated rotations and logrotate's numeric suffixes.
            candidates = sorted(
                (p for p in path.parent.iterdir()
                 if p.name.startswith(path.name) and p != path and p.is_file()
                 and p.suffix not in (".gz", ".bz2", ".xz", ".zst", ".zip")),
                key=modified,
            )
            found = False
            retained: list[Path] = []
            for candidate in candidates:
                stat = candidate.stat()
                if f"{stat.st_dev}:{stat.st_ino}" == identity:
                    found = True
                if found:
                    retained.append(candidate)
            if not found:
                raise RuntimeError("Previous log generation unavailable (removed, compressed, or outside filename prefix); cursor retained")
            candidates = retained
        chunks: list[bytes] = []
        for candidate in [*candidates, path]:
            # Keep the active descriptor stable even if rotation happens during reading.
            stream = active if candidate == path else candidate.open("rb")
            try:
                stat = os.fstat(stream.fileno())
                current_id = f"{stat.st_dev}:{stat.st_ino}"
                start = offset if current_id == identity else 0
                if start:
                    stream.seek(max(0, start - 256))
                    actual = hashlib.sha256(stream.read(min(start, 256))).hexdigest()
                    if stat.st_size < start or actual != anchor:
                        warnings.append("Log truncated or rewritten; restarting generation from byte zero")
                        start = 0
                stream.seek(start)
                data = stream.read(max(0, stat.st_size - start))
                end = data.rfind(b"\n") + 1
                if candidate != path and end != len(data):
                    raise RuntimeError("Rotated log has an incomplete final line; cursor retained")
                chunks.append(data[:end])
                position = start + end
                stream.seek(max(0, position - 256))
                digest = hashlib.sha256(stream.read(min(position, 256))).hexdigest()
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
