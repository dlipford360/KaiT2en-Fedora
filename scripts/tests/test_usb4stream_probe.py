import importlib.util
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "probe", Path(__file__).with_name("usb4stream-probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def test_configfs_index_not_hardcoded_device_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            item = config / "0-1.1" / "probe"
            item.mkdir(parents=True)
            (item / "index").write_text("7\n")
            sysfs = root / "sysfs"
            service = sysfs / "0-1.1"
            service.mkdir(parents=True)
            driver = root / "thunderbolt_stream"
            driver.mkdir()
            (service / "driver").symlink_to(driver, target_is_directory=True)
            device, found = probe.resolve_device(item, config, sysfs, root / "dev")
            self.assertEqual(device, root / "dev/tbstream7")
            self.assertEqual(found, service)
            (service / "driver").unlink()
            with self.assertRaises(ValueError):
                probe.resolve_device(item, config, sysfs, root / "dev")

    def test_all_read_split_positions(self):
        data = b"".join(probe.packet(b"AB", 123, i) for i in range(1, 4))
        for split in range(len(data) + 1):
            decoder = probe.Decoder(b"AB", 123)
            decoder.feed(data[:split])
            decoder.feed(data[split:])
            decoder.finish(3)

    def test_one_byte_reads(self):
        decoder = probe.Decoder(b"AB", 123)
        for byte in probe.packet(b"AB", 123, 1):
            decoder.feed(bytes([byte]))
        decoder.finish(1)

    def test_wrong_full_tag_session_gap_and_corruption(self):
        for data in (probe.packet(b"AC", 123, 1), probe.packet(b"AB", 124, 1),
                     probe.packet(b"AB", 123, 2),
                     probe.packet(b"AB", 123, 1)[:-1] + b"X"):
            with self.assertRaises(ValueError):
                probe.Decoder(b"AB", 123).feed(data)

    def test_duplicate_and_truncated_record(self):
        data = probe.packet(b"P", 1, 1)
        decoder = probe.Decoder(b"P", 1)
        decoder.feed(data)
        with self.assertRaises(ValueError):
            decoder.feed(data)
        decoder = probe.Decoder(b"P", 1)
        decoder.feed(data[:17])
        with self.assertRaises(ValueError):
            decoder.finish(1)

    def test_short_writes_and_eagain(self):
        accepted = bytearray()
        calls = 0

        def write(fd, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise BlockingIOError()
            accepted.extend(data[:3])
            return min(3, len(data))

        with patch.object(probe.select, "select", return_value=([], [7], [])), \
             patch.object(probe.os, "write", side_effect=write):
            probe.write_all(7, b"0123456789", probe.time.monotonic() + 5)
        self.assertEqual(accepted, b"0123456789")


if __name__ == "__main__":
    unittest.main()
