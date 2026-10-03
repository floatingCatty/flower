export PATH="$FLOWER_ENV_PREFIX/bin:$PATH"
# OpenMM's CPU platform uses all cores by default: steps set OPENMM_CPU_THREADS themselves
export OPENMM_CPU_THREADS="${OPENMM_CPU_THREADS:-4}"
