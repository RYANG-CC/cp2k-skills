# Submitting a CP2K job

This skill is about the input file, not about any particular machine. What
follows is the part of job submission that changes what you write *in the
input*, which is why it belongs here at all. Everything else — partitions,
accounts, module names, software environments — is site-specific: take it from
your own site's documentation.

## WALLTIME: the one setting worth never forgetting

```
&GLOBAL
 WALLTIME 85000        # seconds; scheduler limit here is 24:00:00 = 86400
 FLUSH_SHOULD_FLUSH T
&END GLOBAL
```

CP2K watches its own elapsed time and stops cleanly as it approaches
`WALLTIME`, writing a `.restart` you can resume from. Without it the scheduler
kills the process mid-step, and the newest restart file may be truncated —
which is the difference between resuming and starting over.

Leave a real margin below the scheduler's limit: a few hundred to a couple of
thousand seconds, enough for one more optimization step or MD block to finish
and be written to disk. `FLUSH_SHOULD_FLUSH T` pushes output to disk as it is
produced, so even a killed job leaves a readable log.

`cp2k_doctor.py` reads `--time` out of a job script sitting in the run
directory and warns when `WALLTIME` is not below it.

## Ranks, threads, and node count

CP2K is a hybrid MPI+OpenMP code and the split matters:

- Total ranks = nodes × ranks-per-node. Threads per rank is whatever your
  scheduler calls `cpus-per-task`. Their product should equal the cores per
  node.
- More ranks and fewer threads generally wins for large systems; more threads
  helps when memory per rank is tight.
- The DBCSR sparse multiply prefers rank counts with clean factorisations —
  powers of two, or products of small primes.
- For `VIBRATIONAL_ANALYSIS`, total ranks must be divisible by `NPROC_REP`,
  since each replica is given exactly that many.

Doubling the node count rarely halves the time. Benchmark a short run before
committing a long one to a new node count.

## Before submitting

```bash
python scripts/cp2k_doctor.py .
```

The files that must be in the run directory are listed in `input_anatomy.md`,
and restarting a chained run is covered there too, under "The restart switch".

`assets/run-cpu-module.sh` is a skeleton job script with every site-specific
value left as a placeholder. It is a starting point, not something to submit
as-is.
