export PATH="$FLOWER_ENV_PREFIX/bin:$PATH"
# numba/numpy thread pools default to all cores: steps set NUMBA_NUM_THREADS / OMP_NUM_THREADS themselves
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export NUMBA_NUM_THREADS="${NUMBA_NUM_THREADS:-1}"
