.. _applet.memory.sd_card:

``memory-sd-card``
==================

The ``memory-sd-card`` applet emulates an early, byte-addressed SDSC memory card. The card image
remains on the host; Glasgow buffers one 512-byte block at a time and streams reads and writes over
USB. Images must be writable, no larger than 16 MiB, and a multiple of 2 KiB so their capacity can
be represented exactly by a version 1 CSD register.

The applet supports 1-bit and 4-bit SD buses. Pin arguments list the data pins in
``DAT0,DAT1,DAT2,DAT3`` order. For the SD breakout on Glasgow port B, invoke it as follows::

    glasgow run memory-sd-card -V B=SB \
        --clk B2 --cmd B5 --dat B1,B0,B7,B6 card.img

The target supplies the I/O voltage through the port B sense pin in this configuration. Use a copy
of any valuable card image: this applet is preview quality, and all accepted writes and erases are
written to the image.

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

    SDMGR_PID=$(cat /var/run/sdmgr.pid 2>/dev/null)
    test -n "$SDMGR_PID" && su -c "kill $SDMGR_PID"
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
    su -c "rm -f /var/run/sdmgr.pid; /sbin/sdmgr"

Zaurus-specific gotchas
~~~~~~~~~~~~~~~~~~~~~~~

* A warm reboot takes at least two minutes in this setup and drops the telnet connection. Wait for
  the USB network to return before reconnecting.
* The Zaurus removes SD power during reboot, but Glasgow itself remains powered. Restart this
  applet after the Zaurus has booted, then reload ``sharp_mmcsd_m`` as above. Restarting the applet
  resets all of its SD-domain state; do not restart it while the filesystem is mounted.
* A failed probe can leave the Linux 2.4 block driver with stale device state. Reloading the module
  is more reliable than repeatedly opening ``/dev/mmcda``. If ``rmmod`` reports that the module is
  busy, ``sdmgr`` is usually still running.
* Run with ``-v`` while bringing up new wiring. Block addresses logged by the applet confirm that
  the command and host-streaming paths are active. CMD55/ACMD41 timeouts with no logged commands
  usually mean that the applet was not reset or that CLK/CMD wiring is wrong.
* ``FAT: bogus logical sector size 0`` means that the Zaurus received a zero or stale partition
  boot sector. With current gateware, first reload both the applet and driver; with older builds it
  can also be caused by a stale CMD18 prefetch surviving CMD12.
* After a test, ``glasgow safe`` releases every SD signal. Port B may still report approximately
  3.3 V on ``Vsense`` because that voltage comes from the Zaurus.

CLI reference
-------------

.. autoprogram:: glasgow.applet.memory.sd_card:SDCardApplet._get_argparser_for_sphinx("memory-sd-card")
    :prog: glasgow run memory-sd-card


API reference
-------------

.. module:: glasgow.applet.memory.sd_card

.. autoclass:: SDCardInterface
