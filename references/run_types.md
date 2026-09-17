# Run types

One input file carries `&GEO_OPT`, `&CELL_OPT` and `&MD` at once; CP2K reads
only the section matching `RUN_TYPE`. Switching run type is a one-character
edit in the parameter block.

## Contents

- [ENERGY / ENERGY_FORCE](#energy--energy_force)
- [GEO_OPT](#geo_opt)
- [CELL_OPT](#cell_opt)
- [MD](#md)
- [Metadynamics](#metadynamics)
- [VIBRATIONAL_ANALYSIS](#vibrational_analysis)
- [Thermodynamic integration with MIXED force evals](#thermodynamic-integration-with-mixed-force-evals)
- [Sweeps: equation of state and elastic constants](#sweeps-equation-of-state-and-elastic-constants)

---

## ENERGY / ENERGY_FORCE

Single-point. Used for adsorption/formation energy differences, for PDOS and
cube output, and as the cheapest way to verify an input runs at all before
committing to a long optimization.

Energy differences are only meaningful between runs sharing cutoff, basis set,
pseudopotentials, functional, dispersion, and cell. When building a set of
single-points for a difference, generate them from one template with only the
coordinates changing.

`ENERGY_FORCE` additionally prints forces — useful for checking that a geometry
you inherited really is relaxed, and for generating machine-learning training
data.

## GEO_OPT

Relaxes atomic positions at fixed cell.

```
&GEO_OPT
 OPTIMIZER BFGS
 MAX_ITER  1000
 MAX_DR    0.001      # bohr
 RMS_DR    0.0003
 MAX_FORCE 0.0001     # hartree/bohr
 RMS_FORCE 0.00003
&END GEO_OPT
```

All four criteria must be met at once. `MAX_FORCE` is nearly always the last to
fall, so a run that stalls is usually one stubborn atom rather than a global
problem — check the largest force in the final step's force table.

`OPTIMIZER BFGS` is the default choice and converges fastest for a few hundred
atoms. `LBFGS` uses less memory and is better above roughly a thousand atoms.
`CG` is slow but robust when BFGS oscillates.

**Fixing atoms** (holding slab bottom layers at bulk positions):

```
&MOTION
 &CONSTRAINT
  &FIXED_ATOMS
   COMPONENTS_TO_FIX XYZ
   LIST 1..216
  &END FIXED_ATOMS
 &END CONSTRAINT
```

Indices are 1-based and follow the coordinate file's order. Reordering the
`.xyz` silently invalidates the list, so sort the structure before writing the
constraint, not after. `COMPONENTS_TO_FIX Z` pins only the vertical coordinate,
letting atoms relax in-plane.

## CELL_OPT

Relaxes cell and positions together. Requires `STRESS_TENSOR ANALYTICAL` in
`&FORCE_EVAL` — without it CP2K has no stress to work with.

```
&CELL_OPT
 EXTERNAL_PRESSURE [bar] 1.0
 KEEP_ANGLES YES
 TYPE direct_cell_opt
 OPTIMIZER BFGS
 MAX_ITER 1000
 MAX_DR 0.001
 RMS_DR 0.0003
 MAX_FORCE 0.0001
 RMS_FORCE 0.00003
 PRESSURE_TOLERANCE [bar] 100.0
&END CELL_OPT
```

- `KEEP_ANGLES YES` preserves the lattice angles — essential for hexagonal or
  monoclinic systems that would otherwise drift to triclinic.
- `CONSTRAINT XY` / `YZ` / `Z` freezes chosen cell directions. For a slab with
  vacuum along c, `CONSTRAINT Z` keeps the vacuum thickness fixed while a and b
  relax; without it the optimizer will happily crush the vacuum.
- `TYPE direct_cell_opt` optimizes cell and atoms simultaneously (default and
  usually best). `geo_opt` alternates full relaxations with cell steps: slower
  but more stable for soft or layered systems.
- Cell optimization needs a **higher cutoff** than a fixed-cell run. The basis
  is attached to atoms while the grid is attached to the cell, so as the cell
  changes the basis set superposition error changes with it (Pulay stress). If
  the converged volume shifts when you raise the cutoff, the cutoff was too low.
  The cheap check is to restart a CELL_OPT from its own converged cell: it
  should barely move. If it does move, that is the cutoff talking, not the
  physics.

**Make sure you will actually get the cell history.** `&MOTION / &PRINT / &CELL`
must be `ON` — it is the block that writes `<project>-1.cell`, and MD templates
routinely carry it as `off`. The `&CELL` block inside `&CELL_OPT / &PRINT` is a
different section that only prints into `output.out`. The trajectory `.xyz`
stores no lattice, so without the `.cell` file the geometry at step *n* cannot
be reconstructed. See `input_anatomy.md`, "&PRINT: what you get, and how often".

If a run is already finished without it, the cell history is still in the log
(`grep "CELL| Vector" output.out`) and the final cell is in the `&SUBSYS &CELL`
block of `<project>-1.restart`.

## MD

```
&MD
 ENSEMBLE  NVT
 STEPS         ${MD_STEPS}
 TIMESTEP      0.5           # fs
 TEMPERATURE   300
 TEMP_KIND
 TEMP_TOL [K] 0.0
 COMVEL_TOL 1.0E-7
 DISPLACEMENT_TOL [angstrom] 0.2
 &THERMOSTAT
   TYPE CSVR
   REGION massive
   &CSVR
    TIMECON [fs] 100.0
   &END CSVR
 &END THERMOSTAT
&END MD
```

- `TIMESTEP 0.5` fs is right when hydrogen moves freely. 1.0 fs is safe only
  with constrained X–H bonds.
- `CSVR` (canonical sampling through velocity rescaling) is the default choice:
  it gives a correct canonical distribution without the ergodicity problems of
  Nosé–Hoover. `REGION massive` couples a thermostat to every degree of freedom,
  which equilibrates fast — useful at the start, though it damps real dynamics,
  so switch to `REGION GLOBAL` for production if transport properties matter.
- `TIMECON 100 fs` is a reasonable coupling; much shorter over-damps the
  dynamics, much longer fails to control temperature.
- `COMVEL_TOL` stops the "flying ice cube" drift where the cell slowly acquires
  net momentum.
- `ENSEMBLE NPT_F` (flexible cell) or `NPT_I` (isotropic) need a `&BAROSTAT` and
  `STRESS_TENSOR ANALYTICAL`. Equilibrate with NVT first; starting NPT from an
  unrelaxed structure produces violent cell oscillation.

Energy conservation is the health check. `MD| Energy drift per atom [K]` should
stay small; a steady climb means the timestep is too large or `EPS_SCF` too
loose. Because MD forces come from an SCF that is only converged to `EPS_SCF`,
loose SCF injects noise that accumulates as drift.

The `.ener` file is the fastest way to see this:

```bash
awk 'NR>1 {print $2, $4}' PROJECT-1.ener   # time [fs], temperature [K]
```

Compare the mean over the whole run with the mean over the second half — if
those differ much, the run has not equilibrated.

## Metadynamics

Sits inside `&MOTION` alongside `&MD`, driven by collective variables defined in
`&SUBSYS`.

```
&SUBSYS
 &COLVAR
  &COORDINATION
   ATOMS_FROM 453      # 1-based index into the coordinate file
   ATOMS_TO 286
   R0 [angstrom] 2.9
   NN 8
   ND 12               # NN < ND, both positive even integers
  &END COORDINATION
 &END COLVAR
&END SUBSYS

&MOTION
 &FREE_ENERGY
  &METADYN
   DO_HILLS .TRUE.
   NT_HILLS 60          # steps between hills
   WW 0.5E-3            # hill height in hartree
   &METAVAR
    COLVAR 1            # one METAVAR per COLVAR, in order
    SCALE 0.05          # hill width, in CV units
   &END METAVAR
   &PRINT
    &COLVAR
     COMMON_ITERATION_LEVELS 3
     &EACH
      MD 1
     &END EACH
    &END COLVAR
    &HILLS
     COMMON_ITERATION_LEVELS 3
     &EACH
      MD 1
     &END EACH
    &END HILLS
   &END PRINT
  &END METADYN
 &END FREE_ENERGY
&END MOTION
```

`SCALE` should be roughly a third of the CV's thermal fluctuation amplitude —
run plain MD first and measure it from the `COLVAR` output. Too wide and the
free energy surface is smeared; too narrow and filling takes forever.

The coordination number CV counts neighbours smoothly:
`sum over pairs (1-(r/R0)^NN)/(1-(r/R0)^ND)`. Every atom index is 1-based and
tied to the coordinate file's ordering.

## VIBRATIONAL_ANALYSIS

A top-level section, not part of `&MOTION`.

```
&VIBRATIONAL_ANALYSIS
 DX 0.01               # finite-difference step, bohr
 NPROC_REP 8           # MPI ranks per displaced replica
 TC_PRESSURE 101325    # Pa
 TC_TEMPERATURE 298.15 # K
 THERMOCHEMISTRY
 INTENSITIES T
 FULLY_PERIODIC F
 &PRINT
  &MOLDEN_VIB
  &END MOLDEN_VIB
 &END PRINT
&END VIBRATIONAL_ANALYSIS
```

- **The geometry must be tightly converged first.** Vibrational analysis of an
  unrelaxed structure yields imaginary frequencies that mean nothing. Tighten
  `MAX_FORCE` beyond the usual GEO_OPT target before starting.
- `NPROC_REP` sets the parallel decomposition: total ranks divided by
  `NPROC_REP` replicas run concurrently. Choose it so the division is exact,
  and set the job's rank count to match.
- Cost scales with the number of *moving* atoms — 6N displacements. Fixing the
  substrate with `&FIXED_ATOMS` is what makes surface vibrational analysis
  affordable, and is physically reasonable when the adsorbate modes are the
  target.
- `THERMOCHEMISTRY` output (ZPE, entropy, enthalpy) assumes an isolated
  molecule. For periodic systems the translational and rotational contributions
  are meaningless; use the ZPE and the vibrational term only.
- `FULLY_PERIODIC T` skips projecting out rotations, appropriate for bulk.
- A few small imaginary frequencies (under ~50i cm⁻¹) usually indicate a loose
  geometry or a floppy surface mode. A large one means a real saddle point.

## Thermodynamic integration with MIXED force evals

For alchemical transformations (swapping Fe for Al in a lattice site, say), CP2K
mixes two force evaluations linearly and you integrate dE/dλ over λ.

```
&MULTIPLE_FORCE_EVALS
 FORCE_EVAL_ORDER 1 2 3
 MULTIPLE_SUBSYS T
&END MULTIPLE_FORCE_EVALS

@SET METHOD MIXED
@SET fragment 0
@SET element_type 0
@include force_eval.inp

@SET METHOD QS
@SET fragment 1
@SET element_type 1
@include force_eval.inp

@SET METHOD QS
@SET fragment 2
@SET element_type 2
@include force_eval.inp
```

Force eval 1 is the MIXED driver; 2 and 3 are the physical end states. The
`@include`d fragment is written once and specialised by the variables set just
before each inclusion — which is the whole reason this stays maintainable.
Inside it:

```
&MIXED
 MIXING_TYPE LINEAR_COMBINATION
 &LINEAR
  LAMBDA ${val_lam}
 &END LINEAR
&END MIXED
```

The two end states must share atom *ordering* and count; only the kind labels
differ. Labelled kinds (`Bulk`, `Aqua`) map to different elements in each end
state via `ELEMENT`. Run a ladder of λ values in separate directories, each
with `@SET val_lam` set accordingly, then integrate the mean dE/dλ.

Restart files are per-fragment: `FILENAME RESTART-${fragment}` inside
`&SCF/&PRINT/&RESTART` keeps them from overwriting each other.

## Sweeps: equation of state and elastic constants

Bulk modulus and elastic constants come from many runs differing only in a
scaled cell. The workable layout is one directory per strain point, each a
complete run directory, named for the scale factor (`096a`, `098a`, `1005a`…).

Once they finish, collect the final energy and volume from each directory:

```bash
grep -h "ENERGY| Total FORCE_EVAL" */output.out
```

What you want per directory is `dir, energy_hartree, a, b, c, volume,
scf_not_converged, warnings` — energy against volume, ready for a Birch–Murnaghan
fit, with the status column flagging any point that failed and would otherwise
quietly distort the curve.

Check `status` and `scf_not_converged` before fitting. A single unconverged
point near the minimum will skew the bulk modulus badly, and it is far cheaper
to rerun it than to explain the anomaly later.
