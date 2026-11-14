#!/usr/bin/env python3
"""Check USB4STREAM delivery without assuming read/write record boundaries."""

import argparse
import json
import os
from pathlib import Path
import select
import struct
import time
import zlib


HEADER = struct.Struct("!4s8sQQ")
SIZE = HEADER.size + 4


def packet(tag, session, sequence):
    header = HEADER.pack(b"TBS1", tag, session, sequence)
    return header + struct.pack("!I", zlib.crc32(header))


class Decoder:
    def __init__(self, tag, session):
        self.tag = tag.ljust(8, b"\0")
        self.session = session
        self.pending = bytearray()
        self.count = 0

    def feed(self, data):
        self.pending.extend(data)
        while len(self.pending) >= SIZE:
            raw = bytes(self.pending[:SIZE])
            del self.pending[:SIZE]
            magic, tag, session, sequence = HEADER.unpack(raw[:-4])
            crc, = struct.unpack("!I", raw[-4:])
            if magic != b"TBS1" or zlib.crc32(raw[:-4]) != crc:
                raise ValueError("corrupt or misaligned record")
            if tag != self.tag or session != self.session:
                raise ValueError("wrong tag/session: stale or unrelated traffic")
            if sequence != self.count + 1:
                raise ValueError(f"expected sequence {self.count + 1}, got {sequence}")
            self.count += 1

    def finish(self, expected):
        if self.pending or self.count != expected:
            raise ValueError(f"received {self.count}/{expected} records; "
                             f"{len(self.pending)} trailing bytes")


def write_all(fd, data, deadline):
    remaining = memoryview(data)
    while remaining:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise TimeoutError("write deadline exceeded")
        if not select.select([], [fd], [], timeout)[1]:
            raise TimeoutError("write readiness deadline exceeded")
        try:
            written = os.write(fd, remaining)
        except BlockingIOError:
            continue
        if written == 0:
            raise OSError("write returned zero")
        remaining = remaining[written:]


def resolve_device(stream, config_root=Path("/sys/kernel/config/thunderbolt/stream"),
                   sysfs_root=Path("/sys/bus/thunderbolt/devices"), dev_root=Path("/dev")):
    stream = Path(stream).resolve(strict=True)
    root = config_root.resolve(strict=True)
    if stream.parent.parent != root:
        raise ValueError("--stream must identify a stream item below the configfs stream root")
    service = sysfs_root / stream.parent.name
    if not service.exists() or not (service / "driver").exists():
        raise ValueError("stream has no currently bound Thunderbolt service")
    if (service / "driver").resolve().name != "thunderbolt_stream":
        raise ValueError("selected service is not bound to thunderbolt_stream")
    index = int((stream / "index").read_text().strip())
    if index < 0:
        raise ValueError("invalid stream index")
    return dev_root / f"tbstream{index}", service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("recv", "sparse", "solo"))
    parser.add_argument("--stream", required=True, help="exact configfs stream item")
    parser.add_argument("--session", required=True, type=lambda s: int(s, 16),
                        help="shared 64-bit hex ID; use a new ID for each run")
    parser.add_argument("--tag", default="P", help="1-8 ASCII letters/digits/underscores")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--gap", type=float, default=5)
    parser.add_argument("--idle", type=float, default=0)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if not (args.tag.isascii() and 1 <= len(args.tag) <= 8 and
            args.tag.replace("_", "A").isalnum()):
        parser.error("invalid tag")
    if not 0 <= args.session < 2**64 or args.count <= 0:
        parser.error("session must fit 64 bits and count must be positive")
    if args.gap < 0 or args.idle < 0 or args.timeout <= 0:
        parser.error("invalid timing parameters")
    tag = args.tag.encode("ascii")
    count = 1 if args.mode == "solo" else args.count
    idle = (args.idle or 40) if args.mode == "solo" else args.idle
    fd = None
    try:
        device, service = resolve_device(args.stream)
        fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
        # Recheck mapping after open rather than trusting a cached /dev number.
        if resolve_device(args.stream)[0] != device:
            raise ValueError("stream index changed during open")
        print(json.dumps({"event": "ready", "device": str(device),
                          "service": str(service.resolve()), "stream": args.stream,
                          "session": f"{args.session:016x}", "tag": args.tag}), flush=True)
        deadline = time.monotonic() + args.timeout
        if args.mode == "recv":
            decoder = Decoder(tag, args.session)
            eof = False
            while time.monotonic() < deadline:
                timeout = max(0, deadline - time.monotonic())
                if not select.select([fd], [], [], timeout)[0]:
                    break
                try:
                    data = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                if not data:
                    eof = True
                    break
                decoder.feed(data)
            decoder.finish(count)
            if not eof:
                raise TimeoutError("all records arrived, but peer EOF did not")
            print(json.dumps({"event": "received", "verified_records": decoder.count,
                              "eof": eof}), flush=True)
        else:
            for sequence in range(1, count + 1):
                delay = idle if sequence == 1 else args.gap
                if time.monotonic() + delay >= deadline:
                    raise TimeoutError("send schedule exceeds deadline")
                time.sleep(delay)
                write_all(fd, packet(tag, args.session, sequence), deadline)
            # These are local writes, not delivery acknowledgements.
            print(json.dumps({"event": "submitted", "records": count}), flush=True)
    except (OSError, ValueError) as error:
        print(json.dumps({"event": "failed", "error": str(error)}), flush=True)
        return 1
    finally:
        if fd is not None:
            os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
