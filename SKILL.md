---
name: cp2k
description: Write and check CP2K input files - building and editing cp2k.inp for GEO_OPT / CELL_OPT / ENERGY / ab initio MD / metadynamics / vibrational analysis / thermodynamic integration, choosing basis sets and GTH pseudopotentials, setting charge, spin state, broken-symmetry &BS blocks and DFT+U, resolving the @SET / @IF / @INCLUDE preprocessor, explaining an unfamiliar input line by line, and validating an input and its run directory before submission. Use this skill whenever a CP2K input is being written, read, explained or checked, whenever a .inp / .restart file or a Quickstep, MOLOPT, GTH, or CP2K-style @SET input appears, and whenever the user is setting up periodic DFT on solids, surfaces, slabs, or solvated systems - even if they do not name CP2K explicitly.
---

# CP2K

This skill produces and explains **CP2K input files**, and checks them before
they are submitted. Reading results back out of `output.out` is deliberately not
part of it. Job submission is site-specific: `references/running.md` covers only
the parts that change what you write in the input, and never assume a particular
machine or scheduler.

## The rule that matters most

**Never invent numbers.** Cutoffs, Hubbard U values, basis/pseudopotential
pairings, thermostat time constants and convergence thresholds are
system-specific and are arrived at through work you cannot see. A plausible
input built from guessed parameters is worse than no input, because it produces
converged results that are quietly wrong.

When a number is needed and there is no source for it:

1. Take it from a working input for a similar system, and say where it came from.
2. If there is no such input, **ask** — state which specific numbers you need.
3. Only then fall back on a documented default, and label it as one.

Two parameters deserve particular care:

- **Hubbard U (`&DFT_PLUS_U`)** — never add it on your own initiative. U is not
  transferable: it depends on functional, basis set, oxidation state, and what
  property was fitted. If a system contains transition metals, ask whether the
  user wants DFT+U and which U value; if they do not specify one, **leave
  `&DFT_PLUS_U` out entirely** rather than inserting a placeholder. Silently
  guessing a U changes the electronic structure and the answer.
- **Plane-wave `CUTOFF`** — too low gives noisy forces and stress. If unknown,
  say a convergence scan is needed rather than picking a number.

## Getting oriented: reuse a working input

The most reliable way to build an input is to start from one that already works
for a similar system, so that functional, cutoff, basis sets and pseudopotentials
carry over as a consistent set. If the user has an archive of previous runs,
search it before writing anything:

```bash
grep -rl "KIND Fe" --include="*.inp" <archive> | head
grep -rh "CUTOFF" --include="*.inp" <archive> | sort | uniq -c | sort -rn
```

Treat any such archive as **read-only** unless told otherwise: copy what you
need into the working directory rather than editing in place.

With no precedent available, `assets/cp2k.inp` is a commented, general-purpose
starting template. Its comments explain what each block is for and which values
are placeholders that must be replaced.

## The @SET parameter-block idiom

Many CP2K inputs hoist everything that varies into `@SET` variables at the top,
so one file serves every run type and a new system is a short edit:

```
#@SET run_type  ENERGY
#@SET run_type GEO_OPT
@SET run_type CELL_OPT
#@SET run_type MD
```

The commented lines are deliberate history — the **last uncommented `@SET`
wins**. Switch run type by moving one `#`, never by deleting the alternatives.

This means a raw input does not show what a run actually did. Expand it first:

```bash
python scripts/cp2k_input.py cp2k.inp --summary
```

Full details of the idiom, `&KIND`, `&BS`, DFT+U, dispersion and the restart
switch: **`references/input_anatomy.md`**.

## Bundled scripts

Run these instead of writing new parsing code.

| Command | Purpose |
|---|---|
| `python scripts/cp2k_input.py IN --summary` | Resolved settings: functional, cutoff, SCF, cell, kinds |
| `python scripts/cp2k_input.py IN --expand` | Fully preprocessed input, `@SET`/`@IF`/`@INCLUDE` resolved |
| `python scripts/cp2k_input.py IN --annotate` | The same input with `!` comments explaining each setting |
| `python scripts/cp2k_doctor.py RUNDIR` | Pre-flight checks on the input and run directory; exit 1 on errors |
| `python scripts/cp2k_structure.py X.xyz --input IN` | Composition, formal charge vs `CHARGE`, electron count, close contacts |
| `python scripts/test_cp2k_structure.py` | Regression tests for the structure checks (run after editing them) |

Close-contact detection uses the **full lattice** for the minimum image, not a
per-axis fold by `|a|`, `|b|`, `|c|` - that shortcut is only valid for an
orthogonal cell and invents contacts on triclinic ones. Where an extended-XYZ
`Lattice="..."` line is present it is preferred over reconstructing the cell from
`ABC`/`ALPHA_BETA_GAMMA`. `cp2k_input.py` is importable
(`from cp2k_input import load, bs_state`) when a task needs the section tree.

## Reading an unfamiliar input

A working CP2K input is routinely a few hundred uncommented lines, and nothing
in it says which values are the physics, which are the dials, and which are
boilerplate. `--annotate` writes the same file back with that commentary added:

```bash
python scripts/cp2k_input.py cp2k.inp --annotate > annotated.inp
```

Four things shape the output, and they are worth knowing before relying on it:

- **It stays short.** One line per keyword, two at most. An annotation that runs
  to a paragraph defeats the purpose, and the long-form explanations live in
  `references/key_parameters.md` instead.
