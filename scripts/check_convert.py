#!/usr/bin/env python3
"""Round-trip check: decode a converted m5.cpt and compare it with register-info.json.

Usage:
    python3 check_convert.py register-info.json converted/m5.cpt skeleton/m5.cpt gem5_layout.json

Checks, for every CPU:
  - x0..x30 and the current SP in regs.integer (through the gem5 slot layout)
  - z0..z31 in regs.vector, p0..p15 in regs.vector_predicate
  - NZCV in regs.condition_code, PC
  - a set of system registers read back through gem5's alias table
    (including 64-bit registers split over two slots)
and that every non-CPU section is identical to the skeleton.
Exit code 0 if everything matches.
"""
import json
import struct
import sys

from convert import Checkpoint, find_cpus, gem5_candidates, parse_bytes

SYSREGS = ["SCTLR", "TTBR0_EL1", "TTBR1_EL1", "TCR_EL1", "MAIR_EL1", "VBAR",
           "ELR_EL1", "ESR_EL1", "FAR_EL1", "SPSR_EL1", "TPIDR_EL0", "TPIDR_EL1",
           "CONTEXTIDR_EL1", "CNTV_CTL_EL0", "CNTV_CVAL_EL0", "CNTKCTL",
           "ICC_PMR_EL1", "ICC_IGRPEN1_EL1", "ICC_BPR1_EL1", "ICC_CTLR_EL1",
           "FPCR", "FPSR", "CPACR", "MDSCR_EL1", "PAR_EL1"]


def main(json_path, cpt_path, skel_path, layout_path):
    layout = json.load(open(layout_path))
    alias = layout["misc_alias"]
    qemu = {c["cpu_id"]: c["registers"] for c in json.load(open(json_path))["cpus"]}
    cpt, skel = Checkpoint(cpt_path), Checkpoint(skel_path)
    n_int = len(layout["int_regs"])
    failures, checks = [], 0

    def check(what, got, expected):
        nonlocal checks
        checks += 1
        if got != expected:
            failures.append(f"{what}: got {got:#x}, expected {expected:#x}")

    for cpu, (xc_name, misc_name) in find_cpus(cpt).items():
        r, xc, misc = qemu[cpu], cpt.sections[xc_name], cpt.sections[misc_name]
        h = lambda n: int(r[n], 16)

        ints = struct.unpack(f"<{n_int}Q", parse_bytes(xc["regs.integer"]))
        for i in range(31):
            check(f"cpu{cpu} x{i}", ints[layout["x_slot"][i]], h(f"x{i}"))
        cpsr = h("cpsr")
        el, spsel = (cpsr >> 2) & 3, cpsr & 1
        cur = "SP_EL0" if (el == 0 or spsel == 0) else f"SP_EL{el}"
        check(f"cpu{cpu} sp", ints[layout["sp_slot"][cur]], h("sp"))

        vec = parse_bytes(xc["regs.vector"])
        vb = len(vec) // layout["vec"]["NumVecRegs"]
        for i in range(32):
            check(f"cpu{cpu} z{i}", int.from_bytes(vec[vb * i:vb * (i + 1)], "little"),
                  h(f"z{i}"))
        pred = parse_bytes(xc["regs.vector_predicate"])
        pb = len(pred) // layout["vec"]["NumVecPredRegs"]
        for i in range(16):
            got = sum(pred[pb * i + b] << b for b in range(pb))
            check(f"cpu{cpu} p{i}", got, h(f"p{i}"))

        cc = dict(zip(layout["cc_regs"],
                      struct.unpack(f"<{len(layout['cc_regs'])}Q",
                                    parse_bytes(xc["regs.condition_code"]))))
        check(f"cpu{cpu} nzcv", (cc["nz"] << 2) | (cc["c"] << 1) | cc["v"], cpsr >> 28)
        check(f"cpu{cpu} pc", int(xc["_pc"]), h("pc"))
        check(f"cpu{cpu} cpsr", int(misc["cpsr"]), cpsr & 0x0FFFFFFF)

        for q in SYSREGS:
            # Same name resolution as convert.py.
            g = [c for c in gem5_candidates(q) if c in alias or c in misc][0]
            t = alias.get(g, [g])
            got = int(misc[t[0]]) if len(t) == 1 else \
                (int(misc[t[0]]) & 0xFFFFFFFF) | (int(misc[t[1]]) << 32)
            check(f"cpu{cpu} {q}", got, h(q))

    cpu_sections = {s for pair in find_cpus(cpt).values() for s in pair}
    for name, kv in skel.sections.items():
        if name not in cpu_sections and cpt.sections.get(name) != kv:
            failures.append(f"non-CPU section changed: {name}")

    print(f"{checks} checks, {len(failures)} failures")
    for f in failures[:50]:
        print("  FAIL", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:5]))
