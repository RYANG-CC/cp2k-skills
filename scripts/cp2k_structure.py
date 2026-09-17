#!/usr/bin/env python3
"""
Structure and charge sanity checks for a CP2K system.

Answers the question that a converged-but-wrong run turns on: does the CHARGE
in the input match the structure that was actually handed to CP2K? Getting this
wrong does not crash anything - the SCF converges happily to a different
physical system.

Two independent checks, deliberately kept apart because they fail differently:

  1. Electron bookkeeping (exact). Sums the pseudopotential valence counts
     (GTH-PBE-qN) over the structure and subtracts CHARGE. Compared against the
     electron count CP2K reports, this is arithmetic, not chemistry, and it
     catches a wrong CHARGE outright.

  2. Formal charge (heuristic). Assigns each element an oxidation state and
     sums. Main-group states are safe; transition metals are genuinely
     ambiguous, so every assumed state is printed with its alternatives rather
     than folded silently into a total. O-O and O-H contacts are detected so
     peroxo and hydroxo groups are not mis-assigned as oxide.

  python cp2k_structure.py structure.xyz
  python cp2k_structure.py structure.xyz --input cp2k.inp
  python cp2k_structure.py --input cp2k.inp          # finds the coord file itself
"""

from __future__ import annotations

import argparse
import json
import math
import re
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Most common oxidation state, then other states seen often enough to matter.
# The point of the alternatives is to make the assumption arguable rather than
# invisible - a Ni oxyhydroxide is Ni(III), not the Ni(II) a lookup would pick.
OXIDATION = {
    "H": (1, []), "Li": (1, []), "Na": (1, []), "K": (1, []), "Rb": (1, []),
    "Cs": (1, []),
    "Be": (2, []), "Mg": (2, []), "Ca": (2, []), "Sr": (2, []), "Ba": (2, []),
    "B": (3, []), "Al": (3, []), "Ga": (3, [1]), "In": (3, [1]), "Tl": (1, [3]),
    "C": (4, [-4, 2]), "Si": (4, []), "Ge": (4, [2]), "Sn": (4, [2]),
    "Pb": (2, [4]),
    "N": (-3, [3, 5]), "P": (5, [3, -3]), "As": (5, [3]), "Sb": (3, [5]),
    "Bi": (3, [5]),
    "O": (-2, [-1]), "S": (-2, [4, 6]), "Se": (-2, [4, 6]), "Te": (-2, [4, 6]),
    "F": (-1, []), "Cl": (-1, [7]), "Br": (-1, []), "I": (-1, [7]),
    # Transition metals: primary state first, common alternatives after.
    "Sc": (3, []), "Ti": (4, [3]), "V": (5, [3, 4]), "Cr": (3, [6]),
    "Mn": (2, [3, 4, 7]), "Fe": (3, [2]), "Co": (2, [3]), "Ni": (2, [3]),
    "Cu": (2, [1]), "Zn": (2, []),
    "Y": (3, []), "Zr": (4, []), "Nb": (5, []), "Mo": (6, [4]), "Ru": (4, [3]),
    "Rh": (3, []), "Pd": (2, [4]), "Ag": (1, []), "Cd": (2, []),
    "La": (3, []), "Ce": (3, [4]), "Hf": (4, []), "Ta": (5, []), "W": (6, [4]),
    "Re": (7, [4]), "Os": (4, []), "Ir": (4, [3]), "Pt": (2, [4]),
    "Au": (3, [1]), "Hg": (2, [1]),
    "U": (6, [4]),
}

TRANSITION_METALS = {
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    "La", "Ce", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "U",
}

# Distance windows (angstrom) for recognising groups that break the naive
# "oxygen is always -2" rule.
OO_PEROXO_MAX = 1.60      # O-O single bond: peroxide/hydroperoxide/superoxide
OH_MAX = 1.30             # O-H covalent bond
CLOSE_CONTACT = 0.75      # below this, two atoms are on top of each other


