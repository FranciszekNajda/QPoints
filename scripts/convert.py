#!/usr/bin/env python3
"""Convert the CPU, GIC and disk device state of a QEMU snapshot into a gem5
checkpoint (m5.cpt).

The files of the conversion
---------------------------
  m5.cpt.j2          the gem5 checkpoint of the QFlex machine as a Jinja
                     template: everything gem5 writes, with its reset values,
                     and a {{ variable }} for every value that comes from the
                     snapshot, with gem5's reset value as default
                     (made by make_template.py from reference checkpoints)
  conversion_dict.py one entry per variable: where its value comes from and
                     which transform converts it; the gem5 register layout
                     and the machine constants
  transforms.py      the conversion functions
This script reads the snapshot, computes every variable with these files and
renders the template. It knows nothing about registers itself: what to
convert is in conversion_dict.py, how to convert it in transforms.py.

Inputs
------
  register-info.json   the QEMU register dump of the snapshot (written by
                       parallel-qemu in <snapshot>.gem/)
  --gic                the QEMU GICv3 dump of the same snapshot (default:
                       gic-info.json next to register-info.json, where
                       parallel-qemu writes it)
  --virtio             the QEMU virtio-pci dump of the same snapshot
                       (default: virtio-info.json next to
                       register-info.json). The snapshot must come from
                       qflex run with --gem5 (legacy virtio-pci disk).
  --yaml               the device tree of the QEMU machine (dtc -O yaml). It
                       gives the CPU count and the RAM size. Use the same YAML
                       as for the gem5 restore (run_gem5.sh --yaml): gem5
                       builds the machine from it, so the checkpoint and the
                       machine then agree.
  --template           m5.cpt.j2 (default: QPoints/conversion_templates/)
  --dict               conversion_dict.py (default: next to this script)

Output
------
  -o m5.cpt            the gem5 checkpoint. Put it in the snapshot's .gem
                       directory, next to the memory files (*.pmem), which
                       are not made here.
  a report on stdout: how many variables were converted, which ones kept
  their default, and the non-zero QEMU registers that were not used.

Steps
-----
  1. Read the dump, the machine (YAML), the dictionary and the template
     (with the gem5 reset value of every variable).
  2. Check: same CPU count in the dump and the YAML; the template has every
     variable of the dictionary; the snapshot is in a state the conversion
     supports (AArch64, no EL2/EL3 state, SME ZA off, consistent SVE length,
     counter frequency of the gem5 machine; a GICv3 like gem5's, see
     check_gic; a disk like gem5's, see check_virtio). Any problem stops
     the script: a
     wrong checkpoint would make gem5 simulate a different guest without any
     error.
  3. For every variable and every CPU: read the source value (from the
     register dump, the GIC dump, the YAML or a constant), apply the
     transform. If the source has no value: the entry's default if it has
     one, otherwise nothing is given and the template writes gem5's reset
     value.
  4. Render the template and write m5.cpt.
Every value of the output comes either from the snapshot or from gem5's
own reset state; the only values chosen by hand are the explicit defaults of
conversion_dict.py.

Usage (from QPoints)
--------------------
  python3 scripts/convert.py <run>/init_warmed.gem/register-info.json \\
      --yaml ../dtb/refs/virt_4cpu_8gb.yaml -o <run>/init_warmed.gem/m5.cpt
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import time

import jinja2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import transforms                                    # noqa: E402
from transforms import ConversionError, Context      # noqa: E402

# Variable scopes that have one value per CPU (the part of a variable name
# before ":" and before the first "."; see conversion_dict.py). Same list as
# in make_template.py.
PER_CPU_SCOPES = {"cpu", "xc", "misc", "power", "intr", "redist", "cpuif",
                  "timer"}

# A variable in the template: {{ cpu.v.get("name", "default") }} or
# {{ v.get("name", "default") }} (see make_template.py jinja_value)
TEMPLATE_VARIABLE = re.compile(r'\bv\.get\("([^"]+)", "([^"]*)"\)')


def is_per_cpu(var):
    """True if the variable has one value per CPU, e.g. "misc:sctlr_ns" or
    "timer.virt_el1_timer:_offset"; False for "system.realview....:..."."""
    return var.split(":", 1)[0].split(".")[0] in PER_CPU_SCOPES


def fail(msg):
    """Stop without writing a checkpoint."""
    sys.exit(f"convert.py: ERROR: {msg}")


def load_module(path, name):
    """Import a Python file from its path (used for conversion_dict.py, so
    another dictionary can be given with --dict)."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# Machine (from the device tree YAML)
