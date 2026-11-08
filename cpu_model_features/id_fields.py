"""AArch64 ID register fields: the one table shared by the CPU feature tools.

What this is
------------
The AArch64 ID registers (ID_AA64ISAR0_EL1, ID_AA64PFR0_EL1, ...) tell the
software which features the CPU implements. Each 64-bit register is split
into small fields, most of them 4 bits wide; for example bits [23:20] of
ID_AA64ISAR0_EL1 are the field ATOMIC, whose value 2 means "LSE atomics are
implemented". A gem5 checkpoint (m5.cpt) and a QEMU dump
(register-info.json) only contain whole register values; this table says
where each field is, so the tools can read and compare fields one by one.

This module is a table plus small helpers. It is not run directly; it is
imported by:
  - gen_id_cfg.py: writes the .cfg file for QEMU's id-overrides property,
    with the gem5 value of every feature field;
  - compare_id_regs.py: compares the fields of a QEMU CPU with gem5.
QEMU's id-overrides.c (parallel-qemu) has the same list of feature fields
in C: the two must stay identical.

The table
---------
FIELDS maps each register to its fields. Each field is a tuple
(NAME, low bit, width, kind, description):
  NAME         Arm ARM field name in upper case, as used by QEMU's FIELD()
               macros in target/arm/cpu.h (e.g. ID_AA64ISAR0.ATOMIC). The
               same spelling is used in the .cfg files, so the C side can map
               a name straight to R_<REG>_<NAME>_SHIFT / _LENGTH.
  low bit      position of the field's lowest bit in the register.
  width        number of bits (4 for most fields, 1 for some SME fields).
  kind         how the field is compared (see below).
  description  short meaning, copied into the .cfg file as a comment.

kind:
  level   unsigned feature level: higher means more features (e.g. ATOMIC:
          0 = none, 2 = LSE, 3 = LSE128). Goes in the .cfg file; QEMU
          applies min(QEMU value, gem5 value).
  signed  signed feature level: 0xF = -1 = not implemented, 0 = base,
          1 = more (FP and ADVSIMD: 1 = with FP16). Goes in the .cfg file;
          the min is taken on the signed value, so -1 < 0 < 1.
  info    not a feature level: a size, a count, a format or something set
          by the machine rather than the CPU (PARANGE, ASIDBITS, TGRAN*,
          BRPS, GIC, ...). min() makes no sense for these, so they are never
          in the .cfg file; sizes are matched on the gem5 side
          (phys_addr_range_64, have_large_asid_64). GIC is info because both
          QEMU and gem5 compute it on read from the machine's GICv3, and
          gem5 stores 0 in its checkpoints. CSV2, CSV2_FRAC and CSV3 are
          info because they are claims ("not vulnerable to Spectre v2 /
          Meltdown"), not features: no instruction depends on them and gem5
          does not behave differently. Lowering them only changes what the
          guest kernel does at boot: with CSV3 = 0 (and no E0PD) it enables
          KPTI, whose non-global kernel mappings break QFlex's WormCache MMU
          model; with CSV2 = 0 it enables Spectre v2 mitigations. QEMU keeps
          the model's values, like a real server CPU (e.g. Neoverse N1).

Where the table comes from
--------------------------
It is exactly the ID_AA64* FIELD() list of QEMU 8.1.5 target/arm/cpu.h,
for the 10 registers QEMU stores in cpu->isar (same names, bits and
widths). Registers QEMU 8.1 does not store (ID_AA64ISAR2, ID_AA64MMFR3)
always read as 0 in QEMU, so there is nothing to lower; fields QEMU does not
define are 0 in every QEMU model. When QEMU is updated, compare this table
with `grep "^FIELD(ID_AA64" target/arm/cpu.h` again.

Only AArch64 registers. The AArch32 ID registers (ID_ISAR0..6, ID_MMFR*,
ID_PFR*) only matter for 32-bit user programs; TODO if we ever run any.
"""

# The three kinds of field (see the module docstring).
LEVEL, SIGNED, INFO = "level", "signed", "info"

