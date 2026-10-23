#!/usr/bin/env python3
"""Convert a QEMU register-info.json into the CPU state of a gem5 m5.cpt.

Inputs
  register-info.json  produced by parallel-qemu at snapshot time
  skeleton m5.cpt     produced by gem5 itself (qflex_generate_checkpoint.py) with
                      the same configuration as the simulation; it provides every
                      non-CPU object, the array sizes and the misc register list
  gem5_layout.json    produced by gen_gem5_layout.py from the gem5 sources

Output
  a new m5.cpt: the skeleton with the CPU state of every core replaced, and a
  coverage report on stderr.

Principle: a register value comes either from the snapshot or from gem5's own
reset state (the skeleton), never from a value written in this repository.

Usage
  python3 convert.py register-info.json skeleton/m5.cpt gem5_layout.json out/m5.cpt
"""
import argparse
import json
import re
import sys
from collections import OrderedDict

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def hexval(s):
    return int(s, 16)


def le_bytes_from_hex(s, nbytes):
    """QEMU prints registers MSB first ("0x<MSB..LSB>"); gem5 stores LSB first."""
    raw = bytes.fromhex(s[2:])[::-1]
    if len(raw) > nbytes and any(raw[nbytes:]):
        raise ValueError(f"value {s[:18]}... does not fit in {nbytes} bytes")
    return raw[:nbytes].ljust(nbytes, b"\0")


def fmt_bytes(b):
    return " ".join(str(x) for x in b)


def parse_bytes(v):
    return bytes(int(x) for x in v.split()) if v.strip() else b""


# --------------------------------------------------------------------------
# m5.cpt (INI-like) reader / writer that keeps the original order
# --------------------------------------------------------------------------

class Checkpoint:
    def __init__(self, path):
        self.sections = OrderedDict()
        current = None
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                m = re.match(r"^\[(.*)\]$", line)
                if m:
                    current = OrderedDict()
                    self.sections[m.group(1)] = current
                elif current is not None and "=" in line:
                    k, v = line.split("=", 1)
                    current[k] = v

    def write(self, path):
        with open(path, "w") as f:
            for name, kv in self.sections.items():
                f.write(f"[{name}]\n")
                for k, v in kv.items():
                    f.write(f"{k}={v}\n")
                f.write("\n")


# --------------------------------------------------------------------------
# QEMU name -> gem5 misc register name
# --------------------------------------------------------------------------

# QEMU names whose gem5 equivalent cannot be derived by the generic rule.
QEMU_SPECIAL = {
    "AMAIR0": "amair_el1",   # QEMU uses the AArch32 name for AMAIR_EL1
}

# Registers that are NOT copied into miscRegs, with the reason.
# (Values are still read from the JSON where they are used elsewhere.)
NOT_MISC = {
    "pc": "goes to the PC state (_pc/_npc)",
    "sp": "current SP, goes to regs.integer",
    "cpsr": "handled separately (flags -> regs.condition_code)",
    "SP_EL0": "goes to regs.integer (sp0); gem5 reads SP_EL0 from there",
    "SP_EL1": "goes to regs.integer (sp1); gem5 reads SP_EL1 from there",
    "MPIDR_EL1": "computed by gem5 (readMPIDR); checked, not written",
    "CNTVCT_EL0": "counter value lives in gem5's generic timer, not in miscRegs",
    "CNTPCT_EL0": "counter value lives in gem5's generic timer, not in miscRegs",
    "vg": "SVE vector length, gem5 configuration (sve_vl)",
    "fpcr": "lowercase duplicate of FPCR",
    "fpsr": "lowercase duplicate of FPSR",
}

ID_REG = re.compile(r"^(ID_|MIDR|REVIDR|CTR_EL0|CLIDR|CCSIDR|AIDR|DCZID|MVFR|"
                    r"ERRIDR|LORID|SMIDR|PMCEID|PMMIR|MDRAR|RVBAR|CBAR)")


def gem5_candidates(qname):
    if qname in QEMU_SPECIAL:
        return [QEMU_SPECIAL[qname]]
    low = qname.lower()
    if low.endswith("_reserved") or low.startswith("res_0_"):
        return []
    cands = [low]
    if not re.search(r"_el\d+$", low):
        # QEMU names ARM_CP_STATE_BOTH registers by their AArch32 name.
        cands.insert(0, low + "_el1")
    return cands


