#!/usr/bin/env python3
"""
CP2K input handling: preprocessor expansion + section-tree parsing.

CP2K inputs in this workflow lean heavily on the preprocessor (@SET / @IF /
@INCLUDE), and the convention of listing several @SET lines with all but one
commented out means you cannot tell what a run actually did by eye. Expanding
the input first removes that guesswork.

Usable two ways:

  CLI:     python cp2k_input.py cp2k.inp --expand          # fully resolved input
           python cp2k_input.py cp2k.inp --vars            # @SET values in effect
           python cp2k_input.py cp2k.inp --summary         # key settings at a glance
           python cp2k_input.py cp2k.inp --annotate        # same file + explanations

  Import:  from cp2k_input import expand, parse_tree, load
           root, vars = load("cp2k.inp")
           root.find("FORCE_EVAL/DFT/MGRID/CUTOFF")
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap

FALSY = {"", "0", "f", "false", "no", "off", "none"}

_SET_RE = re.compile(r"^\s*@SET\s+(\S+)\s*(.*)$", re.IGNORECASE)
_IF_RE = re.compile(r"^\s*@IF\s+(.*)$", re.IGNORECASE)
_ENDIF_RE = re.compile(r"^\s*@ENDIF\s*$", re.IGNORECASE)
_INCLUDE_RE = re.compile(r"^\s*@INCLUDE\s+(.*)$", re.IGNORECASE)
_VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")
_SECTION_RE = re.compile(r"^\s*&(\w+)\s*(.*?)\s*$")
_END_RE = re.compile(r"^\s*&END\b\s*(\w*)\s*$", re.IGNORECASE)


# --------------------------------------------------------------------------
# Preprocessor
# --------------------------------------------------------------------------

def substitute(text: str, variables: dict) -> str:
    """Replace ${VAR} and $VAR. Unknown names are left alone so that a typo
    stays visible in the output instead of silently becoming an empty string."""

    def repl(m):
        name = m.group(1) or m.group(2)
        return variables.get(name.lower(), m.group(0))

    return _VAR_RE.sub(repl, text)


def evaluate(condition: str) -> bool:
    """Evaluate an @IF condition that has already had its variables substituted.

    CP2K supports `a==b`, `a/=b`, and a bare value that is truthy when it is
    neither empty nor a recognised false token."""
    cond = condition.split("#")[0].split("!")[0].strip()
    if "==" in cond:
        lhs, rhs = cond.split("==", 1)
        return lhs.strip().lower() == rhs.strip().lower()
    if "/=" in cond:
        lhs, rhs = cond.split("/=", 1)
        return lhs.strip().lower() != rhs.strip().lower()
    return cond.strip().lower() not in FALSY


def expand(path: str, variables: dict | None = None, _depth: int = 0):
    """Run the CP2K preprocessor over `path`.

    Returns (lines, variables). Lines are the surviving input lines with all
    variables substituted; `variables` maps lowercased names to final values.
    A later @SET of the same name wins, which is what makes the common
    "four run_type lines, three commented out" idiom resolve correctly."""
    if _depth > 10:
        raise RuntimeError(f"@INCLUDE nested more than 10 deep at {path}")
    if variables is None:
        variables = {}

    out: list[str] = []
    # Each entry is True while the enclosing @IF branch is being emitted.
    stack: list[bool] = []

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw_lines = fh.readlines()

    for raw in raw_lines:
        line = raw.rstrip("\n")
        stripped = line.strip()

        m = _IF_RE.match(stripped)
        if m:
            active = all(stack)
            stack.append(active and evaluate(substitute(m.group(1), variables)))
            continue
        if _ENDIF_RE.match(stripped):
            if stack:
                stack.pop()
            continue
        if not all(stack):
            continue

        # Comments are dropped only when the whole line is one, so that
        # trailing annotations stay attached to the keyword they explain.
        if stripped.startswith("#") or stripped.startswith("!"):
            continue
        if not stripped:
            continue

        m = _SET_RE.match(stripped)
        if m:
            name, value = m.group(1), m.group(2)
            value = substitute(value, variables).split("#")[0].split("!")[0].strip()
            variables[name.lower()] = value
            continue

        m = _INCLUDE_RE.match(stripped)
        if m:
            target = substitute(m.group(1), variables).strip().strip("'\"")
            target = target.split("#")[0].split("!")[0].strip()
            inc_path = target
            if not os.path.isabs(inc_path):
                inc_path = os.path.join(os.path.dirname(os.path.abspath(path)), target)
            if not os.path.exists(inc_path):
                out.append(f"# !! @INCLUDE target not found: {target}")
                continue
            inc_lines, variables = expand(inc_path, variables, _depth + 1)
            out.append(f"# ---- begin @INCLUDE {target} ----")
            out.extend(inc_lines)
            out.append(f"# ---- end @INCLUDE {target} ----")
            continue

        out.append(substitute(line, variables))

    return out, variables


# --------------------------------------------------------------------------
# Section tree
# --------------------------------------------------------------------------

class Section:
    """One &SECTION ... &END block."""

    def __init__(self, name: str, param: str = "", parent: "Section | None" = None):
        self.name = name.upper()
        self.param = param.strip()
        self.parent = parent
        self.subsections: list[Section] = []
        self.keywords: list[tuple[str, str]] = []

    # -- lookups ----------------------------------------------------------
    def sections(self, name: str) -> list["Section"]:
        """Every direct subsection with this name (&KIND appears many times)."""
        return [s for s in self.subsections if s.name == name.upper()]

    def section(self, name: str) -> "Section | None":
        found = self.sections(name)
        return found[0] if found else None

    def get(self, key: str, default=None):
        key = key.upper()
        for k, v in self.keywords:
            if k == key:
                return v
        return default

    def find(self, path: str, default=None):
        """Look up a slash-separated path, e.g. 'FORCE_EVAL/DFT/MGRID/CUTOFF'.

        The last element may be a keyword or a section; returns the keyword
        value or the Section, whichever it turns out to be."""
        parts = [p for p in path.split("/") if p]
        node = self
        for i, part in enumerate(parts):
            nxt = node.section(part)
            if nxt is None:
                if i == len(parts) - 1:
                    value = node.get(part)
                    return default if value is None else value
                return default
            node = nxt
        return node

    def walk(self):
        yield self
        for sub in self.subsections:
            yield from sub.walk()

    def __repr__(self):
        tag = f"&{self.name}" + (f" {self.param}" if self.param else "")
        return f"<Section {tag} ({len(self.subsections)} sub, {len(self.keywords)} kw)>"


def parse_tree(lines) -> Section:
    """Build a section tree from already-expanded input lines."""
    root = Section("ROOT")
    node = root
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue

        # Trailing comments come off BEFORE the section tests, not after.
        # `&END TRAJECTORY  # done` otherwise fails to match _END_RE at all:
        # the section never closes, every later section nests one level too
        # deep, and a lookup for &FORCE_EVAL returns nothing - while the parse
        # still "succeeds", so cp2k_doctor skips its atom, kind and charge
        # checks and reports 0 errors. CP2K itself accepts the file, which is
        # what makes the silence dangerous. Same reason &KIND Ca # calcium must
        # not end up with the comment glued to the kind name.
        stripped = stripped.split("#")[0].split("!")[0].strip()
        if not stripped:
            continue

        if _END_RE.match(stripped):
            if node.parent is not None:
                node = node.parent
            continue

        m = _SECTION_RE.match(stripped)
        if m and not stripped.upper().startswith("&END"):
            child = Section(m.group(1), m.group(2), node)
            node.subsections.append(child)
            node = child
            continue

        parts = stripped.split(None, 1)
        key = parts[0]
        value = parts[1].strip() if len(parts) > 1 else ""
        node.keywords.append((key.upper(), value))

    return root


def load(path: str):
    """Convenience: expand then parse. Returns (root_section, variables)."""
    lines, variables = expand(path)
    return parse_tree(lines), variables


def bs_state(kind: Section):
    """Decode a &KIND's broken-symmetry block into a spin and an ionisation.

    CP2K's &BS NEL values encode a target state, summed over the shells listed
    in each spin channel:

        alpha(NEL) =  2S - dQ
        beta(NEL)  = -2S - dQ

    Inverting gives the two numbers that actually mean something:

        2S = (alpha - beta) / 2     unpaired electrons
        dQ = -(alpha + beta) / 2    formal ionic charge

    So Fe(3+) high-spin d5 is 2S=5, dQ=3, which is what
    ALPHA (-2, +4) / BETA (-2, -6) sums to. Reading NEL values directly is
    close to meaningless; reading them through this rule is not.

    Returns None when there is no &BS, otherwise a dict. `valid` is False when
    the sums do not divide evenly, which means the NEL values cannot describe
    any real state."""
    bs = kind.section("BS")
    if bs is None:
        return None

    # N, L and NEL are parallel lists: the k-th entry of each describes one
    # shell. The spin and the ionisation are therefore SHELL-quantities that
    # happen to add up - Fe(3+) is "empty the 4s, then take one 3d", not one
    # undifferentiated -3. Keep the per-shell split as well as the totals,
    # because the split is where the chemistry is legible and where a block
    # that removes electrons from the wrong shell becomes obvious.
    totals, per_channel = {}, {}
    for channel in ("ALPHA", "BETA"):
        sec = bs.section(channel)
        if sec is None:
            return None
        cols: dict[str, list[int]] = {"N": [], "L": [], "NEL": []}
        for key, value in sec.keywords:
            if key in cols:
                for token in value.split():
                    try:
                        cols[key].append(int(token))
                    except ValueError:
                        return None
        if not cols["NEL"]:
            return None
        totals[channel] = sum(cols["NEL"])
        shells = {}
        # N and L are optional in principle; index the shell by position when
        # they are absent rather than inventing quantum numbers for it.
        for i, nel in enumerate(cols["NEL"]):
            n = cols["N"][i] if i < len(cols["N"]) else None
            l = cols["L"][i] if i < len(cols["L"]) else None
            shells[(n, l, i if n is None else None)] = nel
        per_channel[channel] = shells

    a, b = totals["ALPHA"], totals["BETA"]
    valid = (a - b) % 2 == 0 and (a + b) % 2 == 0

    def _half(x):
        """NEL differences are even in every real case, but do not silently
        round one that is not - show the half and let it look wrong."""
        return x // 2 if x % 2 == 0 else x / 2

    shells_out = []
    for key in list(per_channel["ALPHA"]) + [k for k in per_channel["BETA"]
                                             if k not in per_channel["ALPHA"]]:
        n, l, _ = key
        an = per_channel["ALPHA"].get(key, 0)
        bn = per_channel["BETA"].get(key, 0)
        label = f"{n}{'spdfgh'[l]}" if (n is not None and l is not None
                                        and 0 <= l < 6) else "?"
        shells_out.append({
            "label": label, "n": n, "l": l,
            "alpha_nel": an, "beta_nel": bn,
            "two_s": _half(an - bn), "delta_q": _half(-(an + bn)),
        })

    state = {
        "alpha_nel": a,
        "beta_nel": b,
        "two_s": (a - b) // 2 if valid else None,
        "delta_q": -(a + b) // 2 if valid else None,
        "shells": shells_out,
        "valid": valid,
        "enabled": (bs.param or "ON").upper() not in ("OFF", "FALSE", "F", "NO"),
    }
    return state


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

def summarize(root: Section, variables: dict) -> dict:
    """Pull out the settings that matter most when sanity-checking a run."""
    info: dict = {}
    g = root.section("GLOBAL")
    if g:
        info["project"] = g.get("PROJECT")
        info["run_type"] = g.get("RUN_TYPE")
        info["walltime"] = g.get("WALLTIME")
        info["print_level"] = g.get("PRINT_LEVEL")
        # Linear-algebra backend selection. Reported for completeness; which
        # backend is best is build- and site-specific.
        info["diag_library"] = g.get("PREFERRED_DIAG_LIBRARY")
        info["elpa_kernel"] = g.get("ELPA_KERNEL")
        fm = g.section("FM")
        info["fm_matrix_multiplication"] = (
            fm.get("TYPE_OF_MATRIX_MULTIPLICATION") if fm else None
        )

    force_evals = root.sections("FORCE_EVAL")
    info["n_force_eval"] = len(force_evals)
    fe_list = []
    for fe in force_evals:
        entry = {"method": fe.get("METHOD"), "stress_tensor": fe.get("STRESS_TENSOR")}
        dft = fe.section("DFT")
        if dft:
            entry["basis_file"] = dft.get("BASIS_SET_FILE_NAME")
            entry["potential_file"] = dft.get("POTENTIAL_FILE_NAME")
            entry["charge"] = dft.get("CHARGE")
            entry["multiplicity"] = dft.get("MULTIPLICITY")
            entry["spin_polarized"] = any(
                k in {"LSD", "UKS"} for k, _ in dft.keywords
            )
            entry["wfn_restart"] = dft.get("WFN_RESTART_FILE_NAME")
            mgrid = dft.section("MGRID")
            if mgrid:
                entry["cutoff"] = mgrid.get("CUTOFF")
                entry["rel_cutoff"] = mgrid.get("REL_CUTOFF")
                entry["ngrids"] = mgrid.get("NGRIDS")
            scf = dft.section("SCF")
            if scf:
                entry["eps_scf"] = scf.get("EPS_SCF")
                entry["max_scf"] = scf.get("MAX_SCF")
                entry["scf_guess"] = scf.get("SCF_GUESS")
                ot = scf.section("OT")
                entry["ot"] = None if ot is None else (ot.param or "ON")
                if ot:
                    entry["ot_minimizer"] = ot.get("MINIMIZER")
                    entry["ot_preconditioner"] = ot.get("PRECONDITIONER")
                outer = scf.section("OUTER_SCF")
                if outer:
                    entry["outer_scf_max"] = outer.get("MAX_SCF")
                    entry["outer_scf_eps"] = outer.get("EPS_SCF")
            xc = dft.section("XC")
            if xc:
                xcf = xc.section("XC_FUNCTIONAL")
                entry["functional"] = xcf.param if xcf else None
                vdw = xc.section("VDW_POTENTIAL")
                if vdw:
                    pp = vdw.section("PAIR_POTENTIAL")
                    entry["dispersion"] = pp.get("TYPE") if pp else vdw.get(
                        "DISPERSION_FUNCTIONAL"
                    )
                    if pp:
                        entry["dispersion_file"] = pp.get("PARAMETER_FILE_NAME")
                        entry["dispersion_cutoff"] = pp.get("R_CUTOFF")
        subsys = fe.section("SUBSYS")
        if subsys:
            cell = subsys.section("CELL")
            if cell:
                entry["abc"] = cell.get("ABC")
                entry["angles"] = cell.get("ALPHA_BETA_GAMMA")
                entry["periodic"] = cell.get("PERIODIC")
                entry["multiple_unit_cell"] = cell.get("MULTIPLE_UNIT_CELL")
            topo = subsys.section("TOPOLOGY")
            if topo:
                entry["coord_file"] = topo.get("COORD_FILE_NAME")
                entry["coord_format"] = topo.get("COORD_FILE_FORMAT")
                entry["topology_multiple_unit_cell"] = topo.get("MULTIPLE_UNIT_CELL")
            kinds = {}
            for kind in subsys.sections("KIND"):
                kinds[kind.param] = {
                    "element": kind.get("ELEMENT", kind.param),
                    "basis_set": kind.get("BASIS_SET"),
                    "potential": kind.get("POTENTIAL"),
                    "bs": kind.section("BS") is not None,
                    "bs_state": bs_state(kind),
                    "plus_u": kind.section("DFT_PLUS_U") is not None,
                }
            entry["kinds"] = kinds
        fe_list.append(entry)
    info["force_evals"] = fe_list

    motion = root.section("MOTION")
    if motion:
        md = motion.section("MD")
        if md:
            info["md"] = {
                "ensemble": md.get("ENSEMBLE"),
                "steps": md.get("STEPS"),
                "timestep": md.get("TIMESTEP"),
                "temperature": md.get("TEMPERATURE"),
                "thermostat": (
                    md.section("THERMOSTAT").get("TYPE")
                    if md.section("THERMOSTAT")
                    else None
                ),
                "barostat": md.section("BAROSTAT") is not None,
            }
        for opt_name in ("GEO_OPT", "CELL_OPT"):
            opt = motion.section(opt_name)
            if opt:
                info[opt_name.lower()] = {
                    "optimizer": opt.get("OPTIMIZER"),
                    "max_iter": opt.get("MAX_ITER"),
                    "max_force": opt.get("MAX_FORCE"),
                    "rms_force": opt.get("RMS_FORCE"),
                    "max_dr": opt.get("MAX_DR"),
                    "rms_dr": opt.get("RMS_DR"),
                    "constraint": opt.get("CONSTRAINT"),
                    "keep_angles": opt.get("KEEP_ANGLES"),
                    "type": opt.get("TYPE"),
                }
        constraint = motion.section("CONSTRAINT")
        if constraint and constraint.section("FIXED_ATOMS"):
            info["fixed_atoms"] = constraint.section("FIXED_ATOMS").get("LIST")
        fe_sec = motion.section("FREE_ENERGY")
        if fe_sec and fe_sec.section("METADYN"):
            meta = fe_sec.section("METADYN")
            info["metadyn"] = {
                "nt_hills": meta.get("NT_HILLS"),
                "ww": meta.get("WW"),
                "n_metavar": len(meta.sections("METAVAR")),
            }

    vib = root.section("VIBRATIONAL_ANALYSIS")
    if vib:
        info["vibrational_analysis"] = {
            "dx": vib.get("DX"),
            "nproc_rep": vib.get("NPROC_REP"),
            "tc_temperature": vib.get("TC_TEMPERATURE"),
            "tc_pressure": vib.get("TC_PRESSURE"),
            "intensities": vib.get("INTENSITIES"),
            "fully_periodic": vib.get("FULLY_PERIODIC"),
        }

    ext = root.section("EXT_RESTART")
    if ext:
        info["ext_restart"] = ext.get("RESTART_FILE_NAME")

    info["variables"] = dict(sorted(variables.items()))
    return info


def _print_summary(info: dict) -> None:
    def line(label, value):
        if value not in (None, "", {}, []):
            print(f"  {label:<24} {value}")

    print("GLOBAL")
    line("project", info.get("project"))
    line("run_type", info.get("run_type"))
    line("walltime", info.get("walltime"))

    for i, fe in enumerate(info.get("force_evals", []), 1):
        tag = f"FORCE_EVAL {i}" if info.get("n_force_eval", 1) > 1 else "FORCE_EVAL"
        print(f"\n{tag}")
        line("method", fe.get("method"))
        line("functional", fe.get("functional"))
        line("dispersion", fe.get("dispersion"))
        line("spin polarized", fe.get("spin_polarized"))
        line("charge / multiplicity", f"{fe.get('charge')} / {fe.get('multiplicity')}")
        line("cutoff / rel_cutoff", f"{fe.get('cutoff')} / {fe.get('rel_cutoff')}")
        line("eps_scf / max_scf", f"{fe.get('eps_scf')} / {fe.get('max_scf')}")
        line("scf_guess", fe.get("scf_guess"))
        line("OT minimizer", fe.get("ot_minimizer"))
        line("OT preconditioner", fe.get("ot_preconditioner"))
        line("cell ABC", fe.get("abc"))
        line("cell angles", fe.get("angles"))
        line("coord file", fe.get("coord_file"))
        kinds = fe.get("kinds") or {}
        if kinds:
            print(f"  {'kinds':<24} {', '.join(sorted(kinds))}")
            for name, k in sorted(kinds.items()):
                extras = []
                if k["bs"]:
                    extras.append("BS")
                if k["plus_u"]:
                    extras.append("+U")
                suffix = f"  [{', '.join(extras)}]" if extras else ""
                print(
                    f"      {name:<8} {str(k['basis_set']):<26}"
                    f" {str(k['potential']):<16}{suffix}"
                )

    for key, title in (
        ("md", "MD"),
        ("geo_opt", "GEO_OPT"),
        ("cell_opt", "CELL_OPT"),
        ("metadyn", "METADYN"),
        ("vibrational_analysis", "VIBRATIONAL_ANALYSIS"),
    ):
        block = info.get(key)
        if block:
            print(f"\n{title}")
            for k, v in block.items():
                line(k, v)

    if info.get("fixed_atoms"):
        print(f"\nCONSTRAINT\n  {'fixed atoms':<24} {info['fixed_atoms'][:120]}")
    if info.get("ext_restart"):
        print(f"\nEXT_RESTART\n  {'restart file':<24} {info['ext_restart']}")


# --------------------------------------------------------------------------
# Annotation
# --------------------------------------------------------------------------
#
# What this is for: a working CP2K input is often several hundred lines with no
# comments at all, and nothing in it says which lines are the physics, which are
# the dials you are expected to turn, and which are boilerplate. --annotate
# writes the same file back with that missing commentary attached.
#
# Two rules shape what goes in the tables below.
#
# 1. Annotate the dials people actually turn. A keyword that only an expert
#    touches does not get a note: anyone changing EPS_DEFAULT or
#    EXTRAPOLATION_ORDER already knows what it does, and a line explaining it
#    to them is noise in exactly the file we are trying to make readable.
#    SKIP records the ones left deliberately silent so they do not creep back.
#
# 2. Explain, do not prescribe. Where a value is physics for *this* system -
#    CUTOFF, CHARGE, MULTIPLICITY, a Hubbard U - the note says what the keyword
#    means and how such a value is normally arrived at. It never suggests a
#    number. That is the same rule the rest of this skill follows.
#
# Notes are written as `!` comments. CP2K accepts both `#` and `!`, so using `!`
# for generated text keeps it separable from whatever comments the file already
# had: `grep -v '^\s*!' annotated.inp` gets you back to the original.

# Keywords deliberately left unannotated. Two kinds, both noise for different
# reasons:
#
#   1. Numerical internals and performance knobs. Changing one is a deliberate
#      act by someone who already knows the tradeoff.
#   2. Keywords whose name IS the explanation. Annotating `RUN_TYPE GEO_OPT`
#      with "what to run" or `OPTIMIZER BFGS` with "geometry optimizer" tells
#      the reader nothing they did not just read, and every such line pushes
#      the lines that DO carry information further apart.
#
# The test for the second group: would the note survive being read aloud next
# to the keyword without sounding absurd? "OPTIMIZER: the optimizer" would not.
SKIP = {
    # 1. internals
    "EPS_DEFAULT", "EPS_PGF_ORB", "EPS_GVG_RSPACE", "EPS_RHO_RSPACE",
    "EXTRAPOLATION_ORDER", "NGRIDS", "COMMENSURATE", "PROGRESSION_FACTOR",
    "PREFERRED_DIAG_LIBRARY", "ELPA_KERNEL", "TYPE_OF_MATRIX_MULTIPLICATION",
    "MAX_MEMORY", "LINEAR_SCALING", "EPS_FILTER", "ADD_LAST",
    # 2. self-evident: &GLOBAL, and the optimizer blocks
    "PRINT_LEVEL", "PROJECT", "RUN_TYPE", "WALLTIME", "FLUSH_SHOULD_FLUSH",
    "OPTIMIZER", "MAX_ITER", "MAX_DR", "RMS_DR", "MAX_FORCE", "RMS_FORCE",
    "EXTERNAL_PRESSURE", "PRESSURE_TOLERANCE", "KEEP_ANGLES",
    "ENSEMBLE", "STEPS", "TEMPERATURE", "TEMP_KIND", "TEMP_TOL",
}

# (what it is, how you would change it). The second entry is None when there is
# no choice worth flagging. Keys are matched most-specific-first, so
# "OUTER_SCF/MAX_SCF" wins over "SCF/MAX_SCF" wins over "MAX_SCF".
NOTES = {
    # -- GLOBAL ------------------------------------------------------------
    "PROJECT": ("prefixes every output file: <project>-pos-1.xyz, "
                "<project>-1.restart, ...", None),
    "RUN_TYPE": ("what to run: ENERGY / GEO_OPT / CELL_OPT / MD / "
                 "VIBRATIONAL_ANALYSIS", None),
    "PRINT_LEVEL": ("log verbosity: SILENT / LOW / MEDIUM / HIGH / DEBUG",
                    "MEDIUM shows per-SCF detail; LOW keeps long runs' logs "
                    "small. Each &PRINT block carries its own level, and is "
                    "active when this one is at or above it"),
    "WALLTIME": ("seconds; CP2K stops itself cleanly and writes a restart",
                 "keep it below the scheduler's limit, or the job is killed "
                 "mid-step with no restart"),
    "FLUSH_SHOULD_FLUSH": ("flush output as it is produced, so a killed job "
                           "still leaves a readable log", None),

    # -- MOTION: optimizers ------------------------------------------------
    "OPTIMIZER": ("geometry optimizer",
                  "BFGS is fastest up to a few hundred atoms; LBFGS uses less "
                  "memory for larger systems; CG is slower but robust when "
                  "BFGS oscillates"),
    "MAX_ITER": ("give up after this many optimizer steps", None),
    "MAX_DR": ("convergence: largest allowed atom displacement (bohr)",
               "all four MAX_/RMS_ criteria must be met at once, and MAX_FORCE "
               "is usually the last to fall"),
    "RMS_DR": ("convergence: RMS displacement (bohr)", None),
    "MAX_FORCE": ("convergence: largest allowed force (hartree/bohr)", None),
    "RMS_FORCE": ("convergence: RMS force (hartree/bohr)", None),
    "EXTERNAL_PRESSURE": ("pressure the cell is optimized against", None),
    "PRESSURE_TOLERANCE": ("how close to EXTERNAL_PRESSURE counts as converged",
                           None),
    "KEEP_ANGLES": ("hold the lattice angles fixed",
                    "worth having for hexagonal/monoclinic cells, which "
                    "otherwise drift to triclinic"),
    "CELL_OPT/CONSTRAINT": ("restrict which cell directions may change",
                            "a slab with vacuum wants Z (or XY/YZ) fixed, or the "
                            "optimizer crushes the vacuum"),
    # CELL_OPT/TYPE is deliberately absent: "what moves with the cell" restates
    # the keyword. &PAIR_POTENTIAL/TYPE and &THERMOSTAT/TYPE stay, because
    # those values carry consequences a name does not.

    # -- MOTION: MD --------------------------------------------------------
    "ENSEMBLE": ("NVE / NVT / NPT_I / NPT_F ...", None),
    "STEPS": ("number of MD steps", None),
    "TIMESTEP": ("fs",
                 "0.5 fs is right with free hydrogen; 1.0 fs only with X-H bonds "
                 "constrained"),
    "TEMPERATURE": ("target temperature (K)", None),
    "TEMP_KIND": ("also report temperature per element kind", None),
    "TEMP_TOL": ("rescale velocities if T strays this far from target", None),
    "COMVEL_TOL": ("stops the cell slowly acquiring net momentum "
                   "(the 'flying ice cube')", None),
    "DISPLACEMENT_TOL": ("abort if an atom moves further than this in one step",
                         None),
    "TIMECON": ("thermostat coupling time", None),

    # -- FORCE_EVAL --------------------------------------------------------
    "METHOD": ("the engine: Quickstep is CP2K's DFT", None),
    "POTENTIAL_FILE_NAME": ("pseudopotential file; same requirement", None),
    "CHARGE": ("net charge of the whole cell, in electrons",
               "N + M must be odd, where N is the electron count after CHARGE "
               "and M the multiplicity - so an even electron count needs an odd "
               "multiplicity and vice versa. A non-zero charge in a periodic "
               "cell is neutralised by a uniform background and the resulting "
               "energy depends on cell volume; keep cells neutral unless you "
               "have a finite-size correction in mind"),
    "MULTIPLICITY": ("2S+1; requires LSD (or UKS)", None),
    "LSD": ("spin-polarized: separate alpha and beta densities",
            "needed for transition metals, radicals and defects; without it the "
            "calculation is spin-restricted"),
    "UKS": ("spin-polarized (same as LSD)", None),
    "WFN_RESTART_FILE_NAME": ("reuse a converged wavefunction as the SCF guess",
                              None),

    # -- MGRID -------------------------------------------------------------
    "EXTRAPOLATION": ("reuse the previous step's density as the next guess - the "
                      "single biggest SCF speedup in MD and geometry optimization",
                      None),

    # -- SCF ---------------------------------------------------------------
    "SCF/EPS_SCF": ("SCF convergence threshold",
                    "keep this and the &OUTER_SCF value equal"),
    "OUTER_SCF/EPS_SCF": ("outer-loop convergence threshold; match the inner one",
                          None),
    "SCF/MAX_SCF": ("inner SCF steps before handing back to the outer loop", None),
    "OUTER_SCF/MAX_SCF": ("outer-loop iterations; total SCF steps is roughly the "
                          "product of the two MAX_SCF values", None),
    "SCF_GUESS": ("ATOMIC builds a guess from scratch; RESTART reads the .wfn",
                  None),
    "MINIMIZER": ("OT minimizer",
                  "CG is the robust choice; DIIS converges faster when it works "
                  "and stalls when it does not"),
    "PRECONDITIONER": ("OT preconditioner",
                       "FULL_SINGLE_INVERSE is cheaper; FULL_ALL is stronger and "
                       "handles small or vanishing HOMO-LUMO gaps"),

    # -- XC ----------------------------------------------------------------
    "R_CUTOFF": ("real-space cutoff for the dispersion pair potential (bohr)",
                 None),
    "PAIR_POTENTIAL/TYPE": ("DFTD3 needs dftd3.dat in the run directory", None),
    "REFERENCE_FUNCTIONAL": ("the functional the dispersion parameters were "
                             "fitted for",
                             "must match the functional actually in use above"),
    "DISPERSION_FUNCTIONAL": ("which dispersion scheme this block configures",
                              None),
    "POTENTIAL_TYPE": ("which dispersion scheme this block configures", None),

    # -- SUBSYS ------------------------------------------------------------
    "ABC": ("cell lengths; angstrom unless a unit is given in brackets",
            "take these from the structure source - a cell that disagrees with "
            "the coordinates is a common and silent error"),
    "ALPHA_BETA_GAMMA": ("cell angles (degrees)", None),
    "PERIODIC": ("which directions are periodic; NONE needs a &POISSON solver",
                 None),
    "MULTIPLE_UNIT_CELL": ("replicate the cell",
                           "&CELL and &TOPOLOGY must carry the same value, or the "
                           "cell and the coordinates get replicated differently"),
    "COORD_FILE_NAME": ("starting geometry",
                        "the element column in this file must match the &KIND "
                        "names below"),
    "COORD_FILE_FORMAT": ("format of the coordinate file", None),
    "BASIS_SET": ("basis for this kind", None),
    "POTENTIAL": ("pseudopotential for this kind",
                  "the qN suffix is how many valence electrons it exposes, and it "
                  "must match what the basis was built for - a mismatch does not "
                  "crash, it converges to a meaningless answer"),
    "ELEMENT": ("what this labelled kind actually is; the label itself appears in "
                "the coordinate file", None),
    "U_MINUS_J": ("the Hubbard U",
                  "U is not transferable between systems, functionals or "
                  "oxidation states - carry over the value from whatever study "
                  "this run is meant to compare against, and record where it "
                  "came from"),
    "DFT_PLUS_U/L": ("which shell U applies to: 0=s, 1=p, 2=d, 3=f", None),
    "POISSON_SOLVER": ("solver for a non-periodic cell (MT, wavelet, ...)", None),

    # -- &PRINT internals --------------------------------------------------
    "BACKUP_COPIES": ("how many previous versions of the file to keep", None),
    "FORCE_LAST": ("always write on the final step, whatever &EACH says", None),
}

# Section-level notes. &PRINT blocks get the fullest treatment here: they are
# what decides which files a run leaves behind, and they are the part people
# most often want to change after seeing what the first run produced.
SECTION_NOTES = {
    "MOTION": ("holds GEO_OPT, CELL_OPT and MD settings at once; CP2K reads only "
               "the block matching RUN_TYPE, which is what lets one file serve "
               "every run type", None),
    "EXT_RESTART": ("continue a previous run: restores positions, velocities and "
                    "the step counter", None),
    "OT": ("orbital transformation - the efficient SCF path for systems with a "
           "band gap",
           "genuinely metallic systems want &DIAGONALIZATION with &SMEAR and "
           "&MIXING instead; OT struggles without a gap"),
    "OUTER_SCF": ("with OT the inner loop optimizes orbitals at fixed occupation "
                  "and this outer loop handles the rest", None),
    "VDW_POTENTIAL": ("dispersion correction", None),
    "BS": ("broken symmetry: sets the STARTING spin state, not a constraint - the "
           "SCF may move away from it",
           "NEL sums encode 2S = (alpha-beta)/2 and dQ = -(alpha+beta)/2; the "
           "decode for this file is at the bottom of this listing"),
    "DFT_PLUS_U": ("Hubbard U correction on this kind", None),
    "KIND": ("one per element symbol appearing in the coordinate file; a missing "
             "&KIND is the most common hard failure", None),
    "POISSON": ("Poisson solver, for cells that are not periodic in every "
                "direction", None),

    # &PRINT blocks: what each one actually writes.
    "TRAJECTORY": ("positions every &EACH steps -> <project>-pos-1.xyz", None),
    "VELOCITIES": ("velocities -> <project>-vel-1.xyz", None),
    "MOTION/PRINT/FORCES": ("forces every &EACH steps -> <project>-frc-1.xyz",
                            None),
    "MOTION/PRINT/CELL": ("this is the block that writes the <project>-1.cell "
                          "file - the cell history of a CELL_OPT", None),
    "MOTION/PRINT/STRESS": ("stress tensor per step; needs STRESS_TENSOR in "
                            "&FORCE_EVAL", None),
    "CELL_OPT/PRINT/CELL": ("prints the cell into the main log, NOT into the "
                            ".cell file",
                            "the separate <project>-1.cell file comes from "
                            "&MOTION/&PRINT/&CELL - a different section with a "
                            "confusingly identical name"),
    "RESTART": ("everything needed to continue the run -> <project>-1.restart",
                None),
    "RESTART_HISTORY": ("numbered snapshots kept alongside the restart",
                        "this is the fallback when the newest restart turns out "
                        "to be truncated because the job died mid-write"),
    "ENERGY": ("energies per step -> <project>-1.ener", None),
    "PROGRAM_RUN_INFO": ("this block's running commentary in the main log", None),
    "DFT_CONTROL_PARAMETERS": ("echo the DFT settings actually in use into the "
                               "log", None),
    "TOTAL_NUMBERS": ("atom, electron and basis-function counts", None),
    "ATOMIC_COORDINATES": ("echo the parsed coordinates into the log - this is "
                           "how you confirm CP2K read the geometry you meant",
                           None),
    "SUBSYS/PRINT/KINDS": ("echo each kind's basis and potential into the log",
                           None),
}

# Output blocks worth knowing about, listed at the end when they are absent.
# (path to add it at, what you get, run types it applies to or None for any)
ADDABLE = [
    ("FORCE_EVAL/DFT/PRINT/MULLIKEN",
     "per-atom charges, and spin moments when LSD is on - the usual way to "
     "check a magnetic state actually survived the SCF. Write it as "
     "`&MULLIKEN ON`: its own default level is MEDIUM, so under PRINT_LEVEL "
     "LOW it is silently not printed", None),
    ("FORCE_EVAL/DFT/PRINT/HIRSHFELD",
     "per-atom charges, less basis-set dependent than Mulliken", None),
    ("FORCE_EVAL/DFT/PRINT/PDOS",
     "projected density of states per kind -> .pdos files", None),
    ("FORCE_EVAL/DFT/PRINT/E_DENSITY_CUBE",
     "valence electron density as a .cube file (large)", None),
    ("FORCE_EVAL/DFT/PRINT/MO_CUBES",
     "orbital .cube files; NHOMO/NLUMO choose how many", None),
    ("FORCE_EVAL/DFT/PRINT/V_HARTREE_CUBE",
     "electrostatic potential as a .cube file, for workfunction and "
     "band-alignment work", None),
    ("FORCE_EVAL/PRINT/FORCES", "final per-atom forces in the log", None),
    ("FORCE_EVAL/PRINT/STRESS_TENSOR",
     "the stress tensor in the log; needs STRESS_TENSOR in &FORCE_EVAL",
     {"CELL_OPT"}),
    ("MOTION/PRINT/VELOCITIES", "velocities -> <project>-vel-1.xyz", {"MD"}),
    ("MOTION/PRINT/FORCES", "forces -> <project>-frc-1.xyz", {"MD", "GEO_OPT"}),
    ("MOTION/PRINT/CELL", "cell per step -> <project>-1.cell", {"CELL_OPT"}),
    ("MOTION/PRINT/RESTART_HISTORY",
     "numbered restart snapshots - the fallback when the newest restart is "
     "truncated", {"MD", "GEO_OPT", "CELL_OPT"}),
    ("SUBSYS/PRINT/KINDS",
     "each kind's basis and potential echoed into the log, so a wrong qN is "
     "visible without hunting", None),
]

# Asides: the one place this tool is allowed to be less than strictly useful.
# A CP2K input is a long, joyless file, and a remark that raises a smile costs
# nothing. Three rules keep them from becoming noise:
#   - keep them SHORT. The input is already long. One line, ideally.
#   - the lesson belongs on the `->` line above; the aside is just the wink.
#     Do not re-teach here - `->` means do something, `~` does not.
#   - keep them rare. A fifth one and the joke has become the format.
# Each entry is a callable taking the keyword's value and returning the line,
# or None to stay quiet.

def _aside_max_scf(value):
    """MAX_SCF is the one keyword whose exact value is arbitrary, which makes
    it the safe place to have some fun. Every branch still has to be TRUE about
    the number actually written: 73 really is the 21st prime and a binary
    palindrome, 299792458 really is the speed of light in m/s, and the exponent
    is computed rather than assumed."""
    try:
        n = int(value)
    except ValueError:
        return None

    if n == 299792458:
        return "Biu, off at light speed! That will never converge. I REFUSE to conduct this job."
    if n == 42:
        return ("The final answer of everything. "
                "- The Hitchhiker's Guide to the Galaxy")
    if n == 73:
        return ("Sheldon's best number - the 21st prime, and 1001001 in binary. "
                "Best MAX_SCF too?")
    if n >= 16 and n & (n - 1) == 0:
        msg = f"Congratulations! {n} = 2^{n.bit_length() - 1}. Welcome to the club of the binary round."
        # Still a member - 512 is as binary-round as 128 - but past a couple of
        # hundred the cap has stopped meaning anything. The hint is left in
        # base 2 on purpose; decoding it is the reader's half of the joke.
        if n > 256:
            msg += " Though probably too large for an SCF - try a smaller one. Hint: 1000000(2)."
        return msg
    if 10 <= n <= 100 and n % 10 == 0:
        return f"{n}. Boooooriiiiiing number :("
    return None


def _aside_project(value):
    return ("Naming things - one of the two hard problems in computing. "
            "Forty output files will inherit this one.")


def _aside_walltime(value):
    """The hours only appear when WALLTIME is a literal; under the @SET idiom
    the value here is still `${walltime}`."""
    lead = ""
    try:
        hours = float(value) / 3600.0
    except ValueError:
        hours = None
    if hours is not None and hours > 8:
        lead = f"{hours:.1f} hours straight, no break. "
    return (lead + "Lucky you - computers are not included in the eight-hour "
            "working day. ...YET.")


ASIDES = {
    "SCF/MAX_SCF": _aside_max_scf,
    "PROJECT": _aside_project,
    "WALLTIME": _aside_walltime,
}

def _aside_for_keyword(name):
    """ASIDES is keyed like the notes tables ("SCF/MAX_SCF"), but an @SET line
    has no section context - match on the final segment."""
    if name in ASIDES:
        return ASIDES[name]
    for key, fn in ASIDES.items():
        if key.rsplit("/", 1)[-1] == name:
            return fn
    return None


_ANNOTATE_WIDTH = 46      # column the inline `!` notes start at
_WRAP_WIDTH = 88          # notes wrap here rather than running off the screen


def _lookup(table, stack, name):
    """Most-specific-first match for a keyword or section in a section stack.

    'MAX_SCF' inside &SCF/&OUTER_SCF tries OUTER_SCF/MAX_SCF before SCF/MAX_SCF
    before a bare MAX_SCF, so one keyword can be described differently depending
    on where it sits - which is the whole difficulty with CP2K keyword names."""
    parts = list(stack) + [name]
    for start in range(len(parts)):
        hit = table.get("/".join(parts[start:]))
        if hit:
            return hit
    return None


def annotate(path: str) -> list[str]:
    """Return the input at `path` with `!` explanations woven in.

    The original lines are preserved exactly - this adds commentary and changes
    nothing else, so the result still runs. Each distinct note is emitted once:
    a file with forty &KIND blocks should not carry forty copies of the same
    sentence about pseudopotential suffixes."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw_lines = [line.rstrip("\n") for line in fh]

    # Under the @SET idiom the value sits at the top of the file and the
    # keyword far below as `WALLTIME ${walltime}`. A remark about the number
    # belongs next to the number, so map each variable to the keyword it feeds
    # by reading the file's own wiring rather than guessing from its name.
    var_feeds: dict[str, str] = {}
    for raw in raw_lines:
        s = raw.strip()
        if not s or s[0] in "#!@&":
            continue
        m = re.match(r"^(\w+)\s+\$\{?(\w+)\}?$", s)
        if m:
            var_feeds.setdefault(m.group(2).lower(), m.group(1).upper())

    out: list[str] = []
    stack: list[str] = []
    seen: set[str] = set()
    # True while the previous non-blank source line was a comment. A line the
    # author already explained is left alone: the point is to fill in what the
    # file does not say, not to argue with what it does.
    after_comment = False

    def emit_aside(indent: str, text: str):
        width = max(_WRAP_WIDTH - len(indent), 40)
        for i, chunk in enumerate(textwrap.wrap(text, width - 6)):
            lead = "!   ~ " if i == 0 else "!     "
            out.append(f"{indent}{lead}{chunk}")

    def emit_above(indent: str, what: str, tune: str | None):
        # Wrapped rather than run long: these notes are read in an editor
        # alongside the input, and a 150-column comment defeats the point.
        width = max(_WRAP_WIDTH - len(indent), 40)
        for i, chunk in enumerate(textwrap.wrap(what, width - 2)):
            out.append(f"{indent}! {chunk}" if i == 0 else f"{indent}!   {chunk}")
        if tune:
            for i, chunk in enumerate(textwrap.wrap(tune, width - 6)):
                lead = "!   -> " if i == 0 else "!      "
                out.append(f"{indent}{lead}{chunk}")

    for line in raw_lines:
        stripped = line.strip()
        indent = line[: len(line) - len(line.lstrip())]

        # Comments and blanks pass through untouched, and must not move the
        # section stack - a commented-out &KIND block is not an open section.
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            if stripped:
                after_comment = True
            out.append(line)
            continue

        # Match against the line with any trailing comment removed - `&END KIND
        # # calcium` has to close the section here too - but always emit the
        # original line unchanged.
        stripped = stripped.split("#")[0].split("!")[0].strip()
        if not stripped:
            out.append(line)
            continue

        if _END_RE.match(stripped):
            if stack:
                stack.pop()
            out.append(line)
            after_comment = False
            continue

        # Preprocessor directives: explained once, at the first one seen.
        if stripped.upper().startswith("@"):
            directive = stripped.split(None, 1)[0].upper().lstrip("@")
            note = None
            if note and not after_comment and f"@{directive}" not in seen:
                seen.add(f"@{directive}")
                emit_above(indent, note[0], note[1])
            out.append(line)
            after_comment = False

            m = _SET_RE.match(stripped)
            if m:
                var = m.group(1).lower()
                value = m.group(2).split("#")[0].split("!")[0].strip()
                kw = var_feeds.get(var)
                fn = _aside_for_keyword(kw) if kw else None
                if fn and f"~{kw}" not in seen:
                    text = fn(value)
                    if text:
                        seen.add(f"~{kw}")
                        emit_aside(indent, text)
            continue

        m = _SECTION_RE.match(stripped)
        if m and not stripped.upper().startswith("&END"):
            name = m.group(1).upper()
            note = _lookup(SECTION_NOTES, stack, name)
            key = f"&{name}:{note[0][:24] if note else ''}"
            if note and not after_comment and key not in seen:
                seen.add(key)
                emit_above(indent, note[0], note[1])
            out.append(line)
            stack.append(name)
            after_comment = False
            continue

        key_name = stripped.split(None, 1)[0].upper()
        note = None if key_name in SKIP else _lookup(NOTES, stack, key_name)

        # An &EACH level set to 1 is set to the value it already has: every
        # iteration level defaults to 1. Saying so is more useful than
        # explaining the keyword again, and deleting those lines is the
        # cheapest way to shorten an inherited input without changing it.
        parts = stripped.split(None, 1)
        if stack and stack[-1] == "EACH" and len(parts) > 1 and parts[1].strip() == "1":
            note = ("this is already the default - every &EACH iteration level "
                    "defaults to 1",
                    "the line can be deleted without changing what the run does; "
                    "only a coarser interval (e.g. 20) actually does anything")

        emitted = note and not after_comment and f"{key_name}:{note[0][:24]}" not in seen
        after_comment = False
        aside_fn = _lookup(ASIDES, stack, key_name)
        aside = aside_fn(parts[1].strip()) if (aside_fn and len(parts) > 1) else None
        if aside and f"~{key_name}" in seen:
            aside = None
        if aside:
            seen.add(f"~{key_name}")

        if emitted:
            seen.add(f"{key_name}:{note[0][:24]}")
            what, tune = note
            inline_len = max(len(line), _ANNOTATE_WIDTH) + 2 + len(what)
            if tune or inline_len > _WRAP_WIDTH:
                emit_above(indent, what, tune)
                out.append(line)
            else:
                pad = max(_ANNOTATE_WIDTH - len(line), 1)
                out.append(f"{line}{' ' * pad}! {what}")
            if aside:
                emit_aside(indent, aside)
            continue

        out.append(line)
        if aside:
            emit_aside(indent, aside)

    return out


