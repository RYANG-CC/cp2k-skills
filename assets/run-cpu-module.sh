#!/bin/bash
#SBATCH --job-name=<JOBNAME>
#SBATCH --partition=<PARTITION>
#SBATCH --nodes=4
#SBATCH --ntasks-per-node=32
#SBATCH --cpus-per-task=4
#SBATCH --time=24:00:00
#SBATCH --mem-per-cpu=2G
#SBATCH --dependency=singleton    # queue reruns of the same job name behind each other

module purge
module load CP2K/<VERSION>        # site-specific module name

export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK

ulimit -s unlimited

srun cp2k.psmp -i cp2k.inp -o output.out
