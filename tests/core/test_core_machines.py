"""Machines: one file per person, filled by probing; plans name a machine; protocols map theirs."""
import json

import yaml

from flower import envs, machines
from flower.engine import Engine
from flower.rundir import RunPaths

from test_core_cli import write_plan

SLURM = """user=me
home=/home/me
os=Linux x86_64
cores=8
mem_kb=16000000
gpus=0
scratch=/scratch/me
tool=sbatch
tool=module
tool=apptainer
free_home_gb=40
partition=cpu*|7-00:00:00|64|249000|(null)|900
partition=gpu|2-00:00:00|48|498000|gpu:a100:4|60
account=def-xyz
"""


def test_a_slurm_login_node_is_probed_as_a_cluster():
    p = machines.parse_probe(SLURM)
    assert p["scheduler"] == "slurm" and p["accounts"] == ["def-xyz"]
    assert p["partitions"][0] == {"name": "cpu", "default": True, "max_time": "7-00:00:00", "cores_per_node": 64,
                                  "mem_mb_per_node": 249000, "gres": None, "nodes": 900}
    assert p["partitions"][1]["gres"] == "gpu:a100:4"
    assert p["work_dir"] == "/scratch/me/flower-runs" and p["env_dir"] == "/scratch/me/flower-envs"
    assert "cores" not in p                      # the login node's size says nothing about the jobs


def test_a_workstation_is_probed_for_its_size():
    p = machines.parse_probe("cores=32\nmem_kb=128000000\ngpus=2\nload=1.0 2.0 3.0\ntool=rsync\n")
    assert p["scheduler"] == "none" and p["cores"] == 32 and p["memory_gb"] == 122 and p["gpus"] == 2
    assert p["work_dir"] == "~/flower-runs" and p["env_dir"] == "~/.flower/envs"


def test_the_persons_keys_win_and_become_the_cluster():
    entry = {"ssh": "me@hpc.example.org:2222", "identity_file": "~/.ssh/id_x", "agent_may_use": {"cores": 16},
             "work_dir": "/data/me/runs", "probed": machines.parse_probe("cores=32\nmem_kb=1\n")}
    c = machines.cluster_spec("box", entry)
    assert c["machine"] == "box" and c["transport"] == "ssh" and c["host"] == "me@hpc.example.org"
    assert c["ssh_options"][:2] == ["-p", "2222"] and "IdentitiesOnly=yes" in c["ssh_options"]
    assert c["ssh_options"][-1].startswith("ControlPath=")      # a `flower remote login` connection is reused
    assert c["remote_root"] == "/data/me/runs" and c["cpus"] == 16 and c["scheduler"] == "none"
    s = machines.cluster_spec("hpc", {"ssh": "hpc", "account": "def-xyz", "probed": machines.parse_probe(SLURM)})
    assert s["scheduler"] == "slurm" and s["resources"] == {"account": "def-xyz"} and "cpus" not in s


def test_env_dir_moves_the_installs_and_leaves_the_default_unchanged():
    assert envs.prefix("qe", "sha256:" + "a" * 64) == "$HOME/.flower/envs/qe-aaaaaaaaaaaa"
    assert envs.prefix("qe", "sha256:" + "a" * 64, "~/.flower/envs") == "$HOME/.flower/envs/qe-aaaaaaaaaaaa"
    assert envs.prefix("qe", "sha256:" + "a" * 64, "/scratch/me/flower-envs/") == "/scratch/me/flower-envs/qe-aaaaaaaaaaaa"


def test_remote_add_list_and_a_plan_that_names_the_machine(cli, home, tmp_path):
    code, out = cli("remote", "add", "here", "local", "--cores", "2", "--note", "this machine")
    assert code == 0 and out["data"]["machine"]["scheduler"] == "none", out
    data = yaml.safe_load((tmp_path / "machines.yaml").read_text())
    data["here"]["work_dir"] = str(tmp_path / "work")             # keep the test's jobs out of the real home
    (tmp_path / "machines.yaml").write_text(yaml.safe_dump(data))
    code, out = cli("remote", "add", "here", "local")
    assert code == 2 and out["error"]["code"] == "exists"
    code, out = cli("remote", "list")
    m = out["data"]["machines"]["here"]
    assert m["agent_may_use"] == {"cores": 2} and m["note"] == "this machine" and m["cores"] >= 1
    p = write_plan(tmp_path, [{"id": "a", "cluster": "here", "run": 'echo "{\\"n\\": 3}"', "outputs": {"n": "integer"}}])
    code, out = cli("run", str(p), "--follow", "--timeout", "60")
    assert code == 0, out
    st = Engine(RunPaths(home, out["data"]["run_id"])).state()
    assert st.nodes["a"].result.outputs["n"] == 3
    assert st.plan["clusters"]["here"]["machine"] == "here" and st.plan["clusters"]["here"]["cpus"] == 2


def test_a_protocols_cluster_runs_on_my_machine(cli, home, tmp_path):
    (tmp_path / "machines.yaml").write_text(yaml.safe_dump(
        {"mine": {"ssh": "local", "work_dir": str(tmp_path / "work"), "probed": {"scheduler": "none"}}}))
    p = write_plan(tmp_path, [{"id": "a", "cluster": "remote", "run": 'echo "{\\"ok\\": true}"'}],
                   inputs={"host": {"type": "string", "required": True}},
                   clusters={"remote": {"transport": "ssh", "host": "${inputs.host}", "scheduler": "none"}})
    code, out = cli("run", str(p), "--machine", "remote=mine", "--follow", "--timeout", "60")
    assert code == 0, out                                       # the host input went with the protocol's cluster
    st = Engine(RunPaths(home, out["data"]["run_id"])).state()
    assert st.node_spec("a")["cluster"] == "mine" and "remote" not in st.plan["clusters"]
    code, out = cli("run", str(p), "--machine", "remote=nosuch")
    assert code == 2 and "no machine" in out["error"]["message"]


def test_an_unknown_machine_says_how_to_add_one(cli, tmp_path):
    p = write_plan(tmp_path, [{"id": "a", "cluster": "narval", "run": "true"}])
    code, out = cli("plan", "validate", str(p))
    assert code == 2 and "flower remote add" in json.dumps(out)


def test_an_unreachable_machine_is_kept_for_login(cli, tmp_path):
    code, out = cli("remote", "add", "far", "nobody@localhost:1")
    assert code == 2 and out["error"]["code"] == "unreachable" and "saved" in out["error"]["message"]
    code, out = cli("remote", "list")
    assert "far" in out["data"]["machines"] and "not probed yet" in out["message"]
