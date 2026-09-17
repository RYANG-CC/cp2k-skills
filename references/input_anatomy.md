# CP2K input anatomy and the parameter-block idiom

## Why the parameter block exists

A bare CP2K input hides its physics: the cell is one number among hundreds, the
run type sits inside `&GLOBAL`, and changing a system means editing half a dozen
places consistently. Hoisting everything that varies into `@SET` lines at the top
turns the rest of the file into a fixed scaffold, so a new system is a ten-line
edit rather than a scavenger hunt. It also means the *same* file serves ENERGY,
GEO_OPT, CELL_OPT and MD — you flip one comment character.

```
#################################################
#############PARAMETER SECTION###################
#################################################

#@SET run_type  ENERGY
#@SET run_type GEO_OPT
@SET run_type CELL_OPT
#@SET run_type MD
...
#################################################
#######END OF THE PARAMETER SECTION##############
#################################################
```

The commented-out lines are deliberate. The last uncommented `@SET` of a name
wins, so the alternatives above the active one are a record of what this input
has been used for. **Preserve them when editing.** Switching a run from GEO_OPT
to CELL_OPT means moving one `#`, not deleting the other three lines.

Because of this idiom you cannot read a run's settings off the raw file. Expand
it first:

```bash
python scripts/cp2k_input.py cp2k.inp --summary
python scripts/cp2k_input.py cp2k.inp --expand   # fully resolved input
```

For an input you did not write — the common case, and usually several hundred
lines with no comments at all — `--annotate` returns the same file with `!`
notes attached to each setting, plus a closing list of the `&PRINT` blocks it
does *not* have:

```bash
python scripts/cp2k_input.py cp2k.inp --annotate > annotated.inp
```

It annotates the dials people actually turn and stays silent on numerical
internals, on anything the file already comments, and on what value any
physics-bearing number *should* be. `&BS` blocks are decoded in the footer,
using the same arithmetic set out below.

## Preprocessor directives

| Directive | Behaviour |
|---|---|
| `@SET name value` | Define a variable. Rest of the line is the value. Later definitions override earlier ones. |
| `${name}` / `$name` | Substitute. Both forms work; `${}` is safer next to other text. |
| `@IF cond` / `@ENDIF` | Conditional block. Conditions: `${a}==${b}`, `${a}/=${b}`, or a bare truthy value. There is no `@ELSE` — write two `@IF` blocks with opposite tests. |
| `@INCLUDE file` | Splice in another file, after substitution. Variables set before the include are visible inside it. |
| `#` or `!` | Comment to end of line. |

`@IF` blocks that set a variable must appear *before* the variable is used —
the preprocessor is a single forward pass, not a solver.

## Conventional variable names

These names recur across inputs; reusing them keeps files interchangeable.

| Variable | Meaning |
|---|---|
| `run_type` | Feeds `&GLOBAL RUN_TYPE`. |
| `project` | Feeds `&GLOBAL PROJECT`; prefixes every output file. |
| `mineral`, `surface` | Compose `project`; `surface==bulk` collapses to just the mineral name. |
| `coord_ini` | The starting `.xyz`. |
| `restart` | `yes`/`no`. Gates `&EXT_RESTART`, `SCF_GUESS`, `WFN_RESTART_FILE_NAME`, and whether `&TOPOLOGY` reads the `.xyz` at all. |
| `a b c alpha beta gamma` | Cell parameters. |
| `na nb nc` | `MULTIPLE_UNIT_CELL` replication. |
| `cutoff`, `rel_cutoff` | `&MGRID` plane-wave cutoffs in Ry. |
| `xcf` | Functional name, reused as `REFERENCE_FUNCTIONAL` for dispersion. |
| `PF`, `PFL` | Print frequency (frequent / less frequent) for `&EACH` blocks. |
| `walltime` | `&GLOBAL WALLTIME`, in seconds. |
| `dispersion`, `disp_r_cutoff` | Dispersion correction switch and its real-space cutoff. |
| `MD_STEPS` | Number of MD steps. |

## The restart switch

Flipping `restart` from `no` to `yes` has to change four things together, which
is exactly why it is a variable rather than four manual edits:

