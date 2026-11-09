"""Conversion dictionary: where each value of the converted m5.cpt comes from.

Role in the conversion
----------------------
  m5.cpt.j2        (made by make_template.py) the gem5 checkpoint with a
                   {{ variable }} wherever a value must come from the snapshot
  conversion_dict  (this file) one entry per variable: which file, which
                   value(s) in it, which function converts it
  transforms.py    the conversion functions named here
  convert.py       for each variable: reads the source, applies the transform,
                   and renders the template with the results

Every key of CONVERSION is a variable of the template: make_template.py
turns exactly these keys into {{ ... }}; all other values of m5.cpt stay
gem5's reset value. To convert one more value: add an entry here, add its
transform to transforms.py, and run make_template.py again.

DRAFT (step 1): the list of variables is complete for the register
conversion. The transform names refer to functions of transforms.py, which is
written in step 2; names may still change.

Variable names: "<scope>:<key>"
-------------------------------
<key> is the key as written in m5.cpt; <scope> tells which section it is in.

  Per-CPU scopes (the template loops over the CPUs; convert.py gives one
  value per CPU, computed from that CPU's registers in register-info.json):
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

Entry fields
------------
  file       where the value comes from:
               "register-info.json"  the QEMU register dump of the snapshot;
                                     for per-CPU variables, the registers of
                                     the same CPU
               "machine"             a machine parameter computed by
                                     convert.py: num_cpus, ram_bytes, cur_tick
               None                  not converted yet: the variable keeps
                                     gem5's reset value (it is a variable so
                                     that adding the conversion later does not
                                     change the template)
  path       the name(s) of the value(s) in that file: one register name, or
             a list of names given to the transform together. "{i}" means
             "for i = 0, 1, ..." (e.g. "z{i}" = z0 ... z31)
  transform  the function of transforms.py that turns the value(s) into the
             text written in m5.cpt
  default    used when the source has no value (e.g. a register absent from
             the dump): "reset" means gem5's reset value, read from
             gem5_reset.json; any other string is written as is
  note       optional explanation
"""

QEMU = "register-info.json"
MACHINE = "machine"

# {variable name: entry}. Filled by the entry() calls below.
CONVERSION = {}


def entry(var, file, path, transform, default="reset", note=None):
    """Add one variable to CONVERSION. A variable must be added only once:
    two sources for the same m5.cpt value would be a mistake."""
    assert var not in CONVERSION, f"duplicate variable {var}"
    CONVERSION[var] = {"file": file, "path": path, "transform": transform,
                       "default": default}
    if note:
        CONVERSION[var]["note"] = note


# ===========================================================================
# Per CPU: general registers, vector registers, flags, PC (section xc.0)
# ===========================================================================
# gem5 stores each register file as one line: the bytes of all the registers
# of that file, little-endian, one after the other. These lines are written
# whole by one transform. The slot of each register in the line is given by
# gem5_layout.json (extracted from the gem5 sources):
#   - integer file: 42 slots of 8 bytes. x0-x30 are not in slots 0-30: gem5
#     shares them with the AArch32 banked registers (Reg64Map in
#     src/arch/arm/regs/int.hh), e.g. x15 is slot 20 (r13hyp); SP_EL0..3 are
#     slots 38-41.
#   - vector file: 45 registers of 256 bytes (2048 bits, the SVE maximum);
#     z0-z31 are the first 32, the rest are gem5 internal registers.
#   - predicate file: 18 registers, one byte per bit; p0-p15, FFR is 16.
entry("xc:regs.integer", QEMU,
      [f"x{i}" for i in range(31)] + ["sp", "SP_EL0", "SP_EL1", "cpsr"],
      "int_regs",
      note="x0-x30 in their Reg64Map slots. The SP of the current EL comes "
      "from 'sp' (chosen with cpsr.EL and cpsr.SPSel): QEMU only copies it "
      "to SP_ELx on exception entry/exit, so SP_ELx may be stale")
entry("xc:regs.vector", QEMU, ["z{i}", "v{i}"], "vec_regs",
      note="z0-z31 when SVE is on, otherwise v0-v31 (128 bits); padded with "
      "zeros to 256 bytes. v/q/d/s/h/b are the low bits of z: not separate")
entry("xc:regs.vector_element", QEMU, ["z{i}", "v{i}"], "vec_elems",
      note="the same registers seen as 32-bit elements, each in an 8-byte "
      "slot (a second copy kept by gem5)")
