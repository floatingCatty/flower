# julia from this environment, the pinned packages load, a tiny Optim/FFTW problem solves.
set -e
[ "$(command -v julia)" = "$FLOWER_ENV_PREFIX/bin/julia" ]
julia -e '
using FFTW, Optim, LineSearches, Interpolations, TimerOutputs, Pkg
r = optimize(x -> sum((x .- 1).^2), zeros(3), LBFGS())
@assert Optim.converged(r) && maximum(abs.(Optim.minimizer(r) .- 1)) < 1e-6
@assert abs(real(fft([1.0, 0, 0, 0])[2]) - 1) < 1e-12
v = Dict(p.name => string(p.version) for p in values(Pkg.dependencies()) if p.is_direct_dep)
println("julia ", VERSION, " ", join(sort(["$k $(v[k])" for k in keys(v)]), ", "), "; LBFGS + FFT OK")'
