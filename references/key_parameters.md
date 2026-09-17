# Key parameters, explained

The parameters you actually set, and the ones that confuse people first. Section
ordering, `@SET`, `&KIND` and the full `&BS` arithmetic are in
`input_anatomy.md`; this is the "what does this line mean and what happens if I
change it" companion.

## Contents

- [Charge, spin, and multiplicity](#charge-spin-and-multiplicity)
- [Basis sets and pseudopotentials: two files, two names](#basis-sets-and-pseudopotentials-two-files-two-names)
- [SCF: the inner and the outer loop](#scf-the-inner-and-the-outer-loop)
- [&BS: the starting spin state](#bs-the-starting-spin-state)
- [&PRINT: which block writes which file](#print-which-block-writes-which-file)
- [Defaults you can delete](#defaults-you-can-delete)

---

## Charge, spin, and multiplicity

These three are read together, and getting them inconsistent is the single most
common way to run a calculation that converges beautifully on the wrong system.

### CHARGE

```
&DFT
 CHARGE 0        # net charge of the whole cell, in electrons. Default: 0
```

`CHARGE 1` removes one electron from the cell; `CHARGE -1` adds one. It is the
*cell* charge, not a per-atom or per-molecule charge — there is no way to say
"this ion is 2+" here. Oxidation states come from the structure you built, not
from this keyword.

> **On charged periodic cells.** `CHARGE -2` will not crash. In a periodic cell
> a net charge makes the Coulomb sum diverge, so CP2K quietly adds a uniform
> compensating background and hands you a total energy anyway — one that depends
> on your cell volume. It is the `except: pass` of electronic structure:
> nothing is reported, execution continues, and the answer is whatever it is.
> Energies from differently sized charged cells are not comparable, and neither
> is a charged-cell energy against a neutral one without a finite-size
> correction. Not recommended unless you insist — and if you insist, say in your
> methods section which correction you applied.

For a molecule in a box the story is better, because you can switch off
periodicity (`&CELL PERIODIC NONE` plus a `&POISSON` solver such as `MT`) and
then a charged system is well defined.

### LSD

```
 LSD              # spin-polarised: separate alpha and beta densities
```

Without `LSD` (or its synonym `UKS`) the calculation is **spin-restricted**:
every orbital holds one alpha and one beta electron, the net moment is zero by
construction, and `MULTIPLICITY` is meaningless. Any system with unpaired
electrons — transition metals, radicals, defects, O2 — needs it. It roughly
doubles the cost, because there are now two sets of orbitals to optimise.

### MULTIPLICITY — and the rule

```
 MULTIPLICITY 3   # = 2S + 1. Requires LSD. Default: 1
```

`MULTIPLICITY` is `2S + 1`, where `S` is the total spin: 1 = singlet (no
unpaired electrons), 2 = doublet (1 unpaired), 3 = triplet (2 unpaired), 4 =
quartet (3), 5 = quintet (4).

CP2K splits the electrons as

```
alpha = (N + M - 1) / 2
beta  = (N - M + 1) / 2
```

where `N` is the number of electrons in the cell *after* `CHARGE` is applied and
`M` is the multiplicity. Both have to be whole numbers, which gives the rule:

> **N + M must be odd.**
> An **even** electron count needs an **odd** multiplicity (1, 3, 5, …).
> An **odd** electron count needs an **even** multiplicity (2, 4, …).

An odd-electron system cannot be a singlet. If you find yourself writing
`CHARGE 0` / `MULTIPLICITY 1` for a cell with an odd number of electrons, one of
the two is wrong — and changing `CHARGE` to fix the parity changes the system,
so decide which one you actually meant.

`N` is the **valence** electron count, not the atomic number sum: with GTH
pseudopotentials each atom contributes the `qN` of its potential. A cell of 8 Ca
(`q10`), 12 Si (`q4`), 44 O (`q6`) and 24 H (`q1`) carries
`80 + 48 + 264 + 24 = 416` electrons. `cp2k_structure.py` and `cp2k_doctor.py`
both compute this and check the parity for you.

---

## Basis sets and pseudopotentials: two files, two names

This trips up nearly everyone once, because the same two concepts appear at two
different levels with similar-looking keywords.

**Level 1 — the library files.** In `&DFT`, these name the *files* that must be
present in the run directory (or given as an absolute path):

```
&DFT
 BASIS_SET_FILE_NAME BASIS_MOLOPT      # a file full of basis definitions
 POTENTIAL_FILE_NAME GTH_POTENTIALS    # a file full of pseudopotentials
```

These ship with CP2K, in `$CP2K_DATA_DIR`. Common ones are `BASIS_MOLOPT`,
`BASIS_SET_MOLOPT`, `BASIS_MOLOPT_UZH` (basis) and `GTH_POTENTIALS`,
`POTENTIAL`, `POTENTIAL_UZH` (pseudopotentials). You can list more than one
basis file if different kinds need different libraries.

**Level 2 — one entry inside those files.** In each `&KIND`, these name a single
*entry* to look up:

```
&KIND O
 BASIS_SET DZVP-MOLOPT-SR-GTH          # an entry in BASIS_MOLOPT
 POTENTIAL GTH-PBE-q6                  # an entry in GTH_POTENTIALS
&END KIND
```

So `BASIS_SET_FILE_NAME` is the dictionary and `BASIS_SET` is the word you look
up in it. A missing file is an immediate crash; a missing *entry* is also a
crash, but the message names the entry rather than the file, which is why it
reads as a different problem.

### Reading the names

`DZVP-MOLOPT-SR-GTH`

| Part | Meaning |
|---|---|
| `DZVP` | double-zeta valence + polarisation. `SZV` is smaller and cruder, `TZV2P` larger and better |
| `MOLOPT` | molecularly optimised — the standard choice for condensed-phase CP2K work |
| `SR` | short-range: more compact, less linear-dependence trouble in dense systems |
| `GTH` | built to be used with GTH pseudopotentials |

`GTH-PBE-q6`

| Part | Meaning |
|---|---|
| `GTH` | Goedecker–Teter–Hutter pseudopotential |
| `PBE` | fitted for the PBE functional — **match this to your `&XC_FUNCTIONAL`** |
| `q6` | exposes 6 valence electrons |

**The `qN` rule.** `qN` is how many electrons the pseudopotential treats
explicitly, and the basis set must have been built for that same number. Oxygen
with `q6` is normal; pairing a `q6` basis with a `q16` potential is not. This
mismatch **does not crash** — it converges smoothly to a physically meaningless
answer, which makes it one of the more expensive mistakes available. Some basis
entries state it (`DZVP-MOLOPT-SR-GTH-q2`), most leave it implicit.

To check an entry exists before submitting:

```bash
grep -n "DZVP-MOLOPT-SR-GTH" BASIS_MOLOPT | head
```

`cp2k_doctor.py` does this for every kind, and also flags an element in the
coordinate file that has no `&KIND` at all — the most common hard failure.

---

## SCF: the inner and the outer loop

```
&SCF
 EPS_SCF 1.0E-6
 MAX_SCF 128                 # inner loop
 &OT
  MINIMIZER CG
  PRECONDITIONER FULL_SINGLE_INVERSE
 &END OT
 &OUTER_SCF
  EPS_SCF 1.0E-6             # keep equal to the inner one
  MAX_SCF 10                 # outer loop
 &END OUTER_SCF
&END SCF
```

There are two nested loops because `&OT` does not solve the problem the way
textbook SCF does.

**The inner loop** (`&SCF MAX_SCF`) is orbital transformation: it minimises the
energy directly with respect to orbital rotations, holding the occupations
fixed. This is what makes OT efficient — no diagonalisation each cycle — and
also what makes it struggle without a band gap, since a metal wants fractional
occupations that OT is not varying.

**The outer loop** (`&OUTER_SCF MAX_SCF`) wraps it and handles everything the
inner loop held fixed, then restarts the inner loop from the improved state.

Consequences worth knowing:

- **Total SCF steps is roughly the product**, not the sum. `128 × 10` is a
  budget of 1280 inner iterations, not 138.
- **Convergence is judged on the outer loop.** Keep both `EPS_SCF` values equal;
  a tighter inner than outer wastes time, the reverse never converges.
- **Many outer iterations is a symptom.** If the run keeps handing control back
  to the outer loop, the inner one is not getting there — look at the
  preconditioner (`FULL_ALL` is stronger than `FULL_SINGLE_INVERSE` and handles
  small gaps), the initial guess, or the spin state.
- **A metal probably wants neither.** Replace `&OT` with `&DIAGONALIZATION`
  plus `&SMEAR` and `&MIXING`.

`SCF_GUESS ATOMIC` builds the starting density from superposed atomic densities;
`SCF_GUESS RESTART` reads a converged `.wfn` from a previous run, which is the
single cheapest speedup available when you are restarting or running a sequence
of similar systems.

---

## &BS: the starting spin state

`&BS` (broken symmetry) sets the *initial* occupation of a kind, so an
open-shell metal starts in the spin state you intend rather than wherever the
atomic guess lands. It is **not a constraint** — the SCF can and does move away
from it — but OT tends to stay in the basin it starts in, which is exactly why
the starting point matters.

The `NEL` numbers look arbitrary and are not. Summed over the shells in each
spin channel:

```
alpha(NEL) =  2S - dQ          2S = (alpha - beta) / 2     unpaired electrons
beta(NEL)  = -2S - dQ          dQ = -(alpha + beta) / 2    ionic charge
```

Full worked examples, the two failure modes, and how to check it against the
guess CP2K prints: **`input_anatomy.md`**. `cp2k_input.py --annotate` decodes
every `&BS` block in your file at the bottom of its listing, and
`cp2k_doctor.py` flags blocks that decode to impossible states.

`&BS` requires `LSD`, and it is independent of `CHARGE`: the guess for an oxide
kind is typically built as an ion, while the cell's charge still comes from
`CHARGE`.

---

## &PRINT: which block writes which file

The question "I asked for output, where did it go?" nearly always has one of
three answers: the block is off, its verbosity threshold is above your
`PRINT_LEVEL`, or you are looking for a file when the output went into the log.

`<project>` below is `&GLOBAL PROJECT`.

### From &MOTION — the run's trajectory

| Block | Writes | Contents |
|---|---|---|
| `&MOTION/&PRINT/&TRAJECTORY` | `<project>-pos-1.xyz` | coordinates, one frame per step |
| `&MOTION/&PRINT/&VELOCITIES` | `<project>-vel-1.xyz` | velocities (MD) |
| `&MOTION/&PRINT/&FORCES` | `<project>-frc-1.xyz` | forces, same layout as positions |
| `&MOTION/&PRINT/&CELL` | `<project>-1.cell` | step, time, the 9 lattice components, volume |
| `&MOTION/&PRINT/&STRESS` | `<project>-1.stress` | stress tensor per step |
| `&MOTION/&PRINT/&RESTART` | `<project>-1.restart` | everything needed to continue |
| `&MOTION/&PRINT/&RESTART_HISTORY` | `<project>-1_<step>.restart` | numbered snapshots |
| `&MD/&PRINT/&ENERGY` | `<project>-1.ener` | step, time, E_kin, temperature, E_pot, conserved quantity |

The optimizer also drops `<project>-BFGS.Hessian` — its accumulated Hessian,
which a restart reuses instead of rebuilding. Worth keeping.

**`FORMAT` matters for the trajectory.** `XYZ` and `XMOL` are the same plain
multi-frame format and **carry no cell**, so in a CELL_OPT or NPT run the
lattice lives only in `<project>-1.cell` and must be paired back by step number.
`FORMAT EXTXYZ` embeds the lattice in every frame; `FORMAT DCD` writes a compact
binary `<project>-pos-1.dcd`.

### From &FORCE_EVAL/&DFT — electronic structure

| Block | Writes | Contents |
|---|---|---|
| `&PRINT/&MULLIKEN` | the **main log** by default | per-atom charges, and spin moments when `LSD` is on |
| `&PRINT/&HIRSHFELD` | the main log | Hirshfeld charges — less basis-set dependent than Mulliken |
| `&PRINT/&LOWDIN` | the main log | Löwdin populations |
| `&PRINT/&PDOS` | `<project>-k<N>-1.pdos`, one per kind | projected density of states |
| `&PRINT/&E_DENSITY_CUBE` | `<project>-ELECTRON_DENSITY-1_0.cube` | valence density on a grid (large) |
| `&PRINT/&V_HARTREE_CUBE` | `<project>-v_hartree-1_0.cube` | electrostatic potential — workfunctions, band alignment |
| `&PRINT/&MO_CUBES` | `<project>-WFN_<orbital>_<spin>-1_0.cube` | one cube per orbital; `NHOMO`/`NLUMO` choose how many |
| `&PRINT/&VORONOI` | `<project>-1_0.voronoi` | Voronoi charges and volumes; needs a build with `libvori` |

With `LSD`, `&E_DENSITY_CUBE` additionally writes
`<project>-SPIN_DENSITY-1_0.cube`, and `&PDOS` splits into
`<project>-ALPHA_k<N>-1.pdos` and `<project>-BETA_k<N>-1.pdos`.

Cube files are big — a grid value per point for the whole cell — so print them
once at the end, not every step.

For `VIBRATIONAL_ANALYSIS`, `&PRINT/&MOLDEN_VIB` writes
`<project>-VIBRATIONS-1.mol`, a Molden file you can open to animate the modes.

### The three rules that cause the confusion

**1. A print block's parameter is either a switch or a verbosity threshold, and
they look identical.**

```
&MULLIKEN ON        # switch: print it
&MULLIKEN OFF       # switch: the only thing that truly disables a block
&MULLIKEN MEDIUM    # threshold - not a switch
```

Levels run `SILENT < LOW < MEDIUM < HIGH < DEBUG`, and a block is active when
`&GLOBAL PRINT_LEVEL` is **at or above** the block's own level.

This has a consequence worth internalising, because `&MULLIKEN` is the block
people reach for first: **its default level is `MEDIUM`.** Under the very common
`PRINT_LEVEL LOW`, `LOW < MEDIUM`, so Mulliken analysis is *silently not
printed*. You did not do anything wrong and there is no warning — you either
raise `PRINT_LEVEL` to `MEDIUM`, or write `&MULLIKEN ON` explicitly. The same
logic runs the other way: a block marked `SILENT` prints even in the quietest
run, so `SILENT` means "always on", not "off".

**2. `&EACH` defaults to 1 for every level you do not list.**

```
&EACH
 MD 500           # says what to do during MD, and nothing about anything else
&END EACH
```

Every iteration level (`MD`, `GEO_OPT`, `CELL_OPT`, `QS_SCF`, `METADYNAMICS`, …)
has a default of **1**, so a level you leave out is not disabled — it prints
**every step**. The block above, in a CELL_OPT, writes on every single
iteration. Adding `CELL_OPT 1` changes nothing; only a coarser value like
`CELL_OPT 20` does. Use `0` to suppress a level, though `ADD_LAST` may still
print the final iteration.

**3. Output goes to the log unless the block says otherwise.** Population
analyses default to `FILENAME __STD_OUT__`, i.e. straight into `output.out`. If
you set `FILENAME charges`, CP2K creates `<project>-charges`; a leading `./`
(`FILENAME ./charges`) writes exactly that path instead.

There are also **three different `&CELL` sections**, which is a genuine trap:

| Section | What it is |
|---|---|
| `&FORCE_EVAL/&SUBSYS/&CELL` | the actual lattice — the physics |
| `&MOTION/&PRINT/&CELL` | writes the `<project>-1.cell` **file** |
| `&MOTION/&CELL_OPT/&PRINT/&CELL` | prints the cell **into the log**, not into a file |

A CELL_OPT that produced no `.cell` file almost always has the middle one set to
`off`, inherited from an MD template.

---

## Defaults you can delete

A shorter input is an input you can still read in six months. CP2K fills in
every default it needs, so a keyword set to its own default is pure noise — with
one exception noted below.

The safest examples, because they are verified rather than assumed:

- **`&EACH` entries set to `1`.** That is already the default for every
  iteration level. `GEO_OPT 1`, `MD 1`, `CELL_OPT 1` inside an `&EACH` do
  nothing at all. Delete them and keep only the intervals that differ.
- **`CHARGE 0`** and **`MULTIPLICITY 1`** are the defaults.
- **Print blocks explicitly set to `off`** that you never turned on. A dozen
  `&PDOS off` / `&MO_CUBES off` blocks inherited from a template describe
  nothing; deleting them changes nothing.
- **`&MD`, `&GEO_OPT` or `&CELL_OPT` blocks for run types you are not running**
  are read only when `RUN_TYPE` matches — but *keep* these. They are what lets
  one file serve every run type, which is the point of the `@SET run_type`
  idiom.

**The exception: state the physics even when it matches the default.** `CHARGE`,
`MULTIPLICITY`, the functional, and the cutoffs are worth writing explicitly
even when they happen to equal a default, because their value is a claim about
the system rather than a technical detail. A reader should not have to know
CP2K's defaults to know what you calculated.

**How to find out what a default actually is:** run the job once and read the
`.restart` file. It is a complete input reproducing the state that was reached,
and CP2K writes back every section including the values it filled in silently —
so a `SILENT` you never typed will be sitting there. Careful, though: those
print settings in the restart do **not** control a restarted run. `cp2k.inp`
does; `&EXT_RESTART` only pulls state (coordinates, cell, step counters) out of
the restart file.

`cp2k_input.py --annotate` deliberately does *not* repeat any of this against
your input - it would be a page of commentary on a file you are trying to read.
It does two things this page cannot: it decodes every `&BS` block in your file,
per shell, and it lists the `&PRINT` blocks your run does *not* have, with the
path to add each one.
