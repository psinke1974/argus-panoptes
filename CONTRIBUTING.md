# Contributing to Panoptes

Thanks for looking! Panoptes is early, so almost any contribution helps. All code, comments, user-facing text and docs are in English.

## Most useful right now
1. **Real-world import reports.** Run the importer against your (ideally anonymised or test) Jira/Confluence data and open an issue with what broke. Please include the command, the report the importer printed and, if you can, a minimal export that reproduces it. Never post real customer data or tokens.
2. **Integrations:** SSO (Keycloak/Authentik), other git hosts (GitLab, Forgejo), notifications (Slack/Teams/e-mail), per-assignee or Teams-format digests.
3. **Docs and translations.**

## Development setup
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r glue/requirements.txt -r panoptes_import/requirements.txt
(cd glue && python -m pytest)
python -m pytest tests_import
```
The end-to-end scripts need Docker: see the README ("Try it in about 5 minutes", then `python scripts/tenant_smoke.py demo`).

## Layout
- `glue/`: FastAPI service between Gitea and OpenProject (webhooks, release notes, stale report and weekly digest, docs portal, OIDC sign-in).
- `panoptes_import/`: Jira and Confluence importers (CSV/HTML export and REST API).
- `deploy/`: Caddy proxy, per-tenant compose file, `tenantctl.py` (create/list/enable-sso/digest/backup/restore/destroy).
- `tests_import/mock_atlassian.py`: a fake Jira/Confluence API that mimics the real response shapes.

## Pull requests
- Keep them focused, add or update tests, and run both test suites.
- Describe what you verified. If you could not test something against a real Jira/Confluence/OpenProject, say so.
- By contributing you agree that your contribution is licensed under GPL-3.0, like the rest of the project.

## Security
Please do not open public issues for vulnerabilities. Use GitHub's private vulnerability reporting on this repository.
