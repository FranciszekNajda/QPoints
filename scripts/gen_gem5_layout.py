#!/usr/bin/env python3
"""Extract the gem5 ARM register layout from the gem5 source tree.

Nothing about gem5 is hard-coded in the converter: every fact it needs about
gem5's register file is read here from the gem5 sources it will run with.

Usage:
    python3 gen_gem5_layout.py <gem5 root> > gem5_layout.json

convert.py calls build_layout() directly; running this script by hand is only
needed to inspect the layout.

Reads:
    src/arch/arm/regs/int.hh   integer register file order, AArch64 X-register map
    src/arch/arm/regs/cc.hh    condition-code register order
    src/arch/arm/regs/vec.hh   vector / predicate register counts and indices
    src/arch/arm/regs/misc.cc  misc register aliases (.mapsTo)

Output (JSON):
    int_regs      physical integer slots, in checkpoint order (regs.integer)
    x_slot        AArch64 X0..X31 -> physical integer slot index
    sp_slot       {"SP_EL0": i, "SP_EL1": j, ...}
    cc_regs       condition-code slots, in checkpoint order (regs.condition_code)
    vec           vector/predicate constants (NumVecV8ArchRegs, PREDREG_FFR, ...)
    misc_alias    AArch64 misc reg -> [storage slot] or [low slot, high slot]
"""
import json
import os
import re
import sys


def read(root, rel):
    with open(os.path.join(root, rel)) as f:
        return f.read()


def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def parse_int_layout(src):
    src = strip_comments(src)

    # 1. Physical slots: enum entries of namespace int_reg up to NumRegs.
    enum_body = re.search(r"namespace int_reg\s*\{\s*enum\s*:\s*RegIndex\s*\{(.*?)\};",
                          src, re.S).group(1)
    slots = []
    for item in enum_body.split(","):
        name = item.strip()
        if not name:
            continue
        if name.startswith("NumRegs"):
            break
        assert "=" not in name, f"unexpected explicit value before NumRegs: {name}"
        slots.append(name)                      # e.g. "_R13SvcIdx"
    idx_of = {s: i for i, s in enumerate(slots)}

    # 2. RegId constants: "R13Svc = intRegClass[_R13SvcIdx]".
    regid = dict(re.findall(r"(\w+)\s*=\s*intRegClass\[(\w+)\]", src))
    # 3. Reference aliases: "&R8Usr = R8".
    refs = dict(re.findall(r"&(\w+)\s*=\s*(\w+)", src))

    def slot_of(name):
        seen = set()
        while name in refs and name not in seen:
            seen.add(name)
            name = refs[name]
        return idx_of[regid[name]]

    # 4. AArch64 map: "const RegMap Reg64Map = { ... };"
    body = re.search(r"Reg64Map\s*=\s*\{(.*?)\};", src, re.S).group(1)
    x_names = [n.strip() for n in body.split(",") if n.strip()]
    assert len(x_names) == 32, len(x_names)
    x_slot = [slot_of(n) for n in x_names]

    pretty = [re.sub(r"^_(.*)Idx$", r"\1", s).lower() for s in slots]
    sp_slot = {f"SP_EL{el}": idx_of[f"_Sp{el}Idx"] for el in range(4)}
    return pretty, x_slot, sp_slot


def parse_cc(src):
    body = re.search(r"RegName\[NumRegs\]\s*=\s*\{(.*?)\};", src, re.S).group(1)
    return re.findall(r'"(\w+)"', body)


def parse_vec(src):
    src = strip_comments(src)
    consts = {}
    for name, expr in re.findall(r"(?:constexpr unsigned|const int) (\w+)\s*=\s*([^;]+);",
                                 src):
        try:
            # Expressions only combine integers and earlier constants.
            consts[name] = eval(expr, {"__builtins__": {}}, dict(consts))
        except NameError:
            pass   # depends on another header (e.g. MaxSveVecLenInWords): not needed
    return consts


def parse_misc_alias(src):
    alias = {}
    for block in re.finditer(r"InitReg\((MISCREG_\w+)\)(.*?);", src, re.S):
        m = re.search(r"\.mapsTo\(([^)]*)\)", block.group(2))
        if m:
            name = block.group(1).replace("MISCREG_", "").lower()
            alias[name] = [t.strip().replace("MISCREG_", "").lower()
                           for t in m.group(1).split(",")]
    return alias


def build_layout(root):
    """Return the layout dict for the gem5 source tree at `root`.

    Used directly by convert.py, so the layout always matches the gem5 sources.
    """
    int_regs, x_slot, sp_slot = parse_int_layout(read(root, "src/arch/arm/regs/int.hh"))
    return {
        "int_regs": int_regs,
        "x_slot": x_slot,
        "sp_slot": sp_slot,
        "cc_regs": parse_cc(read(root, "src/arch/arm/regs/cc.hh")),
        "vec": parse_vec(read(root, "src/arch/arm/regs/vec.hh")),
        "misc_alias": parse_misc_alias(read(root, "src/arch/arm/regs/misc.cc")),
    }


def main(root):
    layout = build_layout(root)
    int_regs = layout["int_regs"]
    json.dump(layout, sys.stdout, indent=1)
    print(f"\nint slots: {len(int_regs)}, cc: {len(layout['cc_regs'])}, "
          f"vec: {layout['vec']['NumVecRegs']}, pred: {layout['vec']['NumVecPredRegs']}, "
          f"misc aliases: {len(layout['misc_alias'])}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
