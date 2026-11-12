"""Conversion dictionary: what is converted from a QEMU snapshot to gem5.

Role in the conversion
----------------------
  m5.cpt.j2           (made by make_template.py) the gem5 checkpoint of the
                      QFlex machine: everything gem5 writes, with its reset
                      values, and a {{ variable }} wherever a value comes
                      from the snapshot. Each variable carries gem5's reset
                      value as its default.
  conversion_dict.py  (this file, written by hand) one entry per variable:
                      which file the value comes from, which value(s) in
                      it, and which transform converts it. Also the gem5
                      register layout and the machine constants the
                      transforms need.
  transforms.py       the conversion functions named here
  convert.py          reads the sources, applies each entry, renders the
                      template

Every variable of the template is an entry of this file: make_template.py
turns exactly these names into {{ ... }}; everything else in m5.cpt stays
gem5's reset value. To convert one more value: add an entry here, add its
transform to transforms.py if needed, and run make_template.py again.

Variable names: "<scope>:<key>"
-------------------------------
<key> is the key as written in m5.cpt; <scope> says which section it is in.

  Per-CPU scopes (the template loops over the CPUs; the value of each CPU
  is computed from that CPU's registers in register-info.json):
    cpu        [system.cpu_cluster.cpus<N>]
    xc         [system.cpu_cluster.cpus<N>.xc.0]            registers, PC
    misc       [system.cpu_cluster.cpus<N>.isa.miscRegs]    system registers
    power      [system.cpu_cluster.cpus<N>.power_state]
    redist     [system.realview.gic.redistributors.<N>]
    cpuif      [system.realview.gic.cpuInterface.<N>]
    timer      [system.realview.generic_timer.pe_implementation<N>]
    timer.<t>  its sub-section <t>: virt_el1_timer, phys_el1_timer, ...
  (<N> is the CPU index; with a single CPU gem5 writes "cpus", not "cpus0".
  The template takes care of that, names here never contain <N>.)

  Any other scope is the full name of a section that exists once, e.g.
  "system.realview.sys_counter:_value" is the key _value of
  [system.realview.sys_counter].

Fields of an entry
------------------
  entry(var=..., file=..., path=..., transform=..., default=..., note=...)

  var        the variable (see above)
  file       where the value comes from:
               "register-info.json"  the QEMU register dump of the snapshot;
                                     for a per-CPU variable, the registers of
                                     the same CPU; for a variable that exists
                                     once, the registers of CPU 0
               "machine.yaml"        the device tree of the QEMU machine
                                     (convert.py --yaml): "num_cpus" (number
                                     of cpu@N nodes) or "ram_bytes" (size of
                                     the memory node)
               "conversion_dict.py"  a constant of this file (MACHINE below)
               None                  not converted yet: the variable keeps
                                     gem5's reset value. It is already a
                                     variable, so converting it later does
                                     not change the template.
  path       the name of the value in that file: a register name, a list of
             register names given to the transform together ("z{i}" means
             z0, z1, ...), or a key of "machine.yaml" / MACHINE
  transform  the function of transforms.py that turns the value into the
             text written in m5.cpt
  default    only when the source has no value. Not given: gem5's reset
             value (written in the template by make_template.py). A string:
             written as is; only for a value chosen on purpose (e.g. CNTVOFF
             is 0 because QEMU has no EL2), so that every value chosen by
             hand is visible in this file.
  note       optional explanation
"""

# {variable name: entry}, filled by the entry() calls below.
CONVERSION = {}

# Possible values of the "file" field
FILES = {"register-info.json", "machine.yaml", "conversion_dict.py", None}


def entry(var, file, path, transform, default=None, note=None):
    """Add one variable to CONVERSION. A variable must be added only once:
    two sources for the same m5.cpt value would be a mistake."""
    assert var not in CONVERSION, f"duplicate variable {var}"
    assert file in FILES, f"{var}: unknown file {file}"
    CONVERSION[var] = {"file": file, "path": path, "transform": transform,
                       "default": default, "note": note}


# ===========================================================================
# Constants
# ===========================================================================

