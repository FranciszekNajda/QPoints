"""Conversion functions named in conversion_dict.py (the "transform" field).

Role in the conversion
----------------------
conversion_dict.py says, for each variable of the m5.cpt template, where its
value comes from (e.g. the QEMU register "SCTLR") and which function of this
file converts it (e.g. "hex_to_int"). convert.py reads the value, calls the
function and writes the result into the template. This file only knows
formats: how QEMU writes a value and how gem5 expects it.

Signature of every transform
----------------------------
    transform(src, ctx) -> str

  src  the value(s) read from the source, as the "path" of the entry says:
         - path is a string (one register): the value, a string. QEMU writes
           every register as a hexadecimal string, most significant digit
           first, e.g. "0x00000000000000c6"; machine values are ints.
         - path is a list: a dict {name: value} of every listed name found in
           the dump ("z{i}" stands for z0, z1, ...).
  ctx  a Context (below): the other registers of the same CPU, gem5's reset
       value of the variable, the gem5 register layout and the machine.
  returns the text written after "key=" in m5.cpt.

convert.py only calls a transform when the source has a value; otherwise the
entry's default is used (gem5's reset value unless the entry says
otherwise). A transform raises ConversionError when the value cannot be
converted correctly; convert.py then stops, because a wrong checkpoint would
make gem5 simulate a different guest without any error.

gem5 formats (from the gem5 sources and the reference checkpoints)
------------------------------------------------------------------
  - integers in decimal, booleans as "true"/"false";
  - register files (the regs.* keys of the xc.0 section) as one line of
    bytes in decimal separated by spaces: every register of the file,
    little-endian (least significant byte first), one after the other. The
    number and size of the registers are the GEM5_LAYOUT constants of
    conversion_dict.py (make_template.py checks them against gem5's own
    checkpoints).
"""
import os
import sys

# id_fields.py (QPoints/cpu_model_features): where each field of the AArch64
# ID registers is, and whether it is a feature or an info field. Used by
# id_merge. It is the same table as the one used to make the id-overrides
# .cfg file for QEMU, so the merge and the QEMU lowering agree.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "cpu_model_features"))
import id_fields                                     # noqa: E402


class ConversionError(Exception):
    """A value that cannot be converted correctly: convert.py stops."""


class Context:
    """What a transform may need besides its own source value.

    regs     {name: hex string}: all the QEMU registers of the CPU being
             converted (of CPU 0 for the variables that exist once)
    reset    gem5's reset value of the variable, as text (the default the
             template gives it); used by the transforms that keep or compare
             with gem5's value (id_merge, must_match_gem5)
    layout   conversion_dict.GEM5_LAYOUT: slot numbers and sizes of gem5's
             register files
    machine  {"num_cpus", "ram_bytes"} from the machine YAML, plus
             conversion_dict.MACHINE ("restore_tick", "counter_freq")
    var      name of the variable being converted (for error messages)
    """

    def __init__(self, regs, reset, layout, machine, var):
        self.regs = regs
        self.reset = reset
        self.layout = layout
        self.machine = machine
        self.var = var


# ===========================================================================
# Helpers
# ===========================================================================

def hexval(s):
    """QEMU hexadecimal string ("0x...") -> int."""
    return int(s, 16)


def bit(value, n):
    """Bit n of value (0 or 1)."""
    return (value >> n) & 1


def bits(value, hi, lo):
    """Bits hi..lo of value, both included, as written in the Arm ARM
    (e.g. bits(cpsr, 3, 2) is the field cpsr[3:2])."""
    return (value >> lo) & ((1 << (hi - lo + 1)) - 1)


def fmt_bytes(b):
    """bytes -> the gem5 text of a register-file line: "1 0 255 ..."."""
    return " ".join(str(x) for x in b)


