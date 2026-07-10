import os
import datetime
import json
import logging
import logging.handlers
import schedule
import sys
import time

import xml.etree.ElementTree as ET

import msal
import requests

from datetime import timezone
from msal import ConfidentialClientApplication
from msal.exceptions import MsalServiceError
from office365.runtime.auth.token_response import TokenResponse
from office365.runtime.client_request_exception import ClientRequestException
from office365.sharepoint.client_context import ClientContext
from requests.exceptions import RequestException
from urllib.request import urlopen
from urllib.error import URLError, HTTPError


# Set up logging
logger = logging.getLogger('phonebook')
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s - %(funcName)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

file_handler = logging.handlers.RotatingFileHandler('./Log/phonebook.log', maxBytes=10000000, backupCount=5)
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


def load_cert(cert_path: str) -> str:
    try:
        with open(cert_path, 'r') as file:
            cert = file.read()
            logger.info(f"Load Cert: Certificate loaded from {cert_path}")
    except FileNotFoundError:
        logger.error(f"Load Cert: Error: Certificate file not found at {cert_path}")
        sys.exit(1)

    return cert


def strtobool(val) -> bool:
    """Convert a string representation of truth to true (1) or false (0).

    True values are 'y', 'yes', 't', 'true', 'on', and '1'; false values
    are 'n', 'no', 'f', 'false', 'off', and '0'.  Raises ValueError if
    'val' is anything else.
    """
    val = val.lower()
    if val in ('y', 'yes', 't', 'true', 'on', '1'):
        return True
    elif val in ('n', 'no', 'f', 'false', 'off', '0'):
        return False
    else:
        raise ValueError("invalid truth value {!r}".format(val))


def get_xml_from_rrcs(url: str) -> str: 
    headers = {"Accept": "text/xml",
               "Content-Type": "text/xml"}
    body = """<?xml version="1.0" encoding="UTF-8"?>
<methodCall>
    <methodName>GetTrunkPorts</methodName>
</methodCall>"""

    try:
        response = requests.post(url, headers=headers, data=body)
        response.raise_for_status()
        logger.info(f"Get XML from RRCS: XML data fetched from {url}")
    except RequestException as e:
        logger.error(f"Get XML from RRCS: An error occurred: {e}")

    return response.text


def parse_xml(xml: str) -> list:
    try:
        root = ET.fromstring(xml)

        result = []

        for member in root.findall('.//params/param/value/array/data/value/array/data/value'): #'.//array/data/value'
            member_dict = {
                'enabled_for_trunking': '',
                'port_display_name': '',
                'net_name': '',
                'net_trunk_address': '',
                'port_trunk_address': '',
                'used_as_trunk_line': ''
            }

            for struct in member:
                for item in struct:
                    if item[0].text == "EnabledForTrunking":
                        member_dict['enabled_for_trunking'] = strtobool(item[1][0].text)
                    elif item[0].text == "PortDispName8":
                        member_dict['port_display_name'] = item[1][0].text
                    elif item[0].text == "NetName":
                        member_dict['net_name'] = item[1][0].text
                    elif item[0].text == "NetTrAddr":
                        member_dict['net_trunk_address'] = item[1][0].text
                    elif item[0].text == "PortTrAddr":
                        member_dict['port_trunk_address'] = item[1][0].text.zfill(4)
                    elif item[0].text == "UsedAsTrunkLine":
                        member_dict['used_as_trunk_line'] = strtobool(item[1][0].text)

            result.append(member_dict)
        logger.info(f"Parse XML: XML data parsed. Items found: {len(result)}")
        return result
    except (URLError, HTTPError) as e:
        logger.error(f"Parse XML: URL Error: {e}")
    except ET.ParseError as e:
        logger.error(f"Parse XML: XML Parse Error: {e}")
    except Exception as e:
        logger.error(f"Parse XML: Unexpected Error: {e}")
    return []  

                    
