import argparse
import csv
import tempfile
from pathlib import Path

import httpx

from . import atlassian_api, confluence, jira
from .op_client import OP, load_env


def api_client(a, env):
    token = env.get("ATLASSIAN_TOKEN")
    if not token:
        raise SystemExit("Zet je API-token in de omgeving: export ATLASSIAN_TOKEN=... (Cloud: id.atlassian.com > Beveiliging > API-tokens)")
    return atlassian_api.Client(a.url, a.email or env.get("ATLASSIAN_EMAIL"), token)


def main():
    ap = argparse.ArgumentParser(prog="werkplaats_import", description="Importeer Atlassian-data in Werkplaats")
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("jira", help="Jira CSV-export -> OpenProject")
    j.add_argument("csv")
    j.add_argument("--project-name")
    j.add_argument("--identifier", help="OpenProject-projectidentifier (standaard: Jira-key)")
    j.add_argument("--user-map", help="CSV met kolommen jira_name,op_login")
    j.add_argument("--no-key-prefix", action="store_true", help="zet geen [KEY-1] voor de titel")
    j.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("confluence", help="Confluence HTML-export -> Markdown in een Gitea-repo")
    c.add_argument("export", help="zip of uitgepakte map")
    c.add_argument("--space-name")
    c.add_argument("--out", help="schrijf alleen naar deze map")
    c.add_argument("--repo", help="owner/naam in Gitea (wordt aangemaakt)")
    for name, helptext in (("jira-api", "Jira Cloud/Server via REST-API -> OpenProject (incl. bijlagen)"), ("confluence-api", "Confluence Cloud via REST-API -> Markdown in een Gitea-repo")):
        q = sub.add_parser(name, help=helptext)
        q.add_argument("--url", required=True, help="bijv. https://bedrijf.atlassian.net")
        q.add_argument("--email", help="Atlassian-account (Cloud). Weglaten voor een Personal Access Token (Server/DC)")
        # token via omgeving (ATLASSIAN_TOKEN), niet via argument: dat belandt in je shell-historie
        if name == "jira-api":
            q.add_argument("--project", required=True, help="Jira-projectsleutel")
            q.add_argument("--server", action="store_true", help="Jira Server/Data Center in plaats van Cloud")
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
    env = load_env()

    if a.cmd in ("jira", "jira-api"):
        umap = {}
        if a.user_map:
            umap = {r["jira_name"]: r["op_login"] for r in csv.DictReader(open(a.user_map, encoding="utf-8"))}
        if a.cmd == "jira":
            print(jira.run(OP.from_env(env), a.csv, a.project_name, a.identifier, umap, a.dry_run, not a.no_key_prefix))
        else:
            c = api_client(a, env)
            issues = atlassian_api.fetch_jira_issues(c, a.project, a.server)
            print(jira.run_issues(OP.from_env(env), issues, a.project_name, a.identifier, umap, a.dry_run, not a.no_key_prefix,
                                  download=None if a.no_attachments else c.download))
    elif a.cmd == "confluence-api":
        with tempfile.TemporaryDirectory() as work:
            src = Path(work) / "src"
            warnings = atlassian_api.export_confluence_space(api_client(a, env), a.space, src)
            dest = Path(a.out) if a.out else Path(work) / "out"
            pages = confluence.convert_space(src, dest, a.space)
            print(f"{len(pages)} pagina's geconverteerd" + (f", {len(warnings)} waarschuwingen:" if warnings else ""))
            for w in sorted(set(warnings)):
                print(" -", w)
            if a.repo:
                print("Gepubliceerd:", confluence.publish(dest, env.get("GITEA_URL_LOCAL", "http://localhost:3000"), env["GITEA_TOKEN"], a.repo, httpx.Client(timeout=60)))
            elif not a.out:
                print("Gebruik --out <map> of --repo owner/naam om het resultaat te bewaren")
    else:
        with tempfile.TemporaryDirectory() as work:
            src = confluence.open_export(a.export, Path(work) / "src")
            dest = Path(a.out) if a.out else Path(work) / "out"
            pages = confluence.convert_space(src, dest, a.space_name)
            print(f"{len(pages)} pagina's geconverteerd")
            if a.repo:
                print("Gepubliceerd:", confluence.publish(dest, env.get("GITEA_URL_LOCAL", "http://localhost:3000"), env["GITEA_TOKEN"], a.repo, httpx.Client(timeout=60)))
            elif not a.out:
                print("Gebruik --out <map> of --repo owner/naam om het resultaat te bewaren")


main()
