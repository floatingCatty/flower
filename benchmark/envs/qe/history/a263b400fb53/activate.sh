export PATH="$FLOWER_ENV_PREFIX/bin:$PATH"
# QE here is built with OpenMP: unset, it starts one thread per core, which hangs or crawls on large hosts
# (ld1.x spun for 10+ min with 192 threads on a 96-core machine). Steps that want threads set it themselves.
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