# gem5 register layout (gem5 sources, src/arch/arm/regs/). In m5.cpt, the
# register files of a CPU (xc.0 regs.*) are unlabelled lists of bytes: these
# constants say where each register is. They are gem5 implementation choices:
# check them against the sources if the gem5 version changes.
# make_template.py checks that the register-file lines of the references have
# the sizes these constants give.
GEM5_LAYOUT = {
    # regs.integer: NumRegs slots of 8 bytes (int.hh, enum int_reg)
    "int_regs": 42,
    "int_reg_bytes": 8,
    # Slot of x0..x30 (int.hh, Reg64Map). gem5 shares the AArch64 registers
    # with the AArch32 banked registers: x0-x14 are r0-r14 (slots 0-14),
    # x15 is r13hyp (20), x16 r14irq (26), x17 r13irq (25), x18 r14svc (17),
    # x19 r13svc (16), x20 r14abt (22), x21 r13abt (21), x22 r14und (24),
    # x23 r13und (23), x24-x30 are r8fiq-r14fiq (27-33).
    "x_slot": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
               20, 26, 25, 17, 16, 22, 21, 24, 23,
               27, 28, 29, 30, 31, 32, 33],
    # Slot of the stack pointers (int.hh, _Sp0Idx.._Sp3Idx)
    "sp_slot": {"SP_EL0": 38, "SP_EL1": 39, "SP_EL2": 40, "SP_EL3": 41},
    # regs.condition_code: flag registers in this order, 8 bytes each (cc.hh)
    "cc_regs": ["nz", "c", "v", "ge", "fp", "zero"],
    "cc_reg_bytes": 8,
    # regs.vector: NumVecRegs registers (vec.hh); the first NumVecV8ArchRegs
    # are z0-z31, the others are gem5 internal. Size: sve_vl = 16 quadwords
    # (qflex_system.py) = 2048 bits = 256 bytes.
    "vec_regs": 45,
    "vec_arch_regs": 32,
    "vec_reg_bytes": 256,
    # regs.vector_element: the same registers as 32-bit elements, each in an
    # 8-byte slot (one RegVal)
    "vec_elem_bytes": 8,
    # regs.vector_predicate: NumVecPredRegs registers of 2048 / 8 = 256 bits,
    # one byte per bit; p0-p15 in slots 0-15, FFR in PREDREG_FFR (vec.hh)
    "pred_regs": 18,
    "pred_reg_bits": 256,
    "pred_ffr_slot": 16,
}

# Machine constants: settings of the QFlex gem5 machine that some transforms
# need. make_template.py checks them against the references.
MACHINE = {
    # gem5 tick at which the checkpoint is restored: the references are
    # written after m5.simulate(1000) (qflex_generate_checkpoint.py), and the
    # template keeps their curTick.
    "restore_tick": 1000,
    # Frequency of the gem5 system counter (QEMU_CNTFRQ_HZ in
    # qflex_system.py, = QEMU's CNTFRQ_EL0).
    "counter_freq": 62_500_000,
}


# ===========================================================================
# Per CPU: general registers, vector registers, flags, PC (section xc.0)
# ===========================================================================
# Each regs.* line is written whole by one transform, from GEM5_LAYOUT.
entry(var="xc:regs.integer", file="register-info.json",
      path=[f"x{i}" for i in range(31)] + ["sp", "SP_EL0", "SP_EL1", "cpsr"],
      transform="int_regs",
      note="x0-x30 in their Reg64Map slots. The SP of the current EL comes "
      "from 'sp' (chosen with cpsr.EL and cpsr.SPSel): QEMU only copies it "
      "to SP_ELx on exception entry/exit, so SP_ELx may be stale")
entry(var="xc:regs.vector", file="register-info.json",
      path=["z{i}", "v{i}"], transform="vec_regs",
      note="z0-z31 when SVE is on, otherwise v0-v31 (128 bits); padded with "
      "zeros to 256 bytes. v/q/d/s/h/b are the low bits of z: not separate")
entry(var="xc:regs.vector_element", file="register-info.json",
      path=["z{i}", "v{i}"], transform="vec_elems",
      note="the same registers seen as 32-bit elements, each in an 8-byte "
      "slot (a second copy kept by gem5)")
entry(var="xc:regs.vector_predicate", file="register-info.json",
      path=["p{i}", "ffr"], transform="pred_regs",
      note="p0-p15 in slots 0-15, FFR in slot 16, one byte per bit")
entry(var="xc:regs.condition_code", file="register-info.json",
      path="cpsr", transform="cc_regs",
      note="gem5 keeps the NZCV flags here (nz, c, v slots), not in cpsr")