- **It skips what needs no explaining.** Two kinds: numerical internals
  (`EPS_DEFAULT`, `EXTRAPOLATION_ORDER`, ELPA/SCALAPACK selection), and keywords
  whose name already is the explanation — `RUN_TYPE`, `PROJECT`, `OPTIMIZER`,
  the optimizer convergence thresholds. Annotating `OPTIMIZER BFGS` with
  "geometry optimizer" pushes the lines that *do* carry information apart.
- **It explains, it does not prescribe.** Where a value is physics for *this*
  system — `CHARGE`, `MULTIPLICITY`, a Hubbard `U` — the note says what the
  keyword means, never what number to use.
- **It stays quiet where the author already spoke.** A keyword whose preceding
  line is already a comment is left alone, so a well-commented input picks up
  almost nothing and an uncommented one picks up everything.

The listing closes with two things the body cannot show: every `&BS` block
decoded into the spin and ionisation it asks for — **per shell**, so "empty the
4s, then take one 3d" is legible rather than an undifferentiated −3 — and the
`&PRINT` blocks this run does *not* have, with the path to add each one, which
is the usual answer to "how do I also get charges / a PDOS / cube files out of
this?"

It also drops the occasional aside, marked `~` rather than `->`. Those are
deliberate: a CP2K input is a joyless file to read, and a remark that raises a
smile while making a true point costs nothing. They stay rare and each carries a
real one - `->` means do something, `~` does not. They live in `ASIDES` in
`cp2k_input.py`; delete the table if you want the output plain.

Notes are `!` comments, so the annotated file still runs unchanged. Validation
is a separate job: `cp2k_doctor.py` checks what this only explains.

## Workflow: building an input

1. **Find precedent** — the closest existing working input, if there is one.
2. **Start from it, or from `assets/cp2k.inp`.**
3. **Fill the parameter block:** cell from the structure, coordinate file,
   project name, run type, functional, cutoff.
4. **One `&KIND` per element** in the structure file, with `BASIS_SET` and
   `POTENTIAL`. The `qN` in `GTH-PBE-qN` is the valence electron count and must
   match what the basis set expects — a mismatch converges cleanly to a wrong
   answer rather than crashing.
5. **Charge and spin, stated explicitly.** Set `CHARGE` whenever the system is
   not neutral. `CHARGE` and `MULTIPLICITY` must be parity-compatible: with `N`
   electrons and multiplicity `M`, the alpha count `(N + M - 1)/2` has to be a
   whole number. Open-shell systems need `LSD`/`UKS` plus explicit `&BS` blocks,
   or the atomic guess picks a spin state and OT stays in it.
6. **Transition metals:** ask about DFT+U (see the rule above). Ask whether the
   intended magnetic order is ferromagnetic or antiferromagnetic — for AFM you
   need labelled kinds (`Fe1`/`Fe2`) with mirrored `&BS` channels, and the cell
   multiplicity follows the *net* moment, not the metal count.
7. **Check before running:**
   ```bash
   python scripts/cp2k_doctor.py .
   ```
   This catches missing `&KIND`s, absent basis/potential/dispersion files, cell
   and structure disagreeing, charge/multiplicity parity conflicts, impossible
   `&BS` states, and close contacts.

Run-type-specific settings — optimizer choice, thermostats, constraints,
metadynamics, vibrational analysis, TI with mixed force evals:
**`references/run_types.md`**.

## Workflow: diagnosing a failure

```bash
python scripts/cp2k_doctor.py RUNDIR
```

- **Died in seconds** → missing file or malformed input. Scheduler logs carry
  MPI and module errors; CP2K's own errors go to the output file.
- **SCF not converging** → preconditioner, initial guess, or spin state, roughly
  in that order. Metals may need diagonalization with smearing instead of OT.
- **Optimization stalling** → check which criterion is outstanding; usually
  `MAX_FORCE`, usually one stubborn atom.
- **No `PROGRAM ENDED`** → walltime, out of memory, or still running.
- **Converged but wrong** → charge, spin state, or a `qN` mismatch between basis
  and pseudopotential.

Full decision paths: **`references/troubleshooting.md`**.

## Running the calculation

Site details vary — scheduler, module system, accelerator setup, account
strings — and none of them belong in this skill. `references/running.md` covers
only what changes the input itself: setting `&GLOBAL WALLTIME` below the
scheduler's limit so CP2K stops cleanly and writes a resumable restart, and the
rank/thread split.

Ask which machine the user is on rather than assuming, and never fill in an
account, partition or module name you were not given.

## Reference files

| File | Read when |
|---|---|
| `references/key_parameters.md` | What a parameter means and what changing it does: charge/spin/multiplicity, basis vs potential files, inner/outer SCF, `&BS`, and which `&PRINT` block writes which file |
| `references/input_anatomy.md` | Writing or editing any input; `@SET`, `&KIND`, `&BS`, DFT+U, dispersion, restart |
| `references/run_types.md` | Choosing or configuring a run type; sweeps for EOS and elastic constants |
| `references/troubleshooting.md` | Anything failed, stalled, or looks wrong |
| `references/running.md` | WALLTIME, rank/thread splits, what to check before submitting |

## Assets

`assets/cp2k.inp` is a commented general template.
`assets/run-cpu-module.sh` is a skeleton job script with every site-specific
value left as a placeholder — adapt, do not submit as-is.

Basis set and pseudopotential files (`BASIS_MOLOPT`, `GTH_POTENTIALS`, `dftd3.dat`)
ship with CP2K and are **not** bundled here; point the input at the ones in the
CP2K installation (`$CP2K_DATA_DIR`) or copy them from it into the run directory.