entry("xc:regs.vector_predicate", QEMU, ["p{i}", "ffr"], "pred_regs",
      note="p0-p15 in slots 0-15, FFR in slot 16, one byte per bit")
entry("xc:regs.condition_code", QEMU, "cpsr", "cc_regs",
      note="gem5 keeps the NZCV flags here (nz, c, v slots), not in cpsr")
entry("xc:regs.matrix", None, None, None,
      note="SME ZA array: not in the QEMU dump. Its reset value (zeros) is "
      "exact while SVCR.ZA = 0; convert.py stops if SVCR.ZA = 1")
# PC state: gem5 stores the current and the next PC. AArch64 instructions are
# 4 bytes long, so next = pc + 4. The other PC state keys (flags=4 means
# AArch64, _upc, _nupc, ...) keep the reset value of the references.
entry("xc:_pc", QEMU, "pc", "hex_to_int")
entry("xc:_npc", QEMU, "pc", "pc_plus_4")

# ===========================================================================
# Per CPU: system registers (section isa.miscRegs)
# ===========================================================================
# cpsr: mode, exception masks, ... without the NZCV flags (see cc_regs).
entry("misc:cpsr", QEMU, "cpsr", "cpsr_without_flags")

# QEMU register name -> gem5 miscRegs key(s).
#
# The gem5 key is not always the lower-case QEMU name: gem5 stores many
# AArch64 registers in the key of their AArch32 equivalent (".mapsTo" in
# src/arch/arm/regs/misc.cc, copied to "misc_alias" in gem5_layout.json).
# E.g. SCTLR_EL1 is stored in sctlr_ns, TCR_EL1 in ttbcr_ns, MIDR_EL1 in
# midr. The AArch64 key (sctlr_el1, ...) also exists in m5.cpt but gem5 never
# reads it: writing the value there would be silently lost.
#
# When there are two keys, gem5 splits the 64-bit register into two 32-bit
# AArch32 registers: low 32 bits in the first key, high 32 bits in the
# second. E.g. MAIR_EL1 -> prrr_ns (low) and nmrr_ns (high), FAR_EL1 ->
# dfar_ns and ifar_ns.
#
# QEMU names some registers by their AArch32 name (SCTLR, CPACR, VBAR, ...):
# they are the EL1 registers. AMAIR0 is AMAIR_EL1.
#
# What is not in this table, and why:
#   - EL2/EL3 registers: QEMU runs with virtualization=off, secure=off, so
#     they hold no state; gem5 keeps its reset values (convert.py checks
#     that they are all zero in the dump);
#   - timer registers (CNT*): gem5 keeps them in the generic timer sections
#     (see below), not in miscRegs;
#   - MPIDR_EL1: gem5 computes it from the CPU index (same affinity as QEMU
#     virt for up to 16 CPUs);
#   - pc, sp, cpsr, SP_ELx, vector registers, vg, fpcr/fpsr (lower-case
#     duplicates of FPCR/FPSR): handled above;
#   - QEMU registers without a gem5 equivalent (e.g. ID_PFR2, ID_MMFR5).
#
# The ID registers (ID_*, MIDR, CTR, DCZID, ...) are copied: thanks to
# id-overrides, the QEMU CPU has at most gem5's features, and the guest
# kernel booted with these values, so gem5 must show the same ones.
#
# This table was made from a register-info.json of the QFlex machine and
# gem5_layout.json; check it again if QEMU or gem5 change.
MISC_COPY = [
    ("ACTLR_EL1", ['actlr_ns']),
    ("AFSR0_EL1", ['adfsr_ns']),
    ("AFSR1_EL1", ['aifsr_ns']),
    ("AIDR", ['aidr_el1']),
    ("AMAIR0", ['amair0_ns', 'amair1_ns']),
    ("CBAR_EL1", ['cbar_el1']),
    ("CLIDR", ['clidr_el1']),
    ("CONTEXTIDR_EL1", ['contextidr_ns']),
    ("CPACR", ['cpacr']),
    ("CPUACTLR_EL1", ['cpuactlr_el1']),
    ("CPUECTLR_EL1", ['cpuectlr_el1']),
    ("CPUMERRSR_EL1", ['cpumerrsr_el1']),
    ("CSSELR", ['csselr_ns']),
    ("CTR_EL0", ['ctr']),
    ("DBGBCR0_EL1", ['dbgbcr0']),
    ("DBGBCR1_EL1", ['dbgbcr1']),
    ("DBGBCR2_EL1", ['dbgbcr2']),
    ("DBGBCR3_EL1", ['dbgbcr3']),
    ("DBGBCR4_EL1", ['dbgbcr4']),
    ("DBGBCR5_EL1", ['dbgbcr5']),
    ("DBGBVR0_EL1", ['dbgbvr0', 'dbgbxvr0']),
    ("DBGBVR1_EL1", ['dbgbvr1', 'dbgbxvr1']),
    ("DBGBVR2_EL1", ['dbgbvr2', 'dbgbxvr2']),
    ("DBGBVR3_EL1", ['dbgbvr3', 'dbgbxvr3']),
    ("DBGBVR4_EL1", ['dbgbvr4', 'dbgbxvr4']),
    ("DBGBVR5_EL1", ['dbgbvr5', 'dbgbxvr5']),
    ("DBGCLAIMCLR_EL1", ['dbgclaimclr']),
    ("DBGCLAIMSET_EL1", ['dbgclaimset']),
    ("DBGWCR0_EL1", ['dbgwcr0']),
    ("DBGWCR1_EL1", ['dbgwcr1']),
    ("DBGWCR2_EL1", ['dbgwcr2']),
    ("DBGWCR3_EL1", ['dbgwcr3']),
    ("DBGWVR0_EL1", ['dbgwvr0']),
    ("DBGWVR1_EL1", ['dbgwvr1']),
    ("DBGWVR2_EL1", ['dbgwvr2']),
    ("DBGWVR3_EL1", ['dbgwvr3']),
    ("DCZID_EL0", ['dczid_el0']),
    ("ELR_EL1", ['elr_el1']),
    ("ESR_EL1", ['esr_el1']),
    ("FAR_EL1", ['dfar_ns', 'ifar_ns']),
    ("FPCR", ['fpcr']),
    ("FPSR", ['fpsr']),
    ("ICC_AP0R0_EL1", ['icc_ap0r0']),
    ("ICC_AP0R1_EL1", ['icc_ap0r1']),
    ("ICC_AP0R2_EL1", ['icc_ap0r2']),
    ("ICC_AP0R3_EL1", ['icc_ap0r3']),
    ("ICC_AP1R0_EL1", ['icc_ap1r0']),
    ("ICC_AP1R1_EL1", ['icc_ap1r1']),
    ("ICC_AP1R2_EL1", ['icc_ap1r2']),
    ("ICC_AP1R3_EL1", ['icc_ap1r3']),
    ("ICC_BPR0_EL1", ['icc_bpr0']),
    ("ICC_BPR1_EL1", ['icc_bpr1']),
    ("ICC_CTLR_EL1", ['icc_ctlr']),
    ("ICC_IGRPEN0_EL1", ['icc_igrpen0']),
    ("ICC_IGRPEN1_EL1", ['icc_igrpen1']),
    ("ICC_PMR_EL1", ['icc_pmr']),
    ("ICC_SRE_EL1", ['icc_sre']),
    ("ID_AA64AFR0_EL1", ['id_aa64afr0_el1']),
    ("ID_AA64AFR1_EL1", ['id_aa64afr1_el1']),
    ("ID_AA64DFR0_EL1", ['id_aa64dfr0_el1']),
    ("ID_AA64DFR1_EL1", ['id_aa64dfr1_el1']),
    ("ID_AA64ISAR0_EL1", ['id_aa64isar0_el1']),
    ("ID_AA64ISAR1_EL1", ['id_aa64isar1_el1']),
    ("ID_AA64MMFR0_EL1", ['id_aa64mmfr0_el1']),
    ("ID_AA64MMFR1_EL1", ['id_aa64mmfr1_el1']),
    ("ID_AA64MMFR2_EL1", ['id_aa64mmfr2_el1']),
    ("ID_AA64PFR0_EL1", ['id_aa64pfr0_el1']),
    ("ID_AA64PFR1_EL1", ['id_aa64pfr1_el1']),
    ("ID_AA64SMFR0_EL1", ['id_aa64smfr0_el1']),
    ("ID_AA64ZFR0_EL1", ['id_aa64zfr0_el1']),
    ("ID_AFR0", ['id_afr0']),
    ("ID_DFR0", ['id_dfr0']),
    ("ID_ISAR0", ['id_isar0']),
    ("ID_ISAR1", ['id_isar1']),
    ("ID_ISAR2", ['id_isar2']),
    ("ID_ISAR3", ['id_isar3']),
    ("ID_ISAR4", ['id_isar4']),
    ("ID_ISAR5", ['id_isar5']),
    ("ID_ISAR6", ['id_isar6']),
    ("ID_MMFR0", ['id_mmfr0']),
    ("ID_MMFR1", ['id_mmfr1']),
    ("ID_MMFR2", ['id_mmfr2']),
    ("ID_MMFR3", ['id_mmfr3']),
    ("ID_MMFR4", ['id_mmfr4']),
    ("ID_PFR0", ['id_pfr0']),
    ("ID_PFR1", ['id_pfr1']),
    ("L2ACTLR", ['l2actlr_el1']),
    ("L2CTLR_EL1", ['l2ctlr_el1']),
    ("L2ECTLR_EL1", ['l2ectlr_el1']),
    ("L2MERRSR_EL1", ['l2merrsr_el1']),
    ("MAIR_EL1", ['prrr_ns', 'nmrr_ns']),
    ("MDCCSR_EL0", ['dbgdscrint']),
    ("MDRAR_EL1", ['dbgdrar']),
    ("MDSCR_EL1", ['dbgdscrext']),
    ("MIDR_EL1", ['midr']),
    ("MVFR0_EL1", ['mvfr0']),
    ("MVFR1_EL1", ['mvfr1']),
    ("MVFR2_EL1", ['mvfr2_el1']),
    ("OSDLR_EL1", ['dbgosdlr']),
    ("OSDTRRX_EL1", ['dbgdtrrxext']),
    ("OSDTRTX_EL1", ['dbgdtrtxext']),
    ("OSECCR_EL1", ['dbgoseccr']),
    ("OSLSR_EL1", ['dbgoslsr']),
    ("PAR_EL1", ['par_ns']),
    ("PMCCFILTR_EL0", ['pmccfiltr_el0']),
    ("PMCCNTR_EL0", ['pmccntr']),
    ("PMCEID0_EL0", ['pmceid0']),
    ("PMCEID1_EL0", ['pmceid1']),
    ("PMCNTENCLR_EL0", ['pmcntenclr']),
    ("PMCNTENSET_EL0", ['pmcntenset']),
    ("PMCR_EL0", ['pmcr']),
    ("PMEVCNTR0_EL0", ['pmevcntr0']),
    ("PMEVCNTR1_EL0", ['pmevcntr1']),
    ("PMEVCNTR2_EL0", ['pmevcntr2']),
    ("PMEVCNTR3_EL0", ['pmevcntr3']),
    ("PMEVCNTR4_EL0", ['pmevcntr4']),
    ("PMEVCNTR5_EL0", ['pmevcntr5']),
    ("PMEVTYPER0_EL0", ['pmevtyper0']),
    ("PMEVTYPER1_EL0", ['pmevtyper1']),
    ("PMEVTYPER2_EL0", ['pmevtyper2']),
    ("PMEVTYPER3_EL0", ['pmevtyper3']),
    ("PMEVTYPER4_EL0", ['pmevtyper4']),
    ("PMEVTYPER5_EL0", ['pmevtyper5']),
    ("PMINTENSET_EL1", ['pmintenset']),
    ("PMOVSCLR_EL0", ['pmovsclr_el0']),
    ("PMOVSSET_EL0", ['pmovsset']),
    ("PMSELR_EL0", ['pmselr']),
    ("PMUSERENR_EL0", ['pmuserenr']),
    ("REVIDR_EL1", ['revidr_el1']),
    ("RVBAR_EL1", ['rvbar_el1']),
    ("SCTLR", ['sctlr_ns']),
    ("SMCR_EL1", ['smcr_el1']),
    ("SMIDR_EL1", ['smidr_el1']),
    ("SMPRI_EL1", ['smpri_el1']),
    ("SPSR_ABT", ['spsr_abt']),
    ("SPSR_EL1", ['spsr_svc']),
    ("SPSR_FIQ", ['spsr_fiq']),
    ("SPSR_IRQ", ['spsr_irq']),
    ("SPSR_UND", ['spsr_und']),
    ("SVCR", ['svcr']),
    ("TCR_EL1", ['ttbcr_ns']),
    ("TPIDR2_EL0", ['tpidr2_el0']),
    ("TPIDRRO_EL0", ['tpidruro_ns']),
    ("TPIDR_EL0", ['tpidrurw_ns']),
    ("TPIDR_EL1", ['tpidrprw_ns']),
    ("TTBR0_EL1", ['ttbr0_ns']),
    ("TTBR1_EL1", ['ttbr1_ns']),
    ("VBAR", ['vbar_ns']),
    ("ZCR_EL1", ['zcr_el1']),
]

