export PATH="$FLOWER_ENV_PREFIX/bin:$PATH"
# numpy/scipy/QuSpin thread pools default to all cores: steps set OMP_NUM_THREADS themselves
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"
