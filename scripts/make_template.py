#!/usr/bin/env python3
"""Make the m5.cpt Jinja template from gem5 reference checkpoints.

Why a template
--------------
A gem5 checkpoint (m5.cpt) is a text file of [sections] with key=value lines.
gem5 restores each section into the simulated object of the same name, so a
converted QFlex checkpoint must contain exactly the sections and keys that
gem5 itself writes for the QFlex machine. Most values are gem5's own reset
state (devices, caches, TLBs, ...). Only a few hundred values per CPU come
from the QEMU snapshot (registers, timers).

So we let gem5 write a checkpoint of the machine (a "reference", made by
qflex_generate_checkpoint.py) and turn it into a Jinja template: every value
that comes from QEMU becomes a {{ variable }}, every other value is kept as
written by gem5. convert.py then only has to fill in the variables.

When to run it
--------------
This is a developer tool. Run it again when:
  - the reference checkpoints are regenerated (new gem5 version, or a new
    machine setting in qflex_system.py / qflex_generate_checkpoint.py);
  - a variable is added to or removed from conversion_dict.py.
Users never run it: convert.py only needs the files it writes.

Inputs
------
  --refs   reference checkpoints of the same gem5 machine with different CPU
           counts and RAM sizes, e.g. refs/ref_1cpu_8gb.m5.cpt,
           refs/ref_4cpu_8gb.m5.cpt, ... Any number, at least one with 2 CPUs
           or more.
  --dict   conversion_dict.py. Only its keys (the variable names) are used
           here: each one becomes a {{ ... }} in the template.

Outputs (in --out-dir)
----------------------
  m5.cpt.j2         the template
  gem5_reset.json   gem5's reset value of every variable, read from the
                    references. convert.py uses it when the QEMU dump has no
                    value for a variable: a value always comes either from the
                    snapshot or from gem5, never from a number written by hand.

What the script does
--------------------
  1. Checks the references (and stops if one fails):
       - inside each reference, every CPU has the same sections, keys and
         reset values (otherwise one copy of a CPU cannot describe all CPUs);
       - all references have the same sections and keys, and the same values
         except the ones listed in MACHINE_VARIABLES (the RAM size). A value
         that changes with the machine must not be frozen in the template.
  2. Writes the template from the reference with the most CPUs (the "base"):
       - the sections that exist once are copied as they are;
       - the sections that exist once per CPU are written once, inside a
         Jinja loop over the CPUs, in the same order as gem5 writes them;
       - the keys listed in conversion_dict.py become variables;
       - the generic timer sections are added (they are not in the
         references, see TIMER_KEYS).
  3. Tests the template: rendered with the reset values and with the CPU
     count and RAM size of each reference, it must give back that reference
     exactly, character for character (only the "## checkpoint generated:"
     date line is taken from the reference).

Usage (from QPoints)
--------------------
  python3 scripts/make_template.py --refs refs/ref_*.m5.cpt \\
      --dict scripts/conversion_dict.py --out-dir templates
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import time
from collections import OrderedDict

import jinja2

# ===========================================================================
# What depends on the CPU count in a checkpoint of the QFlex machine
# ===========================================================================
# These tables describe how gem5 names the objects that exist once per CPU.
# They were found by comparing the references with 1, 2 and 4 CPUs.

# Sections that exist once per CPU, as (kind, regex on the section name).
# In each regex, group 1 is the CPU index and group 2 the rest of the name
# (e.g. ".isa.miscRegs"). gem5 names the CPU sections "cpus0", "cpus1", ...
# but just "cpus" when the machine has a single CPU, hence the (\d*).
#   cpu     the CPU and everything inside it (registers, MMU, TLBs, ...):
#           48 sections per CPU
#   redist  the GICv3 redistributor of the CPU
#   cpuif   the GICv3 CPU interface of the CPU
PER_CPU_SECTIONS = [
    ("cpu", re.compile(r"^system\.cpu_cluster\.cpus(\d*)((?:\..*)?)$")),
    ("redist", re.compile(r"^system\.realview\.gic\.redistributors\.(\d+)()$")),
    ("cpuif", re.compile(r"^system\.realview\.gic\.cpuInterface\.(\d+)()$")),
]

# How each kind of per-CPU section is named in the template. "%s" is replaced
# by the rest of the name (group 2 above). Inside the loop, "cpu" is the
# current CPU; cpu_tag(cpu) is a Jinja macro defined at the top of the
# template that gives "" for a single CPU and the CPU index otherwise.
TEMPLATE_SECTION_NAME = {
    "cpu": "system.cpu_cluster.cpus{{ cpu_tag(cpu) }}%s",
    "redist": "system.realview.gic.redistributors.{{ cpu.index }}%s",
    "cpuif": "system.realview.gic.cpuInterface.{{ cpu.index }}%s",
}

# Variable scope of the per-CPU sections that can hold variables (the part
# before ":" in a variable name of conversion_dict.py). The key is
# (kind, rest of the section name). Per-CPU sections not listed here (MMU,
# TLBs, decoder, ...) never hold variables: they stay at gem5's reset value.
SCOPE_OF = {
    ("cpu", ""): "cpu",                  # [system.cpu_cluster.cpus<N>]
    ("cpu", ".xc.0"): "xc",              # registers and PC of the CPU
    ("cpu", ".isa.miscRegs"): "misc",    # system registers
    ("cpu", ".power_state"): "power",
    ("redist", ""): "redist",
    ("cpuif", ""): "cpuif",
}

# Keys that exist once per CPU inside a section that exists once, as
# {section: regex on the key}; group 1 is the CPU index.
# [system] has quiesceEndTick_0, quiesceEndTick_1, ...
PER_CPU_KEYS = {
    "system": re.compile(r"^quiesceEndTick_(\d+)$"),
}

# Variables whose reset value may differ between the references because it
# depends on the machine; convert.py computes them from the machine
# parameters. Any other difference between references stops the script.
MACHINE_VARIABLES = {
    "system.physmem.store1:range_size",   # RAM size in bytes (store0 is the
                                          # 128 MB boot flash, always the same)
}

# ===========================================================================
# Generic timer sections (format of src/dev/arm/generic_timer.cc)
# ===========================================================================
# gem5 creates the architected timers of a CPU at the first access to one of
# its timer registers. The references are written after 1000 ticks, before
# any access, so they only contain "cpu_count=0" in
# [system.realview.generic_timer] and no timer section. A converted snapshot
# needs them, because gem5 keeps the timer registers (CNTV_CTL, CNTV_CVAL,
# CNTKCTL, ...) in these sections and NOT in miscRegs.
#
# GenericTimer::serialize writes cpu_count, then for each CPU i < cpu_count
# CoreTimers::serialize writes [<generic_timer>.pe_implementation<i>] with
# the keys below, and one sub-section per timer (ArchTimer::serialize).
TIMER_SECTION = "system.realview.generic_timer"

# Keys of [pe_implementation<i>], in the order gem5 writes them:
# (key, reset value, condition). A key with a condition is only written by
# gem5 when the condition key is "true" (the time of the next event-stream
# event is only saved when an event is scheduled).
# Reset values: CoreTimers constructor (everything 0 or false, events not
# scheduled). cntfrq is None here: its reset is the GenericTimer "cntfrq"
# parameter, which is not in m5.cpt, so the counter frequency of the
# references (system.realview.sys_counter _freq) is used instead.
TIMER_KEYS = [
    ("cntfrq", None, None),
    ("cntkctl", "0", None),
    ("cnthctl", "0", None),
    ("phys_ev_scheduled", "false", None),
    ("phys_ev_when", "0", "phys_ev_scheduled"),
    ("physEvStream.transitionTo", "0", None),
    ("physEvStream.transitionBit", "0", None),
    ("virt_ev_scheduled", "false", None),
    ("virt_ev_when", "0", "virt_ev_scheduled"),
    ("virtEvStream.transitionTo", "0", None),
    ("virtEvStream.transitionBit", "0", None),
]
# One sub-section per timer of the CPU, in the order gem5 writes them, each
# with the 3 keys of ArchTimer::serialize: control register (CTL), compare
# value (CVAL) and offset (CNTVOFF for the virtual timer). Reset: all 0.
TIMER_SUBSECTIONS = ["phys_el3_timer", "phys_el1_timer", "virt_el1_timer",
                     "phys_ns_el2_timer", "virt_ns_el2_timer",
                     "phys_s_el2_timer", "virt_s_el2_timer"]
TIMER_SUB_KEYS = [("control_serial", "0"), ("_counterLimit", "0"),
                  ("_offset", "0")]

# gem5 starts m5.cpt with one comment line: "## checkpoint generated: <date>"
HEADER_PREFIX = "## "


# ===========================================================================
# Reading m5.cpt
# ===========================================================================

class Checkpoint:
    """One m5.cpt file, as written by gem5.

    Format: comment lines starting with "#", then for every section an empty
    line, "[section name]" and its "key=value" lines. Sections and keys are
    kept in file order (the template must reproduce that order).

    Attributes:
      path      file name
      text      the whole file (for the re-render test)
      header    the comment lines before the first section
      sections  {section name: {key: value}}, in file order
      cpus      sorted list of the CPU indices found (e.g. [0, 1, 2, 3])
    """

    def __init__(self, path):
        self.path = path
        self.text = open(path).read()
        self.header = []
        self.sections = OrderedDict()
        current = None
        for line in self.text.split("\n"):
            m = re.match(r"^\[(.*)\]$", line)
            if m:
                current = OrderedDict()
                self.sections[m.group(1)] = current
            elif current is None:
                if line.startswith("#"):
                    self.header.append(line)
            elif "=" in line:
                # split on the first "=" only: values may contain "="
                k, v = line.split("=", 1)
                current[k] = v
        self.cpus = self._cpu_indices()

    def _cpu_indices(self):
        """CPU indices of every per-CPU section ("cpus" counts as CPU 0)."""
        indices = set()
        for name in self.sections:
            info = per_cpu_kind(name)
            if info:
                indices.add(info[1])
        return sorted(indices)

    @property
    def num_cpus(self):
        return len(self.cpus)


def per_cpu_kind(name):
    """Return (kind, CPU index, rest of the name) if the section exists once
    per CPU, None if it exists once. E.g.
      "system.cpu_cluster.cpus2.isa.miscRegs" -> ("cpu", 2, ".isa.miscRegs")
      "system.cpu_cluster.cpus"               -> ("cpu", 0, "")
      "system.realview.uart"                  -> None
    """
    for kind, rx in PER_CPU_SECTIONS:
        m = rx.match(name)
        if m:
            return kind, int(m.group(1) or 0), m.group(2)
    return None


def section_name(kind, index, suffix, num_cpus):
    """Inverse of per_cpu_kind: the real section name of a per-CPU section in
    a checkpoint with num_cpus CPUs."""
    if kind == "cpu":
        tag = "" if num_cpus == 1 else str(index)
        return f"system.cpu_cluster.cpus{tag}{suffix}"
    if kind == "redist":
        return f"system.realview.gic.redistributors.{index}{suffix}"
    return f"system.realview.gic.cpuInterface.{index}{suffix}"


def fail(msg):
    """Stop with an error message (no template is written)."""
    sys.exit(f"make_template.py: ERROR: {msg}")


# ===========================================================================
# Step 1: checks on the references
# ===========================================================================

def check_cpus_identical(cpt):
    """Inside one reference, every CPU must have the same keys and the same
    reset values as the first CPU: the template writes the sections of one
    CPU and repeats them for all CPUs, so this must hold."""
    for name, kv in cpt.sections.items():
        info = per_cpu_kind(name)
        if info is None or info[1] == cpt.cpus[0]:
            continue                  # once-only section, or the first CPU
        kind, index, suffix = info
        first = cpt.sections[section_name(kind, cpt.cpus[0], suffix,
                                          cpt.num_cpus)]
        if list(kv) != list(first):
            fail(f"{cpt.path}: [{name}] has other keys than CPU 0")
        for k, v in kv.items():
            if first[k] != v:
                fail(f"{cpt.path}: [{name}] {k}={v[:40]} differs from CPU 0 "
                     f"({first[k][:40]}); per-CPU reset values must be equal")
    # same for the per-CPU keys of once-only sections (quiesceEndTick_<N>)
    for sec, rx in PER_CPU_KEYS.items():
        values = {v for k, v in cpt.sections[sec].items() if rx.match(k)}
        if len(values) > 1:
            fail(f"{cpt.path}: [{sec}] per-CPU keys have different values")


def check_against_base(ref, base):
    """A reference may differ from the base only by its CPU count and by the
    MACHINE_VARIABLES.

    To compare checkpoints with different CPU counts, each one is first
    reduced to its once-only sections plus the sections of its first CPU,
    with CPU-independent names ("<cpu>.isa.miscRegs" instead of
    "system.cpu_cluster.cpus0.isa.miscRegs"). Then the section lists, the key
    lists and the values must be equal."""
    def reduce(cpt):
        out = OrderedDict()
        for name, kv in cpt.sections.items():
            info = per_cpu_kind(name)
            if info is not None:
                if info[1] != cpt.cpus[0]:
                    continue                  # only the first CPU is kept
                key_sec = f"<{info[0]}>{info[2]}"
            else:
                key_sec = name
            keys = OrderedDict()
            rx = PER_CPU_KEYS.get(name)
            for k, v in kv.items():
                if rx and rx.match(k):
                    if int(rx.match(k).group(1)) != cpt.cpus[0]:
                        continue              # only the first CPU's key
                    k = rx.pattern            # CPU-independent key name
                keys[k] = v
            out[key_sec] = keys
        return out

    a, b = reduce(base), reduce(ref)
    if list(a) != list(b):
        fail(f"{ref.path}: not the same sections as {base.path} "
             f"(another machine, other devices?)")
    for sec in a:
        if list(a[sec]) != list(b[sec]):
            fail(f"{ref.path}: [{sec}] has other keys than in {base.path}")
        for k in a[sec]:
            if a[sec][k] != b[sec][k] and f"{sec}:{k}" not in MACHINE_VARIABLES:
                fail(f"{ref.path}: [{sec}] {k}={b[sec][k][:40]} but "
                     f"{a[sec][k][:40]} in {base.path}. If this value depends "
                     f"on the machine, add it to MACHINE_VARIABLES and to "
                     f"conversion_dict.py; otherwise the references do not "
                     f"come from the same gem5 machine")


# ===========================================================================
# Step 2: writing the template
# ===========================================================================

def find_layout(cpt):
    """Describe the order of the base sections, for the template.

    Returns a list of items, in file order:
      ("section", name)
          a section that exists once
      ("loop", kind, order, [rest of name, ...])
          all the sections of one kind (cpu, redist or cpuif), for every CPU.
          gem5 writes them CPU after CPU, each CPU with the same list of
          sections; order is "ascending" (CPU 0 first) or "descending" (last
          CPU first). The template writes the list once inside a loop.

    In the QFlex references:
      - redistributors.0, .1, .2, .3 then cpuInterface.0 ... .3 (ascending);
      - the 48 sections of cpus3, then of cpus2, cpus1, cpus0 (descending).
    The script stops if gem5 ever writes the per-CPU sections in another way
    (not contiguous, CPUs mixed, or a CPU with other sections).
    """
    items = []
    names = list(cpt.sections)
    i = 0
    seen_kinds = set()
    while i < len(names):
        info = per_cpu_kind(names[i])
        if info is None:
            items.append(("section", names[i]))
            i += 1
            continue
        kind = info[0]
        if kind in seen_kinds:
            fail(f"{cpt.path}: the {kind} sections are not contiguous")
        seen_kinds.add(kind)
        # the run: this section and every following section of the same kind
        run = []
        while i < len(names):
            info = per_cpu_kind(names[i])
            if info is None or info[0] != kind:
                break
            run.append(info)
            i += 1
        # split the run into one block per CPU: [(index, [rest, ...]), ...]
        blocks = []
        for _, index, suffix in run:
            if not blocks or blocks[-1][0] != index:
                blocks.append((index, []))
            blocks[-1][1].append(suffix)
        order_seen = [b[0] for b in blocks]
        if sorted(order_seen) != cpt.cpus or len(set(order_seen)) != len(blocks):
            fail(f"{cpt.path}: the {kind} sections of a CPU are not contiguous")
        if order_seen == sorted(order_seen):
            order = "ascending"
        elif order_seen == sorted(order_seen, reverse=True):
            order = "descending"
        else:
            fail(f"{cpt.path}: unexpected CPU order {order_seen} for {kind}")
        suffixes = blocks[0][1]
        for index, sfx in blocks:
            if sfx != suffixes:
                fail(f"{cpt.path}: CPU {index} does not have the same "
                     f"{kind} sections as CPU {blocks[0][0]}")
        items.append(("loop", kind, order, suffixes))
    return items


def jinja_value(var, per_cpu):
    """The Jinja expression that renders one variable.

    The context given to the template by convert.py (and by the test below):
      cpus    list of the CPUs, each {"index": i, "v": {variable: value}}
              holding the per-CPU variables of that CPU
      v       {variable: value} for the variables that exist once
      header  text of the "## ..." first line
    Values are strings, written as they must appear in m5.cpt."""
    if per_cpu:
        return '{{ cpu.v["%s"] }}' % var
    return '{{ v["%s"] }}' % var


def literal(value, where):
    """A reset value copied as it is. It must not contain Jinja syntax,
    otherwise Jinja would interpret it (never happens in gem5 checkpoints,
    checked anyway)."""
    if any(t in value for t in ("{{", "{%", "{#")):
        fail(f"{where}: value contains Jinja syntax, cannot be written as is")
    return value


def write_template(base, items, variables, reset):
    """Build the template text from the base reference.

    variables  set of variable names (keys of conversion_dict.py)
    reset      dict filled here with the reset value of each variable found
               (the value written by gem5 in the base)
    Returns (template text, set of the variables actually placed).

    The template is written line by line: each "{% ... %}" or "{# ... #}"
    is alone on its line, so that with trim_blocks/lstrip_blocks (see the
    Environment in main) it leaves no blank line in the output."""
    used = set()
    out = []
    w = out.append

    # Comment and macro at the top: they produce no output.
    w("{#- m5.cpt template, made by make_template.py. Do not edit: edit the")
    w("    references, conversion_dict.py or make_template.py and run it again.")
    w("    Context: cpus = [{index, v: {variable: value}}] (per-CPU variables),")
    w("    v = {variable: value} (other variables), header = comment line. #}")
    w("{%- macro cpu_tag(cpu) -%}{{ '' if cpus|length == 1 else cpu.index }}"
      "{%- endmacro %}")
    w(HEADER_PREFIX + "{{ header }}")

    def emit_keys(section_real, kv, scope, per_cpu):
        """Write the key=value lines of one section: a variable if
        "<scope>:<key>" is in conversion_dict.py, the reset value otherwise.
        scope is None for per-CPU sections that hold no variables."""
        for k, v in kv.items():
            if not per_cpu and section_real in PER_CPU_KEYS \
                    and PER_CPU_KEYS[section_real].match(k):
                continue                     # written by a loop, see below
            var = f"{scope}:{k}" if scope else None
            if var and var in variables:
                used.add(var)
                reset[var] = v
                w(f"{k}={jinja_value(var, per_cpu)}")
            else:
                w(f"{k}={literal(v, section_real)}")

    for item in items:
        if item[0] == "section":
            # ---- a section that exists once -----------------------------
            name = item[1]
            kv = base.sections[name]
            w("")
            w(f"[{name}]")
            # for these sections the scope is the section name itself,
            # e.g. "system.realview.sys_counter:_value"
            emit_keys(name, kv, name, per_cpu=False)
            # per-CPU keys of this section, e.g. quiesceEndTick_<N>: one
            # line per CPU, all with the reset value (checked equal)
            rx = PER_CPU_KEYS.get(name)
            if rx:
                keys = [k for k in kv if rx.match(k)]
                if keys:
                    stem = rx.pattern.lstrip("^").split("_(")[0]
                    value = kv[keys[0]]
                    w("{% for cpu in cpus %}")
                    w(f"{stem}_{{{{ cpu.index }}}}={literal(value, name)}")
                    w("{% endfor %}")
            # the timer sections go right after [system.realview.generic_timer]
            if name == TIMER_SECTION:
                write_timer_sections(w, variables, used, reset, base)
            continue

        # ---- the sections of one kind, for every CPU --------------------
        _, kind, order, suffixes = item
        cpus = "cpus|reverse" if order == "descending" else "cpus"
        if kind in ("redist", "cpuif"):
            w("{# TODO GICv3: state not dumped by QEMU yet, gem5 reset values #}")
        w(f"{{% for cpu in {cpus} %}}")
        first = base.cpus[0]
        for suffix in suffixes:
            # the sections of the first CPU of the base are the model
            real = section_name(kind, first, suffix, base.num_cpus)
            w("")
            w("[" + TEMPLATE_SECTION_NAME[kind] % suffix + "]")
            emit_keys(real, base.sections[real], SCOPE_OF.get((kind, suffix)),
                      per_cpu=True)
        w("{% endfor %}")
    return "\n".join(out) + "\n", used


def write_timer_sections(w, variables, used, reset, base):
    """Write the generic timer sections of every CPU (see TIMER_KEYS).

    They are inside {% if cpu_count > 0 %}: in the references cpu_count is 0
    and they are absent, so the re-render test still gives back the
    references; convert.py sets cpu_count to the number of CPUs."""
    freq = base.sections["system.realview.sys_counter"]["_freq"]
    w("{# Generic timer of each CPU (src/dev/arm/generic_timer.cc). Absent")
    w("   from the references: gem5 creates the timers at their first access. #}")
    w('{%% if v["%s:cpu_count"]|int > 0 %%}' % TIMER_SECTION)
    w("{% for cpu in cpus %}")
    w("")
    w(f"[{TIMER_SECTION}.pe_implementation{{{{ cpu.index }}}}]")
    for key, value, only_if in TIMER_KEYS:
        if value is None:
            value = freq                     # cntfrq: the counter frequency
        var = f"timer:{key}"
        line_value = value
        if var in variables:
            used.add(var)
            reset[var] = value
            line_value = jinja_value(var, per_cpu=True)
        if only_if:
            # conditional key: only written when the condition is "true".
            # If the condition is not a variable it stays at its reset
            # ("false"), so the key is never written.
            cond_var = f"timer:{only_if}"
            if cond_var not in variables:
                continue
            cond = jinja_value(cond_var, True)[3:-3]   # without {{ }}
            w("{%% if %s == \"true\" %%}" % cond)
            w(f"{key}={line_value}")
            w("{% endif %}")
        else:
            w(f"{key}={line_value}")
    for sub in TIMER_SUBSECTIONS:
        w("")
        w(f"[{TIMER_SECTION}.pe_implementation{{{{ cpu.index }}}}.{sub}]")
        for key, value in TIMER_SUB_KEYS:
            var = f"timer.{sub}:{key}"
            if var in variables:
                used.add(var)
                reset[var] = value
                w(f"{key}={jinja_value(var, per_cpu=True)}")
            else:
                w(f"{key}={value}")
    w("{% endfor %}")
    w("{% endif %}")


# ===========================================================================
# Step 3: re-render test
# ===========================================================================

# Scopes whose variables have one value per CPU (see conversion_dict.py).
PER_CPU_SCOPES = {"cpu", "xc", "misc", "power", "redist", "cpuif", "timer"}


def is_per_cpu(var):
    """True if the variable has one value per CPU ("timer.virt_el1_timer:..."
    is per CPU too: its scope starts with "timer")."""
    scope = var.split(":", 1)[0]
    return scope.split(".")[0] in PER_CPU_SCOPES


def render_like(template, ref, reset):
    """Render the template as convert.py will, but with gem5's reset values
    instead of QEMU values, and with the CPU count and RAM size of ref.
    The result must be ref itself."""
    v = {k: val for k, val in reset.items() if not is_per_cpu(k)}
    v["system.physmem.store1:range_size"] = \
        ref.sections["system.physmem.store1"]["range_size"]
    v[f"{TIMER_SECTION}:cpu_count"] = "0"     # references: no timer yet
    per_cpu = {k: val for k, val in reset.items() if is_per_cpu(k)}
    cpus = [{"index": i, "v": per_cpu} for i in ref.cpus]
    header = ref.header[0][len(HEADER_PREFIX):] if ref.header else ""
    return template.render(cpus=cpus, v=v, header=header)


def first_difference(a, b):
    """Describe the first differing line between two texts."""
    la, lb = a.split("\n"), b.split("\n")
    for n, (x, y) in enumerate(zip(la, lb)):
        if x != y:
            return f"line {n + 1}:\n  expected: {x[:120]}\n  rendered: {y[:120]}"
    return f"length differs ({len(la)} lines expected, {len(lb)} rendered)"


# ===========================================================================

def load_dict(path):
    """Import conversion_dict.py from its path and return CONVERSION."""
    spec = importlib.util.spec_from_file_location("conversion_dict", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.CONVERSION


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refs", nargs="+", required=True,
                    help="reference m5.cpt files")
    ap.add_argument("--dict", required=True, help="conversion_dict.py")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    variables = set(load_dict(args.dict))
    refs = [Checkpoint(p) for p in args.refs]
    for r in refs:
        if not r.cpus:
            fail(f"{r.path}: no CPU section found")
    # The base is the reference with the most CPUs: at least 2 are needed to
    # see which sections are per CPU and in which order gem5 writes them.
    # On a tie, the file name decides, so the choice is always the same.
    base = max(refs, key=lambda r: (r.num_cpus, r.path))
    if base.num_cpus < 2:
        fail("at least one reference must have 2 CPUs or more")
    print(f"base reference: {base.path} ({base.num_cpus} CPUs)")

    # ---- step 1: checks ------------------------------------------------------
    for r in refs:
        check_cpus_identical(r)
        if r is not base:
            check_against_base(r, base)
    print(f"checks: {len(refs)} references, same structure, per-CPU values "
          f"identical, differences only in {sorted(MACHINE_VARIABLES)}")

    # ---- step 2: template ----------------------------------------------------
    items = find_layout(base)
    reset = {}
    text, used = write_template(base, items, variables, reset)
    # a variable of conversion_dict.py that was not placed has a wrong
    # section or key name: better stop than silently ignore it
    missing = sorted(variables - used)
    if missing:
        fail("variables of conversion_dict.py not found in the references "
             "(wrong section or key name?): " + ", ".join(missing))

    os.makedirs(args.out_dir, exist_ok=True)
    tpl_path = os.path.join(args.out_dir, "m5.cpt.j2")
    with open(tpl_path, "w") as f:
        f.write(text)
    reset_path = os.path.join(args.out_dir, "gem5_reset.json")
    with open(reset_path, "w") as f:
        json.dump({
            # where these values come from, so convert.py can check that it
            # runs with the gem5 version the template was made for
            "_made_by": "make_template.py",
            "_date": time.ctime(),
            "_references": [os.path.basename(r.path) for r in refs],
            "_gem5_version_tags":
                base.sections["root.globals"].get("version_tags", ""),
            "reset": dict(sorted(reset.items())),
        }, f, indent=1)

    loops = [i for i in items if i[0] == "loop"]
    print(f"template: {tpl_path}")
    print(f"  {sum(1 for i in items if i[0] == 'section')} sections written "
          f"once, {len(loops)} per-CPU loops:")
    for _, kind, order, suffixes in loops:
        print(f"    {kind}: {len(suffixes)} sections per CPU, {order} order")
    print(f"  {len(used)} variables ({sum(map(is_per_cpu, used))} per CPU)")
    print(f"reset values: {reset_path}")

    # ---- step 3: re-render test ---------------------------------------------
    # Same Jinja settings as convert.py must use:
    #   trim_blocks/lstrip_blocks  a line holding only {% %} or {# #} leaves
    #                              no empty line in the output
    #   keep_trailing_newline      m5.cpt ends with a newline
    #   StrictUndefined            a missing variable is an error, not ""
    env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True,
                             keep_trailing_newline=True,
                             undefined=jinja2.StrictUndefined)
    template = env.from_string(text)
    ok = True
    for r in refs:
        rendered = render_like(template, r, reset)
        if rendered == r.text:
            print(f"re-render {os.path.basename(r.path)}: identical "
                  f"({r.num_cpus} CPUs)")
        else:
            ok = False
            print(f"re-render {os.path.basename(r.path)}: DIFFERENT, "
                  + first_difference(r.text, rendered))
    if not ok:
        fail("the template does not give back the references")
    return 0


if __name__ == "__main__":
    sys.exit(main())
