"""Activate canonical user-profile/recovery policies in this new private realm.

The temporary bootstrap administrator is used only over the shared loopback
network. Reader clients keep their existing authorization-code/PKCE boundary.
"""
from __future__ import annotations

import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

REALM = 'admin/realms/graphmind'


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AdminAPI:
    def __init__(self, password):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
        body = urllib.parse.urlencode({'grant_type': 'password', 'client_id': 'admin-cli',
            'username': 'graphmind-bootstrap', 'password': password}).encode()
        result = self._request('POST', 'realms/master/protocol/openid-connect/token', body,
                               'application/x-www-form-urlencoded')
        self.token = result['access_token']

    def _request(self, method, path, body=None, content_type='application/json'):
        headers = {'Content-Type': content_type}
        if hasattr(self, 'token'):
            headers['Authorization'] = 'Bearer ' + self.token
        request = urllib.request.Request('http://127.0.0.1:8080/' + path, data=body,
                                         method=method, headers=headers)
        with self.opener.open(request, timeout=15) as response:
            raw = response.read(1048577)
            if len(raw) > 1048576:
                raise ValueError('Identity bootstrap response exceeds its limit')
            return json.loads(raw) if raw else None

    def request(self, method, path, payload=None):
        if path != REALM and not path.startswith(REALM + '/'):
            raise ValueError('Bootstrap is limited to the isolated GraphMind realm')
        return self._request(method, path, None if payload is None else json.dumps(payload).encode())


def need(condition):
    if not condition:
        raise ValueError('Canonical identity policy read-back did not match')


def activate(api, profile, policy):
    realm = api.request('GET', REALM)
    need(realm.get('realm') == 'graphmind')
    api.request('PUT', REALM + '/users/profile', profile)
    actual = api.request('GET', REALM + '/users/profile')
    attributes = {item['name']: item for item in actual['attributes']}
    need(set(attributes) == {item['name'] for item in profile['attributes']})
    for expected in profile['attributes']:
        need(expected['name'] in attributes)
        got = attributes[expected['name']]
        for key in ('permissions', 'required', 'validations'):
            if key in expected:
                need(got.get(key) == expected[key])
    flow = policy['flow']
    alias = urllib.parse.quote(flow['alias'], safe='')
    flow_path = REALM + '/authentication/flows/' + alias
    flows = api.request('GET', REALM + '/authentication/flows')
    existing = [item for item in flows if item['alias'] == flow['alias']]
    need(len(existing) <= 1)
    if not existing:
        api.request('POST', REALM + '/authentication/flows',
                    {key: value for key, value in flow.items() if key != 'authenticationExecutions'})
    else:
        need(not existing[0].get('builtIn') and existing[0]['providerId'] == flow['providerId'])
    for expected in flow['authenticationExecutions']:
        rows = api.request('GET', flow_path + '/executions')
        matches = [item for item in rows if item.get('providerId') == expected['authenticator']]
        need(len(matches) <= 1)
        if not matches:
            api.request('POST', flow_path + '/executions/execution', {'provider': expected['authenticator']})
            rows = api.request('GET', flow_path + '/executions')
            matches = [item for item in rows if item.get('providerId') == expected['authenticator']]
        need(len(matches) == 1)
        execution = matches[0]
        api.request('PUT', flow_path + '/executions', {'id': execution['id'],
            'requirement': expected['requirement'], 'priority': expected['priority']})
        if expected.get('authenticatorConfig'):
            identifier = execution.get('authenticationConfig')
            if identifier:
                config = api.request('GET', REALM + '/authentication/config/' + identifier)
                need(config['config'] == policy['authenticatorConfig']['config'])
            else:
                api.request('POST', REALM + '/authentication/executions/' + execution['id'] + '/config',
                            policy['authenticatorConfig'])
    rows = api.request('GET', flow_path + '/executions')
    expected_providers = {item['authenticator'] for item in flow['authenticationExecutions']}
    need(len(rows) == len(expected_providers) and {item.get('providerId') for item in rows} == expected_providers)
    for expected in flow['authenticationExecutions']:
        row = next(item for item in rows if item['providerId'] == expected['authenticator'])
        need(row['requirement'] == expected['requirement'] and row['priority'] == expected['priority'])
        if expected.get('authenticatorConfig'):
            need(bool(row.get('authenticationConfig')))
            config = api.request('GET', REALM + '/authentication/config/' + row['authenticationConfig'])
            need(config['config'] == policy['authenticatorConfig']['config'])
    listeners = list(dict.fromkeys([*(realm.get('eventsListeners') or []), policy['eventListener']]))
    settings = dict(policy['realmSettings'])
    settings['attributes'] = {**(realm.get('attributes') or {}), **settings.get('attributes', {})}
    settings['eventsListeners'] = listeners
    settings['emailTheme'] = 'graphmind'
    api.request('PUT', REALM, settings)
    result = api.request('GET', REALM)
    need(result.get('resetPasswordAllowed') is True and result.get('resetCredentialsFlow') == flow['alias'])
    need(policy['eventListener'] in result.get('eventsListeners', []))
    need(result.get('emailTheme') == 'graphmind')
    for key, value in policy['realmSettings'].get('attributes', {}).items():
        need(result.get('attributes', {}).get(key) == value)
    return {'configured': True, 'realm': 'graphmind', 'user_profile_verified': True,
            'recovery_policy_verified': True, 'reader_password_grant_enabled': False}


def main():
    try:
        secret = Path('/run/secrets/keycloak_bootstrap_password').read_bytes()
        if not 20 <= len(secret) <= 4096:
            raise ValueError('Invalid bootstrap input')
        profile = json.loads(Path('/etc/graphmind/user-profile.json').read_text())
        policy = json.loads(Path('/etc/graphmind/recovery.json').read_text())
        result = activate(AdminAPI(secret.decode().strip()), profile, policy)
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit('Private identity setup failed; public reader stays gated. No credentials were logged.') from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