# --------------------------------------------------------------------------
# Coverage: why a gem5 misc slot was not written
# --------------------------------------------------------------------------

RESET_REASONS = [
    (r"(_el2|_el3|_el12|_el02)$|^(h|v)(?!bar)|hyp|^scr|^nsacr|^sder|^mvbar|^mon|_s$",
     "EL2/EL3/Secure state not implemented (QEMU virtualization=off, secure=off)"),
    (r"^(tlbi|at_|ats|dc_|ic_|dccmvau|dccimvac|dcimvac|dccmvac|dccisw|dcisw|dccsw|"
     r"icialluis|iciallu|icimvau|bpiall|bpiallis|bpimva|cp15|tlb|itlb|dtlb|cfp|cpp|dvp)",
     "operation encoding, no state"),
    (r"^(id_|midr|mpidr|revidr|ctr|clidr|ccsidr|csselr|aidr|dczid|mvfr|fpsid|jidr|"
     r"tcmtr|tlbtr|erridr|lorid|smidr|gmid|mpamidr|pmceid|dbgdidr|dbgdevid|dbgauth|"
     r"dbgdrar|dbgdsar|mdrar|rvbar|rmr|cbar)",
     "identification register: gem5 describes its own CPU model"),
    (r"^ap(ia|ib|da|db|ga)key", "pointer authentication disabled (pauth=off)"),
    (r"^icv_|^ich_", "GIC virtual CPU interface (EL2 only)"),
    (r"^(mpam|pir|por|tcr2|sctlr2|gcs|rcw|trf|trc|brb|pms|pmb|spe|amu|amcg|amcn|amev|"
     r"amcr|amcntenclr|amcntenset|amuserenr|tfsr|gcr|rgsr|mec|pfar|hcrx|zcr_el3|smcr|"
     r"smpri|svcr|tpidr2|zt0|erx|err|disr|vdisr|vsesr|scxtnum|lor|hdfg|hfg|hafgrtr)",
     "architecture feature not used by this snapshot / not in QEMU's state"),
    (r"^(dbg|osl|osd|mdcc|mddtr|dlr|dspsr|dbgw|dbgb|teecr|teehbr|joscr|jmcr|"
     r"dl1data|il1data|l2ctlr|l2ectlr|l2merrsr|cpumerrsr|cpuactlr|cpuectlr|"
     r"actlr|adfsr|aifsr|afsr|l2actlr|pmswinc|pmxev|pmintenclr|oslar|isr|nmrr_s|"
     r"prrr_s|fcseidr|tcmtr|nop)",
     "debug / implementation-defined / derived register"),
]


# Slots that gem5 computes from other state instead of reading them (isa.cc).
PSTATE_VIEWS = {
    "nzcv": "view of regs.condition_code (isa.cc readMiscReg)",
    "daif": "view of cpsr (isa.cc readMiscReg)",
    "spsel": "view of cpsr (isa.cc readMiscReg)",
    "currentel": "view of cpsr (isa.cc readMiscReg)",
    "sp_el0": "view of regs.integer sp0 (isa.cc readMiscReg)",
    "sp_el1": "view of regs.integer sp1 (isa.cc readMiscReg)",
    "cpsr_mode": "view of cpsr", "cpsr_q": "view of cpsr",
    "fpscr": "view of fpcr/fpsr (isa.cc readMiscReg)",
    "fpscr_exc": "view of fpcr/fpsr", "fpscr_qc": "view of fpcr/fpsr",
    "cntpct": "counter: lives in gem5's generic timer",
    "cntvct": "counter: lives in gem5's generic timer",
    "lockaddr": "gem5 internal (exclusive monitor)",
    "lockflag": "gem5 internal (exclusive monitor)",
    "sev_mailbox": "gem5 internal (WFE/SEV)",
    "rndr": "random number, no state", "rndrrs": "random number, no state",
}


def reset_reason(name):
    for pattern, reason in RESET_REASONS:
        if re.search(pattern, name):
            return reason
    return None


# --------------------------------------------------------------------------
# Per-CPU conversion
# --------------------------------------------------------------------------

def find_cpus(cpt):
    """Return {cpu_index: (xc_section_name, misc_section_name)}."""
    cpus = {}
    for name in cpt.sections:
        m = re.match(r"^(.*\.cpus?(\d+))\.xc\.0$", name)
        if m:
            base, idx = m.group(1), int(m.group(2))
            cpus[idx] = (name, f"{base}.isa.miscRegs")
    return dict(sorted(cpus.items()))


