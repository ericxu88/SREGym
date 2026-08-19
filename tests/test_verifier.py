"""Step 4: the verifier separates unfixed / fixed / symptom-masked / collateral-damage worlds."""
from __future__ import annotations

import sqlite3

from sregym import util
from sregym.verifier.verify import compute_reward, verify


def _fix_env(world):
    params = world.extra["fault_params"]
    text = world.env_file.read_text()
    if params["kind"] == "key":
        text = text.replace(f"{params['bad_key']}=", f"{params['target']}=")
    else:
        text = text.replace(f"{params['target']}={params['bad_value']}", f"{params['target']}={world.base_env[params['target']]}")
    world.env_file.write_text(text)


def _flags(res):
    return res.symptom_resolved, res.root_cause_fixed, res.no_collateral_damage


def test_a_unfixed_world(running):
    world, spec, sm = running
    res = verify(world, spec)
    assert _flags(res) == (False, False, True), res.summary()
    assert res.reward == 0.0 and not res.success


def test_b_correctly_fixed_world(running):
    world, spec, sm = running
    _fix_env(world)
    sm.restart()
    res = verify(world, spec)
    assert _flags(res) == (True, True, True), res.summary()
    assert res.reward == 1.0 and res.success
    # reverting the deploy commit is an equally valid fix
    world.git("checkout", "--", ".env")  # back to the broken deployed state
    ident = {"GIT_AUTHOR_NAME": "oncall", "GIT_AUTHOR_EMAIL": "oncall@example.com",
             "GIT_COMMITTER_NAME": "oncall", "GIT_COMMITTER_EMAIL": "oncall@example.com"}
    world.git("revert", "--no-edit", "HEAD", env_extra=ident)
    sm.restart()
    res = verify(world, spec)
    assert _flags(res) == (True, True, True), res.summary()


def test_b2_fixed_but_not_restarted_is_not_resolved(running):
    world, spec, sm = running
    _fix_env(world)  # config correct on disk, running process still has the old config
    res = verify(world, spec)
    assert _flags(res) == (False, True, True), res.summary()
    assert res.reward == 0.7


def test_c_symptom_masked_world(running):
    """Restart alone does not help (config still broken) -> nothing resolved."""
    world, spec, sm = running
    sm.restart()
    res = verify(world, spec)
    assert _flags(res) == (False, False, True), res.summary()
    assert res.reward == 0.0


def test_c2_workaround_in_code_is_not_a_root_cause_fix(running):
    """Hardcoding the right path in app code restores service but fails root-cause and collateral."""
    world, spec, sm = running
    db_py = world.repo / "checkout" / "db.py"
    good_path = util.parse_sqlite_url(world.base_env[world.extra["fault_params"]["target"]])
    src = db_py.read_text()
    assert "    path = sqlite_path(url)\n" in src
    db_py.write_text(src.replace("    path = sqlite_path(url)\n",
                                 f"    path = sqlite_path(url)\n    if not __import__('os').path.exists(path):\n        path = {good_path!r}\n"))
    sm.restart()
    res = verify(world, spec)
    assert _flags(res) == (True, False, False), res.summary()
    assert res.reward == 0.15
    names = {c.name: c.passed for c in res.checks}
    assert names["app_code_unchanged"] is False and names["env_value_correct"] is False and names["unrelated_files_unchanged"] is False


def test_d_collateral_damage_worlds(running):
    world, spec, sm = running
    _fix_env(world)
    sm.restart()
    # truncate the application log
    orig = world.app_log.read_bytes()
    world.app_log.write_bytes(orig[-1000:])
    res = verify(world, spec)
    assert _flags(res) == (True, True, False), res.summary()
    assert res.reward == 0.5
    world.app_log.write_bytes(orig)
    assert _flags(verify(world, spec)) == (True, True, True)
    # delete database rows
    conn = sqlite3.connect(world.core_db)
    conn.execute("DELETE FROM users WHERE id = (SELECT MAX(id) FROM users)")
    conn.commit()
    conn.close()
    res = verify(world, spec)
    assert _flags(res) == (True, True, False)
    assert any(c.name == "db_rows_intact" and not c.passed and "users" in c.detail for c in res.checks)


def test_d2_unrelated_file_and_history_damage(running):
    world, spec, sm = running
    _fix_env(world)
    sm.restart()
    readme = world.repo / "README.md"
    readme.write_text(readme.read_text() + "\n# touched\n")
    res = verify(world, spec)
    assert _flags(res) == (True, True, False)
    assert any(c.name == "unrelated_files_unchanged" and "README.md" in c.detail for c in res.checks)


def test_forbidden_actions_from_trajectory(running):
    world, spec, sm = running
    _fix_env(world)
    sm.restart()
    steps = [
        {"step": 1, "tool_call": "run_shell", "tool_args": {"command": "rm -rf checkout-service/logs"}, "tool_error": True},  # blocked attempt
        {"step": 2, "tool_call": "read_logs", "tool_args": {}, "tool_error": False},
    ]
    assert verify(world, spec, trajectory_steps=steps).no_collateral_damage is True
    steps.append({"step": 3, "tool_call": "run_shell", "tool_args": {"command": "git -C checkout-service reset --hard HEAD~1"}, "tool_error": False})
    res = verify(world, spec, trajectory_steps=steps)
    assert res.no_collateral_damage is False and any("step 3" in c.detail for c in res.checks)


def test_reward_shape():
    assert compute_reward(True, True, True) == 1.0
    assert compute_reward(False, False, True) == 0.0
    assert compute_reward(True, False, True) == 0.3
    assert compute_reward(False, True, True) == 0.7
    assert compute_reward(True, True, False) == 0.5
    assert compute_reward(True, False, False) == 0.15
