#!/usr/bin/env bash
# Shallow-clone the prior-art reference repos into context/repos/.
# Re-runnable: existing clones are skipped. See context/notes/ for why each is here.
set -u
cd "$(dirname "$0")/repos" || exit 1

clone() { # clone <dir> <github owner/repo> [branch-or-tag]
  local dir=$1 repo=$2 ref=${3:-}
  [ -d "$dir/.git" ] && { echo "skip  $dir"; return; }
  if [ -n "$ref" ]; then
    git clone -q --depth 1 --branch "$ref" "https://github.com/$repo" "$dir"
  else
    git clone -q --depth 1 "https://github.com/$repo" "$dir"
  fi && echo "ok    $dir ($(git -C "$dir" log -1 --format=%cs))" || echo "FAIL  $dir"
}

# --- Agent-node workflow runners (closest prior art) ---
clone smithers-0.x        smithersai/smithers v0.35.0   # CLI-agent nodes, SQLite, rewind/fork/replay, MCP
clone smithers-main       smithersai/smithers           # 1.0-rc: digest-bound plan approval, append-only plan store
clone archon              coleam00/Archon               # YAML DAG, approval/wait nodes, resume w/ cached outputs
clone orc                 tjdals12/orc                  # claude -p / codex exec per node, frozen spec per run
clone yak                 lchase/yak                    # journal.jsonl as only state, file gates, replay --from
clone gh-aw               github/gh-aw                  # markdown workflows run by coding agents, MCP + audit
# --- Durable execution practice ---
clone dbos-transact-py    dbos-inc/dbos-transact-py     # step checkpoints, fork/rewind from a step
# --- Scientific workflows / HPC execution ---
clone jobflow             materialsproject/jobflow      # dynamic Response: replace / addition / detour
clone jobflow-remote      Matgenix/jobflow-remote       # durable remote/Slurm execution for jobflow
clone catgo               Hello-QM/catgo-LRG            # stateless HPC scanner, PENDING_REVIEW gate, MCP
clone aiida-core          aiidateam/aiida-core          # provenance graph, daemon, WorkChains
clone dflow               deepmodeling/dflow            # Argo-based scientific workflows
clone dpdispatcher        deepmodeling/dpdispatcher     # Slurm/PBS/LSF over SSH, used in materials community
clone psij-python         ExaWorks/psij-python          # portable job submission interface (ExaWorks)
clone snakemake-slurm     snakemake/snakemake-executor-plugin-slurm  # mature Slurm status polling
# --- Agent harness ---
clone pi                  earendil-works/pi             # pi agent harness (Eleforge's default brain)