def convert_cpu(regs, xc, misc, layout, report):
    vec_info = layout["vec"]
    cpsr = hexval(regs["cpsr"])
    el = (cpsr >> 2) & 3
    spsel = cpsr & 1

    # ---- regs.integer -------------------------------------------------------
    int_bytes = parse_bytes(xc["regs.integer"])
    n_int = len(layout["int_regs"])
    slot_bytes = len(int_bytes) // n_int
    assert slot_bytes * n_int == len(int_bytes), "regs.integer size mismatch"
    ints = [0] * n_int
    for i in range(31):                                # x0..x30 (x31 = zero reg)
        ints[layout["x_slot"][i]] = hexval(regs[f"x{i}"])
    for lvl in range(4):
        name = f"SP_EL{lvl}"
        if name in regs:
            ints[layout["sp_slot"][name]] = hexval(regs[name])
    # The banked SP_ELx copies in QEMU are only synced on exception entry/exit,
    # so the SP of the current mode must come from "sp".
    current_sp = "SP_EL0" if (el == 0 or spsel == 0) else f"SP_EL{el}"
    ints[layout["sp_slot"][current_sp]] = hexval(regs["sp"])
    xc["regs.integer"] = fmt_bytes(b"".join(v.to_bytes(slot_bytes, "little")
                                            for v in ints))

    # ---- regs.vector / vector_element / vector_predicate --------------------
    n_vec = vec_info["NumVecRegs"]
    vec_total = len(parse_bytes(xc["regs.vector"]))
    vreg_bytes = vec_total // n_vec
    vectors = [bytes(vreg_bytes)] * n_vec
    src = "z" if "z0" in regs else "v"
    for i in range(vec_info["NumVecV8ArchRegs"]):
        vectors[i] = le_bytes_from_hex(regs[f"{src}{i}"], vreg_bytes)
    xc["regs.vector"] = fmt_bytes(b"".join(vectors))

    # Element view (VecElem = uint32, stored as one RegVal per element).
    elem_total = len(parse_bytes(xc["regs.vector_element"]))
    elems_per_reg = vreg_bytes // 4
    elem_slot = elem_total // (n_vec * elems_per_reg)
    elem_out = bytearray()
    for v in vectors:
        for e in range(elems_per_reg):
            word = int.from_bytes(v[4 * e:4 * e + 4], "little")
            elem_out += word.to_bytes(elem_slot, "little")
    xc["regs.vector_element"] = fmt_bytes(elem_out)

    n_pred = vec_info["NumVecPredRegs"]
    pred_total = len(parse_bytes(xc["regs.vector_predicate"]))
    pbits = pred_total // n_pred                 # one byte per predicate bit
    preds = [bytes(pbits)] * n_pred
    names = {i: f"p{i}" for i in range(16)}
    names[vec_info["PREDREG_FFR"]] = "ffr"
    for idx, qname in names.items():
        if qname in regs:
            val = hexval(regs[qname])
            preds[idx] = bytes((val >> b) & 1 for b in range(pbits))
    xc["regs.vector_predicate"] = fmt_bytes(b"".join(preds))

    # regs.matrix: SME ZA state, only valid if SME is active (SVCR.ZA).
    if hexval(regs.get("SVCR", "0x0")) & 0x2:
        report["errors"].append("SVCR.ZA=1 but ZA is not exported by QEMU")

    # ---- regs.condition_code ------------------------------------------------
    cc_vals = {"nz": (cpsr >> 30) & 3, "c": (cpsr >> 29) & 1, "v": (cpsr >> 28) & 1}
    cc_names = layout["cc_regs"]
    cc_total = len(parse_bytes(xc["regs.condition_code"]))
    cc_slot = cc_total // len(cc_names)
    xc["regs.condition_code"] = fmt_bytes(b"".join(
        cc_vals.get(n, 0).to_bytes(cc_slot, "little") for n in cc_names))

    # ---- PC state ------------------------------------------------------------
    pc = hexval(regs["pc"])
    xc["_pc"] = str(pc)
    xc["_npc"] = str(pc + 4)
    # flags/_size/nextFlags come from the skeleton (AArch64: flags=4).

    # ---- miscRegs --------------------------------------------------------------
    alias = layout["misc_alias"]
    written = {}

    def put(name, value):
        if name not in misc:
            raise KeyError(name)
        misc[name] = str(value)
        written[name] = True

    # CPSR without the NZCV flags (they live in regs.condition_code).
    put("cpsr", cpsr & ~0xF0000000)

    for qname, sval in regs.items():
        if VECTOR_OR_GPR.fullmatch(qname) or qname in NOT_MISC:
            continue
        if ID_REG.match(qname):
            report["id_regs"].add(qname)
            continue
        cands = [c for c in gem5_candidates(qname) if c in alias or c in misc]
        if not cands:
            if hexval(sval) != 0:
                report["lost"].add(f"{qname}={sval}")
            else:
                report["no_gem5_equiv"].add(qname)
            continue
        g = cands[0]
        val = hexval(sval)
        targets = alias.get(g, [g])
        if len(targets) == 2:              # 64-bit value split in two 32-bit slots
            put(targets[0], val & 0xFFFFFFFF)
            put(targets[1], val >> 32)
        else:
            put(targets[0], val)
        report["captured"].add(g)
    return written