entry(var="xc:regs.matrix", file=None, path=None, transform=None,
      note="SME ZA array: not in the QEMU dump. Its reset value (zeros) is "
      "exact while SVCR.ZA = 0; convert.py stops if SVCR.ZA = 1")
# PC state: gem5 stores the current and the next PC. AArch64 instructions are
# 4 bytes, so next = pc + 4. The other PC state keys (flags=4 means AArch64,
# _upc, _nupc, ...) keep gem5's reset value.
entry(var="xc:_pc", file="register-info.json", path="pc",
      transform="hex_to_int")
entry(var="xc:_npc", file="register-info.json", path="pc",
      transform="pc_plus_4")

# ===========================================================================
# Per CPU: system registers (section isa.miscRegs)
# ===========================================================================
# cpsr: mode, exception masks, ... without the NZCV flags (see cc_regs).
entry(var="misc:cpsr", file="register-info.json", path="cpsr",
      transform="cpsr_without_flags")

# QEMU register -> gem5 miscRegs key, one entry per key.
#
# The gem5 key is not always the lower-case QEMU name: gem5 stores many
# AArch64 registers in the key of their AArch32 equivalent (".mapsTo" in
# src/arch/arm/regs/misc.cc). E.g. SCTLR_EL1 is stored in sctlr_ns, TCR_EL1
# in ttbcr_ns. The AArch64 key (sctlr_el1, ...) also exists in m5.cpt but
# gem5 never reads it: a value written there would be silently lost.
#
# low32 / high32: gem5 splits some 64-bit registers into two 32-bit AArch32
# registers, low half in the first key and high half in the second: MAIR_EL1
# -> prrr_ns + nmrr_ns, FAR_EL1 -> dfar_ns + ifar_ns, AMAIR_EL1 (QEMU name
# AMAIR0) -> amair0_ns + amair1_ns, DBGBVRn_EL1 -> dbgbvrn + dbgbxvrn.
#
# QEMU names some EL1 registers by their AArch32 name (SCTLR, CPACR, VBAR,
# CSSELR, ...).
#
# Not in this list, and why:
#   - EL2/EL3 registers: QEMU runs with virtualization=off, secure=off, so
#     they hold no state; gem5 keeps its reset values (convert.py checks
#     that they are all zero in the dump);
#   - timer registers (CNT*): gem5 keeps them in the generic timer sections
#     (see below), not in miscRegs;
#   - MPIDR_EL1: gem5 computes it from the CPU index (same affinity as QEMU
#     virt for up to 16 CPUs);
#   - pc, sp, cpsr, SP_ELx, vector registers, vg, fpcr/fpsr (lower-case
#     duplicates of FPCR/FPSR): handled above;
#   - ID registers: see the next section;
#   - QEMU registers without a gem5 equivalent (e.g. ID_PFR2, ID_MMFR5).
#
# The list was made from a register-info.json of the QFlex machine and the
# .mapsTo of gem5's misc.cc; check it again if QEMU or gem5 change.
entry(var="misc:actlr_ns", file="register-info.json", path="ACTLR_EL1",
      transform="hex_to_int")
entry(var="misc:adfsr_ns", file="register-info.json", path="AFSR0_EL1",
      transform="hex_to_int")
entry(var="misc:aifsr_ns", file="register-info.json", path="AFSR1_EL1",
      transform="hex_to_int")
entry(var="misc:amair0_ns", file="register-info.json", path="AMAIR0",
      transform="low32")
entry(var="misc:amair1_ns", file="register-info.json", path="AMAIR0",
      transform="high32")
entry(var="misc:contextidr_ns", file="register-info.json", path="CONTEXTIDR_EL1",
      transform="hex_to_int")
entry(var="misc:cpacr", file="register-info.json", path="CPACR",
      transform="hex_to_int")
entry(var="misc:cpuactlr_el1", file="register-info.json", path="CPUACTLR_EL1",
      transform="hex_to_int")
entry(var="misc:cpuectlr_el1", file="register-info.json", path="CPUECTLR_EL1",
      transform="hex_to_int")
entry(var="misc:cpumerrsr_el1", file="register-info.json", path="CPUMERRSR_EL1",
      transform="hex_to_int")