# register name -> [(NAME, low bit, width, kind, description), ...]
# Fields are listed from bit 0 upwards; bits not listed are reserved (RES0).
FIELDS = {
    # Instruction set attributes 0: crypto, CRC32, atomics, dot product, TLB maintenance, random numbers.
    "ID_AA64ISAR0_EL1": [
        ("AES", 4, 4, LEVEL, "AES (1) and PMULL (2)"),
        ("SHA1", 8, 4, LEVEL, "SHA1"),
        ("SHA2", 12, 4, LEVEL, "SHA256 (1) and SHA512 (2)"),
        ("CRC32", 16, 4, LEVEL, "CRC32"),
        ("ATOMIC", 20, 4, LEVEL, "LSE atomics (2), LSE128 (3)"),
        ("RDM", 28, 4, LEVEL, "SQRDMLAH/SQRDMLSH"),
        ("SHA3", 32, 4, LEVEL, "SHA3"),
        ("SM3", 36, 4, LEVEL, "SM3"),
        ("SM4", 40, 4, LEVEL, "SM4"),
        ("DP", 44, 4, LEVEL, "dot product"),
        ("FHM", 48, 4, LEVEL, "FMLAL/FMLSL"),
        ("TS", 52, 4, LEVEL, "FlagM (1), FlagM2 (2)"),
        ("TLB", 56, 4, LEVEL, "TLBIOS (1), TLBIRANGE (2)"),
        ("RNDR", 60, 4, LEVEL, "RNDR/RNDRRS random numbers"),
    ],
    # Instruction set attributes 1: cache persistence, pointer authentication, LRCPC, BF16, barriers.
    "ID_AA64ISAR1_EL1": [
        ("DPB", 0, 4, LEVEL, "DC CVAP (1), DC CVADP (2)"),
        ("APA", 4, 4, LEVEL, "pointer auth, QARMA5"),
        ("API", 8, 4, LEVEL, "pointer auth, IMPDEF algorithm"),
        ("JSCVT", 12, 4, LEVEL, "FJCVTZS"),
        ("FCMA", 16, 4, LEVEL, "FCMLA/FCADD"),
        ("LRCPC", 20, 4, LEVEL, "LDAPR (1), LDAPUR (2), RCPC3 (3)"),
        ("GPA", 24, 4, LEVEL, "generic pointer auth, QARMA5"),
        ("GPI", 28, 4, LEVEL, "generic pointer auth, IMPDEF"),
        ("FRINTTS", 32, 4, LEVEL, "FRINT32/64"),
        ("SB", 36, 4, LEVEL, "SB speculation barrier"),
        ("SPECRES", 40, 4, LEVEL, "CFP/DVP/CPP RCTX"),
        ("BF16", 44, 4, LEVEL, "BFloat16"),
        ("DGH", 48, 4, LEVEL, "DGH hint"),
        ("I8MM", 52, 4, LEVEL, "int8 matrix multiply"),
        ("XS", 56, 4, LEVEL, "XS attribute, TLBI nXS"),
        ("LS64", 60, 4, LEVEL, "64-byte loads/stores"),
    ],
    # Processor features 0: exception levels, FP/SIMD, GIC, RAS, SVE, speculation claims.
    "ID_AA64PFR0_EL1": [
        ("EL0", 0, 4, INFO, "EL0 AArch64/AArch32"),
        ("EL1", 4, 4, INFO, "EL1 AArch64/AArch32"),
        ("EL2", 8, 4, INFO, "EL2 (set with virtualization=on/off)"),
        ("EL3", 12, 4, INFO, "EL3 (set with secure=on/off)"),
        ("FP", 16, 4, SIGNED, "floating point (0), FP16 (1)"),
        ("ADVSIMD", 20, 4, SIGNED, "Advanced SIMD (0), FP16 (1)"),
        ("GIC", 24, 4, INFO, "GIC system registers (set by the machine, not the CPU)"),
        ("RAS", 28, 4, LEVEL, "RAS extension"),
        ("SVE", 32, 4, LEVEL, "SVE"),
        ("SEL2", 36, 4, LEVEL, "secure EL2"),
        ("MPAM", 40, 4, LEVEL, "MPAM"),
        ("AMU", 44, 4, LEVEL, "activity monitors"),
        ("DIT", 48, 4, LEVEL, "data-independent timing"),
        ("RME", 52, 4, LEVEL, "realm management"),
        ("CSV2", 56, 4, INFO, "not vulnerable to Spectre v2 (claim, not a feature)"),
        ("CSV3", 60, 4, INFO, "not vulnerable to Meltdown (claim, not a feature)"),
    ],
    # Processor features 1: BTI, SSBS, MTE, SME, NMI.
    "ID_AA64PFR1_EL1": [
        ("BT", 0, 4, LEVEL, "BTI"),
        ("SSBS", 4, 4, LEVEL, "PSTATE.SSBS (1), MSR SSBS (2)"),
        ("MTE", 8, 4, LEVEL, "memory tagging"),
        ("RAS_FRAC", 12, 4, LEVEL, "RAS minor version"),
        ("MPAM_FRAC", 16, 4, LEVEL, "MPAM minor version"),
        ("SME", 24, 4, LEVEL, "SME"),
        ("RNDR_TRAP", 28, 4, LEVEL, "RNDR trap"),
        ("CSV2_FRAC", 32, 4, INFO, "CSV2 minor version (claim, not a feature)"),
        ("NMI", 36, 4, LEVEL, "non-maskable interrupts"),
    ],
    # Memory model 0: address sizes and translation granules (mostly info), plus FGT and ECV.
    "ID_AA64MMFR0_EL1": [
        ("PARANGE", 0, 4, INFO, "physical address size"),
        ("ASIDBITS", 4, 4, INFO, "ASID size"),
        ("BIGEND", 8, 4, INFO, "mixed endian"),
        ("SNSMEM", 12, 4, INFO, "secure/non-secure memory"),
        ("BIGENDEL0", 16, 4, INFO, "mixed endian at EL0"),
        ("TGRAN16", 20, 4, INFO, "16 KiB granule"),
        ("TGRAN64", 24, 4, INFO, "64 KiB granule (0 = yes)"),
        ("TGRAN4", 28, 4, INFO, "4 KiB granule (0 = yes)"),
        ("TGRAN16_2", 32, 4, INFO, "16 KiB granule, stage 2"),
        ("TGRAN64_2", 36, 4, INFO, "64 KiB granule, stage 2"),
        ("TGRAN4_2", 40, 4, INFO, "4 KiB granule, stage 2"),
        ("EXS", 44, 4, LEVEL, "non-context-synchronising exceptions"),
        ("FGT", 56, 4, LEVEL, "fine-grained traps"),
        ("ECV", 60, 4, LEVEL, "enhanced counter virtualization"),
    ],
    # Memory model 1: hardware access/dirty flags, PAN, VHE and other translation features.
    "ID_AA64MMFR1_EL1": [
        ("HAFDBS", 0, 4, LEVEL, "hardware access (1) and dirty (2) flags"),
        ("VMIDBITS", 4, 4, INFO, "VMID size"),
        ("VH", 8, 4, LEVEL, "VHE"),
        ("HPDS", 12, 4, LEVEL, "hierarchical permission disables (1), HPDS2 (2)"),
        ("LO", 16, 4, LEVEL, "limited ordering regions"),
        ("PAN", 20, 4, LEVEL, "PAN (1), PAN2 (2), PAN3 (3)"),
        ("SPECSEI", 24, 4, LEVEL, "SError on speculative reads"),
        ("XNX", 28, 4, LEVEL, "stage-2 execute-never variants"),
        ("TWED", 32, 4, LEVEL, "delayed WFE trapping"),
        ("ETS", 36, 4, LEVEL, "enhanced translation synchronization"),
        ("HCX", 40, 4, LEVEL, "HCRX_EL2"),
        ("AFP", 44, 4, LEVEL, "alternate FP behaviour"),
        ("NTLBPA", 48, 4, LEVEL, "intermediate caching of translations"),
        ("TIDCP1", 52, 4, LEVEL, "EL0 IMPDEF register traps"),
        ("CMOW", 56, 4, LEVEL, "cache maintenance needs write permission"),
    ],
    # Memory model 2: CnP, UAO, LSE2 (AT), TLB hints, virtual address size.
    "ID_AA64MMFR2_EL1": [
        ("CNP", 0, 4, LEVEL, "common not private translations"),
        ("UAO", 4, 4, LEVEL, "user access override"),
        ("LSM", 8, 4, LEVEL, "LSMAOE/nTLSMD"),
        ("IESB", 12, 4, LEVEL, "implicit error synchronization"),
        ("VARANGE", 16, 4, INFO, "virtual address size (52-bit VA)"),
        ("CCIDX", 20, 4, INFO, "CCSIDR format (changes a register layout)"),
        ("NV", 24, 4, LEVEL, "nested virtualization"),
        ("ST", 28, 4, LEVEL, "small translation tables"),
        ("AT", 32, 4, LEVEL, "unaligned atomics in 16-byte blocks"),
        ("IDS", 36, 4, LEVEL, "ID register traps as EC 0x18"),
        ("FWB", 40, 4, LEVEL, "stage-2 forced write-back"),
        ("TTL", 48, 4, LEVEL, "TLBI translation table level hint"),
        ("BBM", 52, 4, LEVEL, "break-before-make levels"),
        ("EVT", 56, 4, LEVEL, "enhanced virtualization traps"),
        ("E0PD", 60, 4, INFO, "E0PD (kept: without it, KASLR forces KPTI, which QFlex's WormCache does not support)"),
    ],
    # SVE features (only meaningful if ID_AA64PFR0.SVE != 0).
    "ID_AA64ZFR0_EL1": [
        ("SVEVER", 0, 4, LEVEL, "SVE (0), SVE2 (1)"),
        ("AES", 4, 4, LEVEL, "SVE AES"),
        ("BITPERM", 16, 4, LEVEL, "SVE bit permute"),
        ("BFLOAT16", 20, 4, LEVEL, "SVE BFloat16"),
        ("SHA3", 32, 4, LEVEL, "SVE SHA3"),
        ("SM4", 40, 4, LEVEL, "SVE SM4"),
        ("I8MM", 44, 4, LEVEL, "SVE int8 matrix multiply"),
        ("F32MM", 52, 4, LEVEL, "SVE FP32 matrix multiply"),
        ("F64MM", 56, 4, LEVEL, "SVE FP64 matrix multiply"),
    ],
    # SME features (only meaningful if ID_AA64PFR1.SME != 0); several 1-bit fields.
    "ID_AA64SMFR0_EL1": [
        ("F32F32", 32, 1, LEVEL, "SME FMOPA FP32"),
        ("B16F32", 34, 1, LEVEL, "SME BFMOPA"),
        ("F16F32", 35, 1, LEVEL, "SME FMOPA FP16 to FP32"),
        ("I8I32", 36, 4, LEVEL, "SME SMOPA int8"),
        ("F64F64", 48, 1, LEVEL, "SME FMOPA FP64"),
        ("I16I64", 52, 4, LEVEL, "SME SMOPA int16 to int64"),
        ("SMEVER", 56, 4, LEVEL, "SME version"),
        ("FA64", 63, 1, LEVEL, "full A64 in streaming mode"),
    ],
    # Debug features: debug/trace/PMU versions and breakpoint counts (mostly info).
    "ID_AA64DFR0_EL1": [
        ("DEBUGVER", 0, 4, INFO, "debug architecture version"),
        ("TRACEVER", 4, 4, LEVEL, "trace unit system registers"),
        ("PMUVER", 8, 4, INFO, "PMU version (0xF = IMPDEF; set with pmu=on/off)"),
        ("BRPS", 12, 4, INFO, "number of breakpoints - 1"),
        ("WRPS", 20, 4, INFO, "number of watchpoints - 1"),
        ("CTX_CMPS", 28, 4, INFO, "number of context-aware breakpoints - 1"),
        ("PMSVER", 32, 4, LEVEL, "statistical profiling (SPE)"),
        ("DOUBLELOCK", 36, 4, INFO, "OS double lock (0 = yes)"),
        ("TRACEFILT", 40, 4, LEVEL, "trace filter"),
        ("TRACEBUFFER", 44, 4, LEVEL, "trace buffer"),
        ("MTPMU", 48, 4, INFO, "multi-threaded PMU (signed)"),
        ("BRBE", 52, 4, LEVEL, "branch record buffer"),
        ("HPMN0", 60, 4, INFO, "PMU with zero event counters at EL2"),
    ],
}

