.. _applet.memory.sd_card:

``memory-sd-card``
==================

The ``memory-sd-card`` applet emulates an early, byte-addressed SDSC memory card. The card image
remains on the host; Glasgow uses two 512-byte ping-pong banks and streams reads and writes over
USB. Read payloads use a dedicated raw pipe whose 512-byte packets map one-to-one to sectors.
While one tagged read bank is shifted onto DAT, the other is refilled from the host, avoiding an
inter-block USB stall without retaining the image in FPGA memory. Each bank follows an explicit
``empty`` → ``filling`` → ``ready`` → ``active`` ownership sequence. CMD12 moves an accepted fill
to ``draining`` until its late host payload has been safely discarded, so it cannot be mistaken
for the current block. Images must be no larger than 1 GiB and a multiple of 2 KiB so their
capacity can be represented exactly by a version 1 CSD register. Add
``--read-only`` to open an image read-only and advertise the CSD temporary-write-protect bit. The
normal CSD advertises a 25 Mbit/s maximum transfer rate. ``--transfer-speed RATE`` accepts values
such as ``12.5m`` or ``5m`` for compatibility experiments with hosts that select an unreliable
clock from the normal profile. If the requested rate is not exactly representable in the CSD,
the applet advertises the greatest standard rate below it (for example, ``12.5m`` becomes
12 Mbit/s) and reports both values when it starts.

The applet supports 1-bit and 4-bit SD buses. Pin arguments list the data pins in
``DAT0,DAT1,DAT2,DAT3`` order. For the SD breakout on Glasgow port B, invoke it as follows::

    glasgow run memory-sd-card -V B=SB \
        --clk B2 --cmd B5 --dat B1,B0,B7,B6 card.img

The target voltage is measured through the port B sense pin in this configuration; Glasgow's
isolated Vio regulator supplies the level translators at the measured voltage. Use a copy of any
valuable card image: this applet is preview quality, and all accepted writes and erases are written
to the image. By default a write or erase remains busy until the host has called ``fsync``;
``--volatile-writes`` provides lower latency at the cost of acknowledging data that may still only
be in the host page cache.

Targets that remove SD power while Glasgow stays connected can be partially back-powered through
idle-high signal pins or pull resistors. With a fixed nonzero Vio supply and power monitoring,
Glasgow automatically uses the target-off high-impedance policy. While the sensed target rail is
absent, CMD and DAT are released, CLK remains an input with a weak pull-down, and every CMD/DAT
programmable pull is disabled. The CLK pull-down prevents the armed engine from counting noise as
clocks, cannot source current into the unpowered target, and is overridden by the powered host. The
card engine and its prefetched boot sectors remain active across the outage; its idle output
enables remain inactive until it receives a command. When the sensed rail returns, the CMD/DAT
pulls are restored without gating command reception on host-side voltage polling.
``--power-idle-high-z`` selects this policy explicitly, and ``--no-power-idle-high-z`` disables
it. It cannot be the default for a sense-following Vio setting because the level translators lose
power with the target.

Each host request and reply is a versioned, length-delimited COBS frame protected by CRC16. A NUL
delimiter lets either endpoint recover at the next frame after truncated or corrupted USB data.
Frames also carry a randomized session generation and a monotonically increasing request ID;
replies from a previous applet run or target-power cycle are discarded and counted. Backend
failures, malformed frames, and stale replies are included in the applet's exit statistics.
The named diagnostics snapshot and exit statistics report buffer-ownership faults, the number of
multi-block prefetch waits, their total and maximum duration, and the maximum host request/reply
service latency, all measured in SD clock cycles. Host backend errors, durable flushes, backend
call count, and the longest blocking backend call are tracked separately. Close-time diagnostics
sample FPGA counters immediately before their SD clock domain is disabled, then finalize the host
fields after the durable flush and backend worker close so those final operations are included.

Reads, writes, erases, and synchronization run on one dedicated worker thread. Backends advertise
their efficient contiguous-read capability: generic backends default to one block, while the file
backend opts into a bounded 4 MiB host-memory read-ahead window. Writes and erases invalidate
that window before reaching the backend. At most one operation may wait behind the operation in
progress, and operations remain strictly ordered, so a slow ``fsync`` cannot block power
monitoring or unboundedly queue later writes.

Pre-init lifecycle trace
------------------------

``--preinit-trace PATH`` appends a flushed JSON Lines record of everything Glasgow can observe
before target userspace exists. Records include a random run identifier, host wall and monotonic
timestamps, emulator generation, target-power transitions, and atomic snapshots of SD clocks,
commands, reads, writes, protocol errors, backend errors, and buffer faults. While counters are
changing the default sampling interval is 250 ms; unchanged state emits a five-second heartbeat.
``--preinit-trace-interval SECONDS`` changes the sampling interval. Existing files are appended,
not replaced, and every run is bracketed by ``trace-start`` and ``trace-stop`` records.