entry(var="misc:csselr_ns", file="register-info.json", path="CSSELR",
      transform="hex_to_int")
entry(var="misc:dbgbcr0", file="register-info.json", path="DBGBCR0_EL1",
      transform="hex_to_int")
entry(var="misc:dbgbcr1", file="register-info.json", path="DBGBCR1_EL1",
      transform="hex_to_int")
entry(var="misc:dbgbcr2", file="register-info.json", path="DBGBCR2_EL1",
      transform="hex_to_int")
entry(var="misc:dbgbcr3", file="register-info.json", path="DBGBCR3_EL1",
      transform="hex_to_int")
entry(var="misc:dbgbcr4", file="register-info.json", path="DBGBCR4_EL1",
      transform="hex_to_int")
entry(var="misc:dbgbcr5", file="register-info.json", path="DBGBCR5_EL1",
      transform="hex_to_int")
entry(var="misc:dbgbvr0", file="register-info.json", path="DBGBVR0_EL1",
      transform="low32")
entry(var="misc:dbgbxvr0", file="register-info.json", path="DBGBVR0_EL1",
      transform="high32")
entry(var="misc:dbgbvr1", file="register-info.json", path="DBGBVR1_EL1",
      transform="low32")
entry(var="misc:dbgbxvr1", file="register-info.json", path="DBGBVR1_EL1",
      transform="high32")
entry(var="misc:dbgbvr2", file="register-info.json", path="DBGBVR2_EL1",
      transform="low32")
entry(var="misc:dbgbxvr2", file="register-info.json", path="DBGBVR2_EL1",
      transform="high32")
entry(var="misc:dbgbvr3", file="register-info.json", path="DBGBVR3_EL1",
      transform="low32")
entry(var="misc:dbgbxvr3", file="register-info.json", path="DBGBVR3_EL1",
      transform="high32")
entry(var="misc:dbgbvr4", file="register-info.json", path="DBGBVR4_EL1",
      transform="low32")
entry(var="misc:dbgbxvr4", file="register-info.json", path="DBGBVR4_EL1",
      transform="high32")
entry(var="misc:dbgbvr5", file="register-info.json", path="DBGBVR5_EL1",
      transform="low32")
entry(var="misc:dbgbxvr5", file="register-info.json", path="DBGBVR5_EL1",
      transform="high32")
entry(var="misc:dbgclaimclr", file="register-info.json", path="DBGCLAIMCLR_EL1",
      transform="hex_to_int")
entry(var="misc:dbgclaimset", file="register-info.json", path="DBGCLAIMSET_EL1",
      transform="hex_to_int")
entry(var="misc:dbgwcr0", file="register-info.json", path="DBGWCR0_EL1",
      transform="hex_to_int")
entry(var="misc:dbgwcr1", file="register-info.json", path="DBGWCR1_EL1",
      transform="hex_to_int")
entry(var="misc:dbgwcr2", file="register-info.json", path="DBGWCR2_EL1",
      transform="hex_to_int")
entry(var="misc:dbgwcr3", file="register-info.json", path="DBGWCR3_EL1",
      transform="hex_to_int")
entry(var="misc:dbgwvr0", file="register-info.json", path="DBGWVR0_EL1",
      transform="hex_to_int")
entry(var="misc:dbgwvr1", file="register-info.json", path="DBGWVR1_EL1",
      transform="hex_to_int")
entry(var="misc:dbgwvr2", file="register-info.json", path="DBGWVR2_EL1",
      transform="hex_to_int")
entry(var="misc:dbgwvr3", file="register-info.json", path="DBGWVR3_EL1",
      transform="hex_to_int")
entry(var="misc:elr_el1", file="register-info.json", path="ELR_EL1",
      transform="hex_to_int")
entry(var="misc:esr_el1", file="register-info.json", path="ESR_EL1",
      transform="hex_to_int")
entry(var="misc:dfar_ns", file="register-info.json", path="FAR_EL1",
      transform="low32")
entry(var="misc:ifar_ns", file="register-info.json", path="FAR_EL1",
      transform="high32")
entry(var="misc:fpcr", file="register-info.json", path="FPCR",
      transform="hex_to_int")
entry(var="misc:fpsr", file="register-info.json", path="FPSR",
      transform="hex_to_int")
entry(var="misc:icc_ap0r0", file="register-info.json", path="ICC_AP0R0_EL1",
      transform="hex_to_int")