# Register names QEMU uses in register-info.json, when they differ from the
# Arm name above: {Arm name: [names to try in order]}. Empty with QEMU 8.1.5
# and qflex-sysregs.c, which export every register under its Arm name.
QEMU_ALIASES = {}


# ---------------------------------------------------------------------------
# Field helpers

def get(value, lo, width):
    """Raw (unsigned) field value, or None if the register is unknown.

    Example: get(0x0000000000210000, 20, 4) == 2  (ISAR0.ATOMIC = 2, LSE)."""
    return None if value is None else (value >> lo) & ((1 << width) - 1)


def put(value, lo, width, field_value):
    """Register value with one field replaced by field_value (other bits kept).

    Same operation as QEMU's deposit64() / FIELD_DP64()."""
    mask = ((1 << width) - 1) << lo
    return (value & ~mask) | ((field_value << lo) & mask)


def as_level(raw, width, kind):
    """Field value as a comparable level.

    Unsigned fields are returned as they are. Signed fields are
    sign-extended: with 4 bits, 0xF -> -1, 0x1 -> 1, so that "not
    implemented" (-1) compares below "implemented" (0 or 1)."""
    if raw is None or kind != SIGNED:
        return raw
    return raw - (1 << width) if raw >> (width - 1) else raw


def feature_fields():
    """Yield (register, name, lo, width, kind, description) for every field
    that goes in a .cfg file, i.e. every field whose kind is not INFO, in
    table order. This is the list gen_id_cfg.py writes and id-overrides.c
    accepts."""
    for reg, fields in FIELDS.items():
        for name, lo, width, kind, desc in fields:
            if kind != INFO:
                yield reg, name, lo, width, kind, desc


