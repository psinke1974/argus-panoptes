import argparse
import csv
import os
import tempfile
from pathlib import Path

import httpx

from . import atlassian_api, confluence, jira
from .op_client import OP, load_env


def api_client(a, env):
    token = env.get("ATLASSIAN_TOKEN")
    if not token:
        raise SystemExit("Put your API token in the environment: export ATLASSIAN_TOKEN=... (Cloud: id.atlassian.com > Security > API tokens)")
    return atlassian_api.Client(a.url, a.email or env.get("ATLASSIAN_EMAIL"), token)


def main():
    ap = argparse.ArgumentParser(prog="panoptes_import", description="Import Atlassian data into Panoptes")
    ap.add_argument("--env", default=".env", help="env file with OP_API_KEY, GITEA_TOKEN, ... (use tenants/<name>/.env for a tenant)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("jira", help="Jira CSV export -> OpenProject")
    j.add_argument("csv")
    j.add_argument("--project-name")
    j.add_argument("--identifier", help="OpenProject project identifier (default: Jira key)")
    j.add_argument("--user-map", help="CSV with columns jira_name,op_login")
    j.add_argument("--no-key-prefix", action="store_true", help="do not prefix titles with [KEY-1]")
    j.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("confluence", help="Confluence HTML export -> Markdown in a Gitea repository")
    c.add_argument("export", help="zip file or extracted directory")
    c.add_argument("--space-name")
    c.add_argument("--out", help="only write to this directory")
    c.add_argument("--repo", help="owner/name in Gitea (created if missing)")
    for name, helptext in (("jira-api", "Jira Cloud/Server via REST API -> OpenProject (incl. attachments)"), ("confluence-api", "Confluence Cloud via REST API -> Markdown in a Gitea repository")):
        q = sub.add_parser(name, help=helptext)
        q.add_argument("--url", required=True, help="bijv. https://bedrijf.atlassian.net")
        q.add_argument("--email", help="Atlassian account (Cloud). Omit for a Personal Access Token (Server/DC)")
        # token via the environment (ATLASSIAN_TOKEN), not as an argument: that would end up in your shell history
        if name == "jira-api":
            q.add_argument("--project", required=True, help="Jira project key")
            q.add_argument("--server", action="store_true", help="Jira Server/Data Center instead of Cloud")
            q.add_argument("--identifier")
            q.add_argument("--project-name")
            q.add_argument("--user-map")
            q.add_argument("--no-key-prefix", action="store_true")
            q.add_argument("--no-attachments", action="store_true")
            q.add_argument("--dry-run", action="store_true")
        else:
            q.add_argument("--space", required=True, help="Confluence space-key")
            q.add_argument("--out")
            q.add_argument("--repo")
    a = ap.parse_args()
    env = load_env(a.env)
    # state lives with the target: a tenant's own import-state folder (the one its glue reads), else ./.import-state
    state_dir = str(Path(env["TENANT_DIR"]) / "import-state") if env.get("TENANT_DIR") else ".import-state"
    if env.get("TENANT_DIR"):  # tenant env file: let scripts/op-enable-types.sh address that tenant's stack
        os.environ["COMPOSE_FILE"] = str(Path(__file__).resolve().parent.parent / "deploy" / "tenant" / "docker-compose.yml")
        os.environ["COMPOSE_ENV_FILE"] = a.env


    if a.cmd in ("jira", "jira-api"):
        umap = {}
        if a.user_map:
            umap = {r["jira_name"]: r["op_login"] for r in csv.DictReader(open(a.user_map, encoding="utf-8"))}
        if a.cmd == "jira":
            print(jira.run(OP.from_env(env), a.csv, a.project_name, a.identifier, umap, a.dry_run, not a.no_key_prefix, state_dir))
        else:
            c = api_client(a, env)
            issues = atlassian_api.fetch_jira_issues(c, a.project, a.server)
            print(jira.run_issues(OP.from_env(env), issues, a.project_name, a.identifier, umap, a.dry_run, not a.no_key_prefix, state_dir,
                                  download=None if a.no_attachments else c.download))
    elif a.cmd == "confluence-api":
        with tempfile.TemporaryDirectory() as work:
            src = Path(work) / "src"
            warnings = atlassian_api.export_confluence_space(api_client(a, env), a.space, src)
            dest = Path(a.out) if a.out else Path(work) / "out"
            pages = confluence.convert_space(src, dest, a.space)
            print(f"{len(pages)} pages converted" + (f", {len(warnings)} warnings:" if warnings else ""))
            for w in sorted(set(warnings)):
                print(" -", w)
            if a.repo:
                print("Published:", confluence.publish(dest, env.get("GITEA_URL_LOCAL", "http://localhost:3000"), env["GITEA_TOKEN"], a.repo, httpx.Client(timeout=60, verify=env.get("TLS_VERIFY", "true") != "false"), env.get("TLS_VERIFY", "true") != "false"))
            elif not a.out:
                print("Use --out <dir> or --repo owner/name to keep the result")
    else:
        with tempfile.TemporaryDirectory() as work:
            src = confluence.open_export(a.export, Path(work) / "src")
            dest = Path(a.out) if a.out else Path(work) / "out"
            pages = confluence.convert_space(src, dest, a.space_name)
            print(f"{len(pages)} pages converted")
            if a.repo:
                print("Published:", confluence.publish(dest, env.get("GITEA_URL_LOCAL", "http://localhost:3000"), env["GITEA_TOKEN"], a.repo, httpx.Client(timeout=60, verify=env.get("TLS_VERIFY", "true") != "false"), env.get("TLS_VERIFY", "true") != "false"))
            elif not a.out:
                print("Use --out <dir> or --repo owner/name to keep the result")


main()