entry(var="misc:icc_ap0r1", file="register-info.json", path="ICC_AP0R1_EL1",
      transform="hex_to_int")
entry(var="misc:icc_ap0r2", file="register-info.json", path="ICC_AP0R2_EL1",
      transform="hex_to_int")
entry(var="misc:icc_ap0r3", file="register-info.json", path="ICC_AP0R3_EL1",
      transform="hex_to_int")
entry(var="misc:icc_ap1r0", file="register-info.json", path="ICC_AP1R0_EL1",
      transform="hex_to_int")
entry(var="misc:icc_ap1r1", file="register-info.json", path="ICC_AP1R1_EL1",
      transform="hex_to_int")
entry(var="misc:icc_ap1r2", file="register-info.json", path="ICC_AP1R2_EL1",
      transform="hex_to_int")
entry(var="misc:icc_ap1r3", file="register-info.json", path="ICC_AP1R3_EL1",
      transform="hex_to_int")
entry(var="misc:icc_bpr0", file="register-info.json", path="ICC_BPR0_EL1",
      transform="hex_to_int")
entry(var="misc:icc_bpr1", file="register-info.json", path="ICC_BPR1_EL1",
      transform="hex_to_int")
entry(var="misc:icc_ctlr", file="register-info.json", path="ICC_CTLR_EL1",
      transform="hex_to_int")
entry(var="misc:icc_igrpen0", file="register-info.json", path="ICC_IGRPEN0_EL1",
      transform="hex_to_int")
entry(var="misc:icc_igrpen1", file="register-info.json", path="ICC_IGRPEN1_EL1",
      transform="hex_to_int")
entry(var="misc:icc_pmr", file="register-info.json", path="ICC_PMR_EL1",
      transform="hex_to_int")
entry(var="misc:icc_sre", file="register-info.json", path="ICC_SRE_EL1",
      transform="hex_to_int")
entry(var="misc:l2actlr_el1", file="register-info.json", path="L2ACTLR",
      transform="hex_to_int")
entry(var="misc:l2ctlr_el1", file="register-info.json", path="L2CTLR_EL1",
      transform="hex_to_int")
entry(var="misc:l2ectlr_el1", file="register-info.json", path="L2ECTLR_EL1",
      transform="hex_to_int")
entry(var="misc:l2merrsr_el1", file="register-info.json", path="L2MERRSR_EL1",
      transform="hex_to_int")
entry(var="misc:prrr_ns", file="register-info.json", path="MAIR_EL1",
      transform="low32")
entry(var="misc:nmrr_ns", file="register-info.json", path="MAIR_EL1",
      transform="high32")
entry(var="misc:dbgdscrint", file="register-info.json", path="MDCCSR_EL0",
      transform="hex_to_int")
entry(var="misc:dbgdscrext", file="register-info.json", path="MDSCR_EL1",
      transform="hex_to_int")
entry(var="misc:dbgosdlr", file="register-info.json", path="OSDLR_EL1",
      transform="hex_to_int")
entry(var="misc:dbgdtrrxext", file="register-info.json", path="OSDTRRX_EL1",
      transform="hex_to_int")
entry(var="misc:dbgdtrtxext", file="register-info.json", path="OSDTRTX_EL1",
      transform="hex_to_int")
entry(var="misc:dbgoseccr", file="register-info.json", path="OSECCR_EL1",
      transform="hex_to_int")
entry(var="misc:dbgoslsr", file="register-info.json", path="OSLSR_EL1",
      transform="hex_to_int")
entry(var="misc:par_ns", file="register-info.json", path="PAR_EL1",
      transform="hex_to_int")
entry(var="misc:pmccfiltr_el0", file="register-info.json", path="PMCCFILTR_EL0",
      transform="hex_to_int")
entry(var="misc:pmccntr", file="register-info.json", path="PMCCNTR_EL0",
      transform="hex_to_int")
entry(var="misc:pmcntenclr", file="register-info.json", path="PMCNTENCLR_EL0",
      transform="hex_to_int")
entry(var="misc:pmcntenset", file="register-info.json", path="PMCNTENSET_EL0",
      transform="hex_to_int")
entry(var="misc:pmcr", file="register-info.json", path="PMCR_EL0",
      transform="hex_to_int")