def get_access_token(tenant_id: str, client_id: str, cert_thumbprint: str, pem_content: str, ressource: str, base_authority_url: str , cert_passphrase: str ) -> TokenResponse:
    try: 
        cert_settings = {
            'tenant': tenant_id,
            'client_id': client_id,
            'thumbprint': cert_thumbprint,
            'private_key': pem_content,
            'resource': ressource
        }
        
        authority_url = f'{base_authority_url}/{cert_settings.get('tenant')}'
        credentials = credentials = {
            'thumbprint': cert_settings.get('thumbprint'),
            'private_key': pem_content,
            'passphrase': cert_passphrase
        }
        scopes = [f'{cert_settings.get("resource")}/.default']   
        #scopes = ['{url}/.default'.format(url=cert_settings.get('resource'))]
        app = ConfidentialClientApplication(
            cert_settings.get('client_id'),
            authority = authority_url,
            client_credential = credentials
        )
        result = app.acquire_token_for_client(scopes)

        if 'access_token' in result:
            logger.info("Get Access Token: Access token acquired")
            return lambda: TokenResponse.from_json(result)
        else:
            raise Exception("Get Access Token: Token acquisition failed: " + str(result.get('error_description', 'Unknown error')))
    except MsalServiceError as e:
        raise Exception(f"Get Access Token: Service error occurred: {e.error}")
    except Exception as e:
        raise Exception(f"Get Access Token: An error occurred: {str(e)}")
        #return lambda: TokenResponse.from_json(result)   


def get_sharepoint_list_items(context: ClientContext, list_name: str, fields: list) -> list:
    all_items_from_sharepoint = []
    try:
        sp_lists = context.web.lists
        s_list = sp_lists.get_by_title(list_name)
        l_items = s_list.items.paged(100).get().execute_query()

        for item in l_items:
            item_from_sharepoint = {
                'id': item.properties[fields['id']],
                'enabled_for_trunking': False if item.properties[fields['enabled_for_trunking']] is None else item.properties[fields['enabled_for_trunking']],
                'net_name': item.properties[fields['net_name']],
                'net_trunk_address': item.properties[fields['net_trunk_address']],
                'port_trunk_address': item.properties[fields['port_trunk_address']],
                'used_as_trunk_line': item.properties[fields['used_as_trunk_line']],
                'last_seen_online': item.properties[fields['last_seen_online']],
                'import_to_scroll_list_zrh': False if item.properties[fields['import_to_scroll_list_zrh']] is None else item.properties[fields['import_to_scroll_list_zrh']],
                'description': item.properties[fields['description']],
                'port_display_name': item.properties[fields['port_display_name']]
            }
            all_items_from_sharepoint.append(item_from_sharepoint)
    except ClientRequestException as e:
        raise Exception(f"Get SharePoint List Items: SharePoint request error occured: {e.message}")
    except KeyError as e:
        raise Exception(f"Get SharePoint List Items: Field key error occured: {str(e)}")
    except Exception as e:
        raise Exception(f"Get SharePoint List Items: An unexpected error occured: {str(e)}")
    
    return all_items_from_sharepoint


def add_sharepoint_list_item(context: ClientContext, list_name: str, new_item: dict) -> None:
    try:
        sp_lists = context.web.lists
        s_list = sp_lists.get_by_title(list_name)
        list_item = s_list.add_item(new_item)
        context.execute_batch()
    except ClientRequestException as e:
        raise Exception (f"Add Sharepoint List Item: SharePoint request error occured, failed to add item: {e.message}")
    except KeyError as e:
        raise Exception (f"Add Sharepoint List Item: Field key error occured, failed to add item: {str(e)}")
    except Exception as e:
        raise Exception (f"Add Sharepoint List Item: An unexpected error occured, failed to add item: {str(e)}")


def update_timestamp_multiple_sharepoint_list_items(context: ClientContext, list_name: str, fields: list, item_ids: list, timestamp: str) -> None:
    try:
        list_items = context.web.lists.get_by_title(list_name)
        items = list_items.get_items().execute_query()
        if len(items) == 0:
            logger.info("Update Timestamps: No items found")
            return
        
        for item_id in item_ids:
            try:
                item = list_items.get_item_by_id(item_id)
                item.set_property(fields['last_seen_online'], timestamp)
                item.update()
                logger.debug(f"Update Timestamp: Item {item_id} has been updated")
            except ClientRequestException as e:
                logger.error(f"Update Timestamp: Failed to update item {item_id} due to Sharpoint request error: {e.message}")
            except KeyError as e:
                logger.error(f"Update Timestamp: Failed to update item {item_id} due to field key error: {str(e)}")
            except Exception as e:
                logger.error(f"Update Timestamp: Failed to update item {item_id} due to an unexpected error: {str(e)}")
        context.execute_batch()

    except ClientRequestException as e:
        logger.error(f"Update Timestamp: An error occured durring Sharepoint request: {e.message}")    
    except Exception as e:
        logger.error(f"Update Timestamp: An unexpected error occured: {str(e)}")


