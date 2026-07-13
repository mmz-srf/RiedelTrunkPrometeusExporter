import glob
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from datetime import datetime

from prometheus_client import Counter, Gauge, Info, start_http_server

logger = logging.getLogger('trunk_log_exporter')
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s - %(funcName)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

LOG_DIR = './Log'
os.makedirs(LOG_DIR, exist_ok=True)

# Keeps at most 5 log files total (1 active + 4 rotated backups) at up to 100 MB each, i.e. 500 MB max on disk.
file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, 'trunk_log_exporter.log'),
    maxBytes=100 * 1024 * 1024,
    backupCount=4
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# --- Prometheus metrics ---------------------------------------------------

MODE_MAP = {'Active': 1, 'Standby': 0, 'Undefined !!': -1}
MODE_DISPLAY = {'Active': 'Active', 'Standby': 'Standby', 'Undefined !!': 'Undefined'}

MODE = Gauge('trunknavigator_mode', 'Trunk Navigator operating mode (1=Active, 0=Standby, -1=Undefined)')
MODE_INFO = Info('trunknavigator_mode_state', 'Trunk Navigator operating mode as text (Active/Standby/Undefined)')
MODE_CHANGES_TOTAL = Counter('trunknavigator_mode_changes_total', 'Number of Trunk Navigator mode changes observed')
RESTARTS_TOTAL = Counter('trunknavigator_restarts_total', 'Number of Trunk Navigator application starts observed in the log')
VERSION_INFO = Info('trunknavigator_version', 'Trunk Navigator version reported in the log')
LOG_LAST_EVENT_TIMESTAMP = Gauge('trunknavigator_log_last_event_timestamp', 'Unix timestamp of the last log line processed')
LOG_TAILER_UP = Gauge('trunknavigator_log_tailer_up', '1 if the exporter currently has a Trunk Navigator log file open, 0 otherwise')
CURRENT_LOG_FILE = Info('trunknavigator_current_log_file', 'Path of the Trunk Navigator log file currently being tailed')
ERRORS_TOTAL = Counter('trunknavigator_errors_total', 'Log lines containing an unclassified error, by type', ['type'])

NODE_UP = Gauge(
    'artist_node_up',
    'Connection status to an Artist node (1=connected, 0=not connected), keyed by the site name learned '
    'from routing traffic (falls back to "net-<NetAddr>" or the raw IP until a name has been learned)',
    ['name']
)
NODE_CONNECT_ERRORS_TOTAL = Counter('artist_node_connect_errors_total', 'Connection errors to an Artist node', ['name', 'reason'])
CONTROLLER_FAILOVER_TOTAL = Counter('artist_controller_failover_total', 'Failovers to the 2nd, redundant Artist controller')
LINK_RESETS_TOTAL = Counter('artist_link_resets_total', 'Link check failures leading to a connection reset', ['name'])
CONNECTION_RETRY_DELAY_MS = Gauge('artist_connection_retry_delay_ms', 'Current connection retry delay in milliseconds')

# IPs of Artist nodes that are known to be intermittently offline by design;
# their connection timeouts are expected and not counted as errors. Populated
# from config in main(). artist_node_up is still reported normally for them.
IGNORED_TIMEOUT_IPS: set[str] = set()

# --- Artist node name resolution -------------------------------------------
#
# The log never states a human-readable name for the IP a Trunk Navigator
# connects to. It can only be derived indirectly:
#   1. Heartbeat lines ("... NetAddr=<N>, IP=<ip> ...") link an IP to its NetAddr.
#   2. Routing lines ("Source: <name>::<device> (net:<N>, port:<P>)") link a
#      NetAddr to the site name used in call routing.
# A name therefore only becomes known once both pieces have been observed at
# least once; until then, metrics for that node are labelled with "net-<N>"
# or, if not even the NetAddr is known yet, the raw IP.