entry(var="misc:pmevcntr0", file="register-info.json", path="PMEVCNTR0_EL0",
      transform="hex_to_int")
entry(var="misc:pmevcntr1", file="register-info.json", path="PMEVCNTR1_EL0",
      transform="hex_to_int")
entry(var="misc:pmevcntr2", file="register-info.json", path="PMEVCNTR2_EL0",
      transform="hex_to_int")
entry(var="misc:pmevcntr3", file="register-info.json", path="PMEVCNTR3_EL0",
      transform="hex_to_int")
entry(var="misc:pmevcntr4", file="register-info.json", path="PMEVCNTR4_EL0",
      transform="hex_to_int")
entry(var="misc:pmevcntr5", file="register-info.json", path="PMEVCNTR5_EL0",
      transform="hex_to_int")
entry(var="misc:pmevtyper0", file="register-info.json", path="PMEVTYPER0_EL0",
      transform="hex_to_int")
entry(var="misc:pmevtyper1", file="register-info.json", path="PMEVTYPER1_EL0",
      transform="hex_to_int")
entry(var="misc:pmevtyper2", file="register-info.json", path="PMEVTYPER2_EL0",
      transform="hex_to_int")
entry(var="misc:pmevtyper3", file="register-info.json", path="PMEVTYPER3_EL0",
      transform="hex_to_int")
entry(var="misc:pmevtyper4", file="register-info.json", path="PMEVTYPER4_EL0",
      transform="hex_to_int")
entry(var="misc:pmevtyper5", file="register-info.json", path="PMEVTYPER5_EL0",
      transform="hex_to_int")
entry(var="misc:pmintenset", file="register-info.json", path="PMINTENSET_EL1",
      transform="hex_to_int")
entry(var="misc:pmovsclr_el0", file="register-info.json", path="PMOVSCLR_EL0",
      transform="hex_to_int")
entry(var="misc:pmovsset", file="register-info.json", path="PMOVSSET_EL0",
      transform="hex_to_int")
entry(var="misc:pmselr", file="register-info.json", path="PMSELR_EL0",
      transform="hex_to_int")
entry(var="misc:pmuserenr", file="register-info.json", path="PMUSERENR_EL0",
      transform="hex_to_int")
entry(var="misc:sctlr_ns", file="register-info.json", path="SCTLR",
      transform="hex_to_int")
entry(var="misc:smcr_el1", file="register-info.json", path="SMCR_EL1",
      transform="hex_to_int")
entry(var="misc:smpri_el1", file="register-info.json", path="SMPRI_EL1",
      transform="hex_to_int")
entry(var="misc:spsr_abt", file="register-info.json", path="SPSR_ABT",
      transform="hex_to_int")
entry(var="misc:spsr_svc", file="register-info.json", path="SPSR_EL1",
      transform="hex_to_int")
entry(var="misc:spsr_fiq", file="register-info.json", path="SPSR_FIQ",
      transform="hex_to_int")
entry(var="misc:spsr_irq", file="register-info.json", path="SPSR_IRQ",
      transform="hex_to_int")
entry(var="misc:spsr_und", file="register-info.json", path="SPSR_UND",
      transform="hex_to_int")
entry(var="misc:svcr", file="register-info.json", path="SVCR",
      transform="hex_to_int")
entry(var="misc:ttbcr_ns", file="register-info.json", path="TCR_EL1",
      transform="tcr_el1")
entry(var="misc:tpidr2_el0", file="register-info.json", path="TPIDR2_EL0",
      transform="hex_to_int")
entry(var="misc:tpidruro_ns", file="register-info.json", path="TPIDRRO_EL0",
      transform="hex_to_int")
entry(var="misc:tpidrurw_ns", file="register-info.json", path="TPIDR_EL0",
      transform="hex_to_int")
entry(var="misc:tpidrprw_ns", file="register-info.json", path="TPIDR_EL1",
      transform="hex_to_int")
entry(var="misc:ttbr0_ns", file="register-info.json", path="TTBR0_EL1",
      transform="hex_to_int")
entry(var="misc:ttbr1_ns", file="register-info.json", path="TTBR1_EL1",
      transform="hex_to_int")
entry(var="misc:vbar_ns", file="register-info.json", path="VBAR",
      transform="hex_to_int")