```
@IF ${restart}==yes
 &EXT_RESTART
  RESTART_FILE_NAME ${project}-1.restart
  RESTART_BAROSTAT F
 &END EXT_RESTART
@ENDIF
```
- `&EXT_RESTART` appears, restoring positions, velocities and step counter.
- `SCF_GUESS RESTART` replaces `SCF_GUESS ATOMIC`.
- `WFN_RESTART_FILE_NAME` points at the saved wavefunction.
- `&TOPOLOGY` stops reading `COORD_FILE_NAME`, because the restart file already
  carries the geometry. Leaving the `.xyz` active would silently rewind the run.

`RESTART_BAROSTAT F` matters when resuming an NPT run whose barostat state you
do not want to inherit. For a `.bak-1` restart (the previous backup, used when
the newest restart was written mid-crash and is truncated) point
`RESTART_FILE_NAME` at `${project}-1.restart.bak-1`.

## Section ordering

CP2K does not care about the order of top-level sections, but a consistent
layout makes diffs readable: parameter block, `&EXT_RESTART`, `&GLOBAL`,
`&MOTION`, `&FORCE_EVAL`. `&VIBRATIONAL_ANALYSIS` sits before `&FORCE_EVAL`.

Within `&MOTION`, `&GEO_OPT`, `&CELL_OPT` and `&MD` can all be present at once —
CP2K reads only the one matching `RUN_TYPE`. This is what lets a single file
serve every run type, so keep all three rather than deleting the inactive ones.

## &KIND blocks

Every element symbol appearing in the coordinate file needs a `&KIND` whose name
matches that symbol. A missing `&KIND` is the single most common hard failure.

```
&KIND Ni
 BASIS_SET DZVP-MOLOPT-SR-GTH
 POTENTIAL GTH-PBE-q18
&END KIND
```

The `qN` suffix is the number of valence electrons the pseudopotential exposes,
and it must match what the basis set was built for. Getting it wrong produces a
run that converges to a physically meaningless answer rather than crashing, so
it is worth checking against the pseudopotential file itself rather than guessing.

**Labelled kinds** let you treat chemically distinct sites of the same element
separately — for constraints, for `&COLVAR` definitions, or for per-site PDOS:

```
&KIND Ow            # water oxygen
 ELEMENT O
 BASIS_SET DZVP-MOLOPT-SR-GTH
 POTENTIAL GTH-PBE-q6
&END KIND
```

The label appears in the `.xyz` in place of the element symbol; `ELEMENT` tells
CP2K what it actually is.

## &BS blocks: setting the initial spin state

For open-shell transition metals the atomic guess routinely lands in the wrong
spin state and then stays there — SCF converges happily to a local minimum that
is 1 eV off. `&BS` forces the starting occupation:

```
&KIND Fe
 BASIS_SET DZVP-MOLOPT-SR-GTH
 POTENTIAL GTH-PBE-q16
### Fe(3+); d^5; spin up
 &BS on
  &ALPHA
   N    4   3      # principal quantum numbers
   L    0   2      # angular momenta (0=s, 2=d)
   NEL -2   4      # electrons to ADD (+) or REMOVE (-) from the neutral atom
  &END ALPHA
  &BETA
   N    4   3
   L    0   2
   NEL -2  -6
  &END BETA
 &END BS
&END KIND
```

`N`/`L` select a shell and `NEL` is a signed change on that shell, per spin
channel. Read literally the numbers look arbitrary; they are not. Summed over
the shells in each channel they encode a target spin and ionisation:

```
alpha(NEL) =  2S - dQ
beta(NEL)  = -2S - dQ
```

which inverts to the two quantities that actually carry meaning:

```
2S = (alpha - beta) / 2      unpaired electrons
dQ = -(alpha + beta) / 2     formal ionic charge
```

Fe(3+) high-spin d⁵ is therefore `ALPHA (-2, +4)`, `BETA (-2, -6)`: alpha sums
to −2, beta to −8, giving 2S = 5 and dQ = +3. The same block written for Fe(2+)
d⁶ only changes the beta 3d term to −4, giving 2S = 4, dQ = +2.

