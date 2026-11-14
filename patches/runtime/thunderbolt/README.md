# Thunderbolt / USB4STREAM runtime patches

The stream patch targets the Linux **7.3.3** implementation. The installer
builds and installs both `thunderbolt.ko` and `thunderbolt_stream.ko` when
`CONFIG_USB4_STREAM=m`. Patching the source without installing the latter
does not update the character-device driver. Kernels without USB4STREAM
continue to receive only the core patch; built-in USB4STREAM needs a kernel
rebuild and is rejected by this module installer.

## Behavior changes

* Preserve mandatory end-to-end flow control on both rings. Opening a stream
  fails with `EOPNOTSUPP` if a core quirk strips E2E. This branch does not
  include the experimental Titan Ridge E2E-disabling patch.
* Wake readers for CRC/overrun completions and return `EIO` rather than copying
  those frames as normal data. Applications must treat this as a failed
  transfer; the protocol has no retransmission layer.
* Process peer CLOSE for write-only applications (`EPIPE`), report poll hangup,
  and wait up to 500 ms for a free CLOSE TX slot in either polling mode.
  CLOSE delivery is not acknowledged; a stalled peer can still exceed that
  deadline. Applications need transfer length/hash verification and timeouts.
* Disconnect stops rings before releasing HopIDs. Existing handles remain
  invalid (`ENXIO`) after reconnection until all old handles close. Reopen to
  create a new session; data from separate sessions is not concatenated.
* System suspend/hibernate is refused with `EBUSY` while a stream is open.
  Close all stream handles before sleeping. The original stop/restart path
  discarded unread RX data without telling applications.
* Streams use a dedicated character-device class, rather than misc devices.
  Waiting for a peer or activating paths in `open()` no longer holds the
  global misc-device mutex. This does not repair a stuck controller or make
  hardware operations reliably interruptible.

`/dev/tbstreamX` names are unchanged, but the udev subsystem is now `tbstream`,
not `misc`. Update any subsystem-matching permission rules. Recreate manually
made device nodes; their old major/minor numbers are not valid. Normal
devtmpfs/udev-created nodes follow the new device numbers automatically.

This does **not** establish the cause of the remaining intermittent transport
failures. The field report retracted the blanket Titan Ridge E2E-failure claim
after finding measurement errors. Preserve required flow control and isolate
the remaining retrain/idle failures with corrected tooling.

The patch is intentionally separate from the core power-management patch.
If a newer kernel already includes some of these changes, rebase it against
that kernel instead of applying rejected hunks by hand.

## Install and verify on Fedora

```sh
sudo bash scripts/fedora/install-thunderbolt.sh install "$(uname -r)"
# Reboot, then check that BOTH resolve to updates/kait2en-thunderbolt:
modinfo -n thunderbolt
modinfo -n thunderbolt_stream
sudo modprobe thunderbolt_stream
```

The kernel-install hook carries the patch series and repeats the build for new
kernels. A build failure is logged in `/var/log/kait2en-thunderbolt.log`;
the existing hook does not prevent booting the distribution kernel.

## Validation

On a source tree after applying the stream patch:

```sh
python3 scripts/tests/usb4stream-regression.py /path/to/linux/drivers/thunderbolt/stream.c
```

This compiles selected actual driver functions against a small host-side
test harness. It checks error wakeups, CLOSE notification, disconnected-handle
validation and full-ring CLOSE retry in interrupt and polling modes. It is
also checked that open streams reject sleep and closed streams allow it. This is
not a substitute for building the kernel modules or testing DMA on hardware.

Before deployment, test both hosts with matching patches:

1. Transfer a deterministic payload larger than the RX ring (at least 64 MiB),
   verify byte count and SHA-256, and confirm EOF. Loop for both directions
   simultaneously, with interrupt mode and `busy_poll` enabled.
2. Pause the receiving process for several seconds while sending. The sender
   should encounter backpressure, and the resumed transfer must match exactly.
3. Close the receiver during a write-only transfer: the writer should fail
   with `EPIPE`, not wait indefinitely. Close a sender with a nearly full TX
   ring and confirm receiver EOF within the application's timeout.