def le_bytes_from_hex(s, nbytes, what):
    """A QEMU register as gem5 stores it: nbytes bytes, least significant
    first.

    QEMU writes "0x<most significant digit ... least significant digit>",
    so the hex digits are turned into bytes and the byte order is reversed.
    A shorter register is padded with zero bytes at the top (e.g. a 128-bit
    v register in a 256-byte gem5 register). A longer one is only accepted
    if the extra bytes are zero, otherwise data would be lost."""
    digits = s[2:] if len(s[2:]) % 2 == 0 else "0" + s[2:]
    raw = bytes.fromhex(digits)[::-1]
    if len(raw) > nbytes and any(raw[nbytes:]):
        raise ConversionError(f"{what}={s[:20]}... does not fit in {nbytes} "
                              "bytes")
    return raw[:nbytes].ljust(nbytes, b"\0")


def current_el_spsel(cpsr):
    """Exception level and stack pointer selection of the CPU, from PSTATE
    as QEMU dumps it (AArch64): cpsr[3:2] = EL, cpsr[0] = SPSel
    (0: the CPU uses SP_EL0, 1: it uses SP_ELx of its own EL)."""
    return bits(cpsr, 3, 2), bit(cpsr, 0)


# ===========================================================================
# Plain values
# ===========================================================================

def hex_to_int(src, ctx):
    """A 64-bit register copied as it is: QEMU hex -> gem5 decimal."""
    return str(hexval(src))


def to_str(src, ctx):
    """A machine value, already a number (e.g. the RAM size)."""
    return str(src)


def low32(src, ctx):
    """Low 32 bits of a 64-bit register.

    gem5 stores some AArch64 registers in two 32-bit AArch32 registers
    (".mapsTo" with two targets in src/arch/arm/regs/misc.cc): MAIR_EL1 is
    PRRR (low half) + NMRR (high half), FAR_EL1 is DFAR + IFAR, ...
    conversion_dict.py then has two entries, with low32 and high32."""
    return str(hexval(src) & 0xFFFFFFFF)


def high32(src, ctx):
    """High 32 bits of a 64-bit register, see low32."""
    return str(hexval(src) >> 32)


def tcr_el1(src, ctx):
    """TCR_EL1 with E0PD0 and E0PD1 (bits 55 and 56) cleared.

    E0PD (FEAT_E0PD) makes EL0 accesses to the kernel half of the address
    space fault at once. The QEMU CPU has it (ID_AA64MMFR2.E0PD is an "info"
    field, not lowered by id-overrides), so the guest kernel set E0PD1 at
    boot. gem5 does not implement FEAT_E0PD (no E0PD in misc.cc): for gem5
    these bits are reserved (RES0) and must be 0. Nothing is lost: EL0
    accesses to kernel addresses still fault, through the page permissions.
    """
    return str(hexval(src) & ~((1 << 55) | (1 << 56)))


# ===========================================================================
# ID registers
# ===========================================================================

