# itsyncs — Performance-Daten und Betriebsempfehlungen

Empirische Werte aus dem Initial-Deployment + Stress-Testing auf
`workbench01.baecktrade.de` (Mai 2026, Quellpostfach ~2.600 Kontakte,
Ziel: Shared Mailbox 3CX und Microsoft 365 GAL).

## Durchsatz-Kennzahlen

### Initial-Sync

| Ziel-Typ | Kontakte | Dauer | Effektive Rate | Bemerkung |
|---|---:|---:|---:|---|
| Shared Mailbox (Graph `/contacts`) | 2.607 | 8 min | ~5,4 Kontakte/s | je Kontakt 1× POST mit allen Feldern |
| GAL (Exchange `New-MailContact`) | 2.113 erfolgreich, 62 Fehler | 2,7 min | ~13 Kontakte/s im Mapping-Pfad, ~3/s im Create-Pfad | Match-Path ist schnell (nur DB-Insert), Create-Path teuer (Replikation + Set-Contact) |

GAL-Create läuft im **Delayed-Batch-Pattern**: Chunks à 50 Kontakte, `New-MailContact` in
einem Burst, dann **15 s Replikations-Wartezeit**, dann `Set-Contact` für die Rich-Fields.
Wartezeit ist nicht reduzierbar (Exchange-Replikation zwischen Backends).

### Incremental-Sync (steady state)

| Pair-Typ | Dauer | Charakteristik |
|---|---:|---|
| Mailbox, keine Änderungen | 3–6 s | nur Reconcile-Check + Delta-Query |
| GAL, keine Änderungen + Konflikt-Baseline | 90–200 s | Reconcile re-versucht persistente Verzeichnis-Konflikte bei jedem Lauf |

### Einzel-Operationen

| Operation | Mailbox-Ziel | GAL-Ziel |
|---|---:|---:|
| Create (1 Kontakt) | 6 s | 100–120 s (1 Batch-Chunk + Replikation) |
| Update | 4 s | ~100 s |
| Delete | 3 s | ~100 s |

### Multi-Op in einem Lauf

3 Creates + 2 Updates + 1 Delete (Mailbox-Ziel, getestet):
**3 s gesamt** — Delta-Query erkennt alle Änderungen in einem Durchgang.

### Massen-Operationen

**Beispiel: 3CX-Postfach-Purge (114.730 Kontakte)**

- Mechanik: Graph `$batch`, **20 DELETEs/Request**, 5 Batches à 100 Kontakte pro Runde
- Gesamt-Dauer: **~2 h**
- Effektive Rate: ~27 Deletes/s (Burst 33/s)
- Throttling: 14× HTTP 429 in 2 h, alle mit `Retry-After` sauber abgefangen
- Empfehlung: Detached-Prozess (`setsid nohup`), Logging in File, idempotent (Wieder-Anlauf möglich)

## Skalierungsverhalten

| Dimension | Skalierung | Sweet Spot |
|---|---|---|
| Quell-Kontakte | linear (~0,18 s/Kontakt Mailbox, ~0,3 s/Kontakt GAL match) | bis ~10.000 unkritisch |
| Mapping-Tabelle | linear, Index-getriebene Lookups (`BINARY` SQL) | < 1 ms je Lookup auch bei 100k |
| Concurrent Pairs | sequentiell pro Long-Worker | aktuelle Bench: 1 Long-Worker → keine Parallelität |
| Token-Cache | 60–75 min Lebenszeit, auto-refresh bei < 5 min Restzeit | für stunden-/tagelange Jobs ausreichend |

## Schedule-Empfehlungen

| Pair-Typ | Empfohlenes Intervall | Begründung |
|---|---|---|
| Mailbox → Mailbox | **30 min** | Jeder Lauf 3–6 s, kann auch alle 15 min |
| Mailbox → GAL | **1–6 h** | Jeder Lauf ~2–3 min wegen Konflikt-Retries; häufiger lohnt nicht |
| Sage-SQL-Quelle | **15–30 min** | Sage delta via Rowversion ist günstig |

**Wichtig:** `initial_sync_complete=0` blockt den Scheduler — Initial-Sync **immer manuell**
und kontrolliert auslösen, nicht über Scheduler.

## Throttling / Robustheit

