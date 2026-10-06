# AGENTS.md

Hinweise für KI-Agenten, die in diesem Repository arbeiten.

## Monitoring-Projektstruktur

Das Monitoring-System besteht aus drei separaten Repositories, die als Geschwisterordner liegen. Relative Pfade ausgehend von diesem Repo:

- **API**: `../Monitoring-API`
- **Frontend**: `../Monitoring-Frontend`
- **Client**: `.` (dieses Repo)

Wenn Änderungen in diesem Client mit der API oder dem Frontend zusammenspielen, sollten die entsprechenden Repos referenziert oder mitgepflegt werden.

## Branch-Workflow

Neue Feature-/Fix-Branches **immer von `main`** erstellen:

```bash
git fetch origin
git checkout main
git pull origin main
git checkout -b fix/mein-fix
```

**Wichtig:**

- **Niemals** direkt auf `main` pushen oder committen.
- Alle Änderungen laufen über einen Feature-/Fix-Branch → Pull Request → Review → Merge nach `main`.
- Nach dem Merge nach `main` (durch Mensch/Review) deployt Coolify automatisch, falls für dieses Repo zutreffend.
- Jede Code-Änderung am Agent (`agent/`) muss `AGENT_VERSION` in `agent/models/constants.py` im selben Commit erhöhen — siehe [Client Version Bump](../Monitoring-API/AGENTS.md#client-version-bump). Ohne Bump erkennt der Auto-Updater die neue Version nicht und ausgerollte Agents bleiben auf dem alten Stand.

## CI & Security (GitHub Actions)

Der Client hat **keinen** Coolify-Deploy; Sicherheit läuft über GitHub Actions auf jedem PR/`main`-Push und wöchentlich:

| Workflow | Jobs (Hard Gate) | Soft / report |
|----------|------------------|---------------|
| `CI` | Pytest, ShellCheck (error), Bandit (medium+) | — |
| `Security` | Gitleaks, Trivy FS CRITICAL | Semgrep (p/default + p/python + OWASP) |

Zusätzlich: Dependabot für `github-actions` (wöchentlich).

**Agent-Hinweis:** Änderungen an `.github/workflows/security.yml` / `.gitleaks.toml` / `.bandit` mit den Geschwister-Repos (API/Frontend) abstimmen — gleiche Action-Pins und Gitleaks-Version wie iboys/ServerMetry-API.

## GitHub Release (nach Merge nach `main`)

Der Agent-Auto-Updater und das Dashboard („Update verfügbar“) nutzen die **GitHub Releases API** (`/releases/latest`) und fallen sonst auf `AGENT_VERSION` in `main` zurück. Deshalb nach jedem Version-Bump **ein Release anlegen**, sobald der PR auf `main` gemerged ist.

### Ablauf (jedes Mal bei neuer Version)

1. PR mergen → `main` enthält die neue `AGENT_VERSION` (z. B. `1.4.6`).
2. Parallel in der **API** `DEFAULT_LATEST_AGENT_VERSION` auf denselben Wert setzen (eigenes PR / gleicher Release-Zug) — siehe API-`AGENTS.md` → Client Version Bump.
3. GitHub Release erstellen (Tag = `v` + Version, Target = `main`):

```bash
# Aus dem Client-Repo (main aktuell, Version aus constants.py):
VERSION=$(grep -oP 'AGENT_VERSION\s*=\s*"\K[^"]+' agent/models/constants.py)

gh release create "v${VERSION}" \
  --repo FloBaMedia/servermetry-client \
  --target main \
  --title "ServerMetry Agent v${VERSION}" \
  --notes "$(cat <<EOF
## Changes
- <kurze Bullet Points aus dem PR / Changelog>

Pair with API \`DEFAULT_LATEST_AGENT_VERSION = ${VERSION}\`.
EOF
)"
```

Oder mit fester Version:

```bash
gh release create v1.4.6 \
  --repo FloBaMedia/servermetry-client \
  --target main \
  --title "ServerMetry Agent v1.4.6" \
  --notes "## Changes
- …
"
```

4. Prüfen: https://github.com/FloBaMedia/servermetry-client/releases/latest zeigt die neue Version; Agents mit `enableAutoUpdates` ziehen sie beim nächsten Lauf.

**Hinweise für Agents:**

- Release **erst nach Merge** erstellen, nie vom Feature-Branch-Tip taggen (außer explizit gewünscht).
- Tag-Format immer `vX.Y.Z` (mit führendem `v`), Titel `ServerMetry Agent vX.Y.Z` — wie bei bestehenden Releases.
- Kein Asset-Upload nötig: Installer und Updater laden Quelldateien von GitHub (`raw` / tree); das Release dient der Versionserkennung.
- `install.sh` auf Hosts zieht weiterhin von `main` (`raw.githubusercontent.com/.../main/agent/...`). Das Release ist primär für Auto-Update und Dashboard-Vergleich.
- Wenn `gh` nicht im PATH liegt, lokalen Wrapper nutzen (z. B. unter `~/.local/share/.../gh-cli/gh`).
