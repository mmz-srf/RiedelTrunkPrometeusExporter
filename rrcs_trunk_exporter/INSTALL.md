# Installation: RRCS Trunk Exporter (Windows Server 2022)

Voraussetzung: Der Riedel RRCS Interface Service läuft bereits lokal auf der Zielmaschine und beantwortet XML-RPC-Requests auf `http://127.0.0.1:8193/`.

## 1. Python installieren

1. Python 3.11+ (64-bit) von https://www.python.org/downloads/windows/ herunterladen und installieren.
2. Bei der Installation **"Add python.exe to PATH"** aktivieren.
3. Prüfen in PowerShell:
   ```powershell
   python --version
   ```

## 2. Applikation auf die Zielmaschine kopieren

Dateien nach `C:\Services\RRCSTrunkExporter\` kopieren:
- `rrcs_trunk_exporter.py`
- `requirements.txt`
- `config.json.example`

```powershell
New-Item -ItemType Directory -Force C:\Services\RRCSTrunkExporter
# Dateien hierher kopieren (robocopy, Netzwerkfreigabe, USB, etc.)
```

## 3. Virtuelle Umgebung anlegen und Abhängigkeiten installieren

```powershell
cd C:\Services\RRCSTrunkExporter
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
  "riedel": {
    "url": "http://127.0.0.1:8193/"
  },
  "metrics": {
    "port": 9200,
    "bind_address": "0.0.0.0"
  }
}
```

- `riedel.url`: Adresse des lokalen RRCS-Interface (normalerweise unverändert lassen).
- `metrics.port`: Port, auf dem `/metrics` für Prometheus bereitgestellt wird.
- `metrics.bind_address`: `0.0.0.0` falls Prometheus von einer anderen Maschine scraped, `127.0.0.1` falls nur lokal.

## 5. Manueller Testlauf (vor der Service-Installation)

```powershell
.\venv\Scripts\python.exe rrcs_trunk_exporter.py
```

In einer zweiten PowerShell-Session prüfen:
```powershell
curl.exe http://localhost:9200/metrics
```

Erwartete Ausgabe enthält u.a. `riedel_trunk_node_info`, `riedel_trunk_nodes_total`, `riedel_trunk_scrape_success`. Mit `Strg+C` beenden, sobald das funktioniert.

## 6. Windows Firewall (nur falls Prometheus remote scraped)

```powershell
New-NetFirewallRule -DisplayName "RRCS Trunk Exporter" -Direction Inbound -Protocol TCP -LocalPort 9200 -Action Allow
```

## 7. Als Service mit NSSM einrichten

1. NSSM herunterladen (https://nssm.cc/download) und z.B. nach `C:\nssm\nssm.exe` entpacken.
2. Service registrieren:
   ```powershell
   C:\nssm\nssm.exe install RRCSTrunkExporter "C:\Services\RRCSTrunkExporter\venv\Scripts\python.exe" "C:\Services\RRCSTrunkExporter\rrcs_trunk_exporter.py"
   C:\nssm\nssm.exe set RRCSTrunkExporter AppDirectory "C:\Services\RRCSTrunkExporter"
   C:\nssm\nssm.exe set RRCSTrunkExporter Start SERVICE_AUTO_START
   ```
3. Optional: stdout/stderr von NSSM zusätzlich mitschreiben lassen (hilfreich falls die Applikation schon vor der eigenen Logger-Initialisierung abstürzt, z.B. bei fehlender `config.json`):
   ```powershell
   C:\nssm\nssm.exe set RRCSTrunkExporter AppStdout "C:\Services\RRCSTrunkExporter\Log\service-stdout.log"
   C:\nssm\nssm.exe set RRCSTrunkExporter AppStderr "C:\Services\RRCSTrunkExporter\Log\service-stderr.log"
   ```
4. Service starten:
   ```powershell
   C:\nssm\nssm.exe start RRCSTrunkExporter
   ```

## 8. Verifikation

```powershell
Get-Service RRCSTrunkExporter
curl.exe http://localhost:9200/metrics
```

Bei Problemen: `C:\Services\RRCSTrunkExporter\Log\rrcs_trunk_exporter.log` prüfen (Anwendungs-Log, Rotation bei 100 MB, max. 5 Dateien = 500 MB gesamt).

## Deinstallation / Update

```powershell
C:\nssm\nssm.exe stop RRCSTrunkExporter
C:\nssm\nssm.exe remove RRCSTrunkExporter confirm
```

Danach Dateien austauschen und ab Schritt 3 wiederholen.
