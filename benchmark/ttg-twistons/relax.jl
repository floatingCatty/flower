# Structural relaxation of twisted trilayer graphene with the authors' configuration-space code
# (Zhu, Cazeaux, Luskin & Kaxiras, PRB 101, 224107; Dataverse files Trilayers.jl, RealSpace.jl,
# GrapheneParameters.jl). This driver follows their example.jl step by step; what changed:
#   * parameters from the command line (twists, N, GSFE amplification, real-space window);
#   * outputs as compact binary maps (Float32) + JSON metadata instead of multi-100 MB text files.
#
# julia relax.jl THETA_TM THETA_BM N [AMP=10] [SCALE=6] [NPIX=300] [WORKERS=4]
# (layer 1 = top, rotated by THETA_TM; layer 2 = middle, fixed; layer 3 = bottom, rotated by THETA_BM)

using Distributed
using TimerOutputs
using FFTW
using LinearAlgebra
using Optim
using LineSearches
using Printf

θTM = parse(Float64, ARGS[1]); θBM = parse(Float64, ARGS[2]); N = parse(Int, ARGS[3])
amp_in = length(ARGS) >= 4 ? parse(Float64, ARGS[4]) : 10.0
wscale = length(ARGS) >= 5 ? parse(Float64, ARGS[5]) : 6.0
npix = length(ARGS) >= 6 ? parse(Int, ARGS[6]) : 300
nw = length(ARGS) >= 7 ? parse(Int, ARGS[7]) : 4

include("RealSpace.jl")
global to = TimerOutput()
FFTW.set_num_threads(Threads.nthreads())
BLAS.set_num_threads(max(1, Threads.nthreads()))
pcs = nw > 0 ? addprocs(nw; exeflags = "--project=$(Base.active_project())") : Int[]
@everywhere include("GrapheneParameters.jl")
@everywhere global amp = $amp_in            # GSFE amplification (the authors' runs: 10, file data_amp/)
include("Trilayers.jl")

θ1 = deg2rad(θTM); θ3 = deg2rad(θBM)
tlg = Trilayer(l*E0, l*P0, θ1, θ3, K, G)
hull = Hull(tlg, N)
hN = hull.hN

fg!(F, Gr, u) = EnergyGradient!(F, Gr, u, hull)
guess = zeros(ComplexF64, 2, hN, N, N, N, 3)
method = LBFGS(; m = 5, P = hull.Precon_Elastic, scaleinvH0 = false)
options = Optim.Options(iterations = 10000, x_tol = 1e-4, f_tol = 1e-11, g_tol = 1e-8, allow_f_increases = true,
                        show_trace = true, store_trace = false, show_every = 25)
t0 = time()
results = optimize(Optim.only_fg!(fg!), guess, method, options)
t_opt = time() - t0
display(results)

# --- as in example.jl: symmetrise, gradient, save, read back as real arrays
begin
  u = similar(results.minimizer)
  u = copyto!(u, results.minimizer)
  ur = hull.iplan * u
  Ur = similar(ur)
  Ur[1,:,:,:,:,1] =   permutedims(ur[1,:,:,:,:,3], [2,1,4,3])
  Ur[1,:,:,:,:,3] =   permutedims(ur[1,:,:,:,:,1], [2,1,4,3])
  Ur[1,:,:,:,:,2] =   permutedims(ur[1,:,:,:,:,2], [4,3,2,1])
  Ur[2,:,:,:,:,1] = - permutedims(ur[2,:,:,:,:,3], [2,1,4,3])
  Ur[2,:,:,:,:,3] = - permutedims(ur[2,:,:,:,:,1], [2,1,4,3])
  Ur[2,:,:,:,:,2] = - permutedims(ur[2,:,:,:,:,2], [4,3,2,1])
  U = hull.plan * Ur
  u = hull.plan * ur