def id_merge(src, ctx):
    """An AArch64 ID register merged field by field (see the "ID registers"
    section of conversion_dict.py).

    For every field listed in id_fields.FIELDS for this register:
      - feature field (level / signed): QEMU's value. The guest kernel saw it
        at boot and enabled features accordingly. It must be at most gem5's
        (compared as signed values for signed fields: 0xF = -1 = absent):
        otherwise gem5 would run a guest that uses a feature gem5 does not
        have; this happens if the snapshot was not taken with
        -cpu ...,id-overrides=<gem5 .cfg>.
      - info field: gem5's value (ctx.reset), which describes the simulated
        machine (PARange, granules, breakpoints, PMU, GIC, CSV2/CSV3, E0PD).
    Bits not listed in the table: QEMU's value. The table is QEMU 8.1's field
    list, so these are fields QEMU does not know; they are 0 in QEMU (the
    guest saw "not implemented") but may be set in gem5 for newer features
    (e.g. ID_AA64ISAR0.TME, ID_AA64ZFR0.B16B16, SME2 fields of
    ID_AA64SMFR0). Keeping gem5's bits would show the guest features it did
    not see at boot. A non-zero unlisted field in QEMU cannot be checked
    against gem5 and stops the conversion (update id_fields.py)."""
    reg = ctx.var.split(":", 1)[1].upper()          # misc:id_aa64pfr0_el1
    if reg not in id_fields.FIELDS:
        raise ConversionError(f"{reg} is not in id_fields.FIELDS")
    qemu = hexval(src)
    gem5 = int(ctx.reset)
    # bits of the fields listed in the table
    listed = 0
    for _, lo, width, _, _ in id_fields.FIELDS[reg]:
        listed |= ((1 << width) - 1) << lo
    if qemu & ~listed:
        raise ConversionError(
            f"{reg} = {src}: QEMU sets bits {qemu & ~listed:#x} that are not "
            f"in id_fields.FIELDS, they cannot be checked against gem5")
    out = qemu                       # unlisted bits: QEMU's (0, see above)
    for name, lo, width, kind, _ in id_fields.FIELDS[reg]:
        q = id_fields.get(qemu, lo, width)
        g = id_fields.get(gem5, lo, width)
        if kind == id_fields.INFO:
            out = id_fields.put(out, lo, width, g)   # the gem5 machine
            continue
        if id_fields.as_level(q, width, kind) > id_fields.as_level(g, width, kind):
            raise ConversionError(
                f"{reg}.{name} = {q} in QEMU but {g} in gem5: the guest may "
                f"use a feature gem5 does not have (snapshot taken without "
                f"id-overrides?)")
        # feature field: QEMU's value, already in out
    return str(out)


def must_match_gem5(src, ctx):
    """A register gem5 keeps as is, but whose value the guest relies on at
    run time (cache geometry: CTR_EL0, DCZID_EL0): it must be equal in QEMU
    and gem5, otherwise the guest's cache maintenance or DC ZVA would use
    wrong sizes."""
    if hexval(src) != int(ctx.reset):
        raise ConversionError(f"QEMU {src} but gem5 {int(ctx.reset):#x}; the "
                              f"guest uses this value at run time")
    return ctx.reset


# ===========================================================================
# PC and PSTATE
# ===========================================================================

def pc_plus_4(src, ctx):
    """_npc: gem5 saves the next PC too. AArch64 instructions are 4 bytes,
    so the next PC of a CPU stopped between two instructions is pc + 4."""
    return str(hexval(src) + 4)


def cpsr_without_flags(src, ctx):
    """cpsr of miscRegs: PSTATE (EL, SPSel, DAIF masks, PAN, ...) without
    the NZCV flags (bits 31..28). gem5 keeps the flags in separate
    registers (regs.condition_code, see cc_regs) and ignores them here.

    The conversion only handles AArch64: cpsr bit 4 (nRW) = 1 would mean
    the CPU runs AArch32 code, whose registers are laid out differently."""
    cpsr = hexval(src)
    if bit(cpsr, 4):
        raise ConversionError(f"cpsr={src}: the CPU is in AArch32 state")
    return str(cpsr & ~0xF0000000)


def cc_regs(src, ctx):
    """regs.condition_code: gem5's flag registers, in the order of
    GEM5_LAYOUT["cc_regs"] (nz, c, v, ge, fp, zero), each in an 8-byte slot.
    nz holds N in bit 1 and Z in bit 0, i.e. cpsr[31:30]. ge (AArch32 SIMD
    flags) and fp (AArch32 FP flags) stay 0 in AArch64."""
    cpsr = hexval(src)
    values = {"nz": bits(cpsr, 31, 30), "c": bit(cpsr, 29), "v": bit(cpsr, 28)}
    slot = ctx.layout["cc_reg_bytes"]
    return fmt_bytes(b"".join(values.get(n, 0).to_bytes(slot, "little")
                              for n in ctx.layout["cc_regs"]))


# ===========================================================================
# Register files (xc.0 regs.*)
# ===========================================================================

