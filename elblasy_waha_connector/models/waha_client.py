"""Thin HTTP layer for the WAHA REST API.

No ORM access here: the client only needs plain values, so it can be used
(and mocked in tests) without holding a database transaction.
"""
import logging

import requests

_logger = logging.getLogger(__name__)


class WahaError(Exception):
    """Base error. ``kind`` is one of: network, auth, not_found, http, response."""

    def __init__(self, kind, message, status_code=None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


class WahaClient:

    def __init__(self, base_url, api_key=None, timeout_connect=5, timeout_read=30, verify_ssl=True):
        self.base_url = (base_url or '').rstrip('/')
        self._api_key = api_key or ''
        self.timeout = (timeout_connect, timeout_read)
        self.verify = bool(verify_ssl)

    def _request(self, method, path, json=None, params=None, accept_json=True):
        if not self.base_url:
            raise WahaError('network', "WAHA Base URL is not configured.")
        headers = {'Accept': 'application/json'} if accept_json else {}
        if self._api_key:
            headers['X-Api-Key'] = self._api_key
        try:
            resp = requests.request(
                method, self.base_url + path, json=json, params=params,
                headers=headers, timeout=self.timeout, verify=self.verify,
            )
        except requests.exceptions.SSLError:
            raise WahaError('network', "TLS certificate verification failed.")
        except requests.exceptions.Timeout:
            raise WahaError('network', "The WAHA server did not answer in time.")
        except requests.exceptions.RequestException:
            # Never include the exception text: it may embed the URL/headers.
            raise WahaError('network', "The WAHA server could not be reached.")
        if resp.status_code in (401, 403):
            raise WahaError('auth', "WAHA rejected the API key.", resp.status_code)
        if resp.status_code == 404:
            raise WahaError('not_found', "Resource not found on the WAHA server.", 404)
        if resp.status_code >= 400:
            raise WahaError('http', f"WAHA answered with HTTP {resp.status_code}.", resp.status_code)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise WahaError('response', "Unexpected (non-JSON) answer from WAHA.", resp.status_code)

    # -- server
    def list_sessions(self):
        return self._request('GET', '/api/sessions', params={'all': 'true'})

    # -- sessions
    def get_session(self, name):
        return self._request('GET', f'/api/sessions/{name}')

    def create_session(self, name, config=None, start=True):
        return self._request('POST', '/api/sessions', json={'name': name, 'start': start, 'config': config or {}})

    def update_session(self, name, config):
        return self._request('PUT', f'/api/sessions/{name}', json={'name': name, 'config': config})

    def start_session(self, name):
        return self._request('POST', f'/api/sessions/{name}/start')

    def stop_session(self, name):
        return self._request('POST', f'/api/sessions/{name}/stop')

    def restart_session(self, name):
        return self._request('POST', f'/api/sessions/{name}/restart')

    def logout_session(self, name):
        return self._request('POST', f'/api/sessions/{name}/logout')

    def get_qr(self, name):
        """Return ``{'mimetype', 'data'}`` (base64 image)."""
        return self._request('GET', f'/api/{name}/auth/qr')

    # -- messages
    def send_text(self, session, chat_id, text):
        return self._request('POST', '/api/sendText', json={'session': session, 'chatId': chat_id, 'text': text})

    def get(self, path, params=None):
        return self._request('GET', path, params=params)

    def lid_to_phone(self, session, lid):
        """Phone number (digits) behind a ``…@lid`` chat id, or False when WAHA does not know it."""
        digits = lid.partition('@')[0]
        data = self._request('GET', f'/api/{session}/lids/{digits}')
        pn = data.get('pn') if isinstance(data, dict) else None
        return ''.join(c for c in (pn or '').partition('@')[0] if c.isdigit()) or False

    def check_exists(self, session, digits):
        """``(exists, chat_id)`` for a phone number: does it have WhatsApp?"""
        data = self._request('GET', '/api/contacts/check-exists', params={'phone': digits, 'session': session})
        data = data if isinstance(data, dict) else {}
        return bool(data.get('numberExists')), data.get('chatId')

    PICTURE_HOSTS = ('.whatsapp.net', '.fbcdn.net', '.whatsapp.com')

    def picture_url(self, session, chat_id, group=False):
        """URL of the profile/group picture of a chat (``False`` when hidden by privacy settings)."""
        kind = 'groups' if group else 'chats'
        data = self._request('GET', f'/api/{session}/{kind}/{chat_id}/picture')
        url = data.get('url') if isinstance(data, dict) else None
        return url if isinstance(url, str) and url else False

    def download_picture(self, url, max_bytes=1024 * 1024):
        """Download a WhatsApp CDN picture. Only https URLs of WhatsApp hosts are fetched (no SSRF)."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or ''
        if parsed.scheme != 'https' or not host.endswith(self.PICTURE_HOSTS):
            raise WahaError('response', "Unexpected picture host.")
        try:
            resp = requests.get(url, timeout=self.timeout, verify=True)
        except requests.exceptions.RequestException:
            raise WahaError('network', "The picture could not be downloaded.")
        if resp.status_code != 200 or len(resp.content) > max_bytes:
            raise WahaError('http', "The picture is unavailable.", resp.status_code)
        return resp.content

    def post(self, path, payload):
        return self._request('POST', path, json=payload)

    def put(self, path, payload):
        return self._request('PUT', path, json=payload)

    def download(self, url, max_bytes=50 * 1024 * 1024):
        """Fetch a media file from WAHA. Only the path of ``url`` is used, always against our own base URL."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path = parsed.path + (f'?{parsed.query}' if parsed.query else '')
        if not path.startswith('/'):
            raise WahaError('response', "Invalid media URL.")
        headers = {'X-Api-Key': self._api_key} if self._api_key else {}
        try:
            resp = requests.get(self.base_url + path, headers=headers, timeout=self.timeout, verify=self.verify)
        except requests.exceptions.RequestException:
            raise WahaError('network', "The WAHA server could not be reached.")
        if resp.status_code in (401, 403):
            raise WahaError('auth', "WAHA rejected the API key.", resp.status_code)
        if resp.status_code >= 400:
            raise WahaError('http', f"WAHA answered with HTTP {resp.status_code}.", resp.status_code)
        if len(resp.content) > max_bytes:
            raise WahaError('response', "Media file too large.")
        return resp.content
