"""Smoke test for encrypted off-site backups: runs a local S3-compatible server (rclone), configures restic against it,
backs up a tenant, proves the stored data is encrypted, checks incremental uploads, verification and a wrong password.
Non-destructive. Usage: python scripts/offsite_smoke.py demo
(The destructive proof, destroy + `offsite restore` + smoke tests, is described in the README.)"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

tenant = sys.argv[1] if len(sys.argv) > 1 else "demo"
root = Path(__file__).resolve().parent.parent
env_file = Path(tempfile.mkdtemp()) / "offsite.env"
s3_data = Path(tempfile.mkdtemp())
os.chmod(s3_data, 0o777)
env = {**os.environ, "PANOPTES_OFFSITE_ENV": str(env_file)}
secret = dict(l.split("=", 1) for l in (root / "tenants" / tenant / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))["OP_SECRET_KEY_BASE"]


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def tctl(*args, expect_ok=True):
    r = subprocess.run(["python3", "deploy/tenantctl.py", *args], capture_output=True, text=True, cwd=root, env=env)
    if expect_ok and r.returncode != 0:
        print(r.stdout[-800:], r.stderr[-800:])
    return r


subprocess.run(["docker", "rm", "-f", "panoptes-test-s3"], capture_output=True)
subprocess.run(["docker", "run", "-d", "--name", "panoptes-test-s3", "-p", "9000:9000", "-v", f"{s3_data}:/data", "rclone/rclone:latest",
                "serve", "s3", "/data", "--auth-key", "testaccess,testsecretkey", "--addr", ":9000"], check=True, capture_output=True)
try:
    time.sleep(3)
    r = tctl("offsite", "configure", "--repository", "s3:http://host.docker.internal:9000/panoptes-test", "--generate-password",
             "--aws-access-key-id", "testaccess", "--aws-secret-access-key", "testsecretkey")
    check(r.returncode == 0 and "Store it in your password manager" in r.stdout, "configured an S3 repository with a generated password (shown once)")
    check(oct(env_file.stat().st_mode)[-3:] == "600", "the settings file is private (chmod 600)")

    t0 = time.time()
    r = tctl("offsite", "backup", tenant)
    check(r.returncode == 0 and "stored" in r.stdout, f"first backup uploaded ({time.time() - t0:.0f}s): {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:]}")
    stored = [p for p in s3_data.rglob("*") if p.is_file()]
    check(len(stored) > 5, f"{len(stored)} objects in the S3 bucket")
    leaked = [p for p in stored if secret.encode() in p.read_bytes()]
    check(not leaked, "the tenant's secret key is NOT readable in any stored object (client-side encryption)")

    r = tctl("offsite", "backup", tenant)
    added = float(r.stdout.split("(")[-1].split(" MB")[0]) if "MB new" in r.stdout else -1
    check(r.returncode == 0 and 0 <= added < 5, f"second backup is incremental ({added} MB new data)")

    r = tctl("offsite", "list", tenant)
    check(r.stdout.count("panoptes-" + tenant) >= 2, "snapshots listed (two)")
    r = tctl("offsite", "verify", tenant)
    check(r.returncode == 0 and "can be restored" in r.stdout, "verification passes: integrity check, snapshot age and a test restore of key files")

    bad = {**env, "PANOPTES_OFFSITE_ENV": str(env_file)}
    txt = env_file.read_text().replace("RESTIC_PASSWORD=", "RESTIC_PASSWORD=wrong-")
    bad_file = env_file.with_name("bad.env")
    bad_file.write_text(txt)
    bad["PANOPTES_OFFSITE_ENV"] = str(bad_file)
    r = subprocess.run(["python3", "deploy/tenantctl.py", "offsite", "list", tenant], capture_output=True, text=True, cwd=root, env=bad)
    check(r.returncode != 0, "a wrong repository password cannot read the backups")
    print("\nALL PASSED")
finally:
    subprocess.run(["docker", "rm", "-f", "panoptes-test-s3"], capture_output=True)
    shutil.rmtree(s3_data, ignore_errors=True)
    shutil.rmtree(env_file.parent, ignore_errors=True)