def read_xyz(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    natoms = int(lines[0].split()[0])
    atoms = []
    for line in lines[2 : 2 + natoms]:
        p = line.split()
        atoms.append((p[0], float(p[1]), float(p[2]), float(p[3])))
    if len(atoms) != natoms:
        raise ValueError(f"{path}: header says {natoms} atoms, found {len(atoms)}")
    return atoms


def read_xyz_lattice(path):
    """Lattice vectors from an extended-XYZ comment line, or None.

    Preferred over ABC/ALPHA_BETA_GAMMA when present: it is the exact cell the
    coordinates were written in, with no reconstruction step.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.readline()
            comment = fh.readline()
    except OSError:
        return None
    m = re.search(r'Lattice\s*=\s*"([^"]+)"', comment)
    if not m:
        return None
    try:
        v = [float(x) for x in m.group(1).split()]
    except ValueError:
        return None
    return [v[0:3], v[3:6], v[6:9]] if len(v) == 9 else None


def element_of(label, kind_map=None):
    """Resolve a kind label to an element symbol.

    Labelled kinds (Ow, Fe1, Bulk) are common, so prefer the input's ELEMENT
    mapping and fall back to stripping trailing digits."""
    if kind_map and label in kind_map and kind_map[label]:
        return kind_map[label]
    stripped = label.rstrip("0123456789")
    if stripped in OXIDATION:
        return stripped
    if len(stripped) > 1 and stripped[:1] in OXIDATION:
        return stripped[:1]
    return stripped


def lattice_matrix(cell):
    """Build a 3x3 lattice (rows = a, b, c) from whatever the caller has.

    Accepts a 3x3 matrix, six cell parameters (a b c alpha beta gamma, angles in
    degrees), or three lengths (treated as orthogonal). Returns None if the cell
    cannot be interpreted.

    Three lengths alone are genuinely ambiguous - an input that gives ABC but no
    ALPHA_BETA_GAMMA really is orthogonal, so that reading is correct there.
    """
    if not cell:
        return None
    if len(cell) == 3 and all(hasattr(v, "__len__") for v in cell):
        m = [[float(x) for x in row[:3]] for row in cell]
        return m if len(m) == 3 else None
    flat = [float(x) for x in cell]
    if len(flat) == 9:
        return [flat[0:3], flat[3:6], flat[6:9]]
    if len(flat) == 3:
        a, b, c = flat
        return [[a, 0.0, 0.0], [0.0, b, 0.0], [0.0, 0.0, c]]
    if len(flat) == 6:
        a, b, c, al, be, ga = flat
        al, be, ga = (math.radians(x) for x in (al, be, ga))
        if abs(math.sin(ga)) < 1e-12:
            return None
        cx = c * math.cos(be)
        cy = c * (math.cos(al) - math.cos(be) * math.cos(ga)) / math.sin(ga)
        cz2 = c * c - cx * cx - cy * cy
        if cz2 <= 0:
            return None
        return [[a, 0.0, 0.0],
                [b * math.cos(ga), b * math.sin(ga), 0.0],
                [cx, cy, math.sqrt(cz2)]]
    return None


def _invert3(m):
    det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
           - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    if abs(det) < 1e-12:
        return None
    c = [[(m[1][1] * m[2][2] - m[1][2] * m[2][1]),
          -(m[0][1] * m[2][2] - m[0][2] * m[2][1]),
          (m[0][1] * m[1][2] - m[0][2] * m[1][1])],
         [-(m[1][0] * m[2][2] - m[1][2] * m[2][0]),
          (m[0][0] * m[2][2] - m[0][2] * m[2][0]),
          -(m[0][0] * m[1][2] - m[0][2] * m[1][0])],
         [(m[1][0] * m[2][1] - m[1][1] * m[2][0]),
          -(m[0][0] * m[2][1] - m[0][1] * m[2][0]),
          (m[0][0] * m[1][1] - m[0][1] * m[1][0])]]
    return [[c[r][k] / det for k in range(3)] for r in range(3)]


def neighbours(atoms, cell=None, rmax=1.8):
    """Pairs closer than rmax, with the minimum-image convention when a cell
    is known - an O-H bond across a periodic boundary is still a bond.

    The image search runs in fractional coordinates using the full lattice, then
    checks the 27 surrounding images. Folding dx, dy, dz independently by |a|,
    |b|, |c| - the obvious shortcut - is only valid for an ORTHOGONAL cell; on a
    triclinic cell it invents contacts that do not exist, and CP2K itself uses
    the real lattice. See test_neighbours_triclinic() for the case that bit.
    """
    lat = lattice_matrix(cell)
    inv = _invert3(lat) if lat else None
    images = None
    if lat and inv:
        images = [(sa * lat[0][k] + sb * lat[1][k] + sc * lat[2][k] for k in range(3))
                  for sa in (-1, 0, 1) for sb in (-1, 0, 1) for sc in (-1, 0, 1)]
        images = [tuple(t) for t in images]
    pairs = []
    n = len(atoms)
    r2max = rmax * rmax
    # refine over the 27 images only when the wrapped candidate is close enough
    # for a neighbouring image to possibly win
    refine2 = (3.0 * rmax) ** 2
    for i in range(n):
        _, xi, yi, zi = atoms[i]
        for j in range(i + 1, n):
            _, xj, yj, zj = atoms[j]
            dx, dy, dz = xj - xi, yj - yi, zj - zi
            if inv:
                fx = dx * inv[0][0] + dy * inv[1][0] + dz * inv[2][0]
                fy = dx * inv[0][1] + dy * inv[1][1] + dz * inv[2][1]
                fz = dx * inv[0][2] + dy * inv[1][2] + dz * inv[2][2]
                fx -= round(fx)
                fy -= round(fy)
                fz -= round(fz)
                dx = fx * lat[0][0] + fy * lat[1][0] + fz * lat[2][0]
                dy = fx * lat[0][1] + fy * lat[1][1] + fz * lat[2][1]
                dz = fx * lat[0][2] + fy * lat[1][2] + fz * lat[2][2]
                r2 = dx * dx + dy * dy + dz * dz
                if r2 < refine2:
                    for sx, sy, sz in images:
                        ex, ey, ez = dx + sx, dy + sy, dz + sz
                        e2 = ex * ex + ey * ey + ez * ez
                        if e2 < r2:
                            r2 = e2
            else:
                r2 = dx * dx + dy * dy + dz * dz
            if r2 < r2max:
                pairs.append((i, j, math.sqrt(r2)))
    return pairs


def analyse(atoms, kind_map=None, cell=None, potentials=None, charge=None,
            oxidation=None):
    """`oxidation` maps an element to a state that is KNOWN rather than
    assumed - a &BS block stating dQ, say. Those elements are then reported as
    derived, so the caller does not warn about an assumption it did not make."""
    oxidation = oxidation or {}
    elements = [element_of(a[0], kind_map) for a in atoms]
    composition = {}
    for e in elements:
        composition[e] = composition.get(e, 0) + 1

    pairs = neighbours(atoms, cell)
    close = [(i, j, d) for i, j, d in pairs if d < CLOSE_CONTACT]

    # --- oxygen environments ---------------------------------------------
    peroxo_o, hydroxo_o = set(), set()
    for i, j, d in pairs:
        ei, ej = elements[i], elements[j]
        if ei == "O" and ej == "O" and d <= OO_PEROXO_MAX:
            peroxo_o.add(i)
            peroxo_o.add(j)
        elif d <= OH_MAX and {ei, ej} == {"O", "H"}:
            hydroxo_o.add(i if ei == "O" else j)

    # --- formal charge ----------------------------------------------------
    contributions = {}
    assumptions = []
    total = 0
    for idx, e in enumerate(elements):
        if e not in OXIDATION:
            assumptions.append(f"{e}: unknown element, contributed 0")
            state = 0
        elif e in oxidation:
            state = oxidation[e]        # stated by the input, not guessed
        elif e == "O" and idx in peroxo_o:
            state = -1          # O-O bond: peroxide-like, not oxide
        else:
            state = OXIDATION[e][0]
        total += state
        key = (e, state)
        contributions[key] = contributions.get(key, 0) + 1

    n_peroxo = len(peroxo_o)
    result = {
        "n_atoms": len(atoms),
        "composition": dict(sorted(composition.items())),
        "formal_charge": total,
        "contributions": {f"{e}({s:+d})": n for (e, s), n in
                          sorted(contributions.items())},
        "peroxo_oxygens": n_peroxo,
        "hydroxo_oxygens": len(hydroxo_o),
        "close_contacts": [
            {"i": i + 1, "j": j + 1, "elements": f"{elements[i]}-{elements[j]}",
             "distance": round(d, 3)}
            for i, j, d in close
        ],
        "transition_metals": {},
        "input_charge": charge,
    }

    for e in sorted(set(elements) & TRANSITION_METALS):
        primary, alts = OXIDATION.get(e, (None, []))
        result["transition_metals"][e] = {
            "count": composition[e],
            "assumed": oxidation.get(e, primary),
            "alternatives": [] if e in oxidation else alts,
            "derived": e in oxidation,
        }

    # If the formal total misses the target by exactly (count x delta) for one
    # metal, that metal is almost certainly in a different oxidation state.
    # NiOOH is the textbook case: assuming Ni(II) leaves a gap of exactly one
    # charge unit per Ni, and Ni(III) closes it.
    target = charge if charge is not None else 0
    gap = target - total
    reassignments = []
    if gap:
        for e, d in result["transition_metals"].items():
            n = d["count"]
            if n and gap % n == 0:
                delta = gap // n
                candidate = d["assumed"] + delta
                if candidate in d["alternatives"] or (
                    1 <= candidate <= 8 and abs(delta) <= 2
                ):
                    reassignments.append(
                        {
                            "element": e,
                            "from": d["assumed"],
                            "to": candidate,
                            "known_alternative": candidate in d["alternatives"],
                        }
                    )
    result["suggested_reassignments"] = reassignments

    # --- electron bookkeeping (exact when pseudopotentials are known) -----
    if potentials:
        missing = [e for e in composition if e not in potentials]
        if missing:
            result["valence_electrons"] = None
            result["valence_missing"] = sorted(missing)
        else:
            neutral = sum(potentials[e] * n for e, n in composition.items())
            result["valence_electrons_neutral"] = neutral
            if charge is not None:
                result["electrons_with_charge"] = neutral - charge
            else:
                result["electrons_with_charge"] = neutral

    return result


def report(res, path):
    print(f"\nStructure check: {path}")
    print("-" * 78)
    comp = " ".join(f"{e}{n}" for e, n in res["composition"].items())
    print(f"  {res['n_atoms']} atoms   {comp}")

    if res["close_contacts"]:
        print(f"\n  [X] {len(res['close_contacts'])} close contact(s) under "
              f"{CLOSE_CONTACT} A - the SCF will struggle or fail:")
        for c in res["close_contacts"][:10]:
            print(f"      atoms {c['i']}-{c['j']} ({c['elements']}) "
                  f"{c['distance']} A")

    print(f"\n  Formal charge analysis (heuristic)")
    for label, n in res["contributions"].items():
        print(f"      {label:<10} x {n}")
    if res["peroxo_oxygens"]:
        print(f"      note: {res['peroxo_oxygens']} oxygen(s) in an O-O bond "
              f"treated as -1 (peroxo), not -2")
    if res["hydroxo_oxygens"]:
        print(f"      note: {res['hydroxo_oxygens']} oxygen(s) bonded to H "
              f"(hydroxo/aqua)")
    print(f"      implied cell charge: {res['formal_charge']:+d}")

    if res["transition_metals"]:
        print(f"\n  Transition metals - oxidation states ASSUMED, not derived:")
        for e, d in res["transition_metals"].items():
            alt = (f"   also common: "
                   + ", ".join(f"{a:+d}" for a in d["alternatives"])
                   ) if d["alternatives"] else ""
            print(f"      {e:<3} x{d['count']:<4} assumed {d['assumed']:+d}{alt}")
        print("      Override these if the chemistry says otherwise "
              "(e.g. Ni(III) in NiOOH).")

    if "valence_electrons_neutral" in res:
        print(f"\n  Electron bookkeeping (exact, from GTH-qN valence counts)")
        print(f"      neutral cell:        {res['valence_electrons_neutral']} e-")
        if res["input_charge"] is not None:
            print(f"      CHARGE {res['input_charge']:+d} in input:  "
                  f"{res['electrons_with_charge']} e-")
    elif res.get("valence_missing"):
        print(f"\n  Electron bookkeeping skipped: no POTENTIAL for "
              f"{', '.join(res['valence_missing'])}")

    charge = res["input_charge"]
    formal = res["formal_charge"]

    if res.get("suggested_reassignments"):
        target = charge if charge is not None else 0
        print(f"\n  The gap between the formal total ({formal:+d}) and "
              f"{target:+d} closes exactly if:")
        for r in res["suggested_reassignments"]:
            flag = "" if r["known_alternative"] else "  (unusual state - check)"
            print(f"      {r['element']} is {r['to']:+d} rather than "
                  f"{r['from']:+d}{flag}")
        print("      An exact fit is strong evidence, not proof - confirm against "
              "the chemistry.")

    print()
    if charge is None:
        if formal != 0:
            print(f"  [!] No CHARGE keyword, so CP2K uses 0, but the composition "
                  f"implies {formal:+d}.")
            print(f"      If that is right, add CHARGE {formal} to &DFT.")
        else:
            print("  [i] No CHARGE keyword (neutral), consistent with the "
                  "composition.")
    elif charge != formal:
        print(f"  [!] CHARGE {charge:+d} in the input, but the composition "
              f"implies {formal:+d}.")
        print("      One of them is wrong, or a transition metal is not in its "
              "assumed state.")
    else:
        print(f"  [i] CHARGE {charge:+d} matches the formal charge of the "
              f"composition.")
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Structure and charge checks.")
    ap.add_argument("structure", nargs="?", help="coordinate file (.xyz)")
    ap.add_argument("--input", help="cp2k.inp, for KIND/POTENTIAL/CHARGE/cell")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    kind_map, potentials, charge, cell = None, None, None, None
    bs_oxidation: dict = {}
    structure = args.structure

    if args.input:
        from cp2k_input import load, summarize

        root, variables = load(args.input)
        info = summarize(root, variables)
        fe = (info.get("force_evals") or [{}])[0]
        kinds = fe.get("kinds") or {}
        kind_map = {k: v.get("element") or k for k, v in kinds.items()}
        # A &BS block states the ionic charge it builds its guess for, which
        # beats the "most common state" table. Ambiguous elements are skipped.
        for kname, v in kinds.items():
            st = v.get("bs_state")
            if not st or not st.get("valid") or st.get("delta_q") is None:
                continue
            el = v.get("element") or kname
            if el in bs_oxidation and bs_oxidation[el] != st["delta_q"]:
                bs_oxidation[el] = None
            elif el not in bs_oxidation:
                bs_oxidation[el] = st["delta_q"]
        bs_oxidation = {e: q for e, q in bs_oxidation.items() if q is not None}
        potentials = {}
        for k, v in kinds.items():
            pot = v.get("potential") or ""
            el = v.get("element") or k
            if "-q" in pot:
                try:
                    potentials[el] = int(pot.rsplit("-q", 1)[1])
                except ValueError:
                    pass
        charge = fe.get("charge")
        charge = int(charge) if charge not in (None, "") else None
        abc = fe.get("abc")
        if abc:
            try:
                cell = [float(x) for x in abc.split()[:3]]
                ang = fe.get("angles")
                if ang:
                    cell += [float(x) for x in str(ang).split()[:3]]
            except ValueError:
                cell = None
        if not structure:
            coord = fe.get("coord_file")
            if coord:
                base = os.path.dirname(os.path.abspath(args.input))
                structure = coord if os.path.isabs(coord) else os.path.join(base, coord)

    if not structure:
        ap.error("give a structure file, or --input with a COORD_FILE_NAME")
    if not os.path.exists(structure):
        print(f"No such file: {structure}", file=sys.stderr)
        return 2

    atoms = read_xyz(structure)
    lat = read_xyz_lattice(structure)
    if lat:
        cell = lat          # exact cell from the file beats a reconstruction

    res = analyse(atoms, kind_map, cell, potentials, charge,
                  oxidation=bs_oxidation)

    if args.json:
        print(json.dumps(res, indent=2))
    else:
        report(res, structure)
    return 0


if __name__ == "__main__":
    sys.exit(main())