def update_sharepoint_list_item(context: ClientContext, list_name: str, item_id: int, updated_item: dict) -> None:
    try:
        list_items = context.web.lists.get_by_title(list_name)
        items = list_items.get_items().execute_query()
        if len(items) == 0:
            logger.info("Update Sharepoint List Item: No items found")
            return
        
        item = items.get_by_id(item_id)
        for key, value in updated_item.items():
            item.set_property(key, value)
        
        item.update()
        context.execute_query()
        logger.info(f"Update Sharepoint List Item: Item {item_id} has been updated")
    
    except ClientRequestException as e:
        logger.error(f"Update Sharepoint List Item: Sharepoint request error occured: {e.message}")
    except KeyError as e:
        logger.error(f"Update Sharepoint List Item: Field key error occured: {str(e)}")
    except Exception as e:
        logger.error(f"Update Sharepoint List Item: An unexpected error occured: {str(e)}")


def delete_sharepoint_list_item(context: ClientContext, list_name: str, item_id: int) -> None:
    try:
        list_items = context.web.lists.get_by_title(list_name)
        items = list_items.get_items().execute_query()
        
        items.get_by_id(item_id).delete_object()
        context.execute_batch()
        logger.info(f"Delete Sharepoint List Item: Item {item_id} has been deleted")
    except ClientRequestException as e:
        logger.error(f"Delete Sharepoint List Item: Sharepoint request error occured: {e.message}")
    except KeyError as e:
        logger.error(f"Delete Sharepoint List Item: Field key error occured: {str(e)}")
    except Exception as e:
        logger.error(f"Delete Sharepoint List Item: An unexpected error occured: {str(e)}")


def item_properties(fields: dict, **kwargs) -> dict:
    new_dict = {}
    try:
        for old_key, value in kwargs.items():
            new_key = fields.get(old_key)
            if new_key:
                new_dict[new_key] = value
    except KeyError as e:
        raise Exception(f"Item Properties: Field key error occured: {str(e)}")
    except Exception as e:
        raise Exception(f"Item Properties: An unexpected error occured: {str(e)}")
    
    return new_dict


def get_new_changed_items(rrcs_list: dict, sharepoint_list: dict) -> tuple:
    keys_to_compare = ['enabled_for_trunking', 'port_display_name', 'net_name', 'net_trunk_address', 'port_trunk_address', 'used_as_trunk_line']
    
    def filter_dict(d):
        return {k: d[k] for k in keys_to_compare if k in d}
    
    try:
        filtered_rrcs_list = [filter_dict(d) for d in rrcs_list]
        filtered_sharepoint_list = [filter_dict(d) for d in sharepoint_list]
        
        missing_entries = []
        changed_entries = []
        unchanged_entries = []
        
        # Create a dictionary with (port_trunk_address, net_trunk_address) as the key for quick lookup
        sharepoint_dict_by_trunk_addresses = {(d['port_trunk_address'], d['net_trunk_address']): d for d in filtered_sharepoint_list if 'port_trunk_address' in d and 'net_trunk_address' in d}
        
        for d1 in filtered_rrcs_list:
            trunk_addresses = (d1['port_trunk_address'], d1['net_trunk_address'])
            if trunk_addresses not in sharepoint_dict_by_trunk_addresses:
                missing_entries.append(d1)
            else:
                d2 = sharepoint_dict_by_trunk_addresses[trunk_addresses]
                if any(d1[k] != d2[k] for k in keys_to_compare if k in d1 and k in d2):
                    changed_entries.append(d1)
                else:
                    unchanged_entries.append(d1)
        
        # Find IDs of changed and unchanged entries
        changed_ids = [d['id'] for d in sharepoint_list if (d['port_trunk_address'], d['net_trunk_address']) in [(entry['port_trunk_address'], entry['net_trunk_address']) for entry in changed_entries]]
        unchanged_ids = [d['id'] for d in sharepoint_list if filter_dict(d) in unchanged_entries]
        
        return missing_entries, changed_entries, changed_ids, unchanged_entries, unchanged_ids
    
    except KeyError as e:
        raise Exception(f"Get New/Chnaged Itesm: Field key error occured: {str(e)}")
    except Exception as e:
        raise Exception(f"Get New/Chnaged Itesm: An unexpected error occured: {str(e)}")


