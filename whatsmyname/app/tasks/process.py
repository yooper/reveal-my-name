""" Provides url fetching and data splitting functionality """
import logging
import os
import tempfile
from asyncio import gather, ensure_future
from typing import List, Dict
import json

import aiohttp
from aiohttp import ClientSession, ClientConnectionError, TCPConnector, ClientTimeout
from multidict import CIMultiDictProxy

from whatsmyname.app.extractors.file_extractors import site_file_extractor
from whatsmyname.app.models.schemas.cli import CliOptionsSchema
from whatsmyname.app.models.schemas.sites import SiteSchema
from whatsmyname.app.utilities.formatters import to_json

logger = logging.getLogger(__name__)


def get_sites_list(cli_options: CliOptionsSchema) -> List[SiteSchema]:
    """
    Returns all the sites, or some of the sites
    :param cli_options:
    :return: List[Schema]
    """
    sites: List[SiteSchema] = site_file_extractor(cli_options.input_file)

    # filter invalid sites
    sites = list(filter(lambda site: site.valid, sites))

    # assign the user agent
    for site in sites:
        site.user_agent = cli_options.user_agent

    if cli_options.category:
        sites = list(filter(lambda site: site.category.lower() == cli_options.category.lower(), sites))

    if cli_options.sites:
        filtered_sites: List[SiteSchema] = []
        site_name: str
        for site_name in cli_options.sites:
            site: SiteSchema
            for site in sites:
                if site.name.lower() == site_name.lower():
                    filtered_sites.append(site)
        if not filtered_sites:
            raise Exception('No sites with id(s) ' + ' '.join(cli_options.sites) + ' used input file ' + cli_options.input_file)

        return filtered_sites
    else:
        return sites


def get_validated_site_list(cli_options: CliOptionsSchema, sites: List[SiteSchema]) -> List[SiteSchema]:
    """
    Return the list of sites using the known usernames
    :param sites:
    :param cli_options:
    :return:
    """
    valid_username_site_list: List[SiteSchema] = []
    for site in sites:
        valid_username_site_list.extend(generate_username_sites(site.known, [site]))
    return valid_username_site_list


def generate_username_sites(usernames: List[str], sites: List[SiteSchema]) -> List[SiteSchema]:
    """
    Generate sites schemas from the usernames list
    :param usernames:
    :param sites:
    :return:
    """
    username: str
    user_site_map: Dict[str, List[SiteSchema]] = {}
    for username in usernames:
        username_has_dot: bool = '.' in username
        site: SiteSchema
        for site in sites:
            # addresses github issue #55
            if (site.uri_check.startswith('http://{account}') or site.uri_check.startswith('https://{account}')) and username_has_dot:
                logger.debug('Skipping site %s, with username %s', site.uri_check, username)
                continue

            site_clone: SiteSchema = site.copy(deep=True)
            site_clone.username = username

            # Fixes issue #673
            if site_clone.invalid_chars:
                for remove_char in site_clone.invalid_chars:
                    username = username.replace(remove_char, '')
                    site_clone.username = username.replace(remove_char, '')

            site_clone.generated_uri = site_clone.uri_check.replace('{account}', username)

            if not user_site_map.get(username):
                user_site_map[username] = []
            user_site_map[username].append(site_clone)
    # flatten dictionary into single list
    big_list_of_sites: List[SiteSchema] = []
    for username, list_of_sites in user_site_map.items():
        big_list_of_sites += list_of_sites

    return big_list_of_sites


async def process_cli(cli_options: CliOptionsSchema) -> List[SiteSchema]:
    """
    Main function for fetching and processing the website requests
    :param cli_options:
    :return:
    """
    sites: List[SiteSchema]
    if cli_options.validate_knowns:
        sites = get_validated_site_list(cli_options, get_sites_list(cli_options))
    else:
        # check the number of usernames we must validate
        sites = generate_username_sites(cli_options.usernames, get_sites_list(cli_options))
    return await request_controller(cli_options, sites)


def filter_list_by(cli_options: CliOptionsSchema, sites: List[SiteSchema]) -> List[SiteSchema]:
    """
    By default, only return sites that had a successful hit.
    :param cli_options:
    :param sites:
    :return:
    """

    if cli_options.all:
        return sites

    filtered_sites = []
    for site in sites:
        if site.raw_response_data:
            filtered_sites.append(site)

    site: SiteSchema
    if cli_options.not_found:
        return list(filter(lambda site: site.http_status_code == site.m_code and site.m_string in site.raw_response_data, filtered_sites))

    return list(filter(lambda site: site.http_status_code == site.e_code and site.e_string in site.raw_response_data, filtered_sites))


async def request_controller(cli_options: CliOptionsSchema, sites: List[SiteSchema]) -> List[SiteSchema]:
    """Initiates all the web requests"""
    connector: TCPConnector = aiohttp.TCPConnector(ssl=False)
    client_timeout = ClientTimeout(total=None, sock_connect=cli_options.timeout, sock_read=cli_options.timeout)
    async with aiohttp.ClientSession(connector=connector, timeout=client_timeout) as session:
        site: SiteSchema
        tasks = [ensure_future(request_worker(session, cli_options, site)) for site in sites]
        results = await gather(*tasks)

    return results


async def request_worker(session: ClientSession, cli_options: CliOptionsSchema, site: SiteSchema) -> SiteSchema:
    """
    Makes the individual requests to web servers
    :param session:
    :param cli_options:
    :param site:
    :return:
    """

    headers = {
        'User-Agent': site.user_agent
    }
    if site.post_body:
        try:
            post_body_altered: str = site.post_body.replace('{account}', site.username)
            form_data = {x[0] : x[1] for x in [x.split("=") for x in post_body_altered[1:].split("&") ]}
            async with session.post(site.generated_uri,
                                   data=form_data,
                                   timeout=cli_options.per_request_timeout,
                                   allow_redirects=cli_options.follow_redirects,
                                   headers=headers
                                   ) as response:
                site.http_status_code = response.status
                site.raw_response_data = await response.text()
                site.response_headers = json.dumps({str(key): value for key, value in response.headers.items()})
                return site

        except ClientConnectionError as cce:
            logger.error('Site Connection Error %s', site.name, exc_info=False)
            site.http_status_code = -1
        finally:
            return site

    else:
        try:
            async with session.get(site.generated_uri,
                                   timeout=cli_options.per_request_timeout,
                                   allow_redirects=cli_options.follow_redirects,
                                   headers=headers
                                   ) as response:
                site.http_status_code = response.status
                site.raw_response_data = await response.text()
                site.response_headers = json.dumps({str(key): value for key, value in response.headers.items()})
                return site

        except ClientConnectionError as cce:
            logger.error('Site Connection Error %s', site.name, exc_info=False)
            site.http_status_code = -1
        finally:
            return site
