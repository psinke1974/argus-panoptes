# 👁 Panoptes

> *the all-seeing project tracker*

Argus Panoptes, de reus met honderd ogen, sliep nooit met alle ogen tegelijk dicht. Jira heeft honderd configuratieschermen en niemand weet meer welk scherm welk oog bestuurt. Panoptes is de middenweg: open-source, EU-gehost en US-onafhankelijk, met projecten en boards, git en documentatie aan elkaar gekoppeld. Géén labyrint van workflow-schema's, permission schemes en screen schemes (het Labyrint van Knossos had minder bochten, en Theseus had in elk geval een draad).

Gebouwd op [OpenProject](https://www.openproject.org) (GPLv3) en [Gitea](https://about.gitea.com) (MIT), met eigen lijm en een Atlassian-importer. Voor wie Atlassian wil ontvluchten zonder de onderwereld in te hoeven: de importer is jullie Charon.

| Mythe | In Panoptes |
|---|---|
| Argus' honderd ogen | `glue/`: ziet elke commit en PR, en werkt de tickets bij |
| De draad van Ariadne | `OP#123` en oude `PROJ-12`-sleutels leiden door git terug naar het ticket |
| Charon, de veerman | `werkplaats_import/`: brengt Jira en Confluence naar de overkant |
| De Augiasstallen | Jira's workflow-schema's, field configurations en screen schemes |
| Sisyphus | Een backlog in Jira die nooit leeg raakt (hier ook niet, maar het rolt lichter) |

*(De map en dockerprojectnaam heten voorlopig nog `werkplaats`.)*

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
- **Rechtstreeks via de API** (geen export nodig): zet je token in de omgeving en draai
  ```bash
  export ATLASSIAN_TOKEN=...            # Cloud: id.atlassian.com > Beveiliging > API-tokens
  python -m werkplaats_import jira-api --url https://bedrijf.atlassian.net --email jij@bedrijf.nl --project PROJ --identifier proj
  python -m werkplaats_import confluence-api --url https://bedrijf.atlassian.net --email jij@bedrijf.nl --space TEAM --repo owner/naam
  ```
  Jira Server/Data Center: `--server` en zonder `--email` (Personal Access Token als Bearer). Neemt ook bijlagen mee en koppelt assignees automatisch op e-mailadres als Jira dat teruggeeft. Confluence-macro's die niet bestaan in Markdown worden gemeld in plaats van stil weggelaten.
- **Niet (nog):** bijlagen bij Jira-issues, worklogs, issue-historie, aangepaste velden, workflows, Confluence-comments en -rechten, Confluence-XML-export.
- Types per project aanzetten kan niet via de OpenProject-API; de importer doet dat via `scripts/op-enable-types.sh`.
- Getest met eigen fixtures en een nep-Atlassian (`tests_import/mock_atlassian.py`), nog niet tegen een echte Jira/Confluence-omgeving of klantexport.

## End-to-end test
`python scripts/e2e.py` (stack moet draaien) maakt een project, werkpakket en repo aan en toetst de hele keten.

## Roadmap (ideeën voor eigen slimmigheden)
- Stale-detector: werkpakketten zonder commit-activiteit signaleren.
- LLM (EU-hosted, bijv. Mistral): werkpakket-samenvattingen en concept-docs uit gesloten tickets.
- Atlassian-importer (Jira CSV/JSON, Confluence export): de echte verkoopdrempel.
- SSO via Keycloak, multi-tenant provisioning, facturatie.

## Tests
`cd glue && python -m pytest`
