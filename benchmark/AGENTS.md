<!-- flower:begin (managed by `flower init`; edit outside these markers) -->
## Working in this project: flower

Multi-step or long work here is done **inside a flower run**, so it is recorded, resumable and visible in
the project's UI (`flower ui`). The full guide is in `.claude/skills/flower/SKILL.md`
(`.agents/skills/flower/SKILL.md`).

1. **Start the run first**, before exploring: `flower start "<goal>"` (prints RUN). Nothing is "too early".
2. **Every computation is a step**, including the first quick test:
   `flower add RUN ID --description "<what it establishes, how to read the result>" --follow -- <command>`
   (remote: `--cluster C --env E --stage-in FILE`). It writes the step into the plan file and runs it; the
   command prints its outputs as a JSON object on its last line. To fix a step: edit its code or the plan file,
   `flower rerun RUN ID --follow`.
3. **Machines** the user set up are in `flower remote list --json` (cores or Slurm partitions, accounts,
   `agent_may_use`, notes): run steps there with `--cluster NAME` instead of asking for hosts. Explore one with
   `flower remote exec --cluster NAME --run RUN -- <cmd>` (logged), not raw ssh.
4. Reading files, papers and results directly is fine; *running* things beside the run is not, including a
   quick check whose answer you rely on (make it a one-line step).
5. When it is done, `flower export RUN STEP` turns the steps behind STEP into a protocol that anyone re-runs
   with `flower run protocol.yaml --follow` and checks with `flower compare RUN expected.json`.

If `FLOWER_INSIDE_RUN` is set you are inside a step: do its task and never call flower.
<!-- flower:end -->