for qname, keys in MISC_COPY:
    if len(keys) == 1:
        entry(f"misc:{keys[0]}", QEMU, qname, "hex_to_int")
    else:
        entry(f"misc:{keys[0]}", QEMU, qname, "low32")
        entry(f"misc:{keys[1]}", QEMU, qname, "high32")

# ===========================================================================
# Per CPU: generic timer
# ===========================================================================
# gem5 keeps the timer registers in [system.realview.generic_timer.
# pe_implementation<N>] and its sub-sections, not in miscRegs. These
# sections are not in the references (gem5 creates the timers at the first
# access); make_template.py adds them to the template, and convert.py sets
# [system.realview.generic_timer] cpu_count to the number of CPUs so that
# gem5 reads them.
#
# Each timer is restored as CTL (control_serial), CVAL (_counterLimit) and
# offset. gem5 does not save the time of the next timer interrupt: after the
# restore it computes it from CVAL and the counter value, so nothing needs to
# be converted to ticks for the timers themselves.
entry("timer:cntfrq", QEMU, "CNTFRQ_EL0", "hex_to_int")
entry("timer:cntkctl", QEMU, "CNTKCTL", "hex_to_int")
# Event stream: Linux sets CNTKCTL.EVNTEN to get a periodic wake-up event
# (used by WFE loops). gem5 schedules it only when CNTKCTL is written by the
# guest, not when it is restored, so convert.py schedules the next event
# itself (virt_ev_when, a tick after the restore tick) and copies the
# CNTKCTL.EVNTDIR / EVNTI fields (transitionTo / transitionBit).
entry("timer:virt_ev_scheduled", QEMU, "CNTKCTL", "evstream_enabled")
entry("timer:virt_ev_when", QEMU, "CNTKCTL", "evstream_next_tick")
entry("timer:virtEvStream.transitionTo", QEMU, "CNTKCTL", "evstream_transition_to")
entry("timer:virtEvStream.transitionBit", QEMU, "CNTKCTL", "evstream_transition_bit")
# EL1 virtual timer (the one Linux uses for its tick on arm64)
entry("timer.virt_el1_timer:control_serial", QEMU, "CNTV_CTL_EL0", "hex_to_int")
entry("timer.virt_el1_timer:_counterLimit", QEMU, "CNTV_CVAL_EL0", "hex_to_int")
entry("timer.virt_el1_timer:_offset", QEMU, "CNTVOFF_EL2", "hex_to_int",
      default="0", note="CNTVOFF: virtual count = physical count - offset. "
      "Not in the dump (no EL2 in QEMU), so 0")