VECTOR_OR_GPR = re.compile(r"[xvqdshbzp]\d+|ffr")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("register_info")
    ap.add_argument("skeleton")
    ap.add_argument("layout")
    ap.add_argument("output")
    args = ap.parse_args()

    qemu = json.load(open(args.register_info))
    layout = json.load(open(args.layout))
    cpt = Checkpoint(args.skeleton)

    qcpus = {c["cpu_id"]: c["registers"] for c in qemu["cpus"]}
    gcpus = find_cpus(cpt)
    if sorted(qcpus) != sorted(gcpus):
        sys.exit(f"CPU count mismatch: snapshot has {len(qcpus)} CPUs, "
                 f"skeleton has {len(gcpus)}. Regenerate the skeleton.")

    report = {"captured": set(), "id_regs": set(), "lost": set(),
              "no_gem5_equiv": set(), "errors": []}
    written_any = {}
    for idx, (xc_name, misc_name) in gcpus.items():
        written = convert_cpu(qcpus[idx], cpt.sections[xc_name],
                              cpt.sections[misc_name], layout, report)
        written_any.update(written)

    cpt.write(args.output)

    # ---- coverage report ------------------------------------------------------
    first_misc = cpt.sections[gcpus[min(gcpus)][1]]
    alias = layout["misc_alias"]
    reasons, unexplained = {}, []
    for name in first_misc:
        if name in written_any:
            continue
        if name in alias:
            r = "alias: gem5 stores this register in another slot (.mapsTo)"
        elif name + "_ns" in first_misc or name.endswith("_ns"):
            r = "AArch32 banked copy (guest runs AArch64, no Secure state)"
        elif name in PSTATE_VIEWS:
            r = PSTATE_VIEWS[name]
        else:
            r = reset_reason(name)
        if r:
            reasons.setdefault(r, []).append(name)
        else:
            unexplained.append(name)

    out = sys.stderr
    print(f"== Converted {len(gcpus)} CPUs -> {args.output}", file=out)
    print(f"regs.*: integer, vector, vector_element, vector_predicate, "
          f"condition_code, PC state written for every CPU", file=out)
    print(f"miscRegs: {len(written_any)} slots written from the snapshot "
          f"(out of {len(first_misc)})", file=out)
    print("Left at gem5 reset value:", file=out)
    for r, names in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(names):4d}  {r}", file=out)
    print(f"  {len(unexplained):4d}  UNEXPLAINED", file=out)
    for n in unexplained:
        print(f"         {n}={first_misc[n]}", file=out)
    print(f"QEMU ID registers not copied (gem5 keeps its own): "
          f"{len(report['id_regs'])}", file=out)
    print(f"QEMU registers without gem5 equivalent (all zero): "
          f"{sorted(report['no_gem5_equiv'])}", file=out)
    if report["lost"]:
        print(f"WARNING non-zero QEMU registers lost: {sorted(report['lost'])}", file=out)
    for e in report["errors"]:
        print(f"ERROR {e}", file=out)
    print("To check at restore: MPIDR per CPU "
          + ", ".join(f"cpu{i}={qcpus[i].get('MPIDR_EL1')}" for i in sorted(qcpus))
          + f"; counter CNTVCT={qcpus[min(qcpus)].get('CNTVCT_EL0')} "
          f"CNTFRQ={qcpus[min(qcpus)].get('CNTFRQ_EL0')}", file=out)
    return 1 if report["errors"] or report["lost"] else 0


if __name__ == "__main__":
    sys.exit(main())