The aggregate ``protocol_errors`` remains the strict sum used by existing qualification. Every
snapshot also breaks it down as ``command_protocol_errors``, ``data_protocol_errors``,
``host_protocol_errors``, and the already separate ``buffer_faults``. Command errors are further
split into malformed start/transmission/end framing and CRC7 failures. These counters are
diagnostic attribution only: rail-correlated errors are not subtracted, cleared, or downgraded.
In particular, a final ``rail-fell`` delta consisting only of command framing errors is consistent
with CMD collapsing while power disappears, but the run remains non-clean until a repeated trace
establishes the fixture-specific rule. Data or host-reply increments identify a different emulator
path that must be investigated rather than treated as an expected power edge.

This trace deliberately does not claim to be a kernel console. An SD memory card is a synchronous
slave: it only receives clocks from the target and drives CMD or DAT as a response to a target
command or data transfer. If MAINTE has finished and Linux has not initialized the PXA MMC host,
Glasgow cannot send a byte to the target or solicit one over CLK/CMD/DAT. SDIO DAT1 interrupts do
not help because the host has not enumerated or enabled an SDIO function. The independently sensed,
target-driven SD supply is the only asynchronous observation on this wiring.

For a pre-MMC failure, pair this trace with a diagnostic image that raises the isolated SD-power
GPIO at exactly one decompressor or early-kernel milestone. A ``rail-rose`` record followed by
zero commands in the freshly rearmed emulator generation proves that the CPU reached that marker
without implying that MMC initialized;
absence of the rise bounds the failure before the marker. ``rail-fell`` captures the final SD
snapshot before the applet rearms and resets the emulator counters. This remains a one-bit stage
probe, not textual logging, but it works while the SD protocol itself is silent and does not
require Glasgow to drive an unpowered target. For example::

    glasgow run memory-sd-card -V B=3.3 \
        --clk B2 --cmd B5 --dat B1,B0,B7,B6 \
        --max-sd-clock 5m --transfer-speed 1m --one-bit \
        --prime-first-read --power-idle-high-z \
        --preinit-trace /tmp/zaurus-preinit.jsonl card.img

Unpartitioned-tail sideband console
-----------------------------------

``--console-pty PATH`` enables a retry-safe byte-stream console without adding a second physical
connection. This mode is deliberately opt-in. The image must reserve at least five unpartitioned
sectors at its end and store a versioned, CRC-protected ``GSCTAIL1`` locator in the final sector.
All four MBR entries may remain in use; in the Zaurus A/B layout p4 remains the persistent ext2
``/data`` partition, shortened by 1 MiB, and boot-control sectors LBA1/LBA2 are unchanged. The
applet validates that no partition overlaps the located tail and intercepts it before the file
backend, so mailbox writes never alter the image. It rejects erase ranges that overlap the tail
and cannot be combined with ``--read-only``.

The first four tail-relative sectors are control (target read), target output (target write),
host input (target read), and host-input acknowledgement (target write). Every sector contains a
versioned 512-byte frame with an IEEE CRC32. Host-input reads peek at one sequence until the target
explicitly acknowledges it. Repeated target-output writes with the same session, sequence, and
contents are accepted once, which makes Linux MMC retries harmless. A new applet process chooses
a new random session; the target renegotiates and restarts its sequence space.

The target discovers the reservation by reading the final sector of the whole-card block device,
then accesses the mailbox with aligned direct I/O outside every partition. A missing or invalid
locator is fail-closed: Glasgow refuses ``--console-pty`` and the target daemon performs no writes.

The PTY is owned by the applet process, not by the target-power lifecycle. It therefore remains
open while SD power is absent and across the applet's off-rail transport rearm. Target output and
host input use bounded 1 MiB queues; overflow is reported in the control-frame flags. For example::

    glasgow run memory-sd-card -V B=3.3 \
        --clk B2 --cmd B5 --dat B1,B0,B7,B6 \
        --max-sd-clock 5m --transfer-speed 1m --one-bit \
        --prime-first-read --console-pty /tmp/glasgow-zaurus-console \
        zaurus-erofs.img

Attach any raw-terminal client to the published symlink. Closing and reopening the client does not
stop the applet or consume queued target input. Restarting the applet does replace the PTY and the
protocol session, so restart the terminal client in that case as well.

Zaurus SL-C860
--------------

The following wiring was verified with a Sharp Zaurus SL-C860:

.. list-table::
    :header-rows: 1

    * - SD signal
      - Glasgow pin
    * - CLK
      - B2
    * - CMD
      - B5
    * - DAT0
      - B1
    * - DAT1
      - B0
    * - DAT2
      - B7
    * - DAT3
      - B6