entry(var="misc:zcr_el1", file="register-info.json", path="ZCR_EL1",
      transform="hex_to_int")

# ===========================================================================
# Per CPU: ID registers (what the CPU says it implements)
# ===========================================================================
# An ID register has two kinds of fields (cpu_model_features/id_fields.py):
#   - feature fields (kind level/signed): LSE, SVE, PAN, RNDR, ... The guest
#     kernel read them at boot in QEMU and enabled features accordingly.
#     Thanks to id-overrides, QEMU's are at most gem5's;
#   - info fields: they describe the machine, not a feature: sizes (PARange,
#     ASIDBits, granules), breakpoint counts, PMU version, GIC, CSV2/CSV3,
#     E0PD... QEMU's values describe QEMU's machine (e.g. PARange 52 bits, a
#     PMU), not the gem5 one.
#
# 1. The 10 AArch64 feature registers of id_fields.py are merged field by
#    field (transform id_merge): feature fields from QEMU (what the guest saw
#    at boot; convert.py stops if one is above gem5's, i.e. the snapshot was
#    not taken with id-overrides), info fields from gem5 (the simulated
#    machine).
#    gem5 does not restore ID_AA64PFR0_EL1 and ID_AA64PFR1_EL1: it always
#    uses its own value (.unserialize(0) in misc.cc). PFR0 merged is equal to
#    gem5's; for PFR1 gem5 shows SME = 2 and MPAM_frac = 1 where the guest
#    saw SME = 1 (harmless: the guest kernel does not use SME).
entry(var="misc:id_aa64isar0_el1", file="register-info.json",
      path="ID_AA64ISAR0_EL1", transform="id_merge")
entry(var="misc:id_aa64isar1_el1", file="register-info.json",
      path="ID_AA64ISAR1_EL1", transform="id_merge")
entry(var="misc:id_aa64pfr0_el1", file="register-info.json",
      path="ID_AA64PFR0_EL1", transform="id_merge")
entry(var="misc:id_aa64pfr1_el1", file="register-info.json",
      path="ID_AA64PFR1_EL1", transform="id_merge")
entry(var="misc:id_aa64mmfr0_el1", file="register-info.json",
      path="ID_AA64MMFR0_EL1", transform="id_merge")
entry(var="misc:id_aa64mmfr1_el1", file="register-info.json",
      path="ID_AA64MMFR1_EL1", transform="id_merge")
entry(var="misc:id_aa64mmfr2_el1", file="register-info.json",
      path="ID_AA64MMFR2_EL1", transform="id_merge")
entry(var="misc:id_aa64zfr0_el1", file="register-info.json",
      path="ID_AA64ZFR0_EL1", transform="id_merge")
entry(var="misc:id_aa64smfr0_el1", file="register-info.json",
      path="ID_AA64SMFR0_EL1", transform="id_merge")
entry(var="misc:id_aa64dfr0_el1", file="register-info.json",
      path="ID_AA64DFR0_EL1", transform="id_merge")

# 2. Cache geometry: gem5 keeps its own value, but the guest uses it at run
#    time (cache maintenance loops read the line sizes from CTR_EL0, DC ZVA
#    zeroes DCZID_EL0 blocks), so it must be the one QEMU had: convert.py
#    stops otherwise (transform must_match_gem5). Both match today.
entry(var="misc:ctr", file="register-info.json", path="CTR_EL0",
      transform="must_match_gem5")
entry(var="misc:dczid_el0", file="register-info.json", path="DCZID_EL0",
      transform="must_match_gem5")

# 3. The other ID / implementation registers keep gem5's values (they are not
#    variables of the template): the guest kernel only read them at boot and
#    keeps its own copy, and they describe the simulated CPU:
#      MIDR, REVIDR, AIDR, CLIDR, SMIDR, MDRAR, RVBAR, CBAR, PMCEID0/1
#      (gem5 has no PMU), ID_AA64AFR0/1, ID_AA64DFR1, and the AArch32 ID
#      registers ID_PFR*, ID_DFR0, ID_AFR0, ID_MMFR*, ID_ISAR*, MVFR*, which
#      only matter for 32-bit programs (not handled by id_fields.py yet).

