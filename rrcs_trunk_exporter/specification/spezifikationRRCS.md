# Spezifikaion RRCS Trunk Exporter


## Ausgangslage und Ziel

Auf einem Windows 2022 Server läuf die Riedel Applikation RRCS interface, Spezifikationen siehe RRCS Interface Specification_8_8_1_Rev1.pdf

über HTML Post request auf den lokalen server port http://127.0.0.1:8193/ soll die XML-RPC Daten vom RRCS ausgelesen werden. Diese Daten sollen aufbereitet als "localip"/metrics port definierbar über ein config file zur verfügung gestellt werden damit diese von Prometheus gescraped werden kann.


Diese Applikation soll als Service auf dem Host laufen gestartet und überwacht mit NSSM Non Sucking Service Manager

## RRCS detail

Folgendes Kommano soll aufgerufen werden wenn Prometheus die /metrics aufruft.

<?xml version="1.0" encoding="UTF-8"?>
<methodCall>
<methodName>GetTrunkPorts</methodName>
</methodCall>


Eine Beispielantwort ist hier zu finden RRCS_GetTrunPorts_response

Diese soll geparsed werden und folgende Metriken daraus gebildet werden:

### Datenstruktur

Die Antwort ist ein XML-RPC `methodResponse` mit dem relevanten Pfad `params/param/value/array/data/value/array/data/value/struct`. Jeder `struct` beschreibt einen Port mit u.a. `NetTrAddr` (Node-ID) und `NetName` (Node-Name). Ein Node wiederholt sich über viele Port-Structs. Für die Metriken interessieren nur die Nodes (eindeutige `NetTrAddr`/`NetName`-Kombinationen), nicht die einzelnen Port-Details.

### Parsing

1. XML-RPC Response mit `xml.etree.ElementTree` parsen.
2. Über alle `struct`-Elemente iterieren, `NetTrAddr` und `NetName` extrahieren.
3. In einem Dict `{net_trunk_address: net_name}` sammeln (Dedup über den Dict-Key).
4. Der RRCS-Request wird live bei jedem `/metrics`-Scrape ausgeführt (kein Caching), umgesetzt über einen Custom Prometheus Collector.
5. Der HTTP-Request an RRCS muss mit einem Timeout (z.B. 5s) abgesichert sein. Ohne Timeout würde ein nicht antwortendes RRCS-Interface den `/metrics`-Aufruf unbegrenzt blockieren, statt `riedel_trunk_scrape_success 0` zurückzugeben.

### Metriken

Prefix: `riedel_trunk_`

| Metrik | Typ | Labels | Beschreibung |
|---|---|---|---|
| `riedel_trunk_node_info` | Gauge, Wert immer `1` | `node_id`, `node_name` | Ein Zeitreihen-Eintrag pro aktuell von RRCS gemeldetem Node. |
| `riedel_trunk_nodes_total` | Gauge, keine Labels | – | Anzahl eindeutiger Nodes im letzten Scrape. |
| `riedel_trunk_scrape_success` | Gauge, keine Labels | – | `1` wenn der letzte RRCS-Request+Parse erfolgreich war, sonst `0`. |

Beispiel:
```
riedel_trunk_node_info{node_id="76",node_name="Zürich UHD 6"} 1
riedel_trunk_node_info{node_id="73",node_name="ZH_UHD3"} 1
riedel_trunk_node_info{node_id="2",node_name="Zürich 2"} 1
riedel_trunk_nodes_total 3
riedel_trunk_scrape_success 1
```