When the Zaurus SD rail is already on, ``-V B=SB`` senses that rail and configures Glasgow's
isolated level-translator supply to match it. Before starting in that mode, ``glasgow voltage``
should report about 3.3 V on port B ``Vsense``. Keep the applet running for the entire time that
the filesystem is mounted.

For the isolated unattended MAINTE SD-first breakout, Glasgow Vio is not connected to the Zaurus
SD supply. Use a fixed translator voltage and the target-off high-impedance state::

    glasgow run memory-sd-card -V B=3.3 \
        --clk B2 --cmd B5 --dat B1,B0,B7,B6 \
        --max-sd-clock 5m --transfer-speed 1m --one-bit \
        --prime-first-read --diagnostics-interval 2 card.img

The fixed Vio setting selects the high-impedance policy by default. This profile uses an off
threshold of 2.7 V and an on threshold of 3.0 V, sampled every 10 ms with two consecutive samples
required. Those thresholds deliberately bracket the 2.36 V back-power plateau measured on the
physical SL-C860 breakout and the real approximately 3.3 V SD rail. Pull restoration has a 5 ms
settling delay, but the card engine is already listening and may respond during that interval.
Override the ``--power-*`` options only after measuring the target.

The Zaurus driver probes the card lazily and ``/sbin/sdmgr`` keeps the module busy through
``/dev/sd_slotstat``. For a deterministic reprobe, first start the applet on the host, then stop the
daemon and reload the driver over the Zaurus console::

    su -c "/usr/bin/killall sdmgr 2>/dev/null || true"
    su -c "rm -f /var/run/sdmgr.pid"
    sleep 2
    su -c "/sbin/rmmod sharp_mmcsd_m"
    sleep 2
    su -c "/sbin/insmod \
        /lib/modules.rom/2.4.18-rmk7-pxa3-embedix/kernel/drivers/block/sharp_mmcsd_m.o"

Opening the whole-card device triggers the lazy probe. Reading its first sector is also a useful
integrity check; its digest should equal the digest of the first sector in the host image::

    su -c "dd if=/dev/mmcda of=/tmp/sd-mbr bs=512 count=1"
    md5sum /tmp/sd-mbr

Once ``/dev/mmcda1`` is available, the usual Cacko mount point is ``/usr/mnt.rom/card``;
``/mnt/card`` is normally a symlink to it::

    su -c "mount -t vfat /dev/mmcda1 /usr/mnt.rom/card"
    cat /usr/mnt.rom/card/HOST.TXT
    su -c "echo zaurus-write-test > /usr/mnt.rom/card/ZAURUS.TXT; sync"
    cat /usr/mnt.rom/card/ZAURUS.TXT

Unmount and synchronize the image before stopping the applet, then restore the daemon::

    su -c "umount /usr/mnt.rom/card"
    su -c "/usr/bin/killall sdmgr 2>/dev/null || true"
    su -c "rm -f /var/run/sdmgr.pid; /sbin/sdmgr"

Zaurus-specific gotchas
~~~~~~~~~~~~~~~~~~~~~~~

* A warm reboot takes at least two minutes in this setup and drops the telnet connection. Wait for
  the USB network to return before reconnecting.
* Give the E2E harness exclusive use of the Zaurus telnet console. The old telnet daemon may accept
  another TCP connection without allocating it a working shell while an earlier console session
  is still present; close any manual or tmux telnet session first.
* The Zaurus removes SD power during reboot, but Glasgow itself remains powered. The applet polls
  the ``Vsense`` rail for the CLK pin's port. In ordinary mode it suspends, resets, changes
  transport generation, flushes the image, and resumes when power returns. In
  ``--power-idle-high-z`` mode the first debounced fall removes the external CMD/DAT pulls,
  flushes the image, resets both protocol and host transports, and rearms the card while the
  target is still off. A minimum rearm interval suppresses another full reset during the same
  startup's rail chatter. MAINTE begins its probe too soon for a host-side USB voltage sample to
  activate or refill the emulator, so command reception never waits for the power poll.

  Hardware pipe reset also resets the applet-domain RW registers. The host lifecycle therefore
  rewrites generation, write protection, boot priming, image size, CID, CSD, and SCR after every
  transport reset and before enabling the card. Omitting that restoration leaves
  ``image_size=0`` and makes the first valid CMD17 return a CRC-valid ``ADDRESS_OUT_OF_RANGE`` R1.
  Reload ``sharp_mmcsd_m`` after reconnecting; the applet itself should remain running.
  ``--no-power-monitor`` restores the old always-active behavior, disables the automatic
  high-impedance default, and must not be used for unattended cold cycling.