ip_to_net_addr: dict[str, int] = {}
net_addr_to_name: dict[int, str] = {}
ip_current_label: dict[str, str] = {}
# Last known artist_node_up value per ip, so a rename can carry it forward
# onto the new label - a stable, already-connected node may never produce
# another "Connected"/"Could not connect" line to re-set it otherwise.
ip_up_state: dict[str, float] = {}
# Optional user-supplied IP -> name overrides from config (trunk_navigator.node_names),
# for nodes that never have routing traffic to learn a name from automatically.
# Takes priority over the auto-learned name if both are present.
STATIC_NODE_NAMES: dict[str, str] = {}


def resolve_label(ip: str) -> str:
    if ip in STATIC_NODE_NAMES:
        return STATIC_NODE_NAMES[ip]
    net_addr = ip_to_net_addr.get(ip)
    if net_addr is None:
        return ip
    return net_addr_to_name.get(net_addr, f'net-{net_addr}')


def relabel(ip: str) -> None:
    new_label = resolve_label(ip)
    # Before the first relabel() call for this ip, resolve_label(ip) would
    # have returned the raw ip itself (see resolve_label above) - so a metric
    # series may already exist under that label even though ip_current_label
    # was never explicitly set. Default to "ip" here, not None, or that stale
    # series never gets cleaned up on the first rename.
    old_label = ip_current_label.get(ip, ip)
    ip_current_label[ip] = new_label
    if old_label == new_label:
        return

    logger.info(f"Relabel: Artist node {ip} identified as {new_label!r} (was {old_label!r})")
    if ip in ip_up_state:
        NODE_UP.labels(name=new_label).set(ip_up_state[ip])
    try:
        NODE_UP.remove(old_label)
    except KeyError:
        pass
    try:
        LINK_RESETS_TOTAL.remove(old_label)
    except KeyError:
        pass
    for reason in ('timeout', 'refused'):
        try:
            NODE_CONNECT_ERRORS_TOTAL.remove(old_label, reason)
        except KeyError:
            pass


def learn_net_addr(ip: str, net_addr: int) -> None:
    if ip_to_net_addr.get(ip) == net_addr:
        return
    ip_to_net_addr[ip] = net_addr
    relabel(ip)


def learn_net_name(net_addr: int, name: str) -> None:
    if net_addr_to_name.get(net_addr) == name:
        return
    net_addr_to_name[net_addr] = name
    for ip, ip_net_addr in ip_to_net_addr.items():
        if ip_net_addr == net_addr:
            relabel(ip)

# --- Log line parsing ------------------------------------------------------

IP = r'\d{1,3}(?:\.\d{1,3}){3}'

LOG_LINE_RE = re.compile(
    r'^(?P<day>\d{2})\.(?P<month>\d{2})\.(?P<year>\d{4})\s+'
    r'(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+'
    r'(?P<millis>\d{3})ms\t(?P<message>.*)$'
)

MODE_CHANGE_RE = re.compile(r'^Change Trunk Navigator Mode from "(?P<from>[^"]*)" to "(?P<to>[^"]*)"$')
CONNECTED_RE = re.compile(rf'^Connected successfully to the Artist node at (?P<ip>{IP}):\d+(?P<redundant> \(the 2nd, redundant controller\))?\.$')
TIMEOUT_RE = re.compile(rf'^Could not connect to the Artist node \(IP address =(?P<ip>{IP})\), the connection attempt timed out')
REFUSED_RE = re.compile(rf'^Error connecting to the Artist node at (?P<ip>{IP}):\d+ : Error connecting the socket')
LINK_RESET_RE = re.compile(rf'^The Artist node \(IP address = (?P<ip>{IP})\) is not responding to a link check.*Resetting the connection')
RETRY_DELAY_RE = re.compile(r'^Connection retry delay extended to (?P<ms>\d+) millisecond')
RESTART_RE = re.compile(r'^Application is starting\.\.\.')
VERSION_RE = re.compile(r'^Trunk Navigator (?P<version>\d+\.\d+\.\S+)$')
NET_INFO_RE = re.compile(rf'^(?:Received network info from|Send network info from TN-ID=\S+ to) NetAddr=(?P<net_addr>\d+), IP=(?P<ip>{IP})')
GENERIC_ERROR_RE = re.compile(r'\bError\b|\bException\b')