# EL1 physical timer
entry("timer.phys_el1_timer:control_serial", QEMU, "CNTP_CTL_EL0", "hex_to_int")
entry("timer.phys_el1_timer:_counterLimit", QEMU, "CNTP_CVAL_EL0", "hex_to_int")
# The EL2 and EL3 timers keep their reset value (no EL2/EL3 in QEMU).

# ===========================================================================
# Per CPU: GIC
# ===========================================================================
# The GIC distributor and redistributors are not in the QEMU dump yet: they
# keep gem5's reset values (all interrupts disabled) until the GICv3 work.
# peInLowPowerState is already a variable because it is the first value the
# GIC conversion will set (Linux clears GICR_WAKER.ProcessorSleep at boot).
# The GIC CPU interface registers (ICC_*) are system registers: they are in
# MISC_COPY above.
entry("redist:peInLowPowerState", None, None, None,
      note="TODO GICv3: Linux clears GICR_WAKER.ProcessorSleep")

# ===========================================================================
# Machine (values that exist once)
# ===========================================================================
entry("system.physmem.store1:range_size", MACHINE, "ram_bytes", "to_str",
      note="RAM size in bytes, from the memory node of the machine YAML "
      "(the same YAML as for the restore). store0 is the 128 MB boot flash")
entry("system.realview.generic_timer:cpu_count", MACHINE, "num_cpus",
      "to_str", note="number of [pe_implementation<N>] timer sections gem5 "
      "reads (0 in the references)")
# System counter: the clock behind CNTPCT/CNTVCT. gem5 computes the counter
# at tick t as _value + (t - _updateTick) / period. Starting from the QEMU
# counter value makes the guest see time continue where it stopped.
entry("system.realview.sys_counter:_value", QEMU, "CNTPCT_EL0", "counter_value",
      note="CNTPCT of CPU 0 (all CPUs share the counter)")
entry("system.realview.sys_counter:_updateTick", MACHINE, "cur_tick",
      "counter_update_tick",
      note="the tick at which _value is valid: the restore tick")
