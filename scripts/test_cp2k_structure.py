#!/usr/bin/env python3
"""Regression tests for cp2k_structure.py.

    python scripts/test_cp2k_structure.py

The case that motivated these: neighbours() used to fold dx, dy, dz
independently by |a|, |b|, |c|, which is only correct for an ORTHOGONAL cell.
On the triclinic tobermorite 9 A cell that invented sub-Angstrom "close
contacts" between atoms genuinely 3.7 A apart, and cp2k_doctor reported them as
errors on structures that were fine. CP2K itself uses the real lattice, so the
runs were unaffected - only the pre-flight check was wrong.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cp2k_structure import neighbours, lattice_matrix, _invert3   # noqa: E402

# The real tobermorite 9 A cell: triclinic, alpha 100.98, beta 88.13 deg.
# Note c = (0.63, -3.67, 18.90) - the large -y component is what breaks a
# per-axis fold.
TOBERMORITE = [[22.8720131125, 0.0, 0.0],
               [0.0032370864, 14.6832210561, 0.0],
               [0.6298513193, -3.6704158445, 18.9018070828]]

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}   {detail}")
        failures.append(name)


def dist_min_image(p, q, lat):
    """Brute-force reference: nearest of the 125 surrounding images."""
    d = [q[k] - p[k] for k in range(3)]
    best = float("inf")
    for sa in range(-2, 3):
        for sb in range(-2, 3):
            for sc in range(-2, 3):
                v = [d[k] + sa * lat[0][k] + sb * lat[1][k] + sc * lat[2][k]
                     for k in range(3)]
                best = min(best, math.sqrt(sum(x * x for x in v)))
    return best


def test_triclinic_false_contact():
    """Two Ca that a per-axis fold puts 0.617 A apart, truly 3.672 A."""
    a = ("Ca", 11.4304779974, 7.3402228149, 0.0003691471)
    b = ("Ca", 11.9270543942, 7.3390510489, 18.9000698930)
    ref = dist_min_image(a[1:], b[1:], TOBERMORITE)
    check("triclinic: reference min-image is ~3.67 A", abs(ref - 3.672) < 0.01,
          f"got {ref:.3f}")
    # the old per-axis fold, reproduced, to show what it used to claim
    lens = [math.sqrt(sum(x * x for x in v)) for v in TOBERMORITE]
    d = [b[k + 1] - a[k + 1] for k in range(3)]
    for k in range(3):
        d[k] -= lens[k] * round(d[k] / lens[k])
    naive = math.sqrt(sum(x * x for x in d))
    check("triclinic: per-axis fold would claim <0.75 A", naive < 0.75,
          f"got {naive:.3f}")
    pairs = neighbours([a, b], TOBERMORITE, rmax=1.8)
    check("triclinic: no spurious contact reported", pairs == [],
          f"got {pairs}")


def test_real_overlap_still_caught():
    """A genuine overlap must still be flagged."""
    a = ("Ca", 5.0, 5.0, 5.0)
    b = ("Ca", 5.3, 5.0, 5.0)
    pairs = neighbours([a, b], TOBERMORITE, rmax=1.8)
    check("genuine 0.3 A overlap is still detected",
          len(pairs) == 1 and abs(pairs[0][2] - 0.3) < 1e-6, f"got {pairs}")


def test_bond_across_boundary_triclinic():
    """An O-H bond split across a face must still be found in a triclinic cell."""
    o = ("O", 0.20, 5.0, 5.0)
    h = [o[1] - 0.99, o[2], o[3]]
    h = ("H", h[0] + TOBERMORITE[0][0], h[1], h[2])   # wrapped to the far face
    pairs = neighbours([o, h], TOBERMORITE, rmax=1.8)
    check("triclinic: bond across a boundary is still found",
          len(pairs) == 1 and abs(pairs[0][2] - 0.99) < 1e-6, f"got {pairs}")


def test_orthogonal_unchanged():
    """Orthogonal cells must behave exactly as before."""
    cell = [10.0, 10.0, 10.0]
    o = ("O", 0.2, 5.0, 5.0)
    h = ("H", 9.71, 5.0, 5.0)         # 0.49 A away across the x face
    pairs = neighbours([o, h], cell, rmax=1.8)
    check("orthogonal: wrap still works",
          len(pairs) == 1 and abs(pairs[0][2] - 0.49) < 1e-6, f"got {pairs}")
    far = neighbours([("O", 0.0, 0.0, 0.0), ("O", 5.0, 0.0, 0.0)], cell)
    check("orthogonal: distant pair not reported", far == [], f"got {far}")


def test_no_cell():
    """Without a cell, plain Cartesian distances."""
    pairs = neighbours([("O", 0.0, 0.0, 0.0), ("H", 0.99, 0.0, 0.0)], None)
    check("no cell: plain distance", len(pairs) == 1 and abs(pairs[0][2] - 0.99) < 1e-9)


def test_lattice_matrix_forms():
    m = lattice_matrix([10.0, 10.0, 10.0])
    check("lattice from 3 lengths is orthogonal",
          m and abs(m[0][0] - 10) < 1e-9 and abs(m[1][0]) < 1e-9)
    m6 = lattice_matrix([22.312, 14.606, 19.132, 101.08, 92.83, 89.98])
    ok = m6 and abs(m6[2][1] - (-3.6764)) < 1e-3 and abs(m6[2][0] - (-0.9446)) < 1e-3
    check("lattice from 6 params reproduces the known c vector", ok,
          f"got {m6[2] if m6 else None}")
    m9 = lattice_matrix([1, 0, 0, 0, 2, 0, 0, 0, 3])
    check("lattice from 9 numbers", m9 == [[1, 0, 0], [0, 2, 0], [0, 0, 3]])
    mm = lattice_matrix(TOBERMORITE)
    check("lattice from a 3x3 matrix passes through", mm == TOBERMORITE)
    check("no cell -> None", lattice_matrix(None) is None)
    inv = _invert3(lattice_matrix([2.0, 4.0, 5.0]))
    check("inverse of a diagonal lattice", abs(inv[0][0] - 0.5) < 1e-12)


def test_agrees_with_bruteforce():
    """On a random skewed set, neighbours() must match the 125-image reference."""
    import random
    random.seed(7)
    atoms = [("O", random.uniform(0, 22), random.uniform(0, 14), random.uniform(0, 18))
             for _ in range(40)]
    got = {(i, j): d for i, j, d in neighbours(atoms, TOBERMORITE, rmax=4.0)}
    bad = 0
    for i in range(len(atoms)):
        for j in range(i + 1, len(atoms)):
            ref = dist_min_image(atoms[i][1:], atoms[j][1:], TOBERMORITE)
            if ref < 4.0:
                if (i, j) not in got or abs(got[(i, j)] - ref) > 1e-9:
                    bad += 1
            elif (i, j) in got:
                bad += 1
    check("matches brute-force 125-image reference on 40 random atoms", bad == 0,
          f"{bad} mismatches")


if __name__ == "__main__":
    print("cp2k_structure regression tests")
    for fn in (test_triclinic_false_contact, test_real_overlap_still_caught,
               test_bond_across_boundary_triclinic, test_orthogonal_unchanged,
               test_no_cell, test_lattice_matrix_forms, test_agrees_with_bruteforce):
        fn()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("all tests passed")