end
∇ur = ∇(u, hull)
mkpath("data")
system = "triG"
tag = @sprintf "%.2f_%.2f_%d.jld" rad2deg(θ1) rad2deg(θ3) N
write("data/$(system)_data_$tag", N, θ1, θ3, tlg.E, tlg.P, K, G)
write("data/$(system)_minimizer_$tag", u, results.minimum)
write("data/$(system)_gradient_$tag", ∇ur[1,1], ∇ur[1,2], ∇ur[2,1], ∇ur[2,2])
tlg, u, ∇u = Read(N, rad2deg(θ1), rad2deg(θ3), "/data/", system)

# --- misfit (stacking) energy per bilayer in configuration space, as in example.jl
hull = Hull(tlg, N)
Γ0 = Array(hull.Γ0)
permutations = Array(hull.permutations)
invE = hull.tl.invtE
u_tmp = reshape(u, (2, N^4, 3))
function misfit(a, b, pcol, eidx)
    shifts = zeros(Float64, (2, N^4))
    uj = u_tmp[:, :, a]; u2 = u_tmp[:, :, b]; perm = permutations[:, pcol]
    m = zeros(Float64, N^4)
    for i in 1:2
        iE = invE[eidx[i]]
        shifts[1,:] = Γ0[1,:] + iE[1,1]*(u2[1, perm] - uj[1,:]) + iE[1,2]*(u2[2, perm] - uj[2,:])
        shifts[2,:] = Γ0[2,:] + iE[2,1]*(u2[1, perm] - uj[1,:]) + iE[2,2]*(u2[2, perm] - uj[2,:])
        m .+= 0.5 .* GSFE(shifts)
    end
    m
end
m12 = misfit(1, 2, 1, (1, 2)); m23 = misfit(2, 3, 2, (2, 3))
mis = zeros((N, N, N, N, 3))
mis[:,:,:,:,1] = reshape(m12, (N, N, N, N)); mis[:,:,:,:,3] = reshape(m23, (N, N, N, N))
itp_mis = Fields([mis], N)
itp_∇u = Fields(∇u, N)

# --- real-space window around the origin: scale x the moire period of the smaller twist
E = abs(tlg.θ[1]) < abs(tlg.θ[3]) ? inv(inv(tlg.tE[1]) - inv(tlg.tE[2])) : inv(inv(tlg.tE[3]) - inv(tlg.tE[2]))
L = wscale * maximum(abs.(E))
X = range(-L, L, length = npix); Y = range(-L, L, length = npix)
Grid = cat(reshape(collect(X), (1, npix, 1)) .* ones(1, 1, npix), ones(1, npix, 1) .* reshape(collect(Y), (1, 1, npix)), dims = 1)
Grid = reshape(Grid, (2, npix^2))
ω = Configuration([0.0; 0.0], [0.0; 0.0], [0.0; 0.0], tlg)
Em = interpolateFields(Grid, itp_mis, ω, N, tlg)
∇U = interpolateFields(Grid, itp_∇u, ω, N, tlg)
curl = [∇U[2,1,k] .- ∇U[1,2,k] for k in 1:3]     # example.jl: grad_k = ∇U[2,1,k] - ∇U[1,2,k]

function wbin(name, a)
    open(name, "w") do f
        write(f, Float32.(a))
    end
end
wbin("misfit12.f32", Em[1]); wbin("misfit23.f32", Em[3])
wbin("curl1.f32", curl[1]); wbin("curl2.f32", curl[2]); wbin("curl3.f32", curl[3])
open("relax_meta.json", "w") do f
    write(f, @sprintf("{\"theta_TM\": %.6f, \"theta_BM\": %.6f, \"N\": %d, \"amp\": %.6f, \"scale\": %.4f, \"npix\": %d, \"L_angstrom\": %.6f, \"x0\": %.6f, \"x1\": %.6f, \"order\": \"x fastest (index = ix + npix*iy)\", \"energy_min\": %.12e, \"iterations\": %d, \"converged\": %s, \"seconds_opt\": %.1f, \"workers\": %d, \"threads\": %d}\n",
        θTM, θBM, N, amp_in, wscale, npix, L, first(X), last(X), results.minimum, Optim.iterations(results),
        Optim.converged(results) ? "true" : "false", t_opt, nw, Threads.nthreads()))
end
nw > 0 && rmprocs(pcs)
println("done")
