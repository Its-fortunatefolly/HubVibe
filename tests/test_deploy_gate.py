"""The deploy gate: two copies of the node, one serving, and nothing a buyer
depends on lost when traffic moves from one to the other.

What costs money or a customer if it is wrong:
  * only the copy the deploy script names takes traffic (/ready), and a
    node that cannot tell keeps serving rather than going dark;
  * a copy that starts never closes a job the other live copy is running
    (that would release a payment for work about to be delivered), while a
    job whose process is gone is closed, unbilled, exactly once;
  * an A2A task opened on the old copy is still there on the new one;
  * the stack, the Caddyfile and the scripts are wired so that the only way
    to change what runs is through the gate.
"""

import importlib.util
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
APP = REPO_ROOT / "wcag-audit-engine" / "app"
MAIN_PATH = APP / "main.py"
PKG = APP / "workers"
VPS = REPO_ROOT / "deploy" / "vps"
SCRIPTS = REPO_ROOT / "scripts"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"


def _load_workers():
    cached = sys.modules.get("wcag_audit_engine_workers")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_engine_workers", PKG / "__init__.py",
        submodule_search_locations=[str(PKG)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


W = _load_workers()


def _load_a2a():
    spec = importlib.util.spec_from_file_location("wcag_audit_engine_a2a_gate", APP / "a2a.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drop_sibling_cache():
    # main.py caches configured siblings in sys.modules (see test_deliver_later).
    for name in [n for n in sys.modules
                 if n.startswith("wcag_audit_engine_") and n != "wcag_audit_engine_workers"]:
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    global W
    _drop_sibling_cache()
    W = _load_workers()
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    W.ledger.reset_for_tests()
    yield
    W.ledger.reset_for_tests()
    _drop_sibling_cache()


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("SANCTIONS_PREFETCH", "0")
    monkeypatch.setenv("A2A_TASKS_PATH", "")
    spec = importlib.util.spec_from_file_location("wcag_audit_main_deploy_gate", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ready(module):
    from fastapi.testclient import TestClient

    return TestClient(module.app).get("/ready")


# --- /ready: which copy takes traffic -----------------------------------------

def test_a_single_instance_is_always_ready(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "NODE_COLOR", None)
    assert _ready(app_module).status_code == 200


def test_only_the_copy_named_live_is_ready(app_module, monkeypatch, tmp_path):
    active = tmp_path / "hubvibe-active"
    monkeypatch.setattr(app_module, "ACTIVE_COLORS_FILE", str(active))
    monkeypatch.setattr(app_module, "NODE_COLOR", "green")
    active.write_text("blue\n")
    response = _ready(app_module)
    assert response.status_code == 503
    assert response.json() == {"ready": False, "color": "green", "build": app_module.BUILD_SHA,
                               "active": ["blue"]}
    active.write_text("blue green\n")   # the moment of a switch: both ready
    assert _ready(app_module).status_code == 200
    active.write_text("green\n")
    assert _ready(app_module).json()["ready"] is True


def test_a_copy_started_for_checking_is_not_ready(app_module, monkeypatch, tmp_path):
    """deploy-box.sh writes the live color before starting the new copy; an
    empty list (the migration from the single container) readies nobody."""
    active = tmp_path / "hubvibe-active"
    active.write_text("\n")
    monkeypatch.setattr(app_module, "ACTIVE_COLORS_FILE", str(active))
    monkeypatch.setattr(app_module, "NODE_COLOR", "blue")
    assert _ready(app_module).status_code == 503


def test_a_node_that_cannot_read_the_file_keeps_serving(app_module, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "ACTIVE_COLORS_FILE", str(tmp_path / "missing"))
    monkeypatch.setattr(app_module, "NODE_COLOR", "blue")
    assert _ready(app_module).status_code == 200


def test_health_says_which_build_and_copy_answered(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "NODE_COLOR", "blue")
    monkeypatch.setattr(app_module, "BUILD_SHA", "abc1234")
    body = TestClient(app_module.app).get("/health").json()
    assert body["build"] == "abc1234" and body["color"] == "blue"


def test_ready_is_not_in_the_published_api(app_module):
    assert "/ready" not in app_module.app.openapi()["paths"]


def test_caddys_probe_does_not_flood_the_access_log(app_module):
    access = logging.getLogger("uvicorn.access")

    def record(path):
        return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                                 '%s - "%s %s HTTP/%s" %d',
                                 ("172.18.0.2:50000", "GET", path, "1.1", 200), None)

    assert access.filter(record("/ready")) is False
    assert access.filter(record("/ready?x=1")) is False
    assert access.filter(record("/health"))
    assert access.filter(record("/work/market/quote"))


# --- handed-back jobs across two copies ------------------------------------------

def _open_job(job_id, mpp_tx="0xabc", key=None):
    call_id = "c-" + job_id
    W.ledger.open_call(call_id=call_id, worker="video.generate", path="/work/video/generate",
                       price_usd=10.0, idempotency_key=key)
    if key:
        W.ledger.claim_idempotency(key, call_id, "video.generate")
    W.ledger.open_deferred(job_id, call_id, "video.generate", rail="mpp", mpp_tx=mpp_tx)


def _set(job_id, **columns):
    conn = W.ledger._safe_connect()
    for name, value in columns.items():
        conn.execute(f"UPDATE deferred_jobs SET {name}=? WHERE job_id=?", (value, job_id))
    conn.commit()


def test_a_job_records_the_process_running_it(tmp_path):
    _open_job("job-mine")
    row = W.ledger.get_deferred("job-mine")
    assert row["owner"] == W.ledger.INSTANCE_ID
    assert (tmp_path / "instances" / (W.ledger.INSTANCE_ID + ".lock")).exists()
    assert W.ledger.owner_alive(W.ledger.INSTANCE_ID)


def test_this_process_never_closes_its_own_running_job():
    _open_job("job-mine")
    released = []
    assert W.router.reconcile_interrupted(released.append) == 0
    assert released == [] and W.ledger.get_deferred("job-mine")["state"] == "running"


def test_a_job_another_live_copy_is_running_is_never_closed(tmp_path):
    """The deploy case, with a real second process holding its lock, the way
    the other container does on the shared volume."""
    _open_job("job-theirs", mpp_tx="0xdef", key="their-key")
    other = "a" * 32
    lock = tmp_path / "instances" / (other + ".lock")
    lock.parent.mkdir(exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, sys, time\n"
         f"f = open({str(lock)!r}, 'a+')\n"
         "fcntl.flock(f, fcntl.LOCK_EX)\n"
         "print('held', flush=True)\n"
         "time.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        _set("job-theirs", owner=other)
        released = []
        assert W.router.reconcile_interrupted(released.append) == 0
        assert released == []
        assert W.ledger.get_deferred("job-theirs")["state"] == "running"
        assert W.ledger.claim_idempotency("their-key", "c-dup", "video.generate")[0] == "in_progress"
    finally:
        holder.kill()
        holder.wait()
    # Its process is gone (killed: no cleanup ran), so the job is closed,
    # unbilled, its payment released and its key freed -- exactly once.
    released = []
    assert W.router.reconcile_interrupted(released.append) == 1
    assert released == ["0xdef"]
    row = W.ledger.get_deferred("job-theirs")
    assert row["state"] == "done" and row["http_status"] == 502 and '"billed": false' in row["body"]
    assert W.ledger.claim_idempotency("their-key", "c-retry", "video.generate")[0] == "claimed"
    assert not lock.exists(), "a dead process's lock file is removed once its jobs are closed"
    assert W.router.reconcile_interrupted(released.append) == 0
    assert released == ["0xdef"], "a payment was released twice"


def test_a_job_whose_owner_left_no_lock_is_closed():
    _open_job("job-gone")
    _set("job-gone", owner="b" * 32)
    assert W.router.reconcile_interrupted(None) == 1


def test_a_job_its_owner_finished_is_never_overwritten():
    _open_job("job-done")
    W.ledger.finish_deferred("job-done", 200, '{"status": "ok"}', "{}")
    assert W.ledger.close_interrupted("job-done", '{"status": "error"}') is False
    row = W.ledger.get_deferred("job-done")
    assert row["http_status"] == 200 and row["body"] == '{"status": "ok"}'


def test_a_job_from_before_owners_is_closed_only_when_none_could_still_run():
    """Rows written by the release before this one carry no owner: judged by
    age, never closed while a job of that age could still be running."""
    _open_job("job-legacy")
    _set("job-legacy", owner=None, created_at=time.time() - 30)
    assert W.router.reconcile_interrupted(None) == 0
    _set("job-legacy", created_at=time.time() - W.router._OWNERLESS_GRACE_SECONDS - 1)
    assert W.router.reconcile_interrupted(None) == 1


def test_the_grace_for_ownerless_jobs_outlasts_the_longest_job():
    assert W.router._OWNERLESS_GRACE_SECONDS > W.catalog.MAX_WORKER_SECONDS + 60


def test_the_migration_adds_the_owner_column_to_an_existing_ledger(tmp_path, monkeypatch):
    import sqlite3

    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE deferred_jobs (job_id TEXT PRIMARY KEY, call_id TEXT NOT NULL, "
                 "worker TEXT NOT NULL, created_at REAL NOT NULL, finished_at REAL, state TEXT NOT NULL, "
                 "rail TEXT, mpp_tx TEXT, http_status INTEGER, body TEXT, headers TEXT)")
    conn.execute("INSERT INTO deferred_jobs (job_id, call_id, worker, created_at, state) "
                 "VALUES ('j', 'c', 'w', 1, 'running')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(old))
    W.ledger.reset_for_tests()
    row = W.ledger.get_deferred("j")
    assert "owner" in row and row["owner"] is None


# --- A2A tasks across the switch --------------------------------------------------

def test_an_a2a_task_opened_on_the_old_copy_is_found_on_the_new_one(tmp_path):
    a2a = _load_a2a()
    path = str(tmp_path / "a2a.db")
    old, new = a2a.TaskStore(path=path), a2a.TaskStore(path=path)
    old.put({"id": "t-1", "state": "input-required", "skill": "market.quote", "arguments": {}})
    assert new.get("t-1")["state"] == "input-required"
    new.put({"id": "t-1", "state": "completed", "skill": "market.quote", "arguments": {}})
    assert old.get("t-1")["state"] == "completed"
    assert new.get("missing") is None and new.get(None) is None


def test_a2a_tasks_still_expire(tmp_path):
    a2a = _load_a2a()
    store = a2a.TaskStore(ttl_seconds=0, path=str(tmp_path / "a2a.db"))
    store.put({"id": "t-old", "state": "input-required"})
    time.sleep(0.01)
    assert store.get("t-old") is None


def test_an_unusable_task_file_falls_back_to_memory(tmp_path):
    a2a = _load_a2a()
    store = a2a.TaskStore(path=str(tmp_path))   # a directory: sqlite cannot open it
    store.put({"id": "t-mem", "state": "input-required"})
    assert store.get("t-mem")["state"] == "input-required"


def test_no_volume_means_memory(monkeypatch):
    a2a = _load_a2a()
    monkeypatch.setenv("A2A_TASKS_PATH", "")
    assert a2a.TaskStore()._path is None


# --- the stack is wired so the gate is the only way in -----------------------------

def _compose():
    return yaml.safe_load((VPS / "docker-compose.yml").read_text())


def test_blue_and_green_are_the_same_node_but_for_their_color():
    services = _compose()["services"]
    blue, green = dict(services["hubvibe-blue"]), dict(services["hubvibe-green"])
    assert blue.pop("profiles") == ["blue"] and green.pop("profiles") == ["green"]
    blue_env, green_env = dict(blue.pop("environment")), dict(green.pop("environment"))
    assert blue_env.pop("HUBVIBE_COLOR") == "blue" and green_env.pop("HUBVIBE_COLOR") == "green"
    assert blue == green and blue_env == green_env
    assert "hubvibe" not in services, "a third, ungated copy of the node"
    assert blue["build"]["args"]["BUILD_SHA"] == "${HUBVIBE_BUILD_SHA:-unknown}"
    assert blue["volumes"] == ["hubvibe_data:/data"]
    assert blue["init"] is True and blue["mem_limit"] == "3g"


def test_a_retired_copy_finishes_every_job_it_holds():
    node = _compose()["services"]["hubvibe-blue"]
    drain = int(node["environment"]["WORKER_DRAIN_SECONDS"])
    grace = int(str(node["stop_grace_period"]).rstrip("s"))
    assert drain >= W.catalog.MAX_WORKER_SECONDS + 30, "a job outlives the drain"
    assert grace >= drain + 15, "Docker kills the copy before its drain ends"


def test_caddy_routes_to_whichever_copy_is_ready():
    services = _compose()["services"]
    assert "depends_on" not in services["caddy"], "caddy must not start a node by name"
    text = (VPS / "Caddyfile").read_text()
    assert "reverse_proxy hubvibe-blue:8080 hubvibe-green:8080 {" in text
    for line in ("lb_policy first", "health_uri /ready", "lb_try_duration 15s",
                 "health_interval 1s", "health_fails 3"):
        assert line in text, line
    assert "reverse_proxy hubvibe:8080" not in text
    # The stopped color's failing probe must not flood the access log.
    head = text[:text.index("{$DOMAIN} {")]
    assert "exclude http.handlers.reverse_proxy.health_checker.active" in head


def test_the_image_says_which_commit_it_was_built_from():
    text = (REPO_ROOT / "wcag-audit-engine" / "Dockerfile").read_text()
    copy_at = text.index("COPY app/ ./app/")
    assert text.index("ARG BUILD_SHA=unknown") > copy_at, "the stamp must not bust the pip layer"
    assert "ENV HUBVIBE_BUILD_SHA=$BUILD_SHA" in text


BOX_SCRIPTS = ["box-lib.sh", "box-checks.sh", "box-exec.sh", "deploy-box.sh", "monitor-box.sh"]


@pytest.mark.parametrize("name", BOX_SCRIPTS)
def test_box_scripts_parse(name):
    assert subprocess.run(["bash", "-n", str(SCRIPTS / name)]).returncode == 0


def test_nothing_reaches_a_node_container_by_a_fixed_name():
    """Which copy is live changes with every deploy; only box-lib knows the
    naming, and only it names the single container blue/green replaced."""
    offenders = []
    for path in sorted(SCRIPTS.glob("*.sh")) + sorted(SCRIPTS.glob("*.py")):
        if path.name == "box-lib.sh":
            continue
        text = path.read_text()
        for needle in ("vps-hubvibe-1", "exec -T hubvibe ", "compose exec hubvibe ",
                       "compose restart hubvibe", "logs hubvibe "):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, offenders


def test_no_script_restarts_or_rebuilds_the_node_behind_the_gate():
    for path in sorted(SCRIPTS.glob("*.sh")):
        if path.name in ("deploy-box.sh",):
            continue
        text = path.read_text()
        assert "up -d --build" not in text, path.name
        assert "compose up -d >" not in text, path.name


def test_the_monitor_checks_the_live_copy_with_the_gate_checks():
    text = (SCRIPTS / "monitor-box.sh").read_text()
    assert ". \"$REPO/scripts/box-lib.sh\"" in text and "hv_live_container" in text
    assert "scripts/box-checks.sh" in text
    deploy = (SCRIPTS / "deploy-box.sh").read_text()
    assert "scripts/box-checks.sh" in deploy, "the gate and the monitor must run the same checks"


def test_deploys_and_health_checks_share_one_lock():
    lib = (SCRIPTS / "box-lib.sh").read_text()
    assert 'HV_LOCK="${HV_LOCK:-/tmp/hv-monitor.lock}"' in lib, "the monitor's cron line locks this file"
    deploy = (SCRIPTS / "deploy-box.sh").read_text()
    assert 'exec 9>>"$HV_LOCK"' in deploy and "flock" in deploy


def test_the_gate_checks_never_touch_real_records():
    text = (SCRIPTS / "box-checks.sh").read_text()
    for isolation in ("KEY_STORE_SQLITE_PATH=/tmp/monitor-keys.db",
                      "PURCHASE_BOOK_PATH=/tmp/monitor-purchases.db",
                      "PURCHASE_ALERT_WEBHOOK=", "A2A_TASKS_PATH="):
        assert isolation in text, isolation


def _stub_docker(tmp_path, volume_dir, running):
    """A docker that answers what box-lib asks: the volume's mountpoint and
    which containers run."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "docker"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = volume ]; then echo "' + str(volume_dir) + '"; exit 0; fi\n'
        'if [ "$1" = inspect ]; then\n'
        '  for c in ' + " ".join(running) + '; do\n'
        '    if [ "$4" = "$c" ]; then echo true; exit 0; fi\n'
        "  done\n"
        "  echo false; exit 0\n"
        "fi\n"
        "exit 0\n")
    script.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def _lib(env, snippet):
    return subprocess.run(["bash", "-c", f". {SCRIPTS / 'box-lib.sh'}; {snippet}"],
                          capture_output=True, text=True, env=env)


def test_box_lib_finds_the_live_copy(tmp_path):
    volume = tmp_path / "volume"
    volume.mkdir()
    env = _stub_docker(tmp_path, volume, ["vps-hubvibe-green-1"])
    assert _lib(env, "hv_write_active green").returncode == 0
    assert (volume / "hubvibe-active").read_text() == "green\n"
    assert _lib(env, "hv_active_colors").stdout.strip() == "green"
    assert _lib(env, "hv_live_container").stdout.strip() == "vps-hubvibe-green-1"
    assert _lib(env, "hv_write_active blue green; hv_active_colors").stdout.strip() == "blue green"
    # blue is listed first but is not running: the running copy is reported.
    assert _lib(env, "hv_live_container").stdout.strip() == "vps-hubvibe-green-1"


def test_box_lib_falls_back_to_the_single_container_until_the_migration(tmp_path):
    volume = tmp_path / "volume"
    volume.mkdir()
    env = _stub_docker(tmp_path, volume, ["vps-hubvibe-1"])
    assert _lib(env, "hv_live_container").stdout.strip() == "vps-hubvibe-1"
    env = _stub_docker(tmp_path, volume, [])
    assert _lib(env, "hv_live_container").returncode != 0


def test_deploy_refuses_to_run_off_the_box(tmp_path):
    env = {**os.environ, "CLOUD_SHELL": "true", "HOME": str(tmp_path)}
    result = subprocess.run(["bash", str(SCRIPTS / "deploy-box.sh")], capture_output=True,
                            text=True, env=env)
    assert result.returncode == 1 and "runs ON the box" in result.stdout


def test_deploy_rejects_an_unknown_action(tmp_path):
    volume = tmp_path / "volume"
    volume.mkdir()
    compose_dir = tmp_path / "vps"
    compose_dir.mkdir()
    (compose_dir / ".env").write_text("DOMAIN=example.com\n")
    env = _stub_docker(tmp_path, volume, [])
    env = {k: v for k, v in env.items() if k not in ("CLOUD_SHELL", "DEVSHELL_PROJECT_ID")}
    env["HV_COMPOSE_DIR"] = str(compose_dir)
    result = subprocess.run(["bash", str(SCRIPTS / "deploy-box.sh"), "explode"],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 2 and "usage:" in result.stdout
