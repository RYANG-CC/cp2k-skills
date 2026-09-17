#!/usr/bin/env python3
"""
Pre-flight checks for a CP2K input and its run directory.

Most failed CP2K jobs die for boring reasons that are cheap to catch before
burning node hours: a &KIND missing for an element that appears in the .xyz,
a basis or potential file that was never copied across, a restart flag set
with no restart file next to it. This walks that checklist against the input
and the files sitting beside it.

  python cp2k_doctor.py                     # check the current directory
  python cp2k_doctor.py path/to/rundir
  python cp2k_doctor.py rundir --json

Exit code is 1 when any ERROR-level problem was found, so it composes with
scripts that submit jobs only on a clean bill of health.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cp2k_input import load, summarize  # noqa: E402
import cp2k_structure  # noqa: E402

ERROR, WARN, INFO = "ERROR", "WARNING", "INFO"

# Rough guidance for GTH/MOLOPT plane-wave cutoffs. These are not hard limits
# but a run far outside the range usually means a copy-paste slip.
CUTOFF_LOW = 280
CUTOFF_HIGH = 1400
REL_CUTOFF_LOW = 30
REL_CUTOFF_HIGH = 120


class Report:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, level, check, message, fix=None):
        # An @INCLUDE'd fragment shared by several force evals yields the same
        # finding once per eval; report it once.
        if any(i["check"] == check and i["message"] == message for i in self.items):
            return
        self.items.append(
            {"level": level, "check": check, "message": message, "fix": fix}
        )

    @property
    def n_errors(self):
        return sum(1 for i in self.items if i["level"] == ERROR)

    @property
    def n_warnings(self):
        return sum(1 for i in self.items if i["level"] == WARN)


def _muc_key(spec):
    """Normalise a MULTIPLE_UNIT_CELL spec so '1 1 1' and '1  1  1' compare equal."""
    if not spec:
        return None
    return tuple(spec.split()[:3])


def read_xyz_elements(path: str):
    """Return (elements, n_atoms, bounding box) for the first frame."""
    elements, coords = [], []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        try:
            declared = int(fh.readline().split()[0])
        except (ValueError, IndexError):
            return None, None, None
        fh.readline()  # comment line
        for _ in range(declared):
            parts = fh.readline().split()
            if len(parts) < 4:
                break
            elements.append(parts[0])
            try:
                coords.append(tuple(float(x) for x in parts[1:4]))
            except ValueError:
                break
    if not coords:
        return elements, declared, None
    extent = tuple(
        max(c[i] for c in coords) - min(c[i] for c in coords) for i in range(3)
    )
    return elements, declared, extent


def find_input(rundir: str):
    for name in ("cp2k.inp", "input.inp"):
        p = os.path.join(rundir, name)
        if os.path.exists(p):
            return p
    candidates = [f for f in sorted(os.listdir(rundir)) if f.endswith(".inp")]
    # An @INCLUDE fragment is not a top-level input; prefer files with &GLOBAL.
    for name in candidates:
        p = os.path.join(rundir, name)
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            if "&GLOBAL" in fh.read(20000).upper():
                return p
    return os.path.join(rundir, candidates[0]) if candidates else None


def check_run(rundir: str) -> tuple[Report, dict]:
    rep = Report()
    context: dict = {"dir": os.path.abspath(rundir)}

    inp_path = find_input(rundir)
    if not inp_path:
        rep.add(ERROR, "input", "No .inp file found in this directory.")
        return rep, context
    context["input"] = os.path.basename(inp_path)

    try:
        root, variables = load(inp_path)
    except Exception as exc:
        rep.add(ERROR, "input", f"Could not parse {inp_path}: {exc}")
        return rep, context

    info = summarize(root, variables)
    context["summary"] = info

    run_type = (info.get("run_type") or "").upper()
    if not run_type:
        rep.add(WARN, "run_type", "&GLOBAL has no RUN_TYPE; CP2K will default to ENERGY_FORCE.")
    context["run_type"] = run_type

    # Almost every check below hangs off &FORCE_EVAL. If it is missing, they
    # all silently find nothing to complain about and this report comes back
    # clean on a file that was never inspected - the worst possible failure for
    # a pre-flight tool. A real input always has one, so treat its absence as an
    # error in its own right rather than letting the silence speak.
    if not info.get("n_force_eval"):
        rep.add(
            ERROR,
            "input",
            "No &FORCE_EVAL section was found, so none of the atom, kind, "
            "charge or basis checks below could run.",
            "Check the input actually contains &FORCE_EVAL, and that every "
            "&END line is balanced - an unclosed section swallows the rest of "
            "the file.",
        )

    # ---- files referenced by the input must be present --------------------
    referenced: list[tuple[str, str]] = []
    for fe in info.get("force_evals", []):
        for label, key in (
            ("basis set", "basis_file"),
            ("pseudopotential", "potential_file"),
            ("coordinates", "coord_file"),
            ("dispersion parameters", "dispersion_file"),
        ):
            value = fe.get(key)
            if value:
                referenced.append((label, value))
    seen = set()
    for label, fname in referenced:
        if fname in seen:
            continue
        seen.add(fname)
        target = fname if os.path.isabs(fname) else os.path.join(rundir, fname)
        if not os.path.exists(target):
            rep.add(
                ERROR,
                "missing file",
                f"{label} file '{fname}' is referenced but not present in the run directory.",
                "Copy it in next to cp2k.inp, or point at an absolute path the compute node can see.",
            )

    # ---- element coverage: every element in the .xyz needs a &KIND --------
    for fe in info.get("force_evals", []):
        coord = fe.get("coord_file")
        kinds = fe.get("kinds") or {}
        if not coord:
            continue
        target = coord if os.path.isabs(coord) else os.path.join(rundir, coord)
        if not os.path.exists(target):
            continue
        elements, n_atoms, extent = read_xyz_elements(target)
        if elements is None:
            rep.add(WARN, "coordinates", f"Could not read '{coord}' as XYZ.")
            continue
        context["n_atoms"] = n_atoms
        context["elements"] = sorted(set(elements))

        # MULTIPLE_UNIT_CELL replicates both the cell and the coordinates, so
        # the atom count CP2K reports is a multiple of what the .xyz holds.
        muc = fe.get("multiple_unit_cell")
        if muc:
            try:
                reps = [int(x) for x in muc.split()[:3]]
                factor = reps[0] * reps[1] * reps[2]
            except (ValueError, IndexError):
                factor = 1
            if factor > 1:
                context["multiple_unit_cell"] = muc
                context["n_atoms_expanded"] = n_atoms * factor
                topo_muc = fe.get("topology_multiple_unit_cell")
                if _muc_key(topo_muc) != _muc_key(muc):
                    rep.add(
                        ERROR,
                        "multiple unit cell",
                        f"&CELL has MULTIPLE_UNIT_CELL '{muc}' but &TOPOLOGY has "
                        f"'{topo_muc or 'nothing'}'. The cell and the coordinates would "
                        "be replicated differently.",
                        "Set the same MULTIPLE_UNIT_CELL in both &CELL and &TOPOLOGY.",
                    )
        missing = sorted({e for e in elements if e not in kinds})
        if missing:
            rep.add(
                ERROR,
                "missing KIND",
                f"'{coord}' contains {', '.join(missing)} with no matching &KIND section.",
                "Add a &KIND for each, with BASIS_SET and POTENTIAL matching the functional "
                "(e.g. DZVP-MOLOPT-SR-GTH / GTH-PBE-qN).",
            )
        unused = sorted({k for k in kinds if k not in set(elements)})
        if unused:
            rep.add(
                INFO,
                "unused KIND",
                f"&KIND defined but absent from the coordinates: {', '.join(unused)}. "
                "Harmless, unless one of them was meant to be a labelled site.",
            )
        for name, k in sorted(kinds.items()):
            if name in set(elements):
                if not k.get("basis_set"):
                    rep.add(ERROR, "KIND", f"&KIND {name} has no BASIS_SET.")
                if not k.get("potential"):
                    rep.add(ERROR, "KIND", f"&KIND {name} has no POTENTIAL.")

        # ---- composition, charge, and close contacts ---------------------
        kind_map = {k: (v.get("element") or k) for k, v in kinds.items()}
        potentials = {}
        for k, v in kinds.items():
            pot = v.get("potential") or ""
            el = v.get("element") or k
            if "-q" in pot:
                try:
                    potentials[el] = int(pot.rsplit("-q", 1)[1])
                except ValueError:
                    pass
        charge_kw = fe.get("charge")
        try:
            charge_kw = int(charge_kw) if charge_kw not in (None, "") else None
        except ValueError:
            charge_kw = None
        cell_abc = None
        if fe.get("abc"):
            try:
                cell_abc = [float(x) for x in fe["abc"].split()[:3]]
                ang = fe.get("angles")
                if ang:
                    cell_abc += [float(x) for x in str(ang).split()[:3]]
            except ValueError:
                cell_abc = None
        # An extended-XYZ Lattice line is the exact cell the coordinates were
        # written in - prefer it over reconstructing one from ABC/angles.
        lat_xyz = cp2k_structure.read_xyz_lattice(target)
        if lat_xyz:
            cell_abc = lat_xyz

        # A &BS block states the ionic charge it is building the guess for,
        # so prefer it over the "most common state" table. Skip an element
        # whose kinds disagree - that is ambiguous, not known.
        bs_ox: dict = {}
        for kname, k in (fe.get("kinds") or {}).items():
            st = k.get("bs_state")
            if not st or not st.get("valid") or st.get("delta_q") is None:
                continue
            el = k.get("element") or kname
            if el in bs_ox and bs_ox[el] != st["delta_q"]:
                bs_ox[el] = None
            elif el not in bs_ox:
                bs_ox[el] = st["delta_q"]
        bs_ox = {e: q for e, q in bs_ox.items() if q is not None}

        try:
            struct = cp2k_structure.analyse(
                cp2k_structure.read_xyz(target), kind_map, cell_abc,
                potentials, charge_kw, oxidation=bs_ox,
            )
        except Exception as exc:
            rep.add(WARN, "structure", f"Structure analysis failed: {exc}")
            struct = None

        if struct:
            context["formal_charge"] = struct["formal_charge"]
            context["input_charge"] = charge_kw
            if struct["close_contacts"]:
                worst = struct["close_contacts"][0]
                rep.add(
                    ERROR,
                    "close contacts",
                    f"{len(struct['close_contacts'])} atom pair(s) closer than "
                    f"{cp2k_structure.CLOSE_CONTACT} A, worst "
                    f"{worst['elements']} at {worst['distance']} A "
                    f"(atoms {worst['i']}-{worst['j']}).",
                    "The SCF will not converge against overlapping atoms. Fix the "
                    "structure before touching SCF settings.",
                )

            formal = struct["formal_charge"]
            tms = struct["transition_metals"]
            tm_note = ""
            if tms:
                tm_note = " Oxidation states: " + ", ".join(
                    f"{e} {d['assumed']:+d}"
                    + (" (from &BS)" if d.get("derived") else " (assumed)")
                    for e, d in sorted(tms.items())
                ) + "."
            if charge_kw is None and formal != 0:
                rep.add(
                    WARN,
                    "charge",
                    f"No CHARGE keyword, so CP2K defaults to 0, but the composition "
                    f"implies {formal:+d}.{tm_note}",
                    f"Add CHARGE {formal} to &DFT if that is the intended system. "
                    "If an output is present, check DFT| Charge in it - the run "
                    "may have used a different value than this input states.",
                )
            elif charge_kw is not None and charge_kw != formal:
                rep.add(
                    WARN,
                    "charge",
                    f"CHARGE {charge_kw:+d} in the input but the composition implies "
                    f"{formal:+d}.{tm_note}",
                    "Either the charge is wrong, or a metal is not in its assumed "
                    "state. Run cp2k_structure.py for the full breakdown.",
                )
            elif tms:
                caveat = ("" if all(d.get("derived") for d in tms.values())
                          else " Assumed states are not derived - confirm them.")
                rep.add(
                    INFO,
                    "charge",
                    f"CHARGE {formal:+d} is consistent with the composition."
                    f"{tm_note}{caveat}",
                )

            # Parity check: alpha = (N + M - 1)/2 must be a whole number, so an
            # odd electron count cannot form a singlet. Whatever CP2K then does,
            # the input does not describe the system the author had in mind.
            mult = fe.get("multiplicity")
            neutral = struct.get("valence_electrons_neutral")
            if mult and neutral is not None:
                try:
                    mult_i = int(mult)
                except ValueError:
                    mult_i = None
                if mult_i:
                    n_el = neutral - (charge_kw or 0)
                    unpaired = mult_i - 1
                    if (n_el - unpaired) % 2 != 0:
                        rep.add(
                            ERROR,
                            "charge/multiplicity",
                            f"MULTIPLICITY {mult_i} needs {unpaired} unpaired "
                            f"electrons, but the cell has {n_el} electrons "
                            f"(CHARGE {charge_kw if charge_kw is not None else 0:+d}) "
                            "- the parities are incompatible.",
                            "The alpha count (N + M - 1)/2 must be a whole number, "
                            "so one of CHARGE or MULTIPLICITY is not what you "
                            "meant. Fix them to agree before running.",
                        )

            if struct.get("valence_electrons_neutral"):
                context["electrons"] = struct.get("electrons_with_charge")

        # ---- cell vs structure extent -----------------------------------
        abc = fe.get("abc")
        if abc and extent:
            try:
                cell = [float(x) for x in abc.split()[:3]]
            except ValueError:
                cell = None
            if cell and len(cell) == 3:
                context["cell"] = cell
                context["extent"] = [round(e, 2) for e in extent]
                # Comparing a Cartesian extent against a cell length is only
                # meaningful for an orthogonal cell. In a monoclinic or
                # triclinic cell the x-extent legitimately exceeds |a|, so
                # this check has to be skipped rather than misfire - many
                # cement and clay phases are monoclinic.
                angles = fe.get("angles")
                orthogonal = True
                if angles:
                    try:
                        orthogonal = all(
                            abs(float(x) - 90.0) < 1.0 for x in angles.split()[:3]
                        )
                    except ValueError:
                        orthogonal = True
                if orthogonal:
                    for axis, (span, box) in enumerate(zip(extent, cell)):
                        axis_name = "abc"[axis]
                        if span > box + 0.5:
                            rep.add(
                                WARN,
                                "cell size",
                                f"Structure spans {span:.2f} A along {axis_name} but "
                                f"the cell is {box:.2f} A. Atoms will be wrapped by "
                                "periodicity.",
                                "Check the cell was taken from the same source as "
                                "the coordinates.",
                            )
                periodic = (fe.get("periodic") or "XYZ").upper()
                if periodic != "NONE":
                    for axis, (span, box) in enumerate(zip(extent, cell)):
                        if box - span > 8.0 and box > 20:
                            rep.add(
                                INFO,
                                "vacuum",
                                f"About {box - span:.1f} A of vacuum along {'abc'[axis]} - "
                                "consistent with a slab. Confirm that is intended.",
                            )

    # ---- numerical settings ----------------------------------------------
    for fe in info.get("force_evals", []):
        if fe.get("method", "").upper() not in ("QUICKSTEP", "QS", ""):
            continue
        cutoff, rel = fe.get("cutoff"), fe.get("rel_cutoff")
        try:
            cutoff = float(cutoff) if cutoff else None
            rel = float(rel) if rel else None
        except ValueError:
            cutoff = rel = None
        if cutoff is None:
            rep.add(
                WARN,
                "cutoff",
                "No CUTOFF in &MGRID; CP2K's default is far too low for MOLOPT basis sets.",
                "Set CUTOFF (600-800 Ry is typical here) and REL_CUTOFF (60-80).",
            )
        elif cutoff < CUTOFF_LOW:
            rep.add(
                WARN,
                "cutoff",
                f"CUTOFF {cutoff:g} Ry is low for GTH/MOLOPT; forces and stress will be noisy.",
                "Run a cutoff convergence scan, or start from 600 Ry.",
            )
        elif cutoff > CUTOFF_HIGH:
            rep.add(
                INFO,
                "cutoff",
                f"CUTOFF {cutoff:g} Ry is high - correct but expensive. Confirm it is needed.",
            )
        if rel is not None and not (REL_CUTOFF_LOW <= rel <= REL_CUTOFF_HIGH):
            rep.add(WARN, "rel_cutoff", f"REL_CUTOFF {rel:g} is outside the usual 40-80 range.")

        if run_type in ("CELL_OPT", "MD") and not fe.get("stress_tensor"):
            level = ERROR if run_type == "CELL_OPT" else INFO
            rep.add(
                level,
                "stress tensor",
                f"RUN_TYPE {run_type} without STRESS_TENSOR in &FORCE_EVAL.",
                "Add STRESS_TENSOR ANALYTICAL (CELL_OPT cannot proceed without it; "
                "NPT MD needs it too).",
            )

        if fe.get("spin_polarized") and fe.get("multiplicity") is None:
            has_bs = any(k.get("bs") for k in (fe.get("kinds") or {}).values())
            if not has_bs:
                rep.add(
                    INFO,
                    "spin",
                    "LSD/UKS is on but neither MULTIPLICITY nor any &BS block is set. "
                    "The atomic guess picks the spin state, which often lands in the "
                    "wrong minimum for transition metals.",
                )
        # ---- &BS: decode the target state from the NEL values --------------
        # alpha(NEL) = 2S - dQ and beta(NEL) = -2S - dQ, so the NEL numbers only
        # mean something once inverted - and it is checkable without running
        # anything, which is what makes it a pre-flight check.
        for name, k in sorted((fe.get("kinds") or {}).items()):
            st = k.get("bs_state")
            if not st:
                continue
            if not st["valid"]:
                rep.add(
                    ERROR,
                    "&BS",
                    f"&KIND {name}: NEL sums alpha={st['alpha_nel']}, "
                    f"beta={st['beta_nel']} do not divide evenly, so they "
                    "describe no physical state.",
                    "alpha(NEL) = 2S - dQ and beta(NEL) = -2S - dQ; both "
                    "(alpha-beta) and (alpha+beta) must be even.",
                )
                continue
            two_s, dq = st["two_s"], st["delta_q"]
            # A negative 2S is not a mistake: it is the spin-down sublattice of
            # an antiferromagnetic pair, built by mirroring the alpha and beta
            # channels between two labelled kinds (Fe1 up, Fe2 down).
            n_unpaired = abs(two_s)
            pot = k.get("potential") or ""
            q = None
            if "-q" in pot:
                try:
                    q = int(pot.rsplit("-q", 1)[1])
                except ValueError:
                    q = None
            if q is not None and dq is not None:
                remaining = q - dq
                if remaining < 0:
                    rep.add(
                        ERROR,
                        "&BS",
                        f"&KIND {name}: decodes to charge {dq:+d} but the "
                        f"pseudopotential only carries {q} valence electron(s) "
                        f"({pot}). That state cannot exist.",
                        f"For a {k.get('element', name)} ion of charge +{q} or "
                        "less, reduce the magnitude of the NEL values "
                        f"(dQ = -(alpha+beta)/2 must be <= {q}).",
                    )
                elif n_unpaired and n_unpaired > remaining:
                    rep.add(
                        ERROR,
                        "&BS",
                        f"&KIND {name}: asks for {n_unpaired} unpaired electrons "
                        f"but only {remaining} remain after ionising to {dq:+d}.",
                    )

        # Summarise the decoded magnetic setup, including any AFM pairing.
        magnetic = {
            n: k["bs_state"]
            for n, k in sorted((fe.get("kinds") or {}).items())
            if k.get("bs_state") and k["bs_state"].get("two_s")
        }
        if magnetic:
            # The per-shell split is what makes the block checkable by eye:
            # "4s dQ +2, 3d dQ +1" reads as "empty the 4s, then take one 3d",
            # which is either the ion you meant or visibly not.
            lines = []
            for n, st in magnetic.items():
                k = (fe.get("kinds") or {}).get(n, {})
                el = k.get("element", n) if k else n
                shells = "  ".join(
                    f"{s['label']} dQ {s['delta_q']:+} 2S {s['two_s']}"
                    for s in st.get("shells", []) if s["label"] != "?")
                line = (f"{n}: {el}({st['delta_q']:+d}), {abs(st['two_s'])} "
                        f"unpaired, spin {'up' if st['two_s'] > 0 else 'down'}")
                if shells:
                    line += f"   [{shells}]"
                lines.append(line)
            ups = sum(1 for st in magnetic.values() if st["two_s"] > 0)
            downs = len(magnetic) - ups
            note = (
                "\n      Opposed sublattices - an antiferromagnetic arrangement."
                if ups and downs
                else ""
            )
            rep.add(
                INFO,
                "&BS",
                "Decoded from alpha(NEL)=2S-dQ, beta(NEL)=-2S-dQ:\n      "
                + "\n      ".join(lines)
                + note,
            )

        u_kinds = [n for n, k in (fe.get("kinds") or {}).items() if k.get("plus_u")]
        if u_kinds:
            rep.add(
                INFO,
                "DFT+U",
                f"Hubbard U applied to {', '.join(sorted(u_kinds))}. "
                "U values are system- and functional-specific, so reuse the value "
                "from the study this run is meant to compare against.",
            )

    # ---- restart consistency ---------------------------------------------
    restart_flag = (variables.get("restart") or "").lower()
    ext_restart = info.get("ext_restart")
    if ext_restart:
        target = os.path.join(rundir, ext_restart)
        if not os.path.exists(target):
            rep.add(
                ERROR,
                "restart",
                f"&EXT_RESTART points at '{ext_restart}' which is not in this directory.",
                "Either copy the restart file in, or set the restart switch back to 'no'.",
            )
    for fe in info.get("force_evals", []):
        wfn = fe.get("wfn_restart")
        if wfn and not os.path.exists(os.path.join(rundir, wfn)):
            rep.add(
                WARN,
                "restart",
                f"WFN_RESTART_FILE_NAME '{wfn}' not found; CP2K will fall back to the "
                "SCF_GUESS and the first step will be slow.",
            )
    if restart_flag == "no":
        existing = [
            f for f in os.listdir(rundir) if f.endswith(".restart") and ".bak" not in f
        ]
        if existing:
            rep.add(
                INFO,
                "restart",
                f"restart is set to 'no' but {existing[0]} exists. Starting from scratch "
                "will overwrite the trajectory of the previous run.",
                "Set the restart switch to 'yes' to continue, or move the old files aside.",
            )

    # ---- walltime vs job script ------------------------------------------
    walltime = info.get("walltime")
    sbatch_time = None
    for name in sorted(os.listdir(rundir)):
        if not (name.endswith(".sh") or name.endswith(".slurm")):
            continue
        with open(os.path.join(rundir, name), "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        m = re.search(r"^#SBATCH\s+--time[= ]\s*([\d:-]+)", text, re.MULTILINE)
        if m and not sbatch_time:
            sbatch_time = (name, m.group(1))
    if walltime and sbatch_time:
        name, spec = sbatch_time
        seconds = _slurm_seconds(spec)
        try:
            cp2k_seconds = _cp2k_walltime_seconds(walltime)
        except ValueError:
            cp2k_seconds = None
        context["walltime"] = {"cp2k": walltime, "slurm": spec}
        if seconds and cp2k_seconds and cp2k_seconds >= seconds:
            rep.add(
                WARN,
                "walltime",
                f"&GLOBAL WALLTIME ({walltime}) is not shorter than the Slurm limit "
                f"({spec} in {name}). CP2K will be killed before it can write a restart.",
                "Set WALLTIME a few hundred seconds below the Slurm limit so the run "
                "stops cleanly and dumps a usable .restart file.",
            )
    elif sbatch_time and not walltime:
        rep.add(
            WARN,
            "walltime",
            "No WALLTIME in &GLOBAL. When Slurm kills the job there will be no clean "
            "restart point.",
            "Add WALLTIME slightly below the Slurm --time limit.",
        )

    # This build checks the input and the run directory only. Reading an
    # output file back - energies, SCF behaviour, optimizer convergence - is a
    # separate job and is not part of it.
    return rep, context


def _slurm_seconds(spec: str):
    """Parse Slurm --time: [DD-]HH:MM:SS, HH:MM:SS, MM:SS, or minutes."""
    days = 0
    if "-" in spec:
        d, spec = spec.split("-", 1)
        days = int(d)
    parts = [int(p) for p in spec.split(":") if p != ""]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    elif len(parts) == 1:
        h, m, s = 0, parts[0], 0
    else:
        return None
    return days * 86400 + h * 3600 + m * 60 + s


def _cp2k_walltime_seconds(spec: str):
    """&GLOBAL WALLTIME is either plain seconds or HH:MM:SS."""
    spec = spec.strip()
    if ":" in spec:
        return _slurm_seconds(spec)
    return float(spec)


ICON = {ERROR: "[X]", WARN: "[!]", INFO: "[i]"}


def print_report(rep: Report, context: dict) -> None:
    print(f"\nCP2K run check: {context['dir']}")
    bits = []
    for key, label in (
        ("input", "input"),
        ("output", "output"),
        ("run_type", "run type"),
        ("n_atoms", "atoms"),
        ("multiple_unit_cell", "unit cell replication"),
        ("n_atoms_expanded", "atoms after replication"),
    ):
        if context.get(key):
            bits.append(f"{label}={context[key]}")
    if bits:
        print("  " + "  ".join(bits))
    if context.get("elements"):
        print(f"  elements={' '.join(context['elements'])}")
    print("-" * 78)

    order = {ERROR: 0, WARN: 1, INFO: 2}
    for item in sorted(rep.items, key=lambda i: order[i["level"]]):
        print(f"{ICON[item['level']]} {item['check']}: {item['message']}")
        if item["fix"]:
            print(f"      -> {item['fix']}")
    if not rep.items:
        print("[i] Nothing to flag.")
    print("-" * 78)
    print(f"{rep.n_errors} error(s), {rep.n_warnings} warning(s)\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check a CP2K run directory.")
    ap.add_argument("rundir", nargs="?", default=".", help="run directory")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.rundir):
        print(f"Not a directory: {args.rundir}", file=sys.stderr)
        return 2

    rep, context = check_run(args.rundir)

    if args.json:
        print(json.dumps({"context": context, "findings": rep.items}, indent=2, default=str))
    else:
        print_report(rep, context)
    return 1 if rep.n_errors else 0


if __name__ == "__main__":
    sys.exit(main())
