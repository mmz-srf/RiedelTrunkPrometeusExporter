# Spezifikaion Riedel Trunk Navigator Log Exporter


## Ausgangslage und Ziel

Auf zwei Windows 2022 Servern läuf die Riedel Applikation Riedel Trunk Navigator diese liefert keine Daten um das System zu überwachen. Die Applikation läauft unter C:\Program Files\Riedel\Trunk Navigator_8.8 wobei sich der Pfad nach jeder softwareaktualisierung ändert.

In dem Verzeichniss loggt auch die Software mit einer Log rotation. Beispiel logs sieehe Verzeichiss Logfiles

Ich möchte ein Python skrip haben welche die logs liest und die intahlte für Prometeus zur verfügung stellt

Diese Applikation soll als Service auf dem Host laufen gestartet und überwacht mit NSSM Non Sucking Service Manager

## Betriebsmodus: Aktiv/Passiv

Der Trunk Navigator läuft als redundantes Paar (die zwei Server) und ist **nicht statisch** aktiv oder passiv. Der Modus wird im Log als Statuswechsel protokolliert:

```
Change Trunk Navigator Mode from "Undefined !!" to "Standby"
Change Trunk Navigator Mode from "Standby" to "Active"
Change Trunk Navigator Mode from "Active" to "Standby"
```

Beim Start ist der Modus zunächst `Undefined !!`, danach wechselt er zu `Standby` oder `Active`. In den Beispiellogs gibt es Instanzen, die tagelang stabil `Active` bleiben, aber auch Instanzen mit sehr häufigem Flapping zwischen `Active`↔`Standby` innerhalb von Minuten (z.B. U06.log, >30 Wechsel an einem Tag) - dies deutet auf Instabilität/Failover-Probleme hin und sollte als Metrik/Alarm erfasst werden.

Ein Konfliktfall zwischen den beiden Nodes des Paares ist ebenfalls sichtbar: `Reply result (-1). Error ...: Other TN activ - wait.`

## Zu exportierende Metriken

Basierend auf der Analyse der Beispiellogs (siehe Verzeichnis Logfiles) kommen folgende Metriken für den Prometheus-Export in Frage.

### Priorität hoch (Basis-Monitoring)

- **`trunknavigator_mode`** (Gauge, 0=Standby, 1=Active, -1=Undefined) - aus `Change Trunk Navigator Mode from "X" to "Y"`
- **`trunknavigator_mode_changes_total`** (Counter) - Anzahl Moduswechsel, zur Flapping-Erkennung
- **`artist_node_up{name=...}`** (Gauge) - Verbindungsstatus je Artist-Node, aus `Connected successfully` / `Could not connect ... timed out` / `Error connecting the socket ... refused`. Der Standortname wird automatisch aus dem Routing-Verkehr gelernt (IP -> NetAddr aus den Heartbeat-Zeilen, NetAddr -> Name aus `Source:`/`Dest:` der Call/Listen/Monitoring-Zeilen); bis ein Name gelernt ist, wird `net-<NetAddr>` bzw. ersatzweise die rohe IP verwendet.
- **`artist_node_connect_errors_total{name=...}`** (Counter) - Timeouts/Verbindungsfehler je Artist-Node (gleiche Namensauflösung wie oben). Über `trunk_navigator.ignore_timeout_ips` in `config.json` können IPs von absichtlich intermittenten Nodes von der Timeout-Zählung ausgenommen werden (`artist_node_up` bleibt für diese Nodes unverändert normal aussagekräftig).
- **`artist_controller_failover_total`** (Counter) - Umschaltung auf "2nd, redundant controller"
- **`trunknavigator_restarts_total`** (Counter) - aus `Application is starting...` / `Trunk Navigator started`
- **`trunknavigator_version_info`** (Info-Metrik) - aus Versionsstring, z.B. `Trunk Navigator 8.8.TN2-13.0d233bf`1
- **`trunknavigator_log_last_event_timestamp`** (Gauge) - Zeitstempel der letzten verarbeiteten Logzeile, zur Staleness-Erkennung falls Service/Log hängt

### Priorität mittel (Verbindungs-Detail)

- **`artist_link_resets_total{name=...}`** (Counter) - aus `... is not responding to a link check ... Resetting the connection`
- **`artist_connection_retry_delay_ms`** (Gauge) - aktueller Retry-Backoff, aus `Connection retry delay extended to Xms`
- **`trunknavigator_errors_total{type=...}`** (Counter) - generischer Fehler-Counter, gruppiert nach Fehlertyp via Regex über `Error`/`Exception`/`Timeout`-Zeilen

### Priorität niedrig (optional, nur falls Verkehrsstatistiken gewünscht)

- **`trunk_requests_total{result=success|failed}`** - aus `Trunk request successful` / `Ignored trunk request` / `Monitoring failed. No destinations available` / `Error in trunking net=...`
- **`trunk_port_status{net=...,port=...}`** (Gauge) - aus `Port went online` / `Port went offline`, `Trunked Panel went offline/active`
- **`trunk_calls_total{type=call|listen|monitor}`** (Counter) - aus `Call to port` / `Listen to port` / `Monitoring port`
- **`trunk_terminations_total`** (Counter) - aus `Kill Forced Event`, `Stop trunk request`, `Deallocate trunkline`
- **`net_heartbeat_ttl{net_addr=...,ip=...}`** (Gauge) - aus `Received network info ... NICount=..., TTL=..., NI-changed=...`