def _present_sections(root: Section) -> set[str]:
    """Every section path in the tree, as 'FORCE_EVAL/DFT/PRINT/MULLIKEN'."""
    paths = set()

    def walk(node, prefix):
        for sub in node.subsections:
            path = f"{prefix}/{sub.name}" if prefix else sub.name
            paths.add(path)
            walk(sub, path)

    walk(root, "")
    return paths


def annotation_footer(root: Section, info: dict) -> list[str]:
    """Closing notes: decoded &BS states, and output blocks that are absent.

    The catalogue is filtered against what the input already has and against the
    run type, so it stays a short list of things that would actually add
    something here rather than a general menu."""
    lines = ["", "! " + "-" * 74]

    # &BS blocks are unreadable as written; bs_state() already knows how to
    # decode them, so say what each one actually asks for.
    decoded = []
    for fe in root.sections("FORCE_EVAL"):
        subsys = fe.section("SUBSYS")
        if not subsys:
            continue
        for kind in subsys.sections("KIND"):
            state = bs_state(kind)
            if not state:
                continue
            if not state["valid"]:
                decoded.append(f"!   &KIND {kind.param}: NEL sums "
                               f"({state['alpha_nel']}, {state['beta_nel']}) do "
                               "not describe any physical state")
            else:
                shells = "   ".join(
                    f"{s['label']} dQ {s['delta_q']:+} 2S {s['two_s']}"
                    for s in state["shells"])
                decoded.append(
                    f"!   &KIND {kind.param}: 2S = {state['two_s']} "
                    f"({state['two_s']} unpaired electrons), "
                    f"dQ = {state['delta_q']:+d}"
                    + ("" if state["enabled"] else "   [block is OFF]"))
                if shells:
                    decoded.append(f"!       per shell: {shells}")
    if decoded:
        lines.append("! &BS blocks decoded (alpha/beta NEL sums -> spin and charge):")
        lines.extend(decoded)
        lines.append("!")

    run_type = (info.get("run_type") or "").upper()
    present = _present_sections(root)
    missing = []
    for path, description, run_types in ADDABLE:
        if run_types and run_type not in run_types:
            continue
        # Match on the tail, since &PRINT paths differ between force_evals.
        if any(p.endswith(path) for p in present):
            continue
        wrapped = textwrap.wrap(description, _WRAP_WIDTH - 44)
        missing.append(f"!   {path:<38} {wrapped[0]}")
        missing.extend(f"!   {'':<38} {chunk}" for chunk in wrapped[1:])

    if missing:
        lines.append("! Output this run will not produce, in case you want it. Add"
                     " the block at the")
        lines.append("! path shown; each takes an &EACH to set how often it is "
                     "written:")
        lines.extend(missing)
        lines.append("!")

    lines.append("! Every note above is a `!` comment, so this file still runs as "
                 "it stands. To get")
    lines.append("! the original back (this also drops any `!` comments the file "
                 "already had; `#`")
    lines.append("! comments are left alone):")
    lines.append("!   sed 's/[[:space:]]*!.*$//' annotated.inp | grep -v "
                 "'^[[:space:]]*$'")
    lines.append("! For validation - missing files, cutoff ranges, "
                 "charge/multiplicity parity, walltime")
    lines.append("! against the scheduler - run cp2k_doctor.py on the run "
                 "directory. It checks what")
    lines.append("! this listing only explains.")
    lines.append("! " + "-" * 74)
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Expand and inspect a CP2K input file.")
    ap.add_argument("input", help="path to cp2k.inp")
    ap.add_argument("--expand", action="store_true", help="print the resolved input")
    ap.add_argument("--vars", action="store_true", help="print @SET values in effect")
    ap.add_argument("--summary", action="store_true", help="print key settings")
    ap.add_argument("--annotate", action="store_true",
                    help="print the input with explanatory ! comments added")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)

    lines, variables = expand(args.input)

    if args.expand:
        print("\n".join(lines))
        return 0

    if args.annotate:
        # The annotated listing is of the file as written - @SET lines and all -
        # because that is the file being read and edited. The footer needs the
        # resolved tree, though, to know what the run will actually do.
        root = parse_tree(lines)
        print("\n".join(annotate(args.input)))
        print("\n".join(annotation_footer(root, summarize(root, variables))))
        return 0

    if args.vars:
        for name, value in sorted(variables.items()):
            print(f"{name} = {value}")
        return 0

    root = parse_tree(lines)
    info = summarize(root, variables)

    if args.json:
        import json

        print(json.dumps(info, indent=2))
        return 0

    _print_summary(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