# ===========================================================================

def read_machine_yaml(path):
    """CPU count and RAM size from the YAML of `dtc -O yaml`.

    Read with regular expressions, so no YAML library is needed:
      - every "cpu@<N>:" node is a CPU;
      - the "memory@<addr>:" node has "reg: [[addr_hi, addr_lo, size_hi,
        size_lo]]": QEMU virt uses 2 cells (2 x 32 bits) for addresses and
        sizes, so size = size_hi << 32 | size_lo (e.g. [0x0, 0x40000000,
        0x2, 0x0] is 8 GiB at 0x40000000)."""
    text = open(path).read()
    num_cpus = len(re.findall(r"^\s+cpu@[0-9a-f]+:\s*$", text, re.M))
    m = re.search(r"^\s+memory@[0-9a-f]+:\s*\n((?:\s+.*\n)*?)\s+reg:\s*"
                  r"\[\[([^\]]*)\]\]", text, re.M)
    if not num_cpus or not m:
        fail(f"{path}: no cpu@N or memory@ node found (not a dtc YAML?)")
    cells = [int(c, 16) for c in m.group(2).replace(" ", "").split(",")]
    if len(cells) != 4:
        fail(f"{path}: memory reg should have 4 cells, found {cells}")
    ram_bytes = (cells[2] << 32) | cells[3]
    return num_cpus, ram_bytes


# ===========================================================================
# Checks on the snapshot
# ===========================================================================

def check_snapshot(cpus, machine):
    """Return the list of errors: states the conversion does not support,
    or in which it would silently lose information. Each check matches an
    assumption of conversion_dict.py / transforms.py."""
    errors = []
    h = lambda r, n: int(r[n], 16) if n in r else 0
    for i, r in enumerate(cpus):
        # AArch64 only: cpsr bit 4 = 1 means the CPU runs AArch32 code, whose
        # registers gem5 lays out differently.
        if h(r, "cpsr") & 0x10:
            errors.append(f"cpu{i}: AArch32 state (cpsr={r['cpsr']})")
        # No EL2/EL3 state: QEMU runs with virtualization=off, secure=off, and
        # the dictionary does not convert EL2/EL3 registers (gem5 keeps its
        # reset values). A non-zero one would be lost.
        el23 = sorted(n for n, v in r.items()
                      if re.search(r"_EL[23]$", n) and int(v, 16) != 0)
        if el23:
            errors.append(f"cpu{i}: EL2/EL3 registers hold state {el23}")
        # SME: the ZA array is not in the dump, so it must be off
        # (SVCR.ZA = bit 1). With ZA off its content does not matter and
        # gem5's reset (zeros) is exact.
        if h(r, "SVCR") & 0x2:
            errors.append(f"cpu{i}: SVCR.ZA = 1 but ZA is not in the dump")
        # SVE: QEMU's current vector length (vg, in 64-bit granules) must
        # match ZCR_EL1.LEN (vector length / 128 - 1), which is converted.
        if "vg" in r and "ZCR_EL1" in r:
            expected = 2 * ((h(r, "ZCR_EL1") & 0xF) + 1)
            if h(r, "vg") != expected:
                errors.append(f"cpu{i}: vg={h(r, 'vg')} but ZCR_EL1.LEN "
                              f"gives {expected}")
        # Features whose registers are not converted: they must be unused.
        for n in ("TCR2_EL1", "PIR_EL1", "PIRE0_EL1"):
            if h(r, n):
                errors.append(f"cpu{i}: {n}={r[n]} (feature not converted)")
        # CPU affinity: gem5 does not restore MPIDR, it computes it from the
        # CPU index (single cluster: Aff0 = index, bit 31 = 1). QEMU virt
        # with GICv3 gives the same up to 16 CPUs (clusters of 16). Linux
        # read MPIDR at boot and uses it to address CPUs: IPIs (the target
        # is computed at each send, so no GIC check can see it) and SPI
        # routing. With another affinity in gem5 they would be lost and the
        # guest would hang. Fix if needed: build the gem5 CPUs in clusters
        # of 16 (the cluster number becomes Aff1).
        mpidr = h(r, "MPIDR_EL1")
        if "MPIDR_EL1" in r and mpidr != (0x80000000 | i):
            errors.append(f"cpu{i}: MPIDR_EL1={mpidr:#x} but gem5 gives this "
                          f"CPU {0x80000000 | i:#x} (more than 16 CPUs?)")
        # Counter frequency: the gem5 machine counts at counter_freq (set in
        # qflex_system.py); if QEMU counted at another one, the guest's
        # time and timer deadlines would be wrong.
        if "CNTFRQ_EL0" in r and h(r, "CNTFRQ_EL0") != machine["counter_freq"]:
            errors.append(f"cpu{i}: CNTFRQ_EL0={h(r, 'CNTFRQ_EL0')} but the "
                          f"gem5 counter runs at {machine['counter_freq']} Hz")
    return errors