# ===========================================================================
# Per CPU: generic timer
# ===========================================================================
# gem5 keeps the timer registers in [system.realview.generic_timer.
# pe_implementation<N>] and its sub-sections, not in miscRegs. These sections
# are not in the references (gem5 creates the timers at their first access);
# make_template.py adds them, and cpu_count (below) makes gem5 read them.
#
# Each timer is restored as CTL (control_serial), CVAL (_counterLimit) and
# offset. gem5 does not save the tick of the next timer interrupt: after the
# restore it computes it from CVAL and the counter.
entry(var="timer:cntfrq", file="register-info.json", path="CNTFRQ_EL0",
      transform="hex_to_int")
entry(var="timer:cntkctl", file="register-info.json", path="CNTKCTL",
      transform="hex_to_int")
# Event stream: Linux sets CNTKCTL.EVNTEN to get a periodic wake-up event
# (used by WFE loops). gem5 schedules it only when the guest writes CNTKCTL,
# not on restore, so the next event is saved here (at the restore tick), with
# the CNTKCTL.EVNTDIR / EVNTI fields (transitionTo / transitionBit).
entry(var="timer:virt_ev_scheduled", file="register-info.json",
      path="CNTKCTL", transform="evstream_enabled")
entry(var="timer:virt_ev_when", file="register-info.json", path="CNTKCTL",
      transform="evstream_next_tick")
entry(var="timer:virtEvStream.transitionTo", file="register-info.json",
      path="CNTKCTL", transform="evstream_transition_to")
entry(var="timer:virtEvStream.transitionBit", file="register-info.json",
      path="CNTKCTL", transform="evstream_transition_bit")
# EL1 virtual timer (the one Linux uses for its tick on arm64)
entry(var="timer.virt_el1_timer:control_serial", file="register-info.json",
      path="CNTV_CTL_EL0", transform="hex_to_int")
entry(var="timer.virt_el1_timer:_counterLimit", file="register-info.json",
      path="CNTV_CVAL_EL0", transform="hex_to_int")
entry(var="timer.virt_el1_timer:_offset", file="register-info.json",
      path="CNTVOFF_EL2", transform="hex_to_int", default="0",
      note="CNTVOFF: virtual count = physical count - offset. Not in the dump "
      "(no EL2 in QEMU), so 0")
# EL1 physical timer
entry(var="timer.phys_el1_timer:control_serial", file="register-info.json",
      path="CNTP_CTL_EL0", transform="hex_to_int")
entry(var="timer.phys_el1_timer:_counterLimit", file="register-info.json",
      path="CNTP_CVAL_EL0", transform="hex_to_int")
# The EL2 and EL3 timers keep gem5's reset value (no EL2/EL3 in QEMU).

# ===========================================================================
# Per CPU: GIC
# ===========================================================================
# The GIC distributor and redistributors are not in the QEMU dump yet: they
# keep gem5's reset values (all interrupts disabled) until the GICv3 work.
# peInLowPowerState is already a variable: it is the first value the GIC
# conversion will set (Linux clears GICR_WAKER.ProcessorSleep at boot).
# The GIC CPU interface registers (ICC_*) are system registers: they are in
# the system register list above.
entry(var="redist:peInLowPowerState", file=None, path=None, transform=None,
      note="TODO GICv3: Linux clears GICR_WAKER.ProcessorSleep")

# ===========================================================================
# Machine (values that exist once)
# ===========================================================================
entry(var="system.physmem.store1:range_size", file="machine.yaml",
      path="ram_bytes", transform="to_str",
      note="RAM size in bytes, from the memory node of the machine YAML (the "
      "same YAML as for the restore). store0 is the 128 MB boot flash")
entry(var="system.realview.generic_timer:cpu_count", file="machine.yaml",
      path="num_cpus", transform="to_str",
      note="number of [pe_implementation<N>] timer sections gem5 reads "
      "(0 in the references)")
# System counter: the clock behind CNTPCT/CNTVCT. gem5 computes the counter
# at tick t as _value + (t - _updateTick) / period. Starting from the QEMU
# counter value makes the guest see time continue where it stopped.
entry(var="system.realview.sys_counter:_value", file="register-info.json",
      path="CNTPCT_EL0", transform="counter_value",
      note="CNTPCT of CPU 0 (all CPUs share the counter)")
entry(var="system.realview.sys_counter:_updateTick",
      file="conversion_dict.py", path="restore_tick",
      transform="counter_update_tick",
      note="the tick at which _value is valid: the restore tick")
      