<p align="center"><img src="assets/panoptes-banner.png" alt="Panoptes: the all-seeing project tracker" width="720"></p>

# 👁 Panoptes

> *the all-seeing project tracker*

Argus Panoptes, the giant with a hundred eyes, never closed all of them at once. Jira has a hundred configuration screens and nobody remembers which screen controls which eye. Panoptes takes the middle road: open source, hostable in the EU (or anywhere you like), with projects and boards, git and documentation wired together. No labyrinth of workflow schemes, permission schemes and screen schemes (the Labyrinth of Knossos had fewer turns, and Theseus at least had a thread).

It is built on [OpenProject](https://www.openproject.org) (GPLv3) and [Gitea](https://about.gitea.com) (MIT), with our own glue and an importer for Jira and Confluence. For anyone who wants to leave the underworld without leaving their history behind: the importer is your Charon.

| Myth | In Panoptes |
|---|---|
| Argus' hundred eyes | `glue/`: sees every commit and pull request and updates the tickets |
| Ariadne's thread | `OP#123` and old `PROJ-12` keys lead from git back to the ticket |
| Charon, the ferryman | `panoptes_import/`: carries Jira and Confluence across |
| The Augean stables | Workflow schemes, field configurations and screen schemes |
| Sisyphus | A backlog that never empties (here too, but it rolls more lightly) |

> **Status: early.** The importers are tested against our own fixtures and a fake Atlassian API (`tests_import/mock_atlassian.py`), not yet against a real Jira/Confluence instance or a real customer export. Reports of what breaks are very welcome.

| Component | Based on | License | URL (local dev) |
|---|---|---|---|
| Work packages, boards, wiki | OpenProject | GPLv3 | http://localhost:8080 |
| Git | Gitea | MIT | http://localhost:3000 |
| Integration + extras | `glue/` (FastAPI) | GPLv3 | http://localhost:8090 |

## Quick start (single stack, for development)
```bash
cp .env.example .env            # set OP_SECRET_KEY_BASE (openssl rand -hex 64) and WEBHOOK_SECRET
docker compose up -d --build
```
1. OpenProject: log in as `admin` / `admin`, change the password immediately, create an API token (My account > Access tokens) and put it in `.env` as `OP_API_KEY`.
2. Gitea: create an admin with `docker compose exec -u git gitea gitea admin user create --admin --username ... --password ... --email ...`, create a token and put it in `.env` as `GITEA_TOKEN`.
3. `docker compose up -d glue`, then add a webhook in your Gitea repo: `http://glue:8090/hooks/gitea`, secret = `WEBHOOK_SECRET`, events: push + pull request.

## What it does
- `OP#123` in a commit or PR text: comment with a link on work package 123.
- After a Jira import the old keys work too: `fixes PROJ-12` in a commit or PR is translated to the new work package through `.import-state/` (only keys the importer knows).
- PR opened: status moves to *In progress*. `fixes OP#123` + merge (or a push to the default branch): status moves to *Closed*.
- `GET /release-notes?repo=owner/name&base=v1&head=v2`: Markdown release notes grouped by work package type.
- Docs-in-repo: Markdown in `docs/` (and the repo root) is served at `http://localhost:8090/docs/<owner>/<repo>` with navigation, search and a list of related work packages (from the commits that touched the file). A commit with `OP#123` that changes docs adds a link to that page on the work package.
  - Deliberate choice: the OpenProject API can only read wiki pages, not write them. Git is therefore the source of truth and the glue provides the reading portal.

## Atlassian importer
Runs locally (`pip install -r panoptes_import/requirements.txt`, reads `.env`).

```bash
# Jira: Issues > Export > CSV (all fields)
python -m panoptes_import jira export.csv --identifier proj --project-name "My project" --dry-run
python -m panoptes_import jira export.csv --identifier proj --project-name "My project" [--user-map users.csv]

# Confluence: Space settings > Export space > HTML
python -m panoptes_import confluence space-export.zip --space-name TEAM --repo owner/name
```
- **Jira:** types, statuses and priorities are mapped (English and Dutch OpenProject installs; unknown values fall back and are reported), hierarchy (epic/parent), comments, `blocks`/`duplicates`/`relates` links. Titles get a `[PROJ-123]` prefix so old keys stay searchable. Jira wiki markup becomes Markdown. Re-runnable: `.import-state/` remembers what was already created.
- **Users:** `--user-map` (CSV: `jira_name,op_login`) maps assignees; otherwise the name goes into the description.
- **Confluence:** the page tree becomes a folder structure in `docs/`, attachments go to `docs/_attachments/`, links between pages keep working, info/warning panels, code blocks and tables become Markdown. The result shows up in the docs portal right away.
- **Straight from the API** (no export needed): put your token in the environment and run
  ```bash
  export ATLASSIAN_TOKEN=...            # Cloud: id.atlassian.com > Security > API tokens
  python -m panoptes_import jira-api --url https://yourcompany.atlassian.net --email you@yourcompany.com --project PROJ --identifier proj
  python -m panoptes_import confluence-api --url https://yourcompany.atlassian.net --email you@yourcompany.com --space TEAM --repo owner/name
  ```
  Jira Server/Data Center: use `--server` and omit `--email` (Personal Access Token as Bearer). Also imports attachments and matches assignees on e-mail address when Jira returns it. Confluence macros that have no Markdown equivalent are reported instead of silently dropped.
- **Not (yet):** worklogs, issue history, custom fields, workflows, Confluence comments and permissions, Confluence XML export.
- Enabling work package types per project is not possible through the OpenProject API; the importer does it with `scripts/op-enable-types.sh`.

## Hosting: one isolated stack per customer
The same code runs on a customer's own server (self-hosted) or as a hosted service you offer. Each customer ("tenant") gets its own OpenProject, Gitea and glue, behind one shared Caddy proxy with automatic HTTPS. Isolation, backups and exports are per customer.

```bash
python3 deploy/tenantctl.py create acme --admin-email admin@acme.com         # local trial: https://acme.localhost
python3 deploy/tenantctl.py create acme --base-domain panoptes.example.com   # production: acme.panoptes.example.com
python3 deploy/tenantctl.py list
python3 deploy/tenantctl.py destroy acme --yes                               # removes containers AND data
```
- Creates `acme.<base>` (OpenProject), `git.acme.<base>` (Gitea, HTTPS clone only) and `docs.acme.<base>` (docs portal), generates all secrets in `tenants/acme/.env` (chmod 600) and bootstraps the admin accounts and API tokens.
- Production needs wildcard DNS (`*.panoptes.example.com`) pointing at the server; Caddy then obtains Let's Encrypt certificates. Locally, `*.localhost` uses Caddy's internal CA (accept the browser warning, or run `caddy trust` inside the Caddy container's CA).
- Rough sizing: about 2 GB RAM per tenant for OpenProject, plus a little for Gitea and the glue.
- **Known limitation:** the docs portal reads all repositories with the service token, so it is protected with HTTP basic auth (`docs` / `DOCS_PASSWORD` in the tenant `.env`) until proper SSO exists. Webhooks are not reachable from outside.
- Not built yet: per-tenant backups and restore tests, SSO (Keycloak/Authentik), billing.

## End-to-end test
`python scripts/e2e.py` (the dev stack must be running) creates a project, work package and repo and tests the whole chain.

## Roadmap
- Stale detector: flag work packages without commit activity.
- EU-hosted LLM (e.g. Mistral): work package summaries and draft docs from closed tickets.
- Tenant backups, SSO, billing.
- Test the importers against real Jira/Confluence exports.

## Tests
```bash
cd glue && python -m pytest        # integration glue
python -m pytest tests_import      # importers (use the fake Atlassian)
```

## Contributing
Issues and pull requests are welcome, especially: real-world import bug reports (anonymised exports help a lot), translations, and integrations (other git hosts, notifications, SSO). Keep user-facing text and code in English.

## License
GPL-3.0, see [LICENSE](LICENSE).