def int_regs(src, ctx):
    """regs.integer: the integer register file, 42 slots of 8 bytes.

    The slots are the AArch32 registers (src/arch/arm/regs/int.hh): r0..r15,
    the banked r13/r14 of each mode, the FIQ r8..r14, the zero register, 3
    gem5 temporaries, and sp0..sp3. gem5 maps the AArch64 registers onto them
    (Reg64Map): GEM5_LAYOUT["x_slot"] gives the slot of x0..x30 (e.g. x15 is
    r13hyp, slot 20) and GEM5_LAYOUT["sp_slot"] the slot of SP_EL0..SP_EL3.

    Stack pointer: QEMU dumps the SP the CPU is using as "sp", and SP_EL0 /
    SP_EL1 as stored in its banked copies. QEMU only updates the banked copy
    of the current SP on an exception entry or return, so that copy may be
    old: the SP of the current EL is taken from "sp" (current EL and SPSel
    from cpsr, see current_el_spsel). The other SP comes from its SP_ELx.

    The slots of other registers (zero register, gem5 temporaries, AArch32
    banked registers not used by x15..x30) stay 0."""
    layout = ctx.layout
    values = [0] * layout["int_regs"]
    for i in range(31):                       # x31 is SP or XZR, not a register
        name = f"x{i}"
        if name not in src:
            raise ConversionError(f"{name} missing from the dump")
        values[layout["x_slot"][i]] = hexval(src[name])
    for name in ("SP_EL0", "SP_EL1"):
        if name in src:
            values[layout["sp_slot"][name]] = hexval(src[name])
    el, spsel = current_el_spsel(hexval(src["cpsr"]))
    current = "SP_EL0" if (el == 0 or spsel == 0) else f"SP_EL{el}"
    values[layout["sp_slot"][current]] = hexval(src["sp"])
    slot = layout["int_reg_bytes"]
    return fmt_bytes(b"".join(v.to_bytes(slot, "little") for v in values))


def _vectors(src, ctx):
    """The gem5 vector registers, as a list of byte strings.

    The first GEM5_LAYOUT["vec_arch_regs"] (32) are the architectural
    registers:
      - SVE on: QEMU dumps z0..z31 at the maximum vector length (2048 bits
        with QEMU max); NEON v0..v31 are their low 128 bits, so they are not
        separate registers and the z values contain them;
      - SVE off: QEMU dumps v0..v31 (128 bits).
    Each value is padded with zeros up to the gem5 register size (256 bytes,
    sve_vl = 16). The other registers (gem5 internal: SVE temporaries,
    interleave registers, LUT) stay 0."""
    layout = ctx.layout
    size = layout["vec_reg_bytes"]
    prefix = "z" if "z0" in src else "v"
    vectors = [bytes(size)] * layout["vec_regs"]
    for i in range(layout["vec_arch_regs"]):
        name = f"{prefix}{i}"
        if name not in src:
            raise ConversionError(f"{name} missing from the dump")
        vectors[i] = le_bytes_from_hex(src[name], size, name)
    return vectors


def vec_regs(src, ctx):
    """regs.vector: 45 registers of 256 bytes (2048 bits) each, one after
    the other. See _vectors for what goes in them."""
    return fmt_bytes(b"".join(_vectors(src, ctx)))