def main() -> None:
    try:
        # Load config and certificate
        CONFIG = load_config()
        CERT = load_cert(CONFIG['sharepoint']['cert_path'])

        # Set global proxy Settings
        proxy = CONFIG['proxy']
        no_proxy = CONFIG['no_proxy']
        os.environ['HTTP_PROXY'] = proxy
        os.environ['HTTPS_PROXY'] = proxy
        os.environ['http_proxy'] = proxy
        os.environ['https_proxy'] = proxy
        os.environ['NO_PROXY'] = no_proxy

        # Get actuall date and time
        now = datetime.datetime.now(timezone.utc).strftime("%FT%XZ")

        # Get data from RRCS and parse it
        xml = get_xml_from_rrcs(CONFIG['riedel']['url'])
        data_from_RRCS = parse_xml(xml)

        # Get access token and create sharepoint client context
        ctx = ClientContext(CONFIG['sharepoint']['site_url']).with_access_token(get_access_token(CONFIG['sharepoint']['tenant_id'], CONFIG['sharepoint']['client_id'], CONFIG['sharepoint']['cert_thumbprint'], CERT, CONFIG['sharepoint']['ressource'], CONFIG['sharepoint']['base_authority_url'], CONFIG['sharepoint']['cert_passphrase']))
        
        # Get current sharepoint list items
        current_sharepoint_list = get_sharepoint_list_items(ctx, CONFIG['sharepoint']['list_name'], CONFIG['sharepoint']['fields'])
        
        # Compare data from RRCS with current sharepoint list
        missing_entries, changed_entries, changed_ids, unchanged_entries, unchanged_ids = get_new_changed_items(data_from_RRCS, current_sharepoint_list)
        logger.info(f"Main: Missing Entries in Sharepoint: {len(missing_entries)} ")
        logger.info(f"Main: Changed Entries in Sharepoint: {len(changed_entries)} ")
        logger.info(f"Main: Changed IDs in Sharepoint: {changed_ids} ")
        logger.info(f"Main: Unchanged Entries in Sharepoint: {len(unchanged_entries)} ")
        logger.info(f"Main: Unchanged IDs in Sharepoint: {unchanged_ids} ")

        # Add missing entries to sharepoint list
        for item in missing_entries:
            add_sharepoint_list_item(ctx, CONFIG['sharepoint']['list_name'], item_properties(CONFIG['sharepoint']['fields'], enabled_for_trunking=item['enabled_for_trunking'], net_name=item['net_name'], net_trunk_address=item['net_trunk_address'], port_trunk_address=item['port_trunk_address'], used_as_trunk_line=item['used_as_trunk_line'], last_seen_online=now, import_to_scroll_list_zrh=False, description=None, port_display_name=item['port_display_name']))

        # Update changed entries in sharepoint list
        for count, item in enumerate(changed_entries):
            update_sharepoint_list_item(ctx, CONFIG['sharepoint']['list_name'], changed_ids[count], item_properties(CONFIG['sharepoint']['fields'], enabled_for_trunking=item['enabled_for_trunking'], net_name=item['net_name'], net_trunk_address=item['net_trunk_address'], port_trunk_address=item['port_trunk_address'], used_as_trunk_line=item['used_as_trunk_line'], last_seen_online=now, port_display_name=item['port_display_name']))

        # Update entries from sharepoint list (not used as only the timestamp is updated)
        # for count, item in enumerate(unchanged_entries):
        #     update_sharepoint_list_item(ctx, CONFIG['sharepoint']['list_name'], unchanged_ids[count], item_properties(CONFIG['sharepoint']['fields'], last_seen_online=now))

        
        # Update "last seen online" timestamp of unchanged entries from sharepoint list   
        update_timestamp_multiple_sharepoint_list_items(ctx, CONFIG['sharepoint']['list_name'], CONFIG['sharepoint']['fields'], unchanged_ids, now)
    except ClientRequestException as e:
        logger.error(f"Main: Sharepoint request error occured: {e.message}")
    except KeyError as e:
        logger.error(f"Main: Field key error occured: {str(e)}")
    except Exception as e:
        logger.error(f"Main: An unexpected error occured: {str(e)}")


if __name__ == "__main__":
    try:
        schedule.every().hour.at(":00").do(main)
        schedule.every().hour.at(":30").do(main)

        main()

        while True:
            schedule.run_pending()
            time.sleep(1)
    except Exception as e:
        logger.error(f"Schedule: An unexpected error occured: {str(e)}")