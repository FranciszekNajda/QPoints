#!/usr/bin/env python3
"""Convert the CPU state of a QEMU snapshot into a gem5 checkpoint (m5.cpt).

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
     counter frequency of the gem5 machine). Any problem stops the script: a
     wrong checkpoint would make gem5 simulate a different guest without any
     error.
  3. For every variable and every CPU: read the source value, apply the
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
PER_CPU_SCOPES = {"cpu", "xc", "misc", "power", "redist", "cpuif", "timer"}

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
    # gem5 does not restore MPIDR: it computes it from the CPU index
    # (Aff0 = index, bit 31 = 1). QEMU virt with GICv3 does the same up to
    # 16 CPUs; beyond, the guest would see other CPU numbers.
    for i, r in enumerate(cpus):
        mpidr = h(r, "MPIDR_EL1")
        if mpidr is not None and mpidr != (0x80000000 | i):
            warns.append(f"cpu{i}: MPIDR_EL1={mpidr:#x}, gem5 will use "
                         f"{0x80000000 | i:#x}")
    # The system counter is shared: CNTPCT of CPU 0 is used for all CPUs.
    pcts = {h(r, "CNTPCT_EL0") for r in cpus}
    if len(pcts) > 1:
        warns.append(f"CNTPCT_EL0 differs between CPUs {sorted(pcts)}; CPU 0's "
                     "value is used")
    return warns


# ===========================================================================
# Conversion of the variables
# ===========================================================================

def read_source(entry, regs, machine, constants):
    """The source value of one dictionary entry, or None if there is none.

      file None                   not converted yet -> None
      file "machine.yaml"         machine[path], e.g. machine["ram_bytes"]
      file "conversion_dict.py"   constants[path] (conversion_dict.MACHINE)
      file "register-info.json"   path is a register name -> its value (None
                                  if absent); path is a list -> {name: value}
                                  of the names found ("{i}" in a name stands
                                  for 0..63, e.g. "z{i}")"""
    file, path = entry["file"], entry["path"]
    if file is None:
        return None
    if file == "machine.yaml":
        return machine[path]
    if file == "conversion_dict.py":
        return constants[path]
    if file != "register-info.json":
        fail(f"unknown source file {file}")
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


def convert_variable(var, entry, regs, defaults, cdict, machine, stats):
    """The value (m5.cpt text) of one variable, for one CPU or for the
    machine, or None to let the template write gem5's reset value.
    stats records which variables were converted and which kept a default,
    for the report."""
    src = read_source(entry, regs, machine, cdict.MACHINE)
    if src is None:
        # no value in the source: the entry's explicit default if it has
        # one, otherwise gem5's reset value (written by the template)
        stats.setdefault("default", set()).add(var)
        return entry.get("default")
    fn = transforms.TRANSFORMS.get(entry["transform"])
    if fn is None:
        fail(f"{var}: transform {entry['transform']} not in transforms.py")
    ctx = Context(regs, defaults.get(var), cdict.GEM5_LAYOUT, machine, var)
    try:
        value = fn(src, ctx)
    except ConversionError as e:
        fail(f"{var}: {e}")
    stats.setdefault("converted", set()).add(var)
    return value


def convert_all(conversion, per_cpu, regs, defaults, cdict, machine, stats):
    """{variable: value} for the per-CPU variables (per_cpu=True) or the
    variables that exist once, leaving out the ones that keep gem5's reset
    value (the template writes it)."""
    values = {}
    for var, e in conversion.items():
        if is_per_cpu(var) != per_cpu:
            continue
        value = convert_variable(var, e, regs, defaults, cdict, machine, stats)
        if value is not None:
            values[var] = value
    return values


# ===========================================================================

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("register_info", help="QEMU register-info.json")
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
    errors = check_snapshot(cpus, machine)
    if errors:
        fail("the snapshot is in a state the conversion does not support:\n  "
             + "\n  ".join(errors))
    for w in warnings_snapshot(cpus, conversion, defaults):
        print(f"WARNING: {w}")

    # ---- step 3: values ------------------------------------------------------
    stats = {}
    # per-CPU variables: computed from the registers of each CPU
    cpu_ctx = [{"index": i,
                "v": convert_all(conversion, True, regs, defaults, cdict,
                                 machine, stats)}
               for i, regs in enumerate(cpus)]
    # variables that exist once: QEMU values are taken from CPU 0 (e.g. the
    # system counter, shared by all CPUs)
    global_values = convert_all(conversion, False, cpus[0], defaults, cdict,
                                machine, stats)

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
    # MPIDR (computed by gem5), CNTVCT (= counter - CNTVOFF), vg (checked),
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
    print(f"converted {len(cpus)} CPUs, RAM {ram_bytes / 2**30:g} GiB "
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
    