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

This does **not** fix the reported Titan Ridge credit-exchange failure. Keeping
E2E may retain that failure; silently removing the protocol's required flow
control is not a reliable substitute. Validate the hardware credit issue
separately before relying on this transport for backups.

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

The kernel-install hook carries both patches and repeats the build for new
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
also checked that open streams reject sleep and closed streams allow it. It is
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

Upstream reference:
`6db21d817b43f8ce5654ccc7aff80d40e4dba4ac` introduced USB4STREAM.
Later upstream code fixes several RX/CLOSE issues; this patch ports the
relevant behavior to 7.3.3 without importing unrelated polling changes.
