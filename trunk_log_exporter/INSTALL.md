# Installation: Trunk Navigator Log Exporter (Windows Server 2022)

Voraussetzung: Der Riedel Trunk Navigator läuft bereits lokal auf der Zielmaschine (z.B. unter `C:\Program Files\Riedel\Trunk Navigator_8.8`) und schreibt dort seine rotierenden Logdateien.

## 1. Python installieren

1. Python 3.11+ (64-bit) von https://www.python.org/downloads/windows/ herunterladen und installieren.
2. Bei der Installation **"Add python.exe to PATH"** aktivieren.
3. Prüfen in PowerShell:
   ```powershell
   python --version
   ```

## 2. Applikation auf die Zielmaschine kopieren

Dateien nach `C:\Services\TrunkLogExporter\` kopieren:
- `trunk_log_exporter.py`
- `requirements.txt`
- `config.json.example`

```powershell
New-Item -ItemType Directory -Force C:\Services\TrunkLogExporter
# Dateien hierher kopieren (robocopy, Netzwerkfreigabe, USB, etc.)
```

## 3. Virtuelle Umgebung anlegen und Abhängigkeiten installieren

```powershell
cd C:\Services\TrunkLogExporter
python -m venv venv
.\venv\Scripts\pip.exe install -r requirements.txt
```

Das `Log`-Verzeichnis wird von der Applikation beim Start automatisch angelegt, falls es fehlt.

## 4. Konfiguration erstellen

`config.json.example` nach `config.json` kopieren und anpassen:

```powershell
Copy-Item config.json.example config.json
notepad config.json
```

```json
{
  "trunk_navigator": {
    "install_dir_glob": "C:\\Program Files\\Riedel\\Trunk Navigator_*",
    "log_file_glob": "Trunk Navigator*.log",
    "poll_interval_seconds": 1,
    "ignore_timeout_ips": [],
    "node_names": {
      "10.94.130.46": "Standort XY",
      "10.94.130.47": "Standort XY"
    }
  },
  "metrics": {
    "port": 9201,
    "bind_address": "0.0.0.0"
  }
}
```

- `trunk_navigator.install_dir_glob`: Suchmuster für das Installationsverzeichnis. Ändert sich der Pfad nach einem Software-Update (z.B. `Trunk Navigator_8.9`), wird automatisch das zuletzt geänderte passende Verzeichnis verwendet - keine Anpassung nötig.
- `trunk_navigator.log_file_glob`: Suchmuster für die Logdateien innerhalb des Installationsverzeichnisses. Bei Rotation wird automatisch die zuletzt geänderte (aktive) Datei weiterverfolgt. Falls die tatsächliche Namenskonvention auf dem Zielsystem abweicht, hier anpassen.
- `trunk_navigator.poll_interval_seconds`: Wartezeit zwischen den Prüfungen auf neue Logzeilen bzw. eine neue/rotierte Logdatei.
- `trunk_navigator.ignore_timeout_ips`: Liste von Artist-Node-IPs, die absichtlich nicht durchgehend online sind (z.B. mobile/temporäre Einheiten). Für diese IPs wird `artist_node_connect_errors_total{reason="timeout"}` nicht erhöht - `artist_node_up` zeigt weiterhin normal 0/1. Falls die Node über Primär- und Redundanz-IP verfügt und beide ignoriert werden sollen, beide eintragen.
- `trunk_navigator.node_names`: Feste IP-zu-Standortname-Zuordnung, als Ergänzung zur automatischen Namenserkennung (siehe unten). Nützlich für Nodes, über die nie Call/Listen-Traffic geroutet wird und die deshalb sonst dauerhaft als `net-<N>` oder IP angezeigt würden. Primär- und Redundanz-IP eines Standorts auf denselben Namen mappen, damit beide zu einer Zeitreihe zusammengeführt werden. Ein hier eingetragener Name hat immer Vorrang vor einem automatisch gelernten.
- `metrics.port`: Port, auf dem `/metrics` für Prometheus bereitgestellt wird.
- `metrics.bind_address`: `0.0.0.0` falls Prometheus von einer anderen Maschine scraped, `127.0.0.1` falls nur lokal.

## 5. Manueller Testlauf (vor der Service-Installation)

```powershell
.\venv\Scripts\python.exe trunk_log_exporter.py
```

Der Exporter beginnt am **Ende** der aktuell aktiven Logdatei zu lesen (Tail-Modus) und wertet ab diesem Zeitpunkt neue Zeilen aus. Um die Metriken zu sehen, in Trunk Navigator einen Modus- oder Verbindungswechsel auslösen (oder einfach etwas warten, das System loggt laufend).

In einer zweiten PowerShell-Session prüfen:
```powershell
curl.exe http://localhost:9201/metrics
```

Erwartete Ausgabe enthält u.a. `trunknavigator_mode`, `trunknavigator_log_tailer_up`, `artist_node_up`, `trunknavigator_version_info`. Mit `Strg+C` beenden, sobald das funktioniert.

**Hinweis zu `artist_node_up{name=...}`:** Der Exporter lernt den Standortnamen primär automatisch aus dem Routing-Verkehr im Log (z.B. `Zürich`, `Genève 20`, `SRF OB 5`). Solange für eine Artist-Node-IP noch kein Name gelernt wurde, erscheint sie als `net-<NetAddr>` oder, falls noch nicht einmal die NetAddr bekannt ist, als rohe IP-Adresse - das betrifft insbesondere Nodes, über die während der Laufzeit nie tatsächlich ein Call geroutet wird (z.B. reine Redundanz-Controller). Für solche Fälle in `trunk_navigator.node_names` einen festen Namen eintragen (siehe Schritt 4) - der Wert von `artist_node_up` ist auch ohne Namen bereits korrekt, es fehlt nur die Lesbarkeit.

## 6. Windows Firewall (nur falls Prometheus remote scraped)

```powershell
New-NetFirewallRule -DisplayName "Trunk Log Exporter" -Direction Inbound -Protocol TCP -LocalPort 9201 -Action Allow
```

## 7. Als Service mit NSSM einrichten

1. NSSM herunterladen (https://nssm.cc/download) und z.B. nach `C:\nssm\nssm.exe` entpacken.
2. Service registrieren:
   ```powershell
   C:\nssm\nssm.exe install TrunkLogExporter "C:\Services\TrunkLogExporter\venv\Scripts\python.exe" "C:\Services\TrunkLogExporter\trunk_log_exporter.py"
   C:\nssm\nssm.exe set TrunkLogExporter AppDirectory "C:\Services\TrunkLogExporter"
   C:\nssm\nssm.exe set TrunkLogExporter Start SERVICE_AUTO_START
   ```
3. Optional: stdout/stderr von NSSM zusätzlich mitschreiben lassen (hilfreich falls die Applikation schon vor der eigenen Logger-Initialisierung abstürzt, z.B. bei fehlender `config.json`):
   ```powershell
   C:\nssm\nssm.exe set TrunkLogExporter AppStdout "C:\Services\TrunkLogExporter\Log\service-stdout.log"
   C:\nssm\nssm.exe set TrunkLogExporter AppStderr "C:\Services\TrunkLogExporter\Log\service-stderr.log"
   ```
4. Service starten:
   ```powershell
   C:\nssm\nssm.exe start TrunkLogExporter
   ```

## 8. Verifikation

```powershell
Get-Service TrunkLogExporter
curl.exe http://localhost:9201/metrics
```

Bei Problemen: `C:\Services\TrunkLogExporter\Log\trunk_log_exporter.log` prüfen (Anwendungs-Log, Rotation bei 100 MB, max. 5 Dateien = 500 MB gesamt). Insbesondere `trunknavigator_log_tailer_up` und `trunknavigator_current_log_file_info` zeigen, ob überhaupt eine Logdatei gefunden und geöffnet wurde.

## Deinstallation / Update

```powershell
C:\nssm\nssm.exe stop TrunkLogExporter
C:\nssm\nssm.exe remove TrunkLogExporter confirm
```

Danach Dateien austauschen und ab Schritt 3 wiederholen.

## Hinweis zu den zwei Servern

Auf beiden Servern des redundanten Trunk-Navigator-Paars läuft eine eigene Instanz dieses Exporters mit eigener `config.json` (z.B. unterschiedlicher `bind_address`, falls nötig). Prometheus scraped beide Instanzen einzeln; der Modus (`trunknavigator_mode`) zeigt pro Host an, ob dieser aktuell aktiv oder passiv ist.
