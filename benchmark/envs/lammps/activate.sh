export PATH="$FLOWER_ENV_PREFIX/bin:$PATH"
# one thread per MPI rank: steps choose their rank count with mpirun -np
export OMP_NUM_THREADS=1