def vec_elems(src, ctx):
    """regs.vector_element: a second copy of the vector registers that gem5
    keeps, seen as 32-bit elements; each element is stored in its own 8-byte
    slot (a RegVal). It must hold the same values as regs.vector, otherwise
    instructions that use the element view would read old data.

    Layout: for each of the 45 registers, its 64 elements (256 bytes / 4),
    each written as 8 little-endian bytes."""
    slot = ctx.layout["vec_elem_bytes"]
    out = bytearray()
    for v in _vectors(src, ctx):
        for e in range(len(v) // 4):
            word = int.from_bytes(v[4 * e:4 * e + 4], "little")
            out += word.to_bytes(slot, "little")
    return fmt_bytes(out)


def pred_regs(src, ctx):
    """regs.vector_predicate: 18 SVE predicate registers.

    gem5 stores a predicate register as one byte per bit: 2048 / 8 = 256
    bits, so 256 bytes of 0 or 1. QEMU dumps p0..p15 and ffr as hex numbers
    of 256 bits (bit 0 = element 0). Slots: p0..p15 in 0..15, FFR in
    GEM5_LAYOUT["pred_ffr_slot"] (16), slot 17 is a gem5 temporary (0).
    A missing register (SVE off) stays 0."""
    layout = ctx.layout
    nbits = layout["pred_reg_bits"]
    names = {i: f"p{i}" for i in range(16)}
    names[layout["pred_ffr_slot"]] = "ffr"
    preds = [bytes(nbits)] * layout["pred_regs"]
    for slot, name in names.items():
        if name in src:
            value = hexval(src[name])
            if value >> nbits:
                raise ConversionError(f"{name} does not fit in {nbits} bits")
            preds[slot] = bytes(bit(value, b) for b in range(nbits))
    return fmt_bytes(b"".join(preds))


# ===========================================================================
# Generic timer and system counter
# ===========================================================================
# Event stream. Linux enables a periodic "event" (the one WFE waits for)
# generated from the counter, so that WFE loops wake up regularly. It is set
# by CNTKCTL_EL1:
#   bit 2     EVNTEN   event stream on
#   bit 3     EVNTDIR  trigger on a 1->0 (1) or 0->1 (0) transition
#   bits 7:4  EVNTI    which counter bit is watched
# gem5 (GenericTimer::handleStream) keeps transitionTo = !EVNTDIR and
# transitionBit = EVNTI, and only schedules the stream when the guest writes
# CNTKCTL. On restore, CNTKCTL is loaded without that write, so the next
# event is saved in the checkpoint (virt_ev_scheduled and virt_ev_when).

def evstream_enabled(src, ctx):
    """virt_ev_scheduled: an event is pending if the event stream is on."""
    return "true" if bit(hexval(src), 2) else "false"


def evstream_next_tick(src, ctx):
    """virt_ev_when: the tick of the next event-stream event. It is the
    restore tick: the first event fires as soon as the simulation starts,
    and its callback schedules the next ones from the counter value, as
    gem5 always does."""
    return str(ctx.machine["restore_tick"])


def evstream_transition_to(src, ctx):
    """virtEvStream.transitionTo = !CNTKCTL.EVNTDIR."""
    return str(0 if bit(hexval(src), 3) else 1)


def evstream_transition_bit(src, ctx):
    """virtEvStream.transitionBit = CNTKCTL.EVNTI."""
    return str(bits(hexval(src), 7, 4))


def counter_period(machine):
    """Ticks per counter increment, as gem5 computes it (SystemCounter::
    _period). gem5 ticks are picoseconds: 10^12 / 62.5 MHz = 16000."""
    return int(round(10**12 / machine["counter_freq"]))


def counter_value(src, ctx):
    """sys_counter _value: the system counter (CNTPCT) at the restore tick.

    It is QEMU's CNTPCT, so the guest sees its clock (CNTPCT, and CNTVCT =
    CNTPCT - CNTVOFF with CNTVOFF = 0) go on from where it stopped. The
    pending timer interrupts (CVAL) are compared with this value."""
    return str(hexval(src))


def counter_update_tick(src, ctx):
    """sys_counter _updateTick: the tick at which _value holds.

    gem5 computes the counter at tick t as
        _value + (t - _updateTick) / period
    and always keeps _updateTick on a period boundary
    (SystemCounter::updateTick). With _updateTick = the restore tick rounded
    down to a period, the counter at the restore tick is exactly _value."""
    tick = src
    return str(tick - tick % counter_period(ctx.machine))


# ===========================================================================
# Every transform, by the name used in conversion_dict.py
# ===========================================================================
TRANSFORMS = {f.__name__: f for f in [
    hex_to_int, to_str, low32, high32, tcr_el1, id_merge, must_match_gem5,
    pc_plus_4, cpsr_without_flags, cc_regs, int_regs, vec_regs, vec_elems,
    pred_regs, evstream_enabled, evstream_next_tick, evstream_transition_to,
    evstream_transition_bit, counter_value, counter_update_tick,
]}