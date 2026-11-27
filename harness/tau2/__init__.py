"""τ²-bench multi-turn test rig for the DSH SAC plugin (see docs/TEST-PLAN-TAU2-MULTITURN.md).

Modules
-------
dsh_agent.py          the ``dsh_agent`` τ² agent: one DSH headless turn per τ² agent turn + the local tool bridge
run.py                the official ``tau2`` CLI with ``dsh_agent`` registered (``python -m harness.tau2.run run …``)
make_profiles.py      writes the DSH headless profiles (tau2-sac / tau2-fha / …) into the workspace-local DSH home
envinfo.py            offline inventory: tasks per split, tools, policy sizes
selftest_offline.py   offline dry run of the whole pipeline with stubs (no real model is called)
analyze.py            post-run analysis: official rewards + SAC-specific checks from the DSH session logs
"""
