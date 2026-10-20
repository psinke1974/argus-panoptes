# Werkplaats

EU-gehoste, open-source werkplek: projecten/boards, git en documentatie, aan elkaar gekoppeld.

| Onderdeel | Basis | Licentie | URL (lokaal) |
|---|---|---|---|
| Werkpakketten, boards, wiki | OpenProject | GPLv3 | http://localhost:8080 |
| Git | Gitea | MIT | http://localhost:3000 |
| Koppeling + slimmigheden | `glue/` (FastAPI) | eigen | http://localhost:8090 |

## Starten
```bash
cp .env.example .env            # vul OP_SECRET_KEY_BASE (openssl rand -hex 64), WEBHOOK_SECRET
docker compose up -d --build
```
1. OpenProject: login `admin` / `admin` (wijzig direct), maak API-token (Mijn account > Toegangstokens) en zet in `.env` als `OP_API_KEY`.
2. Gitea: maak beheerder via `docker compose exec -u git gitea gitea admin user create --admin --username ... --password ... --email ...`, maak een token en zet in `.env` als `GITEA_TOKEN`.
3. `docker compose up -d glue` en voeg in je Gitea-repo een webhook toe: `http://glue:8090/hooks/gitea`, secret = `WEBHOOK_SECRET`, events: push + pull request.

## Wat het nu doet
- Na een Jira-import werken ook de oude sleutels: `fixes PROJ-12` in een commit of PR wordt via `.import-state/` vertaald naar het nieuwe werkpakket (alleen sleutels die de importer kent).
- `OP#123` in commit of PR-tekst: commentaar op werkpakket 123 met link.
- PR geopend: status naar *In progress*. `fixes OP#123` + merge of push naar default branch: status naar *Closed*.
- `GET /release-notes?repo=owner/naam&base=v1&head=v2`: release notes in Markdown, gegroepeerd per werkpakkettype.

- Docs-in-repo: Markdown in `docs/` (en de root) wordt getoond op `http://localhost:8090/docs/<owner>/<repo>`, met navigatie, zoeken en een lijst gerelateerde werkpakketten (uit commits die het bestand raakten). Een commit met `OP#123` die docs wijzigt krijgt een doc-link in het werkpakket.
  - Bewuste keuze: OpenProject's API kan wiki-pagina's alleen lezen, niet schrijven. Git is daarom de bron en de glue levert het leesportaal.

## Atlassian-importer
Draait lokaal (`pip install -r werkplaats_import/requirements.txt`, leest `.env`).

```bash
# Jira: Issues > Exporteren > CSV (alle velden)
python -m werkplaats_import jira export.csv --identifier proj --project-name "Mijn project" --dry-run
python -m werkplaats_import jira export.csv --identifier proj --project-name "Mijn project" [--user-map gebruikers.csv]

# Confluence: Space-instellingen > Ruimte exporteren > HTML
python -m werkplaats_import confluence space-export.zip --space-name TEAM --repo owner/naam
```
- **Jira:** typen, statussen en prioriteiten worden gemapt (NL/EN; onbekend = fallback + rapport), hiërarchie (epic/parent), comments, `blocks`/`duplicates`/`relates`-links. Titel krijgt `[PROJ-123]` voor, zodat oude sleutels vindbaar blijven. Jira-wikimarkup wordt Markdown. Herhaalbaar: `.import-state/` onthoudt wat al is aangemaakt.
- **Gebruikers:** `--user-map` (csv: `jira_name,op_login`) koppelt assignees; anders komt de naam in de beschrijving.
- **Confluence:** paginaboom wordt mappenstructuur in `docs/`, bijlagen naar `docs/_bijlagen/`, links tussen pagina's blijven werken, info-/waarschuwingsblokken, codeblokken en tabellen worden Markdown. Resultaat staat meteen in het docs-portaal.
- **Niet (nog):** bijlagen bij Jira-issues, worklogs, issue-historie, aangepaste velden, workflows, Confluence-comments en -rechten, Confluence-XML-export.
- Types per project aanzetten kan niet via de OpenProject-API; de importer doet dat via `scripts/op-enable-types.sh`.
- Getest met eigen fixtures (`tests_import/`), nog niet met een echte klantexport.

## End-to-end test
`python scripts/e2e.py` (stack moet draaien) maakt een project, werkpakket en repo aan en toetst de hele keten.

## Roadmap (ideeën voor eigen slimmigheden)
- Stale-detector: werkpakketten zonder commit-activiteit signaleren.
- LLM (EU-hosted, bijv. Mistral): werkpakket-samenvattingen en concept-docs uit gesloten tickets.
- Atlassian-importer (Jira CSV/JSON, Confluence export): de echte verkoopdrempel.
- SSO via Keycloak, multi-tenant provisioning, facturatie.

## Tests
`cd glue && python -m pytest`