# Matches "<name>::<device> (net:<N>, port:<P>)", as used for both the
# "Source:" and "Dest:" of "Call to port" / "Listen to port" / "Monitoring
# port" lines, to learn the site name for a NetAddr.
SOURCE_DEST_NAME_RE = re.compile(r'(?P<name>[^:()]+)::[^()]+\(net:(?P<net_addr>\d+), port:\d+\)')


def handle_mode_change(m: re.Match) -> None:
    to_mode = m.group('to')
    MODE.set(MODE_MAP.get(to_mode, -1))
    MODE_INFO.info({'mode': MODE_DISPLAY.get(to_mode, to_mode)})
    MODE_CHANGES_TOTAL.inc()


def set_node_up(ip: str, value: float) -> None:
    ip_up_state[ip] = value
    NODE_UP.labels(name=resolve_label(ip)).set(value)


def handle_connected(m: re.Match) -> None:
    set_node_up(m.group('ip'), 1)
    if m.group('redundant'):
        CONTROLLER_FAILOVER_TOTAL.inc()


def handle_timeout(m: re.Match) -> None:
    ip = m.group('ip')
    set_node_up(ip, 0)
    if ip not in IGNORED_TIMEOUT_IPS:
        NODE_CONNECT_ERRORS_TOTAL.labels(name=resolve_label(ip), reason='timeout').inc()


def handle_refused(m: re.Match) -> None:
    ip = m.group('ip')
    set_node_up(ip, 0)
    NODE_CONNECT_ERRORS_TOTAL.labels(name=resolve_label(ip), reason='refused').inc()


def handle_link_reset(m: re.Match) -> None:
    LINK_RESETS_TOTAL.labels(name=resolve_label(m.group('ip'))).inc()


def handle_net_info(m: re.Match) -> None:
    learn_net_addr(m.group('ip'), int(m.group('net_addr')))


def handle_retry_delay(m: re.Match) -> None:
    CONNECTION_RETRY_DELAY_MS.set(int(m.group('ms')))


def handle_restart(_m: re.Match) -> None:
    RESTARTS_TOTAL.inc()


def handle_version(m: re.Match) -> None:
    VERSION_INFO.info({'version': m.group('version')})


PARSERS = [
    (MODE_CHANGE_RE, handle_mode_change),
    (CONNECTED_RE, handle_connected),
    (TIMEOUT_RE, handle_timeout),
    (REFUSED_RE, handle_refused),
    (LINK_RESET_RE, handle_link_reset),
    (RETRY_DELAY_RE, handle_retry_delay),
    (RESTART_RE, handle_restart),
    (VERSION_RE, handle_version),
    (NET_INFO_RE, handle_net_info),
]


def process_message(message: str) -> None:
    for name_match in SOURCE_DEST_NAME_RE.finditer(message):
        learn_net_name(int(name_match.group('net_addr')), name_match.group('name').strip())

    for regex, handler in PARSERS:
        match = regex.match(message)
        if match:
            handler(match)
            return

    if GENERIC_ERROR_RE.search(message):
        ERRORS_TOTAL.labels(type='unclassified').inc()


def process_line(line: str) -> None:
    line = line.rstrip('\r\n')
    if not line:
        return

    m = LOG_LINE_RE.match(line)
    if not m:
        logger.debug(f"Process Line: Unrecognized line format: {line!r}")
        return

    try:
        ts = datetime(
            int(m.group('year')), int(m.group('month')), int(m.group('day')),
            int(m.group('hour')), int(m.group('minute')), int(m.group('second')),
            int(m.group('millis')) * 1000
        )
        LOG_LAST_EVENT_TIMESTAMP.set(ts.timestamp())
    except ValueError as e:
        logger.debug(f"Process Line: Invalid timestamp in line {line!r}: {e}")

    process_message(m.group('message').rstrip('\t'))


# --- Log file discovery and tailing ---------------------------------------

