import os
import json
import hashlib
import logging
from urllib.parse import urlparse
import mimetypes

import furl
import aiohttp
from aiohttp.errors import ContentEncodingError

from waterbutler.core import streams

from mfr.core import exceptions
from mfr.core import provider
from mfr.providers.osf import settings

logger = logging.getLogger(__name__)


class OsfProvider(provider.BaseProvider):
    """Open Science Framework (https://osf.io) -aware provider.  Knows the OSF ecosystem and
    can request specific metadata for the file referenced by the URL.  Can correctly propagate
    OSF authorization to verify ownership and permisssions of file.
    """

    UNNEEDED_URL_PARAMS = ('_', 'token', 'action', 'mode', 'displayName')
    NAME = 'osf'

    def __init__(self, request, url):
        super().__init__(request, url)
        self.download_url = None
        self.headers = {}

        # capture request authorization
        self.cookies = dict(self.request.cookies)
        self.cookie = self.request.query_arguments.get('cookie')
        self.view_only = self.request.query_arguments.get('view_only')
        self.authorization = self.request.headers.get('Authorization')
        if self.cookie:
            self.cookie = self.cookie[0].decode()
        if self.view_only:
            self.view_only = self.view_only[0].decode()

        self.metrics.merge({
            'auth': {
                'cookies': bool(self.cookies),
                'view_only': bool(self.view_only),
                'cookie_param': bool(self.cookie),
                'auth_header': bool(self.authorization),
            },
        })

    async def metadata(self):
        """Fetch metadata about the file from WaterButler. V0 and V1 urls must be handled
        differently.
        """
        download_url = await self._fetch_download_url()
        if '/file?' in download_url:
            # URL is for WaterButler v0 API
            # TODO Remove this when API v0 is officially deprecated
            self.metrics.add('metadata.wb_api', 'v0')
            metadata_url = download_url.replace('/file?', '/data?', 1)
            metadata_request = await self._make_request('GET', metadata_url)
            metadata = await metadata_request.json()
        else:
            # URL is for WaterButler v1 API
            self.metrics.add('metadata.wb_api', 'v1')
            metadata_request = await self._make_request('HEAD', download_url)
            # To make changes to current code as minimal as possible
            try:
                metadata = {'data': json.loads(metadata_request.headers['x-waterbutler-metadata'])['attributes']}
                await metadata_request.release()
            except KeyError:
                raise exceptions.MetadataError(
                    'Failed to fetch metadata. Received response code {}'.format(str(metadata_request.status)),
                    code=400)
            except ContentEncodingError:
                pass  # hack: aiohttp tries to unzip empty body when Content-Encoding is set

        self.metrics.add('metadata.raw', metadata)

        # e.g.,
        # metadata = {'data': {
        #     'name': 'blah.png',
        #     'contentType': 'image/png',
        #     'etag': 'ABCD123456...',
        #     'extra': {
        #         ...
        #     },
        # }}

        name, ext = os.path.splitext(metadata['data']['name'])
        content_type = metadata['data']['contentType'] or mimetypes.guess_type(metadata['data']['name'])[0]
        cleaned_url = furl.furl(download_url)
        for unneeded in OsfProvider.UNNEEDED_URL_PARAMS:
            cleaned_url.args.pop(unneeded, None)
        self.metrics.add('metadata.clean_url_args', str(cleaned_url))
        unique_key = hashlib.sha256((metadata['data']['etag'] + cleaned_url.url).encode('utf-8')).hexdigest()
        return provider.ProviderMetadata(name, ext, content_type, unique_key, download_url)

    async def download(self):
        """Download file from WaterButler, returning stream."""
        download_url = await self._fetch_download_url()
        headers = {settings.MFR_IDENTIFYING_HEADER: '1'}
        response = await self._make_request('GET', download_url, allow_redirects=False, headers=headers)

        if response.status >= 400:
            err_resp = await response.read()
            logger.error('Unable to download file: ({}) {}'.format(response.status, err_resp.decode('utf-8')))
            raise exceptions.ProviderError(
                'Unable to download the requested file, please try again later.',
                code=response.status
            )

        self.metrics.add('download.saw_redirect', False)
        if response.status in (302, 301):
            await response.release()
            response = await aiohttp.request('GET', response.headers['location'])
            self.metrics.add('download.saw_redirect', True)

        return streams.ResponseStreamReader(response, unsizable=True)

    async def _fetch_download_url(self):
        """Provider needs a WaterButler URL to download and get metadata.  If ``url`` is already
        a WaterButler url, return that.  If not, then the url points to an OSF endpoint that will
        redirect to WB.  Issue a GET request against it, then return the WB url stored in the
        Location header.
        """
        if not self.download_url:
            # v1 Waterbutler url provided
            path = urlparse(self.url).path
            if path.startswith('/v1/resources'):
                self.download_url = self.url
                self.metrics.add('download_url.orig_type', 'wb_v1')
            else:
                self.metrics.add('download_url.orig_type', 'osf')
                # make request to osf, don't follow, store waterbutler download url
                request = await self._make_request(
                    'GET',
                    self.url,
                    allow_redirects=False,
                    headers={
                        'Content-Type': 'application/json'
                    }
                )
                await request.release()

                if request.status != 302:
                    raise exceptions.ProviderError(request.reason, request.status)
                self.download_url = request.headers['location']

            self.metrics.add('download_url.derived_url', str(self.download_url))

        return self.download_url

    async def _make_request(self, method, url, *args, **kwargs):
        """Pass through OSF credentials."""
        if self.cookies:
            kwargs['cookies'] = self.cookies
        if self.cookie:
            kwargs.setdefault('params', {})['cookie'] = self.cookie
        if self.view_only:
            kwargs.setdefault('params', {})['view_only'] = self.view_only
        if self.authorization:
            kwargs.setdefault('headers', {})['Authorization'] = 'Bearer ' + self.token

        return await aiohttp.request(method, url, *args, **kwargs)
