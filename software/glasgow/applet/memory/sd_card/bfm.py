from . import BLOCK_SIZE, crc7, crc16


__all__ = ["SDHostBFM"]


class SDHostBFM:
    """Small SD-native host bus-functional model for applet simulations."""

    def __init__(self, context, testcase, *, clk, cmd, dat, half_period=250e-9):
        self.context = context
        self.testcase = testcase
        self.clk = clk
        self.cmd = cmd
        self.dat = tuple(dat)
        self.half_period = half_period

        context.set(clk.i, 0)
        context.set(cmd.i, 1)
        for pin in self.dat:
            context.set(pin.i, 1)

    async def cycle(self, command_bit=1):
        self.context.set(self.cmd.i, command_bit)
        await self.context.delay(self.half_period)
        self.context.set(self.clk.i, 1)
        await self.context.delay(self.half_period)
        sample = (
            self.context.get(self.cmd.oe), self.context.get(self.cmd.o),
            tuple((self.context.get(pin.oe), self.context.get(pin.o)) for pin in self.dat),
        )
        self.context.set(self.clk.i, 0)
        await self.context.delay(self.half_period)
        return sample

    async def idle(self, cycles):
        for _ in range(cycles):
            await self.cycle()

    async def send_command(self, index, argument=0, *, valid_crc=True):
        protected = (1 << 38) | (index << 32) | argument
        command_crc = crc7(protected, 40)
        if not valid_crc:
            command_crc ^= 1
        frame = (protected << 8) | (command_crc << 1) | 1
        for bitno in reversed(range(48)):
            await self.cycle((frame >> bitno) & 1)

    async def receive_response(self, length, *, timeout_cycles=16):
        bits = []
        started = False
        for _ in range(length + timeout_cycles):
            command_oe, command_o, _ = await self.cycle()
            # A card may turn the bidirectional CMD buffer around while keeping the line at its
            # idle-high level. The wire-format response starts at the first driven low bit, not
            # at output-enable assertion.
            if not started:
                started = command_oe and command_o == 0
            if started:
                self.testcase.assertEqual(command_oe, 1)
                bits.append(command_o)
                if len(bits) == length:
                    break
        self.testcase.assertEqual(len(bits), length)
        return sum(bit << bitno for bitno, bit in enumerate(reversed(bits)))

    def assert_r1(self, response, index):
        self.testcase.assertEqual(response >> 46, 0)
        self.testcase.assertEqual((response >> 40) & 0x3f, index, hex(response))
        self.testcase.assertEqual((response >> 1) & 0x7f, crc7(response >> 8, 40))
        self.testcase.assertEqual(response & 1, 1)

    async def receive_data_block(self, expected, *, timeout_cycles=2048,
            timeout_diagnostic=None):
        for _ in range(timeout_cycles):
            _, _, data_sample = await self.cycle()
            if data_sample[0] == (1, 0):
                break
        else:
            diagnostic = ""
            if timeout_diagnostic is not None:
                diagnostic = f" ({await timeout_diagnostic()})"
            self.testcase.fail(f"timed out waiting for SD data start bit{diagnostic}")

        block = 0
        for _ in range(len(expected) * 8):
            _, _, data_sample = await self.cycle()
            self.testcase.assertEqual(data_sample[0][0], 1)
            block = (block << 1) | data_sample[0][1]
        block = block.to_bytes(len(expected), "big")
        self.testcase.assertEqual(block, expected)

        received_crc = 0
        for _ in range(16):
            _, _, data_sample = await self.cycle()
            received_crc = (received_crc << 1) | data_sample[0][1]
        self.testcase.assertEqual(received_crc, crc16(block))

        _, _, data_sample = await self.cycle()
        self.testcase.assertEqual(data_sample[0], (1, 1))

    async def receive_data_block_4bit(self, expected, *, timeout_cycles=2048):
        for _ in range(timeout_cycles):
            _, _, pins = await self.cycle()
            if pins == ((1, 0),) * 4:
                break
        else:
            self.testcase.fail("timed out waiting for a four-bit SD data start")

        lane_bits = [[] for _ in self.dat]
        for octet in expected:
            for nibble in (octet >> 4, octet & 0xf):
                _, _, pins = await self.cycle()
                self.testcase.assertEqual(pins, tuple((1, (nibble >> lane) & 1)
                    for lane in range(4)))
                for lane, (_, bit) in enumerate(pins):
                    lane_bits[lane].append(bit)

        lane_crcs = [crc16(bytes(
            sum(bit << (7 - index) for index, bit in enumerate(bits[offset:offset + 8]))
            for offset in range(0, len(bits), 8))) for bits in lane_bits]
        for bitno in reversed(range(16)):
            _, _, pins = await self.cycle()
            self.testcase.assertEqual(pins, tuple((1, (crc >> bitno) & 1)
                for crc in lane_crcs))
        _, _, pins = await self.cycle()
        self.testcase.assertEqual(pins, ((1, 1),) * 4)

    def drive_data(self, value):
        for lane, pin in enumerate(self.dat):
            self.context.set(pin.i, (value >> lane) & 1)

    async def send_data_block_1bit(self, block, *, valid_crc=True):
        self.drive_data(0xf)
        await self.cycle()
        await self.cycle()
        self.drive_data(0xe)
        await self.cycle()
        for octet in block:
            for bitno in reversed(range(8)):
                self.drive_data(0xe | ((octet >> bitno) & 1))
                await self.cycle()
        block_crc = crc16(block) ^ (1 if not valid_crc else 0)
        for bitno in reversed(range(16)):
            self.drive_data(0xe | ((block_crc >> bitno) & 1))
            await self.cycle()
        self.drive_data(0xf)
        await self.cycle()

    async def send_data_block_4bit(self, block, *, valid_crc=True):
        lane_bits = [[] for _ in range(4)]
        self.drive_data(0xf)
        await self.cycle()
        await self.cycle()
        self.drive_data(0)
        await self.cycle()
        for octet in block:
            for nibble in (octet >> 4, octet & 0xf):
                self.drive_data(nibble)
                await self.cycle()
                for lane in range(4):
                    lane_bits[lane].append((nibble >> lane) & 1)

        lane_crcs = []
        for bits in lane_bits:
            lane_data = bytes(
                sum(bit << (7 - index) for index, bit in enumerate(bits[offset:offset + 8]))
                for offset in range(0, len(bits), 8))
            lane_crc = crc16(lane_data)
            lane_crcs.append(lane_crc ^ (1 if not valid_crc else 0))
        for bitno in reversed(range(16)):
            self.drive_data(sum(((lane_crcs[lane] >> bitno) & 1) << lane
                                for lane in range(4)))
            await self.cycle()
        self.drive_data(0xf)
        await self.cycle()

    async def receive_data_response(self, *, timeout_cycles=32):
        response = []
        for _ in range(timeout_cycles):
            _, _, data_sample = await self.cycle()
            if data_sample[0][0]:
                response.append(data_sample[0][1])
                if len(response) == 5:
                    break
        self.testcase.assertEqual(len(response), 5)
        return response