* The physical breakout routes SD VDD to SENSE and leaves Glasgow Vio isolated. With the old
  always-active profile, idle-high SD signals and their pull-ups raised the unpowered Zaurus SD
  rail to 2.36 V even when the lab PSU was off. Setting all six pins high-impedance and disabling
  their pulls reduced SENSE to 0.00 V while Vio remained at 3.3 V. Driving the pins low did the
  same, but is not used because it could contend with the Zaurus as power returns. The automatic
  target-off state uses only a weak CLK pull-down; CMD and DAT remain undriven with no pulls.
* A failed probe can leave the Linux 2.4 block driver with stale device state. Reloading the module
  is more reliable than repeatedly opening ``/dev/mmcda``. If ``rmmod`` reports that the module is
  busy, check for every ``sdmgr`` process rather than trusting only ``/var/run/sdmgr.pid``; an
  interrupted test can leave an older daemon that the current PID file does not name.
* An intermittent high-speed CRC failure has been observed around CMD7 with the Sharp driver.
  ``--transfer-speed 5m`` changes only the advertised CSD transfer speed, providing a controlled
  way to determine whether the driver's selected SD clock is the cause. Intermediate rates such as
  ``--transfer-speed 12.5m`` can narrow down the threshold. This is optional; 25 Mbit/s remains the
  default.
* Sharp MAINTE probes MMC with CMD1 before its SD ACMD41 path. An SD card must remain silent to
  CMD1; replying with an illegal-command R1 can prevent MAINTE from selecting the SD path.
  MAINTE also requires ACMD13 plus its 64-byte SD Status data phase before it considers the drive
  open. The emulator implements both behaviors; older bitstreams can initialize far enough to
  read CID/CSD but still fail the final drive-open step.
* Run with ``-v`` while bringing up new wiring. Block addresses logged by the applet confirm that
  the command and host-streaming paths are active. CMD55/ACMD41 timeouts with no logged commands
  usually mean that the applet was not reset or that CLK/CMD wiring is wrong. The E2E harness uses
  normal logging to avoid timing-sensitive per-block output; add ``--verbose-glasgow`` only when
  those addresses are needed.
* ``FAT: bogus logical sector size 0`` means that the Zaurus received a zero or stale partition
  boot sector. With current gateware, first reload both the applet and driver; with older builds it
  can also be caused by a stale CMD18 prefetch surviving CMD12.
* After a test, ``glasgow safe`` releases every SD signal. Port B may still report approximately
  3.3 V on ``Vsense`` because that voltage comes from the Zaurus.

Automated end-to-end check
~~~~~~~~~~~~~~~~~~~~~~~~~~

An opt-in hardware harness performs the driver reload, raw MBR and BPB digest comparison, FAT16
mount, target read, target write, unmount, and structural verification of the written file through
the FAT cluster chain. With no image argument it creates a deterministic, partitioned 16 MiB
FAT16 image containing ``HOST.TXT``. An existing image may still be supplied as the final argument.
Add ``--reboot`` to exercise automatic SD-rail power-cycle recovery; the harness allows 240 seconds
by default and rejects reboot timeouts below two minutes::

    python -m glasgow.applet.memory.sd_card.e2e_zaurus \
        --glasgow software/.venv/bin/glasgow \
        --host-marker glasgow-host-marker --reboot

Add ``--transfer-speed 5m`` to run the same check with the experimental 5 Mbit/s CSD profile.

To test a prepared image instead::

    python -m glasgow.applet.memory.sd_card.e2e_zaurus card.img

The harness includes its own asyncio Telnet implementation, exports ``USER=zaurus`` through Telnet
NEW-ENVIRON negotiation (override it with ``--username``), and assumes that ``su -c`` does not
prompt for a password. A directly attached console can be used instead with ``--serial-device
/dev/tty... --serial-baud 115200``. Driver unload/load, daemon ownership, and unmounting are managed
by one idempotent async context manager, so cleanup can safely be retried after a partial failure.
Before taking over it snapshots whether the driver and ``sdmgr`` were running and records the
mounted device, filesystem, and mount options. Cleanup restores that exact state rather than
always starting the daemon or mounting the card, then captures the state again to verify that the
restoration converged. If both the hardware check and cleanup fail, both exceptions are retained.
The harness also runs ``glasgow safe`` during cleanup, including after a failed check.

CLI reference
-------------

.. autoprogram:: glasgow.applet.memory.sd_card:SDCardApplet._get_argparser_for_sphinx("memory-sd-card")
    :prog: glasgow run memory-sd-card


API reference
-------------

.. module:: glasgow.applet.memory.sd_card

.. autoclass:: SDCardInterface

.. autoclass:: SDCardDiagnostics

.. autoclass:: BlockBackend

.. autoclass:: FileBlockBackend

.. autoclass:: BackendWorker

.. autoclass:: FAT16Image

.. autofunction:: create_fat16_image
