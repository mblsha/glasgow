import asyncio
import os
import struct
import tempfile
import time
import unittest
from unittest import mock

from glasgow.gateware import cobs as gateware_cobs

from . import (
    BLOCK_SIZE, BackendError, BackendStatus, BackendWorker, FileBlockBackend,
    HostEvent, HostReply, ReplyFrame, RequestFrame, SDCardDiagnostics, SDCardError,
)
from ._fat16 import FAT16Image, create_fat16_image
from ._protocol import (
    HOST_OPERATIONS, HOST_OPERATION_FOR_EVENT, HOST_OPERATION_FOR_REPLY,
    FrameError, frame_crc16,
)
from ._test_support import (
    ImmediateRegister as _ImmediateRegister,
    InlineWorker as _InlineWorker, MemoryBackend as _MemoryBackend,
    TestSDCardInterface, packed_diagnostics,
)


GENERATION = 0x4321
REQUEST_ID = 0x1234


class SDCardInterfaceTestCase(unittest.TestCase):
    def make_iface(self, backend, request, *, generation=GENERATION, sync_writes=True):
        return TestSDCardInterface(
            backend, request, generation=generation, sync_writes=sync_writes)

    def request(self, opcode, payload=b"", *, generation=GENERATION, request_id=REQUEST_ID):
        return RequestFrame.create(opcode, generation, request_id, payload).encode_packet()

    def reply(self, pipe):
        reply = ReplyFrame.decode_packet(bytes(pipe.replies))
        return reply.header, reply.payload

    def test_host_read_event(self):
        image = bytes(index & 0xff for index in range(BLOCK_SIZE * 4))
        backend = _MemoryBackend(image)
        request = self.request(HostEvent.Read, struct.pack("<IB", BLOCK_SIZE, 1))
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        self.assertEqual(
            iface._read_pipe.replies, image[BLOCK_SIZE:BLOCK_SIZE * 2])
        self.assertEqual(iface._read_pipe.flushes, 1)
        self.assertEqual(iface._pipe.replies, b"")

    def test_host_read_error_is_reported(self):
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
        request = self.request(HostEvent.Read, struct.pack("<IB", 1, 1))
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        self.assertEqual(iface._read_pipe.replies, bytes([0xff]) * BLOCK_SIZE)
        self.assertEqual(iface._host_backend_errors, 1)

    def test_host_write_event(self):
        image_size = BLOCK_SIZE * 4
        payload = bytes([0xa5]) * BLOCK_SIZE
        backend = _MemoryBackend(bytes(image_size))
        request = self.request(
            HostEvent.Write, struct.pack("<I", BLOCK_SIZE) + payload)
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        header, reply_payload = self.reply(iface._pipe)
        self.assertEqual(header.opcode, HostReply.WriteAck)
        self.assertEqual(header.status, BackendStatus.OK)
        self.assertEqual(reply_payload, b"")
        self.assertEqual(backend.image[BLOCK_SIZE:BLOCK_SIZE * 2], payload)
        self.assertEqual(backend.flushes, 1)

    def test_volatile_write_skips_per_request_flush(self):
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
        payload = bytes([0xa5]) * BLOCK_SIZE
        request = self.request(
            HostEvent.Write, struct.pack("<I", BLOCK_SIZE) + payload)
        iface = self.make_iface(backend, request, sync_writes=False)
        asyncio.run(iface.serve_once())
        header, _ = self.reply(iface._pipe)
        self.assertEqual(header.status, BackendStatus.OK)
        self.assertEqual(backend.flushes, 0)

    def test_host_erase_event(self):
        image_size = BLOCK_SIZE * 4
        backend = _MemoryBackend(bytes([0x55]) * image_size)
        request = self.request(HostEvent.Erase, struct.pack(
            "<II", BLOCK_SIZE, BLOCK_SIZE * 2))
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        header, payload = self.reply(iface._pipe)
        self.assertEqual(header.opcode, HostReply.EraseAck)
        self.assertEqual(header.status, BackendStatus.OK)
        self.assertEqual(payload, b"")
        self.assertEqual(
            backend.image[BLOCK_SIZE:BLOCK_SIZE * 3], bytes([0xff]) * BLOCK_SIZE * 2)
        self.assertEqual(backend.flushes, 1)

    def test_stale_generation_is_discarded(self):
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
        payload = bytes([0xa5]) * BLOCK_SIZE
        request = self.request(
            HostEvent.Write, struct.pack("<I", BLOCK_SIZE) + payload,
            generation=GENERATION ^ 0xffff)
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        self.assertEqual(iface._pipe.replies, b"")
        self.assertEqual(backend.image, bytes(BLOCK_SIZE * 4))

    def test_read_only_backend_reports_status(self):
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4), read_only=True)
        payload = bytes([0xa5]) * BLOCK_SIZE
        request = self.request(
            HostEvent.Write, struct.pack("<I", BLOCK_SIZE) + payload)
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        header, reply_payload = self.reply(iface._pipe)
        self.assertEqual(header.opcode, HostReply.WriteAck)
        self.assertEqual(header.status, BackendStatus.ReadOnly)
        self.assertEqual(reply_payload, b"")
        self.assertEqual(iface._host_backend_errors, 1)

    def test_malformed_frame_is_discarded_before_next_request(self):
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
        malformed = bytearray(self.request(HostEvent.Read, struct.pack("<IB", 0, 1)))
        malformed[2] ^= 0x40
        valid = self.request(HostEvent.Read, struct.pack("<IB", BLOCK_SIZE, 1))
        iface = self.make_iface(backend, bytes(malformed) + valid)
        asyncio.run(iface.serve_once())
        self.assertEqual(iface._read_pipe.replies, bytes(BLOCK_SIZE))

    def test_semantically_invalid_request_length_is_rejected(self):
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
        request = self.request(HostEvent.Write, b"too short")
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        header, payload = self.reply(iface._pipe)
        self.assertEqual(header.opcode, HostReply.WriteAck)
        self.assertEqual(header.status, BackendStatus.Invalid)
        self.assertEqual(payload, b"")

    def test_named_diagnostics_snapshot(self):
        class Worker:
            service_count = 91
            max_service_latency = 0.125

        field_names = (
            "sd_clocks", "command_low_clocks", "commands", "reads", "writes",
            "protocol_errors", "backend_errors", "stale_replies", "buffer_faults",
            "prefetch_waits", "prefetch_wait_cycles", "max_prefetch_wait_cycles",
            "max_host_service_latency_cycles",
        )
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
        iface = self.make_iface(backend, b"")
        iface._session = iface._session.__class__(backend, Worker(), True)
        iface._diagnostics_snapshot.value = packed_diagnostics(**{
            name: value for value, name in enumerate(field_names, 1)
        })
        iface._host_backend_errors = 14
        iface._flush_count = 15

        diagnostics = asyncio.run(iface.read_diagnostics())
        self.assertIsInstance(diagnostics, SDCardDiagnostics)
        self.assertEqual(diagnostics.sd_clocks, 1)
        self.assertEqual(diagnostics.prefetch_wait_cycles, 11)
        self.assertEqual(diagnostics.max_prefetch_wait_cycles, 12)
        self.assertEqual(diagnostics.max_host_service_latency_cycles, 13)
        self.assertEqual(diagnostics.host_backend_errors, 14)
        self.assertEqual(diagnostics.durable_flushes, 15)
        self.assertEqual(diagnostics.backend_calls, 91)
        self.assertEqual(diagnostics.max_backend_call_seconds, 0.125)
        self.assertEqual(iface._diagnostics_snapshot.reads, 1)

    def test_close_flushes_before_final_diagnostics(self):
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
        iface = self.make_iface(backend, b"")
        iface._active = True
        field_names = (
            "sd_clocks", "command_low_clocks", "commands", "reads", "writes",
            "protocol_errors", "backend_errors", "stale_replies", "buffer_faults",
            "prefetch_waits", "prefetch_wait_cycles", "max_prefetch_wait_cycles",
            "max_host_service_latency_cycles",
        )
        iface._diagnostics_snapshot.value = packed_diagnostics(**{
            name: value for value, name in enumerate(field_names, 1)
        })
        diagnostics = []
        iface._log_diagnostics = diagnostics.append

        asyncio.run(iface.close())

        self.assertEqual(iface._enabled.value, 0)
        self.assertFalse(iface._active)
        self.assertEqual(backend.flushes, 1)
        self.assertTrue(backend.closed)
        self.assertIsNone(iface.backend)
        self.assertIsNone(iface._session)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].sd_clocks, 1)
        self.assertEqual(diagnostics[0].max_host_service_latency_cycles, 13)
        self.assertEqual(diagnostics[0].durable_flushes, 1)

    def test_close_still_retires_backend_when_diagnostics_fail(self):
        class FailingRegister(_ImmediateRegister):
            def __await__(self):
                async def fail():
                    raise OSError("diagnostics unavailable")
                return fail().__await__()

        async def scenario():
            backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
            iface = self.make_iface(backend, b"")
            iface._diagnostics_snapshot = FailingRegister()
            with self.assertRaisesRegex(OSError, "diagnostics unavailable"):
                await iface.close()
            self.assertIsNone(iface.backend)
            self.assertFalse(iface._active)
            self.assertTrue(backend.closed)
            self.assertEqual(backend.flushes, 1)

        asyncio.run(scenario())

    def test_reconfigure_retires_old_session_before_enabling_new_one(self):
        events = []

        class NamedBackend(_MemoryBackend):
            def __init__(self, name):
                super().__init__(bytes(BLOCK_SIZE * 4))
                self.name = name

        class TrackedWorker(_InlineWorker):
            async def flush(self):
                events.append(f"{self.backend.name}:flush")
                return await super().flush()

            async def close(self):
                events.append(f"{self.backend.name}:close")
                return await super().close()

        class EnabledRegister(_ImmediateRegister):
            async def set(self, value):
                events.append(f"enabled:{value}")
                await super().set(value)

        async def scenario():
            old = NamedBackend("old")
            new = NamedBackend("new")
            iface = TestSDCardInterface(old, worker_factory=TrackedWorker)
            iface._enabled = EnabledRegister(1)
            await iface.configure(backend=new)
            self.assertIs(iface.backend, new)
            self.assertEqual(events[:4], [
                "enabled:0", "old:flush", "old:close", "enabled:1"])
            self.assertTrue(old.closed)
            self.assertFalse(new.closed)
            await iface.close()

        asyncio.run(scenario())

    def test_configure_enables_gateware_boot_bank_priming(self):
        async def scenario():
            image = bytes(index & 0xff for index in range(BLOCK_SIZE * 4))
            backend = _MemoryBackend(image)
            iface = TestSDCardInterface()

            await iface.configure(backend=backend, prime_first_read=True)

            self.assertEqual(iface._prime_first_read_reg.value, 1)
            self.assertEqual(iface._pipe.replies, b"")
            self.assertEqual(iface._pipe.flushes, 0)
            await iface.close()

        asyncio.run(scenario())

    def test_configure_can_leave_card_inactive_until_target_power_returns(self):
        async def scenario():
            backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
            iface = TestSDCardInterface()

            await iface.configure(backend=backend, activate=False)

            self.assertIs(iface.backend, backend)
            self.assertEqual(iface._enabled.value, 0)
            self.assertFalse(iface._active)
            await iface.close()

        asyncio.run(scenario())

    def test_configure_rejects_image_that_does_not_match_embedded_boot_prefix(self):
        async def scenario():
            backend = _MemoryBackend(bytes([0x55]) * BLOCK_SIZE * 4)
            iface = TestSDCardInterface()
            iface._boot_data = bytes([0xaa]) * BLOCK_SIZE * 2
            iface._boot_addresses = (0, BLOCK_SIZE * 3)

            with self.assertRaisesRegex(
                    SDCardError, "do not match the data embedded"):
                await iface.configure(backend=backend, prime_first_read=True)

            self.assertTrue(backend.closed)
            self.assertIsNone(iface.backend)
            self.assertFalse(iface._active)

        asyncio.run(scenario())

    def test_reconfigure_failure_disables_and_closes_both_sessions(self):
        events = []

        class NamedBackend(_MemoryBackend):
            def __init__(self, name):
                super().__init__(bytes(BLOCK_SIZE * 4))
                self.name = name

        class FailingWorker(_InlineWorker):
            async def flush(self):
                events.append(f"{self.backend.name}:flush")
                if self.backend.name == "old":
                    raise BackendError(BackendStatus.IOError, "flush failed")
                return await super().flush()

            async def close(self):
                events.append(f"{self.backend.name}:close")
                return await super().close()

        async def scenario():
            old = NamedBackend("old")
            new = NamedBackend("new")
            iface = TestSDCardInterface(old, worker_factory=FailingWorker)
            with self.assertRaises(BackendError):
                await iface.configure(backend=new)
            self.assertEqual(events, ["old:flush", "old:close", "new:close"])
            self.assertIsNone(iface.backend)
            self.assertEqual(iface._enabled.value, 0)
            self.assertFalse(iface._active)
            self.assertTrue(old.closed)
            self.assertTrue(new.closed)

        asyncio.run(scenario())

    def test_close_waits_for_in_flight_backend_request(self):
        events = []
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingWorker(_InlineWorker):
            async def read_block(self, address):
                events.append("read:start")
                entered.set()
                await release.wait()
                events.append("read:done")
                return await super().read_block(address)

            async def close(self):
                events.append("close")
                return await super().close()

        async def scenario():
            backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
            request = self.request(HostEvent.Read, struct.pack("<IB", 0, 1))
            iface = TestSDCardInterface(
                backend, request, worker_factory=BlockingWorker)
            service = asyncio.create_task(iface.serve_once())
            await entered.wait()
            closing = asyncio.create_task(iface.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            self.assertNotIn("close", events)
            release.set()
            await asyncio.gather(service, closing)
            self.assertLess(events.index("read:done"), events.index("close"))
            self.assertTrue(backend.closed)

        asyncio.run(scenario())

    def test_power_monitor_suspends_and_resumes(self):
        class Device:
            def __init__(self):
                self.samples = iter((3.3, 0.0, 0.0, 3.3, 3.3))
                self.last = 3.3

            async def measure_voltage(self, port):
                self.last = next(self.samples, self.last)
                return self.last

        async def scenario():
            iface = TestSDCardInterface(_MemoryBackend(bytes(BLOCK_SIZE * 4)))
            transitions = []
            complete = asyncio.Event()

            async def flush():
                transitions.append("flush")

            async def suspend(*, reason):
                transitions.append("suspend")
                iface._active = False

            async def resume(*, reason):
                transitions.append("resume")
                iface._active = True
                complete.set()

            iface.flush = flush
            iface.suspend = suspend
            iface.resume = resume
            task = asyncio.create_task(iface.monitor_power(
                Device(), "B", interval=0.001, debounce_samples=2))
            await asyncio.wait_for(complete.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(transitions, ["suspend", "flush", "resume"])

        asyncio.run(scenario())

    def test_power_monitor_rearms_before_on_and_suppresses_late_chatter(self):
        class Device:
            def __init__(self):
                self.samples = iter((
                    0.0, 0.0,
                    3.3, 3.3,
                    2.36, 2.36,
                ))
                self.last = 2.36
                self.transitions = []
                self.delayed_rise = False

            async def measure_voltage(self, port):
                self.last = next(self.samples, self.last)
                if self.last == 3.3 and not self.delayed_rise:
                    self.delayed_rise = True
                    # The card may remain off longer than the chatter cooldown before ON.
                    # The subsequent rise must restart that cooldown.
                    await asyncio.sleep(0.02)
                return self.last

            async def set_pulls(self, port, low=set(), high=set()):
                self.transitions.append(
                    ("pulls", port, frozenset(low), frozenset(high)))

        async def scenario():
            device = Device()
            backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
            iface = TestSDCardInterface(backend)
            complete = asyncio.Event()
            original_rearm = iface.rearm

            async def flush():
                device.transitions.append(("flush",))
                await iface._flush_session(iface._session)
                if backend.flushes == 2:
                    complete.set()

            async def rearm(**kwargs):
                device.transitions.append(("rearm",))
                self.assertEqual(device.transitions[-2][0], "pulls")
                self.assertEqual(device.transitions[-2][-1], frozenset())
                await original_rearm(**kwargs)

            iface.flush = flush
            iface.rearm = rearm
            generation = iface._generation
            task = asyncio.create_task(iface.monitor_power(
                device, "B", off_threshold=2.7, on_threshold=3.0,
                interval=0.001, debounce_samples=2,
                manage_signal_pulls=True, resume_settle=0,
                rearm_min_interval=0.01, rearm_reset_hold=0))
            await asyncio.wait_for(complete.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            self.assertEqual(
                [transition[0] for transition in device.transitions],
                ["pulls", "rearm", "flush", "pulls", "pulls", "flush"])
            self.assertEqual(
                device.transitions[0],
                ("pulls", "B", frozenset({2}), frozenset()))
            self.assertTrue(iface._active)
            self.assertIs(iface.backend, backend)
            self.assertFalse(backend.closed)
            self.assertEqual(backend.flushes, 2)
            self.assertNotEqual(iface._generation, generation)
            self.assertEqual(iface._enabled.writes, [0, 1])
            self.assertEqual(iface._pipe.resets, 1)
            self.assertEqual(iface._read_pipe.resets, 1)

        asyncio.run(scenario())

    def test_rearm_holds_reset_while_external_sd_clock_is_stopped(self):
        async def scenario():
            backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
            iface = TestSDCardInterface(backend)
            observed_holds = []

            async def observe_sleep(delay):
                observed_holds.append(delay)
                self.assertFalse(iface._active)
                self.assertEqual(iface._enabled.value, 0)
                self.assertEqual(iface._pipe.resets, 0)
                self.assertEqual(iface._read_pipe.resets, 0)
                self.assertEqual(iface._pipe.pauses, 1)
                self.assertEqual(iface._read_pipe.pauses, 1)
                self.assertEqual(iface._pipe.resumes, 0)
                self.assertEqual(iface._read_pipe.resumes, 0)

            with mock.patch(
                    "glasgow.applet.memory.sd_card.asyncio.sleep",
                    side_effect=observe_sleep):
                await iface.rearm(reason="clockless target-off regression", reset_hold=0.125)

            self.assertEqual(observed_holds, [0.125])
            self.assertEqual(iface._enabled.writes, [0, 1])
            self.assertEqual(iface._enabled.value, 1)
            self.assertTrue(iface._active)
            self.assertIs(iface.backend, backend)
            self.assertFalse(backend.closed)
            self.assertEqual(iface._pipe.resets, 0)
            self.assertEqual(iface._read_pipe.resets, 0)
            self.assertEqual(iface._pipe.pauses, 1)
            self.assertEqual(iface._read_pipe.pauses, 1)
            self.assertEqual(iface._pipe.resumes, 1)
            self.assertEqual(iface._read_pipe.resumes, 1)

        asyncio.run(scenario())

    def test_transport_quarantine_resumes_every_pipe_after_cleanup_error(self):
        async def scenario():
            iface = TestSDCardInterface(_MemoryBackend(bytes(BLOCK_SIZE * 4)))
            original_resume = iface._pipe.resume

            async def failing_resume():
                await original_resume()
                raise OSError("injected control-pipe resume failure")

            iface._pipe.resume = failing_resume
            with self.assertRaisesRegex(OSError, "injected control-pipe resume failure"):
                await iface._quarantine_transport(0)

            self.assertEqual(iface._pipe.pauses, 1)
            self.assertEqual(iface._pipe.resumes, 1)
            self.assertEqual(iface._read_pipe.pauses, 1)
            self.assertEqual(iface._read_pipe.resumes, 1)

        asyncio.run(scenario())

    def test_rearm_restores_configuration_cleared_by_hardware_pipe_reset(self):
        async def scenario():
            backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
            iface = TestSDCardInterface()
            await iface.configure(
                backend=backend, prime_first_read=True, support_4bit=False,
                transfer_speed=1_000_000)

            configuration_registers = (
                iface._generation_reg,
                iface._read_only_reg,
                iface._prime_first_read_reg,
                iface._image_size,
                iface._cid,
                iface._csd,
                iface._scr,
            )
            expected = tuple(register.value for register in configuration_registers)
            original_reset_transport = iface._reset_transport

            async def destructive_hardware_reset():
                await original_reset_transport()
                # HardwareAssembly associates the applet's RW registers with the same domain
                # reset asserted by each pipe reset. Model that behavior rather than the lighter
                # fake-pipe reset used by the other host lifecycle tests.
                for register in configuration_registers:
                    register.value = 0

            iface._reset_transport = destructive_hardware_reset
            old_generation = iface._generation
            await iface.rearm(reason="destructive hardware reset", reset_hold=0)

            actual = tuple(register.value for register in configuration_registers)
            self.assertNotEqual(actual[0], expected[0])
            self.assertNotEqual(iface._generation, old_generation)
            self.assertEqual(actual[1:], expected[1:])
            self.assertEqual(iface._image_size.value, backend.size)
            self.assertEqual(iface._prime_first_read_reg.value, 1)
            self.assertTrue(iface._active)
            await iface.close()

        asyncio.run(scenario())

    def test_service_retains_backend_across_timed_transport_rearm(self):
        class ResetPipe:
            def __init__(self):
                self.receiving = asyncio.Event()
                self.receive = None
                self.resets = 0

            async def recv_until(self, delimiter):
                self.receive = asyncio.get_running_loop().create_future()
                self.receiving.set()
                return await self.receive

            async def reset(self):
                self.resets += 1
                if self.receive is not None and not self.receive.done():
                    self.receive.set_exception(OSError("transport reset"))

        async def scenario():
            backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
            iface = self.make_iface(backend, b"")
            pipe = ResetPipe()
            iface._pipe = pipe

            class DisableRegister(_ImmediateRegister):
                async def set(self, value):
                    if value == 0 and pipe.receive is not None and not pipe.receive.done():
                        # Hardware pipe reset cancels the blocked receive future; it does not
                        # cancel the long-running service task itself.
                        pipe.receive.cancel()
                        await asyncio.sleep(0)
                    await super().set(value)

            iface._enabled = DisableRegister(1)
            service = asyncio.create_task(iface.serve())
            await asyncio.wait_for(pipe.receiving.wait(), 1)

            pipe.receiving.clear()
            await iface.rearm(reason="test target-power loss", reset_hold=0)
            await asyncio.wait_for(pipe.receiving.wait(), 1)
            self.assertFalse(service.done())
            self.assertIs(iface.backend, backend)
            self.assertFalse(backend.closed)
            self.assertTrue(iface._active)
            self.assertEqual(iface._enabled.value, 1)
            self.assertGreaterEqual(pipe.resets, 1)

            service.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await service
            self.assertTrue(backend.closed)

        asyncio.run(scenario())


class SDCardProtocolTestCase(unittest.TestCase):
    def test_operation_schema_is_the_payload_source_of_truth(self):
        self.assertEqual(
            set(HOST_OPERATION_FOR_EVENT.values()), set(HOST_OPERATIONS))
        self.assertEqual(
            set(HOST_OPERATION_FOR_REPLY.values()), set(HOST_OPERATIONS))

        read = HOST_OPERATION_FOR_EVENT[HostEvent.Read]
        self.assertEqual(read.reply, HostReply.ReadData)
        encoded = read.request_payload.encode(address=0x1234_5600, blocks=2)
        self.assertEqual(read.request_payload.decode(encoded), {
            "address": 0x1234_5600,
            "blocks": 2,
        })

        write = HOST_OPERATION_FOR_EVENT[HostEvent.Write]
        block = bytes(range(256)) * 2
        encoded = write.request_payload.encode(address=BLOCK_SIZE, data=block)
        self.assertEqual(write.request_payload.decode(encoded), {
            "address": BLOCK_SIZE,
            "data": block,
        })
        self.assertEqual(write.reply_payload.size, 0)

        erase = HOST_OPERATION_FOR_EVENT[HostEvent.Erase]
        encoded = erase.request_payload.encode(start=BLOCK_SIZE, end=BLOCK_SIZE * 3)
        self.assertEqual(erase.request_payload.decode(encoded), {
            "start": BLOCK_SIZE,
            "end": BLOCK_SIZE * 3,
        })

    def test_request_and_reply_frames_round_trip(self):
        request = RequestFrame.create(
            HostEvent.Write, GENERATION, REQUEST_ID,
            struct.pack("<I", BLOCK_SIZE) + bytes([0xa5]) * BLOCK_SIZE)
        self.assertEqual(RequestFrame.decode_packet(request.encode_packet()).payload,
            request.payload)

        reply = ReplyFrame.create(
            HostReply.ReadData, GENERATION, REQUEST_ID,
            BackendStatus.OK, bytes(range(256)) * 2)
        decoded = ReplyFrame.decode_packet(reply.encode_packet())
        self.assertEqual(decoded.header.generation, GENERATION)
        self.assertEqual(decoded.payload, reply.payload)

    def test_frame_crc_rejects_corruption(self):
        packet = bytearray(RequestFrame.create(
            HostEvent.Read, GENERATION, REQUEST_ID, struct.pack("<IB", 0, 1)).encode_packet())
        packet[3] ^= 1
        with self.assertRaises(FrameError):
            RequestFrame.decode_packet(packet)
        self.assertEqual(frame_crc16(b"123456789"), 0x31c3)

    def test_unknown_opcode_is_a_frame_error(self):
        raw = struct.pack("<BBHHH", 1, 0x7f, GENERATION, REQUEST_ID, 0)
        packet = gateware_cobs.encode(raw + struct.pack("<H", frame_crc16(raw))) + b"\0"
        with self.assertRaises(FrameError):
            RequestFrame.decode_packet(packet)


class FAT16ImageTestCase(unittest.TestCase):
    def test_generator_is_deterministic_and_structurally_readable(self):
        files = {
            "HOST.TXT": b"glasgow-host-marker\n",
            "BLOCK.BIN": bytes(range(256)) * 7,
        }
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, "first.img")
            second = os.path.join(directory, "second.img")
            create_fat16_image(first, files)
            create_fat16_image(second, files)
            with open(first, "rb") as first_file, open(second, "rb") as second_file:
                self.assertEqual(first_file.read(), second_file.read())
            image = FAT16Image.from_path(first)
            self.assertEqual(image.read_file("host.txt"), files["HOST.TXT"])
            self.assertEqual(image.read_file("BLOCK.BIN"), files["BLOCK.BIN"])


class BackendWorkerTestCase(unittest.TestCase):
    def test_read_ahead_is_selected_by_backend_capability(self):
        class CountingBackend(_MemoryBackend):
            def __init__(self, image, *, preferred=1):
                super().__init__(image)
                self.preferred = preferred
                self.runs = []

            @property
            def preferred_read_ahead_blocks(self):
                return self.preferred

            def read_blocks(self, address, count):
                self.runs.append((address, count))
                return super().read_blocks(address, count)

        async def scenario():
            image = bytes(BLOCK_SIZE * 8)
            generic = CountingBackend(image)
            generic_worker = BackendWorker(generic)
            self.assertEqual(generic_worker.read_ahead_blocks, 1)
            await generic_worker.read_block(0)
            await generic_worker.read_block(BLOCK_SIZE)
            self.assertEqual(generic.runs, [(0, 1), (BLOCK_SIZE, 1)])
            await generic_worker.close()

            optimized = CountingBackend(image, preferred=4)
            optimized_worker = BackendWorker(optimized)
            self.assertEqual(optimized_worker.read_ahead_blocks, 4)
            await optimized_worker.read_block(0)
            await optimized_worker.read_block(BLOCK_SIZE)
            self.assertEqual(optimized.runs, [(0, 4)])
            await optimized_worker.close()

            with tempfile.NamedTemporaryFile() as image_file:
                image_file.truncate(BLOCK_SIZE * 8)
                file_backend = FileBlockBackend(image_file.name)
                file_worker = BackendWorker(file_backend)
                self.assertEqual(file_worker.read_ahead_blocks, 8192)
                await file_worker.close()

        asyncio.run(scenario())

    def test_sequential_reads_use_bounded_read_ahead_and_writes_invalidate_it(self):
        class CountingBackend(_MemoryBackend):
            def __init__(self, image):
                super().__init__(image)
                self.runs = []

            def read_blocks(self, address, count):
                self.runs.append((address, count))
                return super().read_blocks(address, count)

        async def scenario():
            image = b"".join(bytes([index]) * BLOCK_SIZE for index in range(8))
            backend = CountingBackend(image)
            worker = BackendWorker(backend, read_ahead_blocks=4)
            self.assertEqual(await worker.read_block(0), bytes([0]) * BLOCK_SIZE)
            self.assertEqual(await worker.read_block(BLOCK_SIZE), bytes([1]) * BLOCK_SIZE)
            self.assertEqual(backend.runs, [(0, 4)])

            replacement = bytes([0xa5]) * BLOCK_SIZE
            await worker.write_block(BLOCK_SIZE, replacement)
            self.assertEqual(await worker.read_block(BLOCK_SIZE), replacement)
            self.assertEqual(backend.runs, [(0, 4), (BLOCK_SIZE, 4)])
            await worker.close()

        asyncio.run(scenario())

    def test_blocking_backend_does_not_stall_event_loop(self):
        class SlowBackend(_MemoryBackend):
            def read_block(self, address):
                time.sleep(0.05)
                return super().read_block(address)

        async def scenario():
            backend = SlowBackend(bytes(BLOCK_SIZE * 4))
            worker = BackendWorker(backend, read_ahead_blocks=1)
            read = asyncio.create_task(worker.read_block(0))
            await asyncio.sleep(0.005)
            self.assertFalse(read.done())
            self.assertEqual(await read, bytes(BLOCK_SIZE))
            self.assertGreaterEqual(worker.max_service_latency, 0.05)
            self.assertGreaterEqual(worker.service_count, 1)
            await worker.close()
            self.assertTrue(backend.closed)

        asyncio.run(scenario())


class FileBlockBackendTestCase(unittest.TestCase):
    def test_file_backend_read_write_erase_and_flush(self):
        with tempfile.NamedTemporaryFile() as image:
            image.write(bytes([0x55]) * BLOCK_SIZE * 4)
            image.flush()
            backend = FileBlockBackend(image.name)
            try:
                self.assertEqual(backend.read_block(BLOCK_SIZE), bytes([0x55]) * BLOCK_SIZE)
                backend.write_block(BLOCK_SIZE, bytes([0xa5]) * BLOCK_SIZE)
                backend.erase(BLOCK_SIZE * 2, BLOCK_SIZE * 3)
                backend.flush()
                self.assertEqual(backend.read_block(BLOCK_SIZE), bytes([0xa5]) * BLOCK_SIZE)
                self.assertEqual(
                    backend.read_block(BLOCK_SIZE * 2), bytes([0xff]) * BLOCK_SIZE)
                self.assertEqual(
                    backend.read_block(BLOCK_SIZE * 3), bytes([0xff]) * BLOCK_SIZE)
            finally:
                backend.close()

    def test_file_backend_read_only(self):
        with tempfile.NamedTemporaryFile() as image:
            image.truncate(BLOCK_SIZE * 4)
            backend = FileBlockBackend(image.name, read_only=True)
            try:
                self.assertEqual(backend.read_block(0), bytes(BLOCK_SIZE))
                with self.assertRaises(BackendError) as context:
                    backend.write_block(0, bytes([0xa5]) * BLOCK_SIZE)
                self.assertEqual(context.exception.status, BackendStatus.ReadOnly)
            finally:
                backend.close()
