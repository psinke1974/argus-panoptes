import argparse
import csv
import tempfile
from pathlib import Path

import httpx

from . import confluence, jira
from .op_client import OP, load_env


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
    a = ap.parse_args()
    env = load_env()

    if a.cmd == "jira":
        umap = {}
        if a.user_map:
            umap = {r["jira_name"]: r["op_login"] for r in csv.DictReader(open(a.user_map, encoding="utf-8"))}
        print(jira.run(OP.from_env(env), a.csv, a.project_name, a.identifier, umap, a.dry_run, not a.no_key_prefix))
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
