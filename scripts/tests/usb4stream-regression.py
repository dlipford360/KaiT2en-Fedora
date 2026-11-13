#!/usr/bin/env python3
"""Run selected actual stream.c functions with deterministic ring/peer mocks."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def function(source, name):
    """Extract a definition, including its return type, from kernel C."""
    start = source.index(name + "(")
    start = source.rfind("\nstatic ", 0, start) + 1
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


source = Path(sys.argv[1]).read_text()
definitions = "\n".join(function(source, name) for name in (
    "tbstream_dev_valid", "tbstream_dev_close_received",
    "tbstream_dev_rx_callback", "tbstream_dev_send_close",
    "tbstream_suspend",
))

prefix = r"""
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <errno.h>
#define READ_ONCE(x) (x)
#define __maybe_unused
#define WRITE_ONCE(x, v) ((x) = (v))
#define container_of(p, t, m) ((t *)((char *)(p) - offsetof(t, m)))
#define EPOLLIN 1
#define EPOLLRDNORM 2
#define EPOLLHUP 4
#define RING_DESC_CRC_ERROR 8
#define RING_DESC_BUFFER_OVERRUN 16
#define TBSTREAM_CLOSE 3
#define SZ_256 256
#define IS_ERR(p) ((long)(p) < 0)
#define PTR_ERR(p) ((long)(p))
struct tbstream { bool valid; };
struct tb_ring { int unused; };
struct ring_frame { unsigned flags, eof; };
struct tbstream_ring { unsigned prod; struct tb_ring *ring; };
struct tbstream_dev {
    struct tbstream *stream;
    bool disconnected, close_received, busy_poll;
    int in_hopid, out_hopid, wait;
    int users, lock;
    struct tbstream_dev *next;
    struct tbstream_ring rx_ring, tx_ring;
};
struct tbstream_frame {
    struct tbstream_dev *sdev;
    bool completed;
    struct ring_frame frame;
};
static unsigned wake_mask, wake_count, ticks, available_at, polls;
static bool tbstream_valid(const struct tbstream *s) { return s && s->valid; }
static void wake_up_interruptible_poll(int *wait, unsigned mask)
{ (void)wait; wake_mask = mask; wake_count++; }
typedef unsigned ktime_t;
static ktime_t ktime_get(void) { return ticks; }
static ktime_t ktime_add_ms(ktime_t t, unsigned ms) { return t + ms; }
static bool ktime_before(ktime_t a, ktime_t b) { return a < b; }
static void fsleep(unsigned delay) { (void)delay; ticks++; }
static bool tbstream_ring_available(struct tbstream_ring *r)
{ (void)r; return ticks >= available_at; }
static void tbstream_dev_poll_ring(struct tbstream_dev *s, struct tbstream_ring *r)
{ (void)r; if (s->busy_poll) polls++; }
static struct tbstream_frame tx;
static struct tbstream_frame *tbstream_dev_alloc_tx(struct tbstream_dev *s,
    unsigned pdf, void *data, unsigned size)
{
    (void)s; (void)pdf; (void)data; (void)size;
    return ticks >= available_at ? &tx : (void *)(long)-ENOBUFS;
}
static int tb_ring_tx(struct tb_ring *r, struct ring_frame *f)
{ (void)r; (void)f; return 0; }
struct device { int unused; };
struct tb_service { int unused; };
struct tbstream_group { int lock, group; struct tbstream_dev *dev_list; };
static struct tbstream_group group;
static bool group_present;
static unsigned group_puts;
static struct tb_service *tb_to_service(struct device *d)
{ (void)d; return NULL; }
static struct tbstream *tb_service_get_drvdata(struct tb_service *s)
{ (void)s; return NULL; }
static struct tbstream_group *tbstream_group_find(struct tbstream *s)
{ (void)s; return group_present ? &group : NULL; }
static void config_group_put(int *g) { (void)g; group_puts++; }
static void mock_lock(int *lock) { (void)lock; }
#define scoped_guard(type, lock) for (int once = 1; once; once = 0)
#define guard(type) mock_lock
#define list_for_each_entry(item, head, member) \
    for (item = *(head); item; item = item->next)
"""

tests = r"""
int main(void)
{
    struct tbstream stream = { .valid = true };
    struct tbstream_dev s = { .stream = &stream, .in_hopid = 8, .out_hopid = 8 };
    struct tbstream_frame f = { .sdev = &s };
    assert(tbstream_dev_valid(&s) == 0);
    s.disconnected = true;
    assert(tbstream_dev_valid(&s) == -ENXIO); /* Reattachment is not recovery. */
    s.disconnected = false;
    assert(tbstream_dev_valid(&s) == 0);
    s.in_hopid = 0;
    assert(tbstream_dev_valid(&s) == -EINVAL);
    s.in_hopid = 8;
    s.stream = NULL;
    assert(tbstream_dev_valid(&s) == -ENXIO);
    s.stream = &stream;

    f.frame.flags = RING_DESC_CRC_ERROR;
    tbstream_dev_rx_callback(NULL, &f.frame, false);
    assert(f.completed && s.rx_ring.prod == 1 && wake_count == 1);
    assert(wake_mask & EPOLLIN);
    f.frame.flags = RING_DESC_BUFFER_OVERRUN;
    tbstream_dev_rx_callback(NULL, &f.frame, false);
    assert(wake_count == 2);
    tbstream_dev_rx_callback(NULL, &f.frame, true);
    assert(wake_count == 2 && s.rx_ring.prod == 2);
    f.frame.flags = 0;
    f.frame.eof = TBSTREAM_CLOSE;
    tbstream_dev_rx_callback(NULL, &f.frame, false);
    assert(s.close_received && (wake_mask & EPOLLHUP));

    for (unsigned busy = 0; busy < 2; busy++) {
        s.busy_poll = busy;
        ticks = polls = 0;
        available_at = 10;
        assert(tbstream_dev_send_close(&s) == 0);
        assert(ticks == 10);
        assert(busy ? polls > 0 : polls == 0);
        ticks = 0;
        available_at = 501;
        assert(tbstream_dev_send_close(&s) == -ENOBUFS);
        assert(ticks == 500); /* A stalled peer must not hang close forever. */
    }
    assert(tbstream_suspend(NULL) == 0);
    assert(group_puts == 0);
    group_present = true;
    group.dev_list = &s;
    assert(tbstream_suspend(NULL) == 0);
    assert(group_puts == 1);
    s.users = 1;
    assert(tbstream_suspend(NULL) == -EBUSY);
    assert(group_puts == 2);
    s.users = 0;
    assert(tbstream_suspend(NULL) == 0);
    assert(group_puts == 3);
    return 0;
}
"""

with tempfile.TemporaryDirectory(prefix="usb4stream-test-") as tmp:
    path = Path(tmp)
    (path / "test.c").write_text(prefix + definitions + tests)
    subprocess.run([os.environ.get("CC", "cc"), "-std=gnu11", "-Wall", "-Wextra",
                    "-Wno-unused-parameter", "-Werror", str(path / "test.c"),
                    "-o", str(path / "test")], check=True)
    subprocess.run([str(path / "test")], check=True)
print("USB4STREAM regression checks passed")