- Graph mailbox-Operationen: dokumentiertes Limit ~10.000 Requests/10 min pro App+Mailbox.
  itsyncs liegt deutlich darunter, ausgenommen Massen-Purge.
- Bei 429 sendet Graph `Retry-After`-Header — **immer respektieren**.
- Exchange-`InvokeCommand` hat eigene 429/503-Logik (`_invoke_command` im Code regelt das).
- Outlook-Item-IDs sind Standardmäßig **case-sensitive** — Mapping-Spalten **müssen** in einer
  Binary-Collation liegen (`utf8mb4_bin`), siehe Patch `v0_0_3`. Code-seitig zusätzlich
  `BINARY %s` in allen ID-Lookups (siehe `_find_mapping`).

## Operative Hinweise

### Deployment-Routine
1. Code-Änderung auf dem Dev-Branch committen + pushen.
2. Server: `git fetch upstream && git reset --hard upstream/version-16` (als `frappe`-User,
   nicht root — sonst Besitzrechte-Konflikt im `.git/`).
3. `bench --site <site> migrate` — auch wenn nur Code (sicher idempotent).
4. **`bench restart` zwingend** — RQ-Worker importieren App-Code beim Start, ohne Restart
   läuft alter Code weiter.

### Server-Git-Hygiene
- Server-Repo immer als `frappe`-User pullen (Repo gehört `frappe`). Root-Operationen
  hinterlassen root-owned Files in `.git/`, was spätere frappe-Pulls bricht.
- Wenn `git fetch` direkten GitHub-Zugriff vom Server nicht hat: Git-Bundle vom Dev-Rechner
  scp'en und lokal mit `git fetch /tmp/bundle <branch>` anziehen.

### Ad-hoc-Skripte im Bench-Kontext
```bash
echo 'exec(open("/tmp/script.py").read(), {"frappe": frappe})' \
  | sudo -u frappe bench --site <site> console
```
Das `{"frappe": frappe}` ist nötig: ohne Namespace-Dict bekommen `def`s im exec'd Skript
keinen Zugriff auf Modul-Imports.

### Manueller Sync-Trigger
```python
log = frappe.new_doc("ITSync Log")
log.sync_pair = "<pair>"
log.sync_type = "Initial"  # oder "Incremental"
log.started_at = frappe.utils.now_datetime()
log.status = "Running"
log.insert(ignore_permissions=True)
frappe.db.commit()
frappe.enqueue(
    "itsyncs.sync.engine.run_sync",
    pair_name="<pair>", sync_type="Initial", log_name=log.name,
    queue="long", timeout=7200, deduplicate=True,
    job_id="manual_" + log.name,
    on_failure="itsyncs.sync.engine.mark_sync_failed",
)
```

### Monitoring
- `tabITSync Log.error_count > 0` über mehrere Läufe → Daten-Problem im Quell- oder Zielsystem,
  kein transientes Sync-Issue.
- `tabITSync Log.progress_phase` während laufender Syncs für UI-Polling nutzen
  (wird vom Engine alle 50 Kontakte aktualisiert).
- Mapping-Anomalie-Check: `COUNT(*)` der Mappings sollte ≈ Quell-Kontakt-Anzahl sein.
  Größere Abweichung → Konflikte oder Sync-Problem.

## Anti-Patterns / Lessons learned

- **Case-insensitive Collation auf opaken IDs** — fataler Bug-Boden (Mai 2026).
  Konsequenz: still wirkende „Mapping-Cap", die Tausende Dubletten ins Ziel pumpt.
  Korrekt: `utf8mb4_bin` für ID-Spalten, im Code zusätzlich `BINARY %s`.
- **Eligibility-Filter vor API-Call** — kein E-Mail für GAL-Ziel → vorab skippen,
  sonst pro Kontakt 3 s API-Zeit auf garantiertem Fehler.
- **Reconcile-Phase in Incremental** — fängt verlorene Mappings ab (Worker-SIGKILL,
  Replikations-Race, Server-Reboot mitten im Initial). Wegfall = sukzessive Daten-Drift.
- **Direkt-Edit auf Server ohne Commit** — wenn der Server-Working-Tree von Git divergiert,
  ist beim Deploy unklar, was verloren geht. Server-Git **immer** auf dem Commit halten,
  der dem Working-Tree entspricht.