def find_active_log_file(install_dir_glob: str, log_file_glob: str) -> str | None:
    install_dirs = glob.glob(install_dir_glob)
    if not install_dirs:
        return None
    # The Trunk Navigator install path changes with every software update
    # (e.g. "Trunk Navigator_8.8" -> "Trunk Navigator_8.9"); use the most
    # recently modified matching directory.
    install_dir = max(install_dirs, key=os.path.getmtime)

    log_files = glob.glob(os.path.join(install_dir, log_file_glob))
    if not log_files:
        return None
    # Log rotation creates a new file over time; the one currently being
    # written to is the most recently modified one.
    return max(log_files, key=os.path.getmtime)


def read_complete_lines(fh):
    """Yield only fully-written lines, leaving a trailing partial line (still
    being written) unconsumed so it can be re-read once it is complete."""
    while True:
        position = fh.tell()
        line = fh.readline()
        if not line:
            return
        if not line.endswith('\n'):
            fh.seek(position)
            return
        yield line


def tail_logs(config: dict) -> None:
    install_dir_glob = config['install_dir_glob']
    log_file_glob = config['log_file_glob']
    poll_interval = config.get('poll_interval_seconds', 1)

    current_path = None
    fh = None
    # On the very first log file opened, replay it from the start so that
    # metrics (mode, node up/down, learned names, ...) reflect the state that
    # already existed before the exporter started, not just events that
    # happen to occur afterwards. Counters (e.g. connect error totals) will
    # include this backlog too, causing a one-off jump right after start -
    # accepted trade-off for correct gauges. Later log rotations don't need
    # this: in-memory state already reflects reality and just keeps going.
    backfilled = False

    while True:
        active_path = find_active_log_file(install_dir_glob, log_file_glob)

        if active_path != current_path:
            if fh:
                fh.close()

            current_path = active_path
            if current_path:
                fh = open(current_path, 'r', encoding='utf-8-sig', errors='replace')
                if not backfilled:
                    logger.info(f"Tail Logs: Reading {current_path} from the start to establish current state...")
                    for line in read_complete_lines(fh):
                        try:
                            process_line(line)
                        except Exception as e:
                            logger.warning(f"Tail Logs: Failed to process line {line!r}: {e}")
                    backfilled = True
                else:
                    fh.seek(0, os.SEEK_END)
                LOG_TAILER_UP.set(1)
                CURRENT_LOG_FILE.info({'path': current_path})
                logger.info(f"Tail Logs: Now tailing {current_path}")
            else:
                fh = None
                LOG_TAILER_UP.set(0)
                CURRENT_LOG_FILE.info({'path': ''})
                logger.warning(f"Tail Logs: No Trunk Navigator log file found matching {install_dir_glob}\\{log_file_glob}")

        if fh:
            try:
                if os.path.getsize(current_path) < fh.tell():
                    # File was truncated or replaced in place.
                    fh.seek(0)

                got_line = False
                for line in read_complete_lines(fh):
                    got_line = True
                    try:
                        process_line(line)
                    except Exception as e:
                        logger.warning(f"Tail Logs: Failed to process line {line!r}: {e}")
                if got_line:
                    continue
            except OSError as e:
                logger.warning(f"Tail Logs: Lost access to {current_path}: {e}")
                fh.close()
                fh = None
                current_path = None
                LOG_TAILER_UP.set(0)

        time.sleep(poll_interval)


def load_config(file_path: str = "config.json") -> dict:
    try:
        with open(file_path, 'r') as file:
            config = json.load(file)
            logger.info(f"Load Config: Config loaded from {file_path}")
    except FileNotFoundError:
        logger.error(f"Load Config: Error: Config file not found at {file_path}")
        sys.exit(1)

    return config


def main() -> None:
    config = load_config()

    IGNORED_TIMEOUT_IPS.update(config['trunk_navigator'].get('ignore_timeout_ips', []))
    STATIC_NODE_NAMES.update(config['trunk_navigator'].get('node_names', {}))

    start_http_server(config['metrics']['port'], addr=config['metrics'].get('bind_address', '0.0.0.0'))
    logger.info(f"Main: Metrics server started on port {config['metrics']['port']}")

    tail_logs(config['trunk_navigator'])


if __name__ == "__main__":
    main()