# ---------------------------------------------------------------------------
# Reading ID registers from QEMU and gem5 files

def qemu_values(path, cpu=0):
    """QEMU ID registers {reg: int}, from a snapshot dump (register-info.json)
    or from a CPU model file {"ID_AA64ISAR0_EL1": "0x...", ...}.
    In a model file, a register that is not listed is 0 (as in QEMU's init
    functions). In a dump, a register QEMU did not export is left out, so
    callers can tell "0" from "unknown".

    register-info.json looks like
        {"cpus": [{"cpu_id": 0, "registers": {"ID_AA64ISAR0_EL1": "0x...",
                                              ...}}, ...]}
    and values are hexadecimal strings."""
    import json

    with open(path) as f:
        data = json.load(f)
    if "cpus" not in data:                      # CPU model file
        return {reg: int(data.get(reg, "0x0"), 16) for reg in FIELDS}
    regs = data["cpus"][cpu]["registers"]       # snapshot dump
    out = {}
    for reg in FIELDS:
        for name in QEMU_ALIASES.get(reg, [reg]):
            if name in regs:
                out[reg] = int(regs[name], 16)
                break
    return out


def gem5_values(path, cpu=0):
    """gem5 ID registers {reg: int} of one CPU, from an m5.cpt file.

    m5.cpt is an INI-like text file. The ID registers of CPU n are in the
    section "[system.cpu_cluster.cpus<n>.isa.miscRegs]", one per line, with
    lower-case names and decimal values:
        id_aa64isar0_el1=1306342959387578656
    These are the values gem5 stores; a field that gem5 computes on read
    (ID_AA64PFR0.GIC) is stored as 0.
    A register gem5 does not have reads as 0 (RES0 ID space)."""
    import re

    # Section header of this CPU's misc registers. gem5 numbers the CPUs
    # (cpus0, cpus1, ...) only when there are several; with a single CPU
    # the section is "...cpus.isa.miscRegs" without an index.
    index = f"(?:{cpu})?" if cpu == 0 else f"{cpu}"
    want = re.compile(rf"\.cpus{index}\.isa\.miscRegs\]$")

    out, section = {}, None
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("["):            # new section
                section = line
                continue
            # "name=value" lines of the wanted section only
            if section and want.search(section) and "=" in line:
                key, val = line.split("=", 1)
                if key.upper() in FIELDS:
                    out[key.upper()] = int(val)
    if not out:
        raise ValueError(f"no ID registers for cpus{cpu} in {path}")
    return {reg: out.get(reg, 0) for reg in FIELDS}