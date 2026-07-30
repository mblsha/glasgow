import io
from types import SimpleNamespace
import unittest

from glasgow.hardware.device import GlasgowDevice


class FakeDevice:
    def __init__(self, current_id):
        self.current_id = current_id
        self.downloads = []

    async def bitstream_id(self):
        return self.current_id

    async def download_bitstream(self, bitstream, bitstream_id):
        self.downloads.append((bitstream, bitstream_id))
        self.current_id = bitstream_id


def prebuilt_file(bitstream_id, payload=b"gateware"):
    stream = io.BytesIO(bitstream_id + payload)
    stream.name = "test-prebuilt.bin"
    return stream


class DownloadPrebuiltTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_mismatched_prebuilt_replaces_current_design(self):
        design_id = bytes.fromhex("11" * 16)
        prebuilt_id = bytes.fromhex("22" * 16)
        device = FakeDevice(design_id)
        plan = SimpleNamespace(bitstream_id=design_id)

        await GlasgowDevice.download_prebuilt(
            device, plan, prebuilt_file(prebuilt_id)
        )

        self.assertEqual(device.downloads, [(b"gateware", prebuilt_id)])
        self.assertEqual(device.current_id, prebuilt_id)

    async def test_matching_prebuilt_is_not_downloaded_again(self):
        design_id = bytes.fromhex("11" * 16)
        prebuilt_id = bytes.fromhex("22" * 16)
        device = FakeDevice(prebuilt_id)
        plan = SimpleNamespace(bitstream_id=design_id)

        await GlasgowDevice.download_prebuilt(
            device, plan, prebuilt_file(prebuilt_id)
        )

        self.assertEqual(device.downloads, [])

    async def test_all_ones_header_still_forces_design_id(self):
        design_id = bytes.fromhex("11" * 16)
        device = FakeDevice(design_id)
        plan = SimpleNamespace(bitstream_id=design_id)

        await GlasgowDevice.download_prebuilt(
            device, plan, prebuilt_file(b"\xff" * 16)
        )

        self.assertEqual(device.downloads, [(b"gateware", design_id)])