This is worth internalising because it makes `&BS` checkable by hand and,
usefully, **without running anything**. Two failure modes fall straight out:

- Both `(alpha - beta)` and `(alpha + beta)` must be even, or the values
  describe no physical state.
- `dQ` cannot exceed the valence electrons the pseudopotential carries. An
  archive input pairs `GTH-PBE-q1` hydrogen with `NEL -2` in both channels,
  which decodes to H²⁺ — two electrons removed from a one-electron
  pseudopotential. `NEL -1` is the intended H⁺.

`cp2k_doctor.py` decodes every `&BS` block this way and reports both.

**The split by shell is where the chemistry is legible.** `N`, `L` and `NEL` are
parallel lists — the k-th entry of each describes one shell — so the spin and the
ionisation are per-shell quantities that happen to add up. Decomposing the Fe(3+)
block above:

| shell | alpha | beta | dQ | 2S |
|---|---|---|---|---|
| 4s | −2 | −2 | +2 | 0 |
| 3d | +4 | −6 | +1 | 5 |
| **total** | | | **+3** | **5** |

which reads as "empty the 4s, then take one 3d" — neutral Fe is 3d⁶4s², so that
is exactly Fe(3+) d⁵. An undifferentiated total of +3 would be equally correct
and far less checkable: a block that takes its electrons from the wrong shell
still sums to a plausible-looking charge. `cp2k_doctor.py` and
`cp2k_input.py --annotate` both print the per-shell split alongside the total.

**CP2K also prints the guess it actually built**, which confirms the decode:

```
 Guess for atomic kind: Fe
    Total number of valence electrons                                      13.00
    Total number of electrons                                              23.00
    Multiplicity                                                          sextet
    Alpha Electrons
    S   [  1.00  1.00] 1.00
    P   [  3.00] 3.00
    D      5.00
    Beta Electrons
    ...
    D      0.00
```

`Z` minus total electrons gives the ionic charge, and alpha minus beta the
moment: here Fe(3+) with 5 unpaired, i.e. high-spin d⁵. `cp2k_doctor.py` reports
this per kind whenever an output is present, so an intended spin state can be
confirmed rather than assumed.

Two things `&BS` is not:

- **It is not a constraint.** It sets the *starting* density only. The SCF can
  and does move away from it; OT tends to stay in whatever basin it starts in,
  which is precisely why the starting point matters, but a converged run may not
  hold the state you asked for. Check the final Mulliken moments, not the guess.
- **It is not a charge setting.** The guess for an oxide kind is typically built
  as an ion (O²⁻, Fe³⁺), but the cell's charge comes from `CHARGE`.

Alpha minus beta over the whole cell sets the total moment, so an
antiferromagnetic arrangement is built by giving some sites one alpha/beta
pattern and others the mirror image — which needs labelled kinds (`Fe1`, `Fe2`).
`&BS` requires `LSD` (or `UKS`) in `&DFT`.

Annotate each `&BS` block with the oxidation state and d-count it encodes.
Six months later that comment is the only way to tell an intentional
high-spin Fe(3+) from a typo — and it is checkable against the printed guess.

## DFT+U

```
&KIND Ni
 ...
 &DFT_PLUS_U ON
  L 2                    # 2 = d orbitals
  U_MINUS_J [eV] 3.0
 &END DFT_PLUS_U
&END KIND
```

`PLUS_U_METHOD MULLIKEN` in `&DFT` is the default and may be omitted. U is not
transferable: it depends on functional, basis, and what property was fitted.
Reuse the value from whatever study the run is meant to compare against, and
record where it came from.

## Dispersion

```
&VDW_POTENTIAL
 DISPERSION_FUNCTIONAL PAIR_POTENTIAL
 &PAIR_POTENTIAL
  R_CUTOFF ${disp_r_cutoff}
  TYPE DFTD3
  PARAMETER_FILE_NAME dftd3.dat
  REFERENCE_FUNCTIONAL ${xcf}
 &END PAIR_POTENTIAL
&END VDW_POTENTIAL
```

`dftd3.dat` must be in the run directory. `REFERENCE_FUNCTIONAL` has to match the
functional actually in use — driving `${xcf}` from the same variable makes that
automatic. `R_CUTOFF` is in bohr; 30 is generous and cheap.