4. Unplug during blocked reads/writes. Old descriptors must fail with `ENXIO`;
   reconnecting must not revive them. Close all old handles, reopen and verify
   a fresh transfer.
5. Attempt system suspend with an open stream: sleep must be rejected. Close
   the stream, suspend/resume, reopen and verify a fresh transfer.
6. With driver fault injection, flag an RX completion with CRC/overrun error;
   a sleeping reader must wake and receive `EIO`, not corrupt payload bytes.
7. Repeat long-idle first-frame tests without keepalives, and repeat using a
   non-Titan-Ridge peer. Capture kernel logs, kernel release and both module
   paths alongside the byte counts/hashes.

## Field report follow-up (November 13)

The A2141-pair report retracted the evidence for disabling E2E: stale device
selection and a tag-comparison error contaminated the original measurements.
Stock E2E reportedly delivered in both directions, including first frames
after 40 seconds idle, during a clean window. Retrain/idle-related failures
remain to be isolated. This supersedes the earlier claim that Titan Ridge
E2E universally fails.

The supplied logs establish hop deactivation/activation timeouts. They do
not establish persistent firmware corruption, cable failure or recovery by
S5. Those require controlled comparisons and full logs. A `misc_open` wait
channel alone does not identify the lock holder; capture blocked-task stacks
to distinguish waiters from the task inside the driver's open callback.

Configfs attributes are kernel-created: `rm -rf` tries to unlink those
attributes and can legitimately report EPERM. Remove stream directories
with `rmdir` (leaf first), not their attribute files. Likewise, writing a
nonexistent sysfs `remove` attribute does not demonstrate a teardown bug.
Directory mtimes are not a reliable test of peer liveness. Configfs state
is kernel memory; apparent persistence across reboot needs investigation of
the scripts recreating it, rather than an assumption of persistent items.

Use `scripts/tests/usb4stream-probe.py` for small delivery tests. It selects
the device from the explicit configfs item's `index`, checks for a bound
service, handles partial reads/writes, checks the complete tag/session ID,
validates CRC and sequence numbers, and requires receiver EOF. A bound
service is necessary but does not prove the peer is responsive. Start the
receiver first and wait for its `ready` event; use a fresh shared session ID:

```sh
# Receiver: substitute its own exact configfs item path.
sudo python3 scripts/tests/usb4stream-probe.py recv \
  --stream /sys/kernel/config/thunderbolt/stream/0-1.1/probe \
  --session 1234abcd --tag AB --count 1 --timeout 90
# Sender: substitute its own exact configfs item path.
sudo python3 scripts/tests/usb4stream-probe.py solo \
  --stream /sys/kernel/config/thunderbolt/stream/1-3.1/probe \
  --session 1234abcd --tag AB --timeout 90
```

`submitted` reports local writes only. `received` reports verified delivery.
User-space deadlines cannot preempt a kernel operation stuck in D-state.
Run the probe's host-only tests with:

```sh
python3 scripts/tests/test_usb4stream_probe.py
```

Additional lab cases, with complete logs and module identities on both hosts:

* Boot with the cable out, establish one link, verify a baseline in both
  directions. Compare against cable-present boot and a deliberate retrain.
* Repeat idle gaps of 0, 1, 5, 30 and 40 seconds without keepalives. Stop and
  retain evidence after a failure rather than flooding the same failed path.
* Open/close ten successive sessions and verify a fresh transfer after each.
  Capture the first deactivation timeout and the next activation failure.
* On the patched driver, leave an open waiting for a missing peer and check
  that unrelated misc-device opens/registration still complete. Interrupt
  the waiting open, and race configfs removal with open under KASAN/lockdep.
* Obtain the full oops/register dump and task stacks for the reported
  `tb_ring_free` crash before attempting a teardown-order fix. The supplied
  excerpt lacks the full fault trace; NHI unbind is not a recovery step here.

Upstream reference:
`6db21d817b43f8ce5654ccc7aff80d40e4dba4ac` introduced USB4STREAM.
Later upstream code fixes several RX/CLOSE issues; this patch ports the
relevant behavior to 7.3.3 without importing unrelated polling changes.
