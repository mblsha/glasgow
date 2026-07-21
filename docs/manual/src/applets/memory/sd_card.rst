.. _applet.memory.sd_card:

``memory-sd-card``
==================

The ``memory-sd-card`` applet emulates an early, byte-addressed SDSC memory card. The card image
remains on the host; Glasgow uses two 512-byte ping-pong banks and streams reads and writes over
USB. While one read bank is shifted onto DAT, the other is refilled from the host, avoiding an
inter-block USB stall without retaining the image in FPGA memory. Each bank follows an explicit
``empty`` → ``filling`` → ``ready`` → ``active`` ownership sequence, so a late prefetch cannot be
mistaken for the current block after CMD12. Images must be no larger than 16 MiB and a multiple of
2 KiB so their capacity can be represented exactly by a version 1 CSD register. Add
``--read-only`` to open an image read-only
and advertise the CSD temporary-write-protect bit. The normal CSD advertises a 25 Mbit/s maximum
transfer rate. ``--conservative-speed`` instead advertises 5 Mbit/s for compatibility experiments
with hosts that select an unreliable clock from the normal profile.

The applet supports 1-bit and 4-bit SD buses. Pin arguments list the data pins in
``DAT0,DAT1,DAT2,DAT3`` order. For the SD breakout on Glasgow port B, invoke it as follows::

    glasgow run memory-sd-card -V B=SB \
        --clk B2 --cmd B5 --dat B1,B0,B7,B6 card.img

The target supplies the I/O voltage through the port B sense pin in this configuration. Use a copy
of any valuable card image: this applet is preview quality, and all accepted writes and erases are
written to the image. By default a write or erase remains busy until the host has called ``fsync``;
``--volatile-writes`` provides lower latency at the cost of acknowledging data that may still only
be in the host page cache.

Each host request and reply is a versioned, length-delimited COBS frame protected by CRC16. A NUL
delimiter lets either endpoint recover at the next frame after truncated or corrupted USB data.
Frames also carry a randomized session generation and a monotonically increasing request ID;
replies from a previous applet run or target-power cycle are discarded and counted. Backend
failures, malformed frames, and stale replies are included in the applet's exit statistics.
The statistics also report buffer-ownership faults, multi-block prefetch waits, and the maximum
host request/reply service latency measured in SD clock cycles. The longest blocking backend call
is reported separately in milliseconds.

Reads, writes, erases, and synchronization run on one dedicated worker thread. Backends advertise
their efficient contiguous-read capability: generic backends default to one block, while the file
backend opts into a bounded 128 KiB host-memory read-ahead window. Writes and erases invalidate
that window before reaching the backend. At most one operation may wait behind the operation in
progress, and operations remain strictly ordered, so a slow ``fsync`` cannot block power
monitoring or unboundedly queue later writes.

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

Always use ``-V B=SB`` with this wiring. The Zaurus supplies the SD I/O rail, so configuring
Glasgow to source 3.3 V would connect two power supplies together. Before starting the applet,
``glasgow voltage`` should report about 3.3 V on port B ``Vsense``. Keep the applet running for the
entire time that the filesystem is mounted.

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
  the ``Vsense`` rail for the CLK pin's port and automatically suspends, resets, changes transport
  generation, flushes the image after releasing the SD pins, and resumes when power returns.
  Reload ``sharp_mmcsd_m`` after reconnecting; the
  applet itself should remain running. ``--no-power-monitor`` restores the manual behavior.
* A failed probe can leave the Linux 2.4 block driver with stale device state. Reloading the module
  is more reliable than repeatedly opening ``/dev/mmcda``. If ``rmmod`` reports that the module is
  busy, check for every ``sdmgr`` process rather than trusting only ``/var/run/sdmgr.pid``; an
  interrupted test can leave an older daemon that the current PID file does not name.
* An intermittent high-speed CRC failure has been observed around CMD7 with the Sharp driver.
  ``--conservative-speed`` changes only the advertised CSD transfer speed to 5 Mbit/s, providing a
  controlled way to determine whether the driver's selected SD clock is the cause. It is optional;
  the normal 25 Mbit/s profile remains the default.
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

Add ``--conservative-speed`` to run the same check with the experimental 5 Mbit/s CSD profile.

To test a prepared image instead::

    python -m glasgow.applet.memory.sd_card.e2e_zaurus card.img

The harness includes its own asyncio Telnet implementation, exports ``USER=zaurus`` through Telnet
NEW-ENVIRON negotiation (override it with ``--username``), and assumes that ``su -c`` does not
prompt for a password. A directly attached console can be used instead with ``--serial-device
/dev/tty... --serial-baud 115200``. Driver unload/load, daemon ownership, and unmounting are managed
by one idempotent controller, so cleanup can safely be retried after a partial failure. The harness
restores ``sdmgr`` and runs ``glasgow safe`` during cleanup, including after a failed check.

CLI reference
-------------

.. autoprogram:: glasgow.applet.memory.sd_card:SDCardApplet._get_argparser_for_sphinx("memory-sd-card")
    :prog: glasgow run memory-sd-card


API reference
-------------

.. module:: glasgow.applet.memory.sd_card

.. autoclass:: SDCardInterface

.. autoclass:: BlockBackend

.. autoclass:: FileBlockBackend

.. autoclass:: BackendWorker

.. autoclass:: FAT16Image

.. autofunction:: create_fat16_image