The non-local alternative (rVV10) needs `rVV10_kernel_table` in the run
directory instead:

```
&VDW_POTENTIAL
 DISPERSION_FUNCTIONAL NON_LOCAL
 &NON_LOCAL
  TYPE RVV10
  KERNEL_FILE_NAME rVV10_kernel_table
  PARAMETERS 9.3 9.3E-003
 &END NON_LOCAL
&END VDW_POTENTIAL
```

## &PRINT: what you get, and how often

Three rules explain almost every "why did I get no `.cell` file" and "why do I
have 55 restart files" question. All three were established against real runs,
because none of them is guessable from the input.

**1. A print block's parameter is either a switch or a verbosity threshold, and
they look identical.**

```
&CELL ON        # switch: on
&CELL OFF       # switch: off - the only thing that actually disables a block
&CELL SILENT    # threshold, NOT off - see below
```

`SILENT < LOW < MEDIUM < HIGH < DEBUG`. A block is active when `&GLOBAL
PRINT_LEVEL` is **at or above** the block's own level. So under the usual
`PRINT_LEVEL LOW`, a block marked `SILENT` prints *always*, and one marked
`HIGH` is the quiet one. CP2K writes these defaults back into the `.restart`
file, so a `SILENT` you never typed will appear there.

**2. `&EACH` defaults to 1 for every iteration level you do not list.**

```
&EACH
 MD 500        # only says what to do during MD
&END EACH
```

That is *not* "off for CELL_OPT". Every iteration level (`MD`, `GEO_OPT`,
`CELL_OPT`, `QS_SCF`, …) defaults to **1**, so an unlisted level prints **every
step**. This is why the block above produced 55 numbered restart files in a
55-step CELL_OPT. Adding `CELL_OPT 1` to a block changes nothing; only a
*coarser* value like `CELL_OPT 20` does.

**3. There are three different `&CELL` sections and they do different jobs.**

| Section | What it is |
|---|---|
| `&FORCE_EVAL / &SUBSYS / &CELL` | The actual lattice. The physics. In a `.restart` this holds the relaxed cell. |
| `&MOTION / &PRINT / &CELL` | Writes the separate **`<project>-1.cell` file** — the cell history of a CELL_OPT. |
| `&MOTION / &CELL_OPT / &PRINT / &CELL` | Prints the cell **into `output.out`**, not into a file. |

A CELL_OPT that produced no `.cell` file almost always has the second one set to
`off`, inherited from an MD template where it was disabled.

**Trajectory format carries no cell.** `FORMAT XYZ` and `FORMAT XMOL` are the
same thing — XMOL is CP2K's internal name, and it writes it back into the
restart file whichever you typed. Neither stores the lattice, so for a CELL_OPT
the positions in `<project>-pos-1.xyz` have to be paired with
`<project>-1.cell` by step number. `FORMAT EXTXYZ` embeds the lattice in every
frame instead, making the trajectory self-contained.

**Print settings inside a `.restart` file do not control a restarted run.** The
`.restart` is a complete input describing the state that was reached, defaults
and all, but `&EXT_RESTART` only pulls state out of it — coordinates, cell, step
counters. What gets printed next time comes from `cp2k.inp`.

`cp2k_input.py --annotate` writes all of this against the blocks in your own
input, including the print blocks a run does *not* have.

## Files that must sit beside cp2k.inp

The compute node sees only what is in the run directory (or an absolute path).
A job that dies in the first seconds is almost always missing one of:

- `cp2k.inp`
- the coordinate `.xyz` named by `coord_ini`
- every basis file named by `BASIS_SET_FILE_NAME` (`BASIS_MOLOPT`, and
  `BASIS_MOLOPT_UZH` etc. if referenced)
- every pseudopotential file named by `POTENTIAL_FILE_NAME` (`GTH_POTENTIALS`)
- `dftd3.dat` when using DFTD3
- `rVV10_kernel_table` when using rVV10
- the job script
- any `@INCLUDE`d fragment
- the `.restart` and `.wfn` files when restarting

`scripts/cp2k_doctor.py` checks this list against what the input actually
references.