def warnings_snapshot(cpus, conversion, defaults):
    """Things worth knowing that do not stop the conversion."""
    warns = []
    h = lambda r, n: int(r[n], 16) if n in r else None
    # The system counter is shared: CNTPCT of CPU 0 is used for all CPUs.
    pcts = {h(r, "CNTPCT_EL0") for r in cpus}
    if len(pcts) > 1:
        warns.append(f"CNTPCT_EL0 differs between CPUs {sorted(pcts)}; CPU 0's "
                     "value is used")
    return warns


# ===========================================================================
# Checks on the GIC dump
# ===========================================================================

def gem5_affinity(index):
    """Affinity gem5 gives to CPU index (Aff3.Aff2.Aff1.Aff0 as one 32-bit
    value, as in GICD_IROUTER without IRM): a single cluster, Aff0 = index.
    QEMU virt (GICv3) gives the same up to 16 CPUs (clusters of 16)."""
    return index


def check_gic(gic, gic_cpus, cpus, defaults):
    """Return the list of errors: GIC states the conversion does not
    support. Each check matches an assumption of the GICv3 entries of
    conversion_dict.py (see also the GIC tables of the documentation)."""
    errors = []
    h = lambda v: int(v, 16)
    if gic.get("format") != "qemu-gicv3-dump":
        return [f"gic-info.json: unknown format {gic.get('format')!r}"]
    d = gic["gicd"]
    # Same GIC model as gem5's Gicv3(gicv4=False): GICv3 frames (0x20000
    # per redistributor), one security state.
    if gic["revision"] != 3:
        errors.append(f"GIC revision {gic['revision']}, gem5 is a GICv3 "
                      "(gicv4=False): use gic-version=3 in QEMU")
    if gic["security_extn"]:
        errors.append("GIC has security extensions; gem5 runs without "
                      "SECURITY (DS=1)")
    ctlr = h(d["gicd_ctlr"])
    if not (ctlr >> 4) & 1 or not (ctlr >> 6) & 1 or (ctlr >> 2) & 1:
        errors.append(f"GICD_CTLR={d['gicd_ctlr']}: expected ARE=1, DS=1, "
                      "EnableGrp1S=0")
    # One redistributor per CPU, CPU entries 0..N-1
    if gic["num_cpu"] != len(cpus) or \
            [c["cpu_id"] for c in gic_cpus] != list(range(len(cpus))):
        errors.append(f"GIC has {gic['num_cpu']} CPUs, the register dump "
                      f"{len(cpus)}")
    # The QEMU interrupts must fit in gem5's distributor lists (it_lines)
    lines = len(defaults.get("system.realview.gic.distributor:irqGroup",
                             "").split())
    if gic["num_irq"] > lines:
        errors.append(f"QEMU has {gic['num_irq']} interrupts, the gem5 GIC "
                      f"{lines} (it_lines)")
    # Group modifier and NS access only matter with two security states:
    # with DS=1 they must be 0, otherwise the guest relies on a state that
    # gem5 (DS=1) ignores.
    nonzero = lambda l: any(h(x) for x in l)
    if nonzero(d["grpmod"]) or nonzero(d["gicd_nsacr"]):
        errors.append("GICD_IGRPMODR/GICD_NSACR not zero with DS=1")
    for c in gic_cpus:
        if h(c["gicr_igrpmodr0"]) or h(c["gicr_nsacr"]):
            errors.append(f"cpu{c['cpu_id']}: GICR_IGRPMODR0/GICR_NSACR not "
                          "zero with DS=1")
        # virtual interrupt lines: EL2 only, which QEMU does not run
        if h(c["interrupt_request"]) & (0x0040 | 0x0200):
            errors.append(f"cpu{c['cpu_id']}: virtual IRQ/FIQ raised "
                          f"(interrupt_request={c['interrupt_request']})")
    # Error status registers: gem5 does not keep them (RAZ)
    for c in [d] + gic_cpus:
        key = "gicd_statusr" if c is d else "gicr_statusr"
        if nonzero(c[key]):
            errors.append(f"{key} not zero: {c[key]}")
    # Every enabled SPI must be routed to a CPU that exists in gem5;
    # otherwise gem5 keeps it pending for ever (Gicv3Distributor::route).
    enabled = [h(w) for w in d["enabled"]]
    targets = {gem5_affinity(i) for i in range(len(cpus))}
    for irq in range(32, gic["num_irq"]):
        if not (enabled[irq // 32] >> (irq % 32)) & 1:
            continue
        r = h(d["gicd_irouter"][irq])
        if (r >> 31) & 1:
            errors.append(f"SPI {irq}: 1-of-N routing (IRM=1) not supported")
            continue
        aff = ((r >> 32) & 0xff) << 24 | (r & 0xffffff)
        if aff not in targets:
            errors.append(f"SPI {irq} routed to affinity {aff:#x}, which no "
                          "gem5 CPU has")
    return errors


def warnings_gic(gic_cpus):
    """GIC state that is not converted but does not stop the conversion."""
    warns = []
    lpi = [c["cpu_id"] for c in gic_cpus if int(c["gicr_ctlr"], 16) & 1]
    if lpi:
        warns.append(f"LPIs enabled on CPUs {lpi} (GICR_CTLR.EnableLPIs): the "
                     "ITS and LPI state are not converted. Fine as long as no "
                     "device sends MSIs (checked: no MSI-X vector on the "
                     "disk, no pending LPI)")
    return warns


# ===========================================================================
# Checks on the virtio dump (the disk)
# ===========================================================================

def virtio_source(dev):
    """The disk of virtio-info.json as one flat dict, the source of the
    "virtio-info.json" entries of conversion_dict.py:
    "pci.<key>", "virtio.<key>" and "queue<N>.<key>"
    (e.g. "pci.config", "virtio.guest_features", "queue0.desc_addr")."""
    src = {f"pci.{k}": v for k, v in dev["pci"].items()}
    src.update({f"virtio.{k}": v for k, v in dev["virtio"].items()
                if k != "queues"})
    for q in dev["virtio"]["queues"]:
        src.update({f"queue{q['index']}.{k}": v for k, v in q.items()})
    return src


def check_virtio(virtio, gic_cpus, machine, defaults):
    """Return the list of errors: disk states the conversion does not
    support. gem5's disk is PciVirtIO + VirtIOBlock (qflex_system.py): the
    legacy virtio-pci interface, one queue, INTx. Each check matches an
    assumption of the disk entries of conversion_dict.py."""
    h = lambda v: int(v, 16)
    if virtio.get("format") != "qemu-virtio-dump":
        return [f"virtio-info.json: unknown format {virtio.get('format')!r}"]
    devs = virtio["devices"]
    # gem5's machine has exactly one PCI device, the disk: any other
    # virtio-pci device (e.g. a NIC) would not exist after the restore.
    if len(devs) != 1 or "virtio" not in devs[0]:
        return [f"{len(devs)} virtio-pci devices, gem5 has one (the disk): "
                "take the snapshot with qflex --gem5 (no NIC)"]
    dev = devs[0]
    p, v = dev["pci"], dev["virtio"]
    errors = []
    # Transport: legacy only (gem5's PciVirtIO), at gem5's slot, INTx only.
    if not p["legacy"] or p["modern"]:
        errors.append(f"disk is not legacy-only virtio-pci (legacy="
                      f"{p['legacy']}, modern={p['modern']}): use qflex "
                      "--gem5 (disable-legacy=off,disable-modern=on)")
    if (p["bus"], p["slot"], p["function"]) != (0, machine["pci_disk_slot"],
                                                 0):
        errors.append(f"disk at {p['bus']:02x}:{p['slot']:02x}."
                      f"{p['function']}, gem5 puts it at 00:"
                      f"{machine['pci_disk_slot']:02x}.0 (another PCI device "
                      "before it?)")
    if p["nvectors"]:
        errors.append(f"disk has {p['nvectors']} MSI-X vectors: gem5's "
                      "PciVirtIO uses INTx (vectors=0)")
    # One BAR: BAR0, I/O (the legacy register window)
    bars = [b for b in p["bars"] if h(b["size"])]
    if [(b["index"], b["type"]) for b in bars] != [(0, 1)]:
        errors.append(f"disk BARs {[(b['index'], b['type'], b['size']) for b in bars]}, "
                      "gem5's PciVirtIO has one I/O BAR0")
    # PCI header: gem5 keeps 64 bytes and no capability list (Status.CapList,
    # bit 4 of byte 6): a capability would be lost.
    cfg = p["config"]
    header = len(defaults.get("system.pci_devices:_config.data", "").split())
    if len(cfg) < header:
        errors.append(f"PCI config space of {len(cfg)} bytes, gem5 needs "
                      f"{header}")
    elif (cfg[6] >> 4) & 1:
        errors.append("disk has PCI capabilities (Status.CapList = 1): gem5 "
                      "does not restore them")
    # Virtio device: a block device, no interrupt pending (gem5 does not
    # checkpoint interruptDeliveryPending), supported features.
    if v["device_id"] != 2:
        errors.append(f"virtio device id {v['device_id']}, gem5's disk is a "
                      "block device (2)")
    if h(v["isr"]):
        errors.append(f"ISR={v['isr']}: a disk interrupt was pending, gem5 "
                      "cannot restore it (take the snapshot again)")
    extra = h(v["guest_features"]) & ~machine["virtio_blk_features"]
    if extra:
        errors.append(f"guest features {v['guest_features']} include "
                      f"{extra:#x}, not supported by gem5's VirtIOBlock")
    # One queue, of gem5's size, with the legacy ring layout gem5 recomputes
    # from the descriptor table address (VirtQueue::setAddress): avail ring
    # right after the descriptors (16 bytes each), used ring at the next
    # 4 KiB boundary after the avail ring (flags, idx, num entries of 2
    # bytes; no used_event field without EVENT_IDX).
    qs = v["queues"]
    if len(qs) != 1:
        errors.append(f"disk has {len(qs)} queues, gem5's VirtIOBlock one")
    if v["queue_sel"] >= len(qs):
        errors.append(f"queue_sel={v['queue_sel']} out of range")
    for q in qs:
        n = q["num"]
        if n != machine["virtio_queue_size"]:
            errors.append(f"queue {q['index']}: {n} entries, gem5's queueSize "
                          f"is {machine['virtio_queue_size']}")
        desc, avail, used = (h(q[k]) for k in ("desc_addr", "avail_addr",
                                                "used_addr"))
        if not desc:
            continue                    # queue not set up by the guest
        align = lambda a: (a + 4095) & ~4095
        if desc & 4095 or avail != desc + 16 * n or \
                used != align(avail + 4 + 2 * n):
            errors.append(f"queue {q['index']}: ring layout desc={q['desc_addr']} "
                          f"avail={q['avail_addr']} used={q['used_addr']} is not "
                          "the legacy layout gem5 recomputes")
        # No request in flight (popped by QEMU, not completed: QEMU would
        # resubmit it from its own state, gem5 cannot) and none waiting in
        # the avail ring (gem5 only looks at the ring when notified).
        last = q["last_avail_idx"]
        if q["used_idx"] != last:
            errors.append(f"queue {q['index']}: {(last - q['used_idx']) % 65536} "
                          "request(s) in flight (take the snapshot again)")
        if q["avail_idx"] != last:
            errors.append(f"queue {q['index']}: {(q['avail_idx'] - last) % 65536} "
                          "request(s) waiting in the avail ring (take the "
                          "snapshot again)")
    # LPIs: the ITS is not converted, so no LPI may be pending.
    for c in gic_cpus:
        if c["hppi_irq"] >= 8192 and int(c["hppi_prio"], 16) != 0xff:
            errors.append(f"cpu{c['cpu_id']}: LPI {c['hppi_irq']} pending, "
                          "LPIs are not converted")
    return errors


# ===========================================================================
# Conversion of the variables
# ===========================================================================

def read_source(entry, sources, machine, constants):
    """The source value of one dictionary entry, or None if there is none.

      file None                   not converted yet -> None
      file "machine.yaml"         machine[path], e.g. machine["ram_bytes"]
      file "conversion_dict.py"   constants[path] (conversion_dict.MACHINE)
      file "register-info.json"   sources[file], a {name: value} dict (the
      or "gic-info.json"          registers or the GIC entry of the CPU being
                                  converted, see main): path is a name -> its
                                  value (None if absent); path is a list ->
                                  {name: value} of the names found ("{i}" in
                                  a name stands for 0..63, e.g. "z{i}")"""
    file, path = entry["file"], entry["path"]
    if file is None:
        return None
    if file == "machine.yaml":
        return machine[path]
    if file == "conversion_dict.py":
        return constants[path]
    if file not in sources:
        fail(f"unknown source file {file}")
    regs = sources[file]
    if isinstance(path, str):
        return regs.get(path)
    found = {}
    for pattern in path:
        names = ([pattern.format(i=i) for i in range(64)] if "{i}" in pattern
                 else [pattern])
        for n in names:
            if n in regs:
                found[n] = regs[n]
    return found or None


def source_names(entry):
    """Names of every QEMU register an entry may read (for the report of
    unused registers)."""
    if entry["file"] != "register-info.json":
        return set()
    path = entry["path"]
    path = [path] if isinstance(path, str) else path
    out = set()
    for p in path:
        out |= {p.format(i=i) for i in range(64)} if "{i}" in p else {p}
    return out


def convert_variable(var, entry, sources, defaults, cdict, machine, stats):
    """The value (m5.cpt text) of one variable, for one CPU or for the
    machine, or None to let the template write gem5's reset value.
    sources: {"register-info.json": registers, "gic-info.json": GIC entry}
    of the CPU (or of the machine). stats records which variables were
    converted and which kept a default, for the report."""
    src = read_source(entry, sources, machine, cdict.MACHINE)
    if src is None:
        # no value in the source: the entry's explicit default if it has
        # one, otherwise gem5's reset value (written by the template)
        stats.setdefault("default", set()).add(var)
        return entry.get("default")
    fn = transforms.TRANSFORMS.get(entry["transform"])
    if fn is None:
        fail(f"{var}: transform {entry['transform']} not in transforms.py")
    ctx = Context(sources["register-info.json"], defaults.get(var),
                  cdict.GEM5_LAYOUT, machine, var)
    try:
        value = fn(src, ctx)
    except ConversionError as e:
        fail(f"{var}: {e}")
    stats.setdefault("converted", set()).add(var)
    return value


def convert_all(conversion, per_cpu, sources, defaults, cdict, machine,
                stats):
    """{variable: value} for the per-CPU variables (per_cpu=True) or the
    variables that exist once, leaving out the ones that keep gem5's reset
    value (the template writes it)."""
    values = {}
    for var, e in conversion.items():
        if is_per_cpu(var) != per_cpu:
            continue
        value = convert_variable(var, e, sources, defaults, cdict, machine,
                                 stats)
        if value is not None:
            values[var] = value
    return values


# ===========================================================================

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("register_info", help="QEMU register-info.json")
    ap.add_argument("--gic", help="QEMU gic-info.json (default: next to "
                    "register_info)")
    ap.add_argument("--virtio", help="QEMU virtio-info.json (default: next "
                    "to register_info)")
    ap.add_argument("--yaml", required=True, help="machine device tree (YAML)")
    ap.add_argument("-o", "--output", required=True, help="m5.cpt to write")
    ap.add_argument("--template",
                    default=os.path.join(HERE, "..", "conversion_templates",
                                         "m5.cpt.j2"))
    ap.add_argument("--dict", default=os.path.join(HERE, "conversion_dict.py"))
    args = ap.parse_args()

    # ---- step 1: inputs ------------------------------------------------------
    # The dump: {"cpus": [{"cpu_id": 0, "registers": {name: hex}}, ...]}.
    # CPUs are sorted by cpu_id, which must be 0..N-1 (the CPU index gem5
    # uses in its section names).
    dump = json.load(open(args.register_info))
    dump_cpus = sorted(dump["cpus"], key=lambda c: c["cpu_id"])
    if [c["cpu_id"] for c in dump_cpus] != list(range(len(dump_cpus))):
        fail("cpu_id values of the dump are not 0..N-1")
    cpus = [c["registers"] for c in dump_cpus]
    # The GIC dump: {"revision", "num_irq", ..., "gicd": {distributor},
    # "cpus": [{"cpu_id": 0, redistributor...}, ...]}. parallel-qemu writes
    # it next to register-info.json. Without it the GIC would stay at gem5's
    # reset (all interrupts disabled) and the guest would hang: required.
    gic_path = args.gic or os.path.join(
        os.path.dirname(os.path.abspath(args.register_info)), "gic-info.json")
    if not os.path.exists(gic_path):
        fail(f"{gic_path} not found (GIC dump of the snapshot, written by "
             "parallel-qemu since qflex-gic.c; give it with --gic)")
    gic = json.load(open(gic_path))
    gic_cpus = sorted(gic.get("cpus", []), key=lambda c: c["cpu_id"])
    # Source of the GIC variables that exist once: the top-level values
    # (num_irq, ...) and the distributor fields, in one dict.
    gic_global = {k: v for k, v in gic.items() if k not in ("cpus", "gicd")}
    gic_global.update(gic.get("gicd", {}))
    # The virtio dump: {"devices": [{"pci": {...}, "virtio": {..., "queues":
    # [...]}}]}. Without it the disk would stay at gem5's reset and the
    # guest's first disk access would hang: required.
    virtio_path = args.virtio or os.path.join(
        os.path.dirname(os.path.abspath(args.register_info)),
        "virtio-info.json")
    if not os.path.exists(virtio_path):
        fail(f"{virtio_path} not found (virtio-pci dump of the snapshot, "
             "written by parallel-qemu since qflex-virtio.c; give it with "
             "--virtio)")
    virtio = json.load(open(virtio_path))
    cdict = load_module(args.dict, "conversion_dict")
    conversion = cdict.CONVERSION
    template_text = open(args.template).read()
    # gem5's reset value of every variable, as written in the template
    defaults = dict(TEMPLATE_VARIABLE.findall(template_text))
    num_cpus, ram_bytes = read_machine_yaml(args.yaml)
    # machine values, used by the "machine.yaml" entries and some transforms
    machine = {"num_cpus": num_cpus, "ram_bytes": ram_bytes, **cdict.MACHINE}

    # ---- step 2: checks ------------------------------------------------------
    if num_cpus != len(cpus):
        fail(f"the dump has {len(cpus)} CPUs but {args.yaml} has {num_cpus}: "
             "use the YAML of the machine the snapshot was taken on")
    # every variable must be in the template: otherwise the dictionary was
    # changed without running make_template.py again
    missing = sorted(set(conversion) - set(defaults))
    if missing:
        fail("conversion_dict.py has variables that are not in the template "
             f"(run make_template.py again): {missing}")
    errors = (check_snapshot(cpus, machine)
              + check_gic(gic, gic_cpus, cpus, defaults)
              + check_virtio(virtio, gic_cpus, machine, defaults))
    if errors:
        fail("the snapshot is in a state the conversion does not support:\n  "
             + "\n  ".join(errors))
    for w in warnings_snapshot(cpus, conversion, defaults) + warnings_gic(
            gic_cpus):
        print(f"WARNING: {w}")

    # ---- step 3: values ------------------------------------------------------
    stats = {}
    # per-CPU variables: computed from the registers and the GIC entry of
    # each CPU
    cpu_ctx = [{"index": i,
                "v": convert_all(conversion, True,
                                 {"register-info.json": regs,
                                  "gic-info.json": gic_cpus[i]},
                                 defaults, cdict, machine, stats)}
               for i, regs in enumerate(cpus)]
    # variables that exist once: register values are taken from CPU 0 (e.g.
    # the system counter, shared by all CPUs), GIC values from the
    # distributor, disk values from the single virtio-pci device (checked)
    global_values = convert_all(conversion, False,
                                {"register-info.json": cpus[0],
                                 "gic-info.json": gic_global,
                                 "virtio-info.json":
                                     virtio_source(virtio["devices"][0])},
                                defaults, cdict, machine, stats)

    # ---- step 4: render ------------------------------------------------------
    # Same Jinja settings as make_template.py, whose re-render test proved
    # that this template gives back gem5's own checkpoints:
    #   trim_blocks/lstrip_blocks  lines with only {% %} leave no blank line
    #   keep_trailing_newline      m5.cpt ends with a newline
    #   StrictUndefined            a missing name is an error, not ""
    env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True,
                             keep_trailing_newline=True,
                             undefined=jinja2.StrictUndefined)
    template = env.from_string(template_text)
    # first line of m5.cpt, a comment (gem5 writes the date there)
    header = (f"checkpoint converted by convert.py on {time.ctime()} from "
              f"{os.path.abspath(args.register_info)}")
    text = template.render(cpus=cpu_ctx, v=global_values, header=header)
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as f:
        f.write(text)

    # ---- report --------------------------------------------------------------
    used = set().union(*(source_names(e) for e in conversion.values()))
    # QEMU registers deliberately not used (see conversion_dict.py):
    # MPIDR (computed by gem5, checked equal), CNTVCT (= counter - CNTVOFF), vg (checked),
    # lower-case fpcr/fpsr (duplicates of FPCR/FPSR), NEON views v/q/d/s/h/b
    # (contained in z), EL2/EL3 registers (checked zero), and the ID /
    # implementation registers that keep gem5's values (AArch32 ID_*, MVFR*,
    # MIDR, REVIDR, AIDR, CLIDR, SMIDR, MDRAR, RVBAR, CBAR, PMCEID*)
    ignored = re.compile(r"^(MPIDR_EL1|CNTVCT_EL0|vg|fpcr|fpsr|"
                         r"[vqdshb]\d+|.*_EL[23]|"
                         r"ID_\w+|MVFR\d_EL1|MIDR_EL1|REVIDR_EL1|AIDR|CLIDR|"
                         r"SMIDR_EL1|MDRAR_EL1|RVBAR_EL1|CBAR_EL1|PMCEID\d_EL0)$")
    unused = sorted(n for n, v in cpus[0].items()
                    if n not in used and not ignored.match(n)
                    and int(v, 16) != 0)
    converted = stats.get("converted", set())
    kept = stats.get("default", set())
    print(f"converted {len(cpus)} CPUs, RAM {ram_bytes / 2**30:g} GiB, "
          f"GIC {gic['num_irq']} interrupts, legacy virtio-pci disk "
          f"-> {args.output}")
    print(f"  {len(converted)} variables from the snapshot or the machine")
    print(f"  {len(kept)} variables at their default:")
    for var in sorted(kept):
        d = conversion[var].get("default")
        print(f"      {var}  ("
              + (f"explicit default {d!r}" if d is not None
                 else "gem5 reset value") + ")")
    # non-zero QEMU registers that no entry reads: state that may be lost
    # (to check by hand: a register without gem5 equivalent, or a missing
    # entry in conversion_dict.py)
    if unused:
        print(f"  non-zero QEMU registers of CPU 0 not used ({len(unused)}):")
        print("      " + " ".join(unused))
    return 0


if __name__ == "__main__":
    sys.exit(main())
