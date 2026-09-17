# Troubleshooting CP2K runs

Start with `python scripts/cp2k_doctor.py <rundir>` — it covers the mechanical
failures below without reading a line of output. What follows is for what it
cannot decide for you.

## Contents

- [The job dies immediately](#the-job-dies-immediately)
- [SCF does not converge](#scf-does-not-converge)
- [SCF converges but very slowly](#scf-converges-but-very-slowly)
- [Geometry optimization will not converge](#geometry-optimization-will-not-converge)
- [Cell optimization drifts or collapses](#cell-optimization-drifts-or-collapses)
- [MD energy drift](#md-energy-drift)
- [The job was killed](#the-job-was-killed)
- [Out of memory](#out-of-memory)
- [Results look wrong rather than broken](#results-look-wrong-rather-than-broken)

---

## The job dies immediately

Seconds-long jobs are nearly always missing files or a malformed input. Check
the Slurm log first (`slurm-*.out`), then `output.out` — CP2K's own error goes
to the output file, while MPI and module errors go to the Slurm log.

| Symptom | Cause |
|---|---|
| `Cannot open file BASIS_SET_MOLOPT` | Basis/potential/`dftd3.dat` not copied into the run directory. |
| `Kind <X> not found` / missing element | No `&KIND` for an element present in the `.xyz`. |
| `Unknown keyword` / `Invalid section` | Typo, or a keyword from a different CP2K version. Check it against the CP2K version you are actually running. |
| `The requested basis set <name> is not found` | Name mismatch with `BASIS_SET_MOLOPT`, or the basis exists only for other elements. |
| `unexpected end of file` in the `.xyz` | Header count does not match the number of coordinate lines. |
| Immediate MPI abort, no CP2K banner | Wrong module or software environment loaded, or a launcher wrapper script that is not executable (`chmod +x`). |

## SCF does not converge

`*** SCF run NOT converged ***` in a geometry optimization is not always fatal —
CP2K carries on with a bad gradient, so the optimization wanders and wastes
hours. Treat repeated failures as a stop condition.

Work through these roughly in order of cost:

**1. Preconditioner.** `FULL_ALL` is the strongest and handles small or vanishing
HOMO-LUMO gaps; `FULL_SINGLE_INVERSE` is cheaper and usually enough. Metals and
narrow-gap oxides generally need `FULL_ALL`.

```
&OT
 MINIMIZER CG
 PRECONDITIONER FULL_ALL
&END OT
```

**2. Minimizer.** `CG` is robust. `DIIS` is faster when it works and diverges
when it does not. If `DIIS` oscillates, switch to `CG`. Adding
`LINESEARCH 2PNT` helps `CG` on rough surfaces.

**3. Outer SCF.** With OT, the inner loop optimizes orbitals at fixed occupation
and the outer loop handles the rest. Both need enough iterations:

```
&SCF
 MAX_SCF 128
 EPS_SCF 1.0E-6
 &OUTER_SCF on
  EPS_SCF 1.0E-6      # must match the inner EPS_SCF
  MAX_SCF 10
 &END OUTER_SCF
&END SCF
```

A mismatch between inner and outer `EPS_SCF` makes the outer loop chase a target
the inner loop is not aiming at.

**4. Starting guess.** `SCF_GUESS ATOMIC` is the default for a fresh run.
`RESTART` reuses a `.wfn` and is much faster — but restarting from a wavefunction
computed for a *different* spin state or geometry can trap the SCF in the wrong
solution. When results look strange after a restart, rerun once from `ATOMIC` to
check.

**5. Spin state.** For transition metals this is the usual culprit. The atomic
guess picks a spin arrangement and OT then refuses to leave it. Set `&BS` blocks
explicitly (see `input_anatomy.md`), and set `MULTIPLICITY` when you know the
total. An antiferromagnetic structure will not emerge on its own — it has to be
imposed through labelled kinds with mirrored alpha/beta occupations.

**6. Diagonalization instead of OT.** For genuinely metallic systems OT struggles
because there is no gap. Smearing with traditional diagonalization is more
appropriate:

```
&SCF
 &DIAGONALIZATION on
  ALGORITHM STANDARD
 &END DIAGONALIZATION
 &MIXING on
  METHOD BROYDEN_MIXING
  ALPHA 0.2
  BETA 1.5
  NBUFFER 8
 &END MIXING
 ADDED_MOS 100
 &SMEAR on
  METHOD FERMI_DIRAC
  ELECTRONIC_TEMPERATURE [K] 300
 &END SMEAR
&END SCF
```

Lower `ALPHA` (0.1–0.2) mixes more conservatively and helps charge sloshing in
large or ionic cells. `ADDED_MOS` must be generous enough to cover the smearing
window.

**7. The geometry itself.** Atoms far too close together produce an SCF that
cannot converge at any setting. If the failure is at step 1 of a fresh run,
inspect the structure before touching SCF parameters — overlapping atoms from a
bad supercell build or a botched adsorbate placement are common.

## SCF converges but very slowly

Mean SCF steps above ~40 is worth attention: it
multiplies the cost of every geometry step.

- Tighten `EPS_DEFAULT` to `1.0E-12`; a loose default makes the SCF chase noise.
- Enable extrapolation between steps — this is the single biggest win in MD and
  optimization, since each step starts near the previous solution:
  ```
  &QS
   EXTRAPOLATION ASPC
   EXTRAPOLATION_ORDER 3
  &END QS
  ```
- Check the cutoff. Too low a cutoff makes the energy surface rough and the SCF
  works harder for it.
- Loosen `EPS_SCF` to `1.0E-6` if it is tighter. Geometry optimization does not
  need `1.0E-8`; MD does need at least `1.0E-6` to keep drift down.

## Geometry optimization will not converge

Look at which criterion is outstanding — the optimizer prints all four with
their flags. Usually only `MAX_FORCE` is left.

- **One stubborn atom.** A single hydrogen rattling between two minima, or an
  adsorbate hunting for its site. The forces table at the last step identifies
  it. Sometimes the honest fix is a better starting geometry.
- **Fighting constraints.** Fixed atoms adjacent to relaxing ones create forces
  that can never go to zero. Widen the fixed region's boundary, or accept a
  looser `MAX_FORCE`.
- **Optimizer oscillating.** BFGS building a bad Hessian shows up as energy
  going up and down. `LBFGS` or `CG` is more forgiving.
- **SCF noise.** If SCF is failing intermittently, forces are unreliable and the
  optimizer cannot converge against them. Fix SCF first — this is the most
  commonly missed cause.
- **It has already converged in practice.** Energy flat to 1e-6 Ha over the last
  twenty steps with `MAX_FORCE` at 1.2e-4 against a 1e-4 target is a converged
  structure for most purposes. Say so rather than burning another day.

Restarting continues from the best geometry so far:

```
@SET restart yes
```

## Cell optimization drifts or collapses

- **Vacuum being crushed.** A slab with vacuum along c will have the optimizer
  reduce c, since vacuum costs energy nothing but contributes to pressure. Use
  `CONSTRAINT Z`.
- **Angles drifting.** `KEEP_ANGLES YES` for anything with symmetry worth
  preserving.
- **Volume that shifts with cutoff.** Pulay stress from an incomplete basis. Raise
  the cutoff and check the converged volume stops moving. Cell optimization needs
  a higher cutoff than fixed-cell work.
- **Pressure will not converge** while forces do: `PRESSURE_TOLERANCE 100 bar` is
  a reasonable target; 10 bar is very tight for a DFT stress tensor.

## MD energy drift

`MD| Energy drift per atom [K]` climbing steadily means energy is not conserved.

| Cause | Fix |
|---|---|
| Timestep too large | 0.5 fs with free hydrogen; 1.0 fs only with constrained X–H. |
| `EPS_SCF` too loose | Tighten to 1e-6 or below; loose SCF injects force noise. |
| Cutoff too low | Rough energy surface. Raise `CUTOFF`. |
| No wavefunction extrapolation | Add `EXTRAPOLATION ASPC`. |
| Thermostat masking it | A `massive` thermostat absorbs drift and hides the problem. Check drift in an NVE segment if it matters. |

Some drift is normal in Born–Oppenheimer MD. A few K/atom over tens of
picoseconds is usually acceptable; a steady climb of hundreds is not.

## The job was killed

No `PROGRAM ENDED AT` line in the output means CP2K did not finish. Distinguish:

- **Walltime.** The Slurm log says `DUE TO TIME LIMIT`. If `&GLOBAL WALLTIME` was
  set below the Slurm limit, CP2K stopped itself cleanly and wrote a restart —
  just set `restart yes` and resubmit. If it was not set, the last
  `RESTART_HISTORY` dump is the fallback.
- **Node failure or OOM.** The Slurm log names it. See below.
- **Still running.** Check `squeue` before concluding anything.

This is exactly why `WALLTIME` belongs a few hundred seconds under the Slurm
limit: the difference between resuming cleanly and losing a day.

## Out of memory

- Reduce ranks per node and raise `--cpus-per-task`, so each rank has more
  memory. Total cores stay the same.
- Use more nodes for the same rank count.
- `LBFGS` instead of `BFGS` avoids storing a dense Hessian.
- Turn off cube output (`&E_DENSITY_CUBE`, `&MO_CUBES WRITE_CUBE`) — cube files
  for a large cell run to hundreds of MB each and are written from one rank.
- Check `OPT| Estimated peak process memory` in the output against what the
  partition allows per rank.

## Charge and multiplicity

`CHARGE` and `MULTIPLICITY` must be parity-compatible. With `N` electrons and
multiplicity `M`, the alpha count is `(N + M - 1) / 2`, which has to be a whole
number: a singlet needs an even `N`, a doublet an odd one. If the parity is
wrong the input is internally inconsistent and one of the two values is not what
you meant — fix it rather than relying on whatever CP2K decides to do with it.

```bash
python scripts/cp2k_structure.py structure.xyz --input cp2k.inp
```

Three independent numbers to reconcile:

1. **Electron count** — sums the `GTH-*-qN` valence electrons over the
   composition and subtracts `CHARGE`. Pure arithmetic; compare it against
   `Number of electrons` in the output (per spin channel, so alpha + beta).
2. **Formal charge** — sums assumed oxidation states. Main-group states are
   safe; O–O contacts are detected so peroxo/hydroperoxo oxygens count as −1
   rather than −2, which is what makes an OOH adsorbate come out at −1 instead
   of −3.
3. **`CHARGE` in the input** — what you asked for.

Transition metals are where the formal charge gets unreliable, so the assumed
state is always printed with its alternatives rather than folded into a total.
When the total misses by exactly (metal count × an integer), that metal is
probably in a different state: NiOOH assuming Ni(II) is off by exactly 32 for
32 Ni atoms, and Ni(III) closes it exactly — which is the correct chemistry.
An exact fit is strong evidence, not proof.

## The local input is not the one that ran

A run edited directly on the cluster leaves the local copy stale, and every
input later derived from it silently inherits the wrong settings. This is easy
to mistake for a CP2K bug, because the output plainly disagrees with the input
sitting next to it.

CP2K echoes its resolved settings into the output header:

```
 DFT| Multiplicity                                                             1
 DFT| Charge                                                                  -1
 QS| Density cutoff [a.u.]:                                                400.0
 GLOBAL| Run type                                                        GEO_OPT
 GLOBAL| Coordinate file name                                     structure.xyz
```

`cp2k_doctor.py` compares these against the input and reports any mismatch as an
error. Note the density cutoff is echoed in hartree while `&MGRID CUTOFF` is in
Ry, so the echoed number is half the input value.

If they disagree, trust the output — it records what actually ran — and sync the
cluster copy back before reusing the directory as a template.

## Results look wrong rather than broken

The dangerous failures are the ones that converge cleanly to the wrong answer.

- **Wrong valence.** A `GTH-PBE-qN` whose N does not match the basis set gives a
  smooth, converged, meaningless result. Check `qN` against the pseudopotential file you are actually using.
- **Charge set by inference rather than intent.** See the section above.
- **Overlapping atoms.** A structure with atoms far too close will not converge
  at any SCF setting. `cp2k_structure.py` reports the closest contacts.
- **Wrong spin state.** Verify the total moment in the output matches intent, and
  check Mulliken populations per site for antiferromagnetic setups.
- **Energies compared across different settings.** Cutoff, basis, dispersion and
  cell must all match for a difference to mean anything. `--collect` puts the
  runs side by side so mismatches are visible.
- **`MULTIPLE_UNIT_CELL` mismatch** between `&CELL` and `&TOPOLOGY`: the cell and
  the coordinates get replicated differently. `cp2k_doctor.py` flags this.
- **Restart that rewound.** With `restart yes`, `&TOPOLOGY` must not still be
  reading the original `.xyz`, or the run silently restarts from the initial
  geometry with the restart file's step counter.
- **Dispersion reference functional** not matching the actual functional.
