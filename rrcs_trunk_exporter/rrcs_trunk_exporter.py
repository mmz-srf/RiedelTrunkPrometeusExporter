import json
import logging
import logging.handlers
import os
import sys
import time
import xml.etree.ElementTree as ET

import requests
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from requests.exceptions import RequestException

GET_TRUNK_PORTS_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<methodCall>
    <methodName>GetTrunkPorts</methodName>
</methodCall>"""

REQUEST_TIMEOUT_SECONDS = 5

logger = logging.getLogger('rrcs_trunk_exporter')
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s - %(funcName)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

LOG_DIR = './Log'
os.makedirs(LOG_DIR, exist_ok=True)

# Keeps at most 5 log files total (1 active + 4 rotated backups) at up to 100 MB each, i.e. 500 MB max on disk.
file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, 'rrcs_trunk_exporter.log'),
    maxBytes=100 * 1024 * 1024,
    backupCount=4
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


def load_config(file_path="config.json") -> dict:
    try:
        with open(file_path, 'r') as file:
            config = json.load(file)
            logger.info(f"Load Config: Config loaded from {file_path}")
    except FileNotFoundError:
        logger.error(f"Load Config: Error: Config file not found at {file_path}")
        sys.exit(1)

    return config


def get_xml_from_rrcs(url: str) -> str:
    headers = {"Accept": "text/xml",
               "Content-Type": "text/xml"}

    response = requests.post(url, headers=headers, data=GET_TRUNK_PORTS_BODY, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    logger.info(f"Get XML from RRCS: XML data fetched from {url}")

    return response.text


def parse_nodes(xml: str) -> dict:
    root = ET.fromstring(xml)

    nodes = {}
    for struct in root.findall('.//params/param/value/array/data/value/array/data/value/struct'):
        net_trunk_address = None
        net_name = None
        for member in struct:
            name = member.find('name').text
            if name == 'NetTrAddr':
                net_trunk_address = member.find('value')[0].text
            elif name == 'NetName':
                net_name = member.find('value')[0].text

        if net_trunk_address is not None:
            nodes[net_trunk_address] = net_name

    logger.info(f"Parse Nodes: Nodes found: {len(nodes)}")
    return nodes


class RrcsTrunkCollector(Collector):
    def __init__(self, url: str):
        self.url = url

    def collect(self):
        node_info = GaugeMetricFamily(
            'riedel_trunk_node_info',
            'RRCS trunk node presence (always 1, one series per known node)',
            labels=['node_id', 'node_name']
        )
        nodes_total = GaugeMetricFamily(
            'riedel_trunk_nodes_total',
            'Number of distinct RRCS trunk nodes in the last scrape'
        )
        scrape_success = GaugeMetricFamily(
            'riedel_trunk_scrape_success',
            '1 if the last GetTrunkPorts request and parse succeeded, 0 otherwise'
        )

        try:
            xml = get_xml_from_rrcs(self.url)
            nodes = parse_nodes(xml)
            for node_id, node_name in nodes.items():
                node_info.add_metric([node_id, node_name], 1)
            nodes_total.add_metric([], len(nodes))
            scrape_success.add_metric([], 1)
        except (RequestException, ET.ParseError) as e:
            logger.error(f"RrcsTrunkCollector: scrape failed: {e}")
            scrape_success.add_metric([], 0)

        yield node_info
        yield nodes_total
        yield scrape_success


def main() -> None:
    config = load_config()

    REGISTRY.register(RrcsTrunkCollector(config['riedel']['url']))
    start_http_server(config['metrics']['port'], addr=config['metrics'].get('bind_address', '0.0.0.0'))
    logger.info(f"Main: Metrics server started on port {config['metrics']['port']}")

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
