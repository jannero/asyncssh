# Copyright (c) 2016-2018 by Ron Frederick <ronf@timeheart.net>.
# All rights reserved.
#
# This program and the accompanying materials are made available under
# the terms of the Eclipse Public License v1.0 which accompanies this
# distribution and is available at:
#
#     http://www.eclipse.org/legal/epl-v10.html
#
# Contributors:
#     Ron Frederick - initial implementation, API, and documentation

"""Unit tests for AsyncSSH connection authentication"""

import asyncio
import os
import unittest

from unittest.mock import patch

import asyncssh
from asyncssh.packet import String
from asyncssh.public_key import CERT_TYPE_USER, CERT_TYPE_HOST

from .keysign_stub import create_subprocess_exec_stub
from .server import Server, ServerTestCase
from .util import asynctest, gss_available, patch_getnameinfo, patch_gss
from .util import make_certificate, x509_available


class _FailValidateHostSSHServerConnection(asyncssh.SSHServerConnection):
    """Test error in validating host key signature"""

    @asyncio.coroutine
    def validate_host_based_auth(self, username, key_data, client_host,
                                 client_username, msg, signature):
        """Validate host based authentication for the specified host and user"""

        return (yield from super().validate_host_based_auth(username, key_data,
                                                            client_host,
                                                            client_username,
                                                            msg + b'\xff',
                                                            signature))


class _AsyncGSSServer(asyncssh.SSHServer):
    """Server for testing async GSS authentication"""

    @asyncio.coroutine
    def validate_gss_principal(self, username, user_principal, host_principal):
        """Return whether password is valid for this user"""

        return super().validate_gss_principal(username, user_principal,
                                              host_principal)


class _HostBasedServer(Server):
    """Server for testing host-based authentication"""

    def validate_host_based_user(self, username, client_host, client_username):
        """Return whether remote host and user is authorized for this user"""

        return client_username == 'user'


class _AsyncHostBasedServer(Server):
    """Server for testing async host-based authentication"""

    @asyncio.coroutine
    def validate_host_based_user(self, username, client_host, client_username):
        """Return whether remote host and user is authorized for this user"""

        return super().validate_host_based_user(username, client_host,
                                                client_username)


class _InvalidUsernameClientConnection(asyncssh.connection.SSHClientConnection):
    """Test sending a client username with invalid Unicode to the server"""

    @asyncio.coroutine
    def host_based_auth_requested(self):
        """Return a host key pair, host, and user to authenticate with"""

        keypair, host, _ = yield from super().host_based_auth_requested()

        return keypair, host, b'\xff'


class _PublicKeyClient(asyncssh.SSHClient):
    """Test client public key authentication"""

    def __init__(self, keylist, delay=0):
        self._keylist = keylist
        self._delay = delay

    @asyncio.coroutine
    def public_key_auth_requested(self):
        """Return a public key to authenticate with"""

        if self._delay:
            yield from asyncio.sleep(self._delay)

        return self._keylist.pop(0) if self._keylist else None


class _AsyncPublicKeyClient(_PublicKeyClient):
    """Test async client public key authentication"""

    @asyncio.coroutine
    def public_key_auth_requested(self):
        """Return a public key to authenticate with"""

        return super().public_key_auth_requested()


class _PublicKeyServer(Server):
    """Server for testing public key authentication"""

    def __init__(self, client_keys=(), authorized_keys=None):
        super().__init__()
        self._client_keys = client_keys
        self._authorized_keys = authorized_keys

    def connection_made(self, conn):
        """Called when a connection is made"""

        super().connection_made(conn)
        conn.send_auth_banner('auth banner')

    def begin_auth(self, username):
        """Handle client authentication request"""

        if self._authorized_keys:
            self._conn.set_authorized_keys(self._authorized_keys)
        else:
            self._client_keys = asyncssh.load_public_keys(self._client_keys)

        return True

    def public_key_auth_supported(self):
        """Return whether or not public key authentication is supported"""

        return True

    def validate_public_key(self, username, key):
        """Return whether key is an authorized client key for this user"""

        return key in self._client_keys

    def validate_ca_key(self, username, key):
        """Return whether key is an authorized CA key for this user"""

        return key in self._client_keys


class _AsyncPublicKeyServer(_PublicKeyServer):
    """Server for testing async public key authentication"""

    @asyncio.coroutine
    def begin_auth(self, username):
        """Handle client authentication request"""

        return super().begin_auth(username)

    @asyncio.coroutine
    def validate_public_key(self, username, key):
        """Return whether key is an authorized client key for this user"""

        return super().validate_public_key(username, key)

    @asyncio.coroutine
    def validate_ca_key(self, username, key):
        """Return whether key is an authorized CA key for this user"""

        return super().validate_ca_key(username, key)


class _PasswordClient(asyncssh.SSHClient):
    """Test client password authentication"""

    def __init__(self, password, old_password, new_password):
        self._password = password
        self._old_password = old_password
        self._new_password = new_password

    def password_auth_requested(self):
        """Return a password to authenticate with"""

        if self._password:
            result = self._password
            self._password = None
            return result
        else:
            return None

    def password_change_requested(self, prompt, lang):
        """Change the client's password"""

        return self._old_password, self._new_password


class _AsyncPasswordClient(_PasswordClient):
    """Test async client password authentication"""

    @asyncio.coroutine
    def password_auth_requested(self):
        """Return a password to authenticate with"""

        return super().password_auth_requested()

    @asyncio.coroutine
    def password_change_requested(self, prompt, lang):
        """Change the client's password"""

        return super().password_change_requested(prompt, lang)


class _PasswordServer(Server):
    """Server for testing password authentication"""

    def password_auth_supported(self):
        """Enable password authentication"""

        return True

    def validate_password(self, username, password):
        """Accept password of pw, trigger password change on oldpw"""

        if password == 'oldpw':
            raise asyncssh.PasswordChangeRequired('Password change required')
        else:
            return password == 'pw'

    def change_password(self, username, old_password, new_password):
        """Only allow password change from password oldpw"""

        return old_password == 'oldpw'


class _AsyncPasswordServer(_PasswordServer):
    """Server for testing async password authentication"""

    @asyncio.coroutine
    def validate_password(self, username, password):
        """Return whether password is valid for this user"""

        return super().validate_password(username, password)

    @asyncio.coroutine
    def change_password(self, username, old_password, new_password):
        """Handle a request to change a user's password"""

        return super().change_password(username, old_password, new_password)


class _KbdintClient(asyncssh.SSHClient):
    """Test keyboard-interactive client auth"""

    def __init__(self, responses):
        self._responses = responses

    def kbdint_auth_requested(self):
        """Return the list of supported keyboard-interactive auth methods"""

        return '' if self._responses else None

    def kbdint_challenge_received(self, name, instructions, lang, prompts):
        """Return responses to a keyboard-interactive auth challenge"""

        # pylint: disable=unused-argument

        if not prompts:
            return []
        elif self._responses:
            result = self._responses
            self._responses = None
            return result
        else:
            return None


class _AsyncKbdintClient(_KbdintClient):
    """Test keyboard-interactive client auth"""

    @asyncio.coroutine
    def kbdint_auth_requested(self):
        """Return the list of supported keyboard-interactive auth methods"""

        return super().kbdint_auth_requested()

    @asyncio.coroutine
    def kbdint_challenge_received(self, name, instructions, lang, prompts):
        """Return responses to a keyboard-interactive auth challenge"""

        return super().kbdint_challenge_received(name, instructions,
                                                 lang, prompts)


class _KbdintServer(Server):
    """Server for testing keyboard-interactive authentication"""

    def __init__(self):
        super().__init__()
        self._kbdint_round = 0

    def kbdint_auth_supported(self):
        """Enable keyboard-interactive authentication"""

        return True

    def get_kbdint_challenge(self, username, lang, submethods):
        """Return an initial challenge with only instructions"""

        return '', 'instructions', '', []

    def validate_kbdint_response(self, username, responses):
        """Return a password challenge after the instructions"""

        if self._kbdint_round == 0:
            result = ('', '', '', [('Password:', False)])
        else:
            if len(responses) == 1 and responses[0] == 'kbdint':
                result = True
            else:
                result = ('', '', '', [('Other Challenge:', True)])

        self._kbdint_round += 1
        return result


class _AsyncKbdintServer(_KbdintServer):
    """Server for testing async keyboard-interactive authentication"""

    @asyncio.coroutine
    def get_kbdint_challenge(self, username, lang, submethods):
        """Return a keyboard-interactive auth challenge"""

        return super().get_kbdint_challenge(username, lang, submethods)

    @asyncio.coroutine
    def validate_kbdint_response(self, username, responses):
        """Return whether the keyboard-interactive response is valid
           for this user"""

        return super().validate_kbdint_response(username, responses)


class _UnknownAuthClientConnection(asyncssh.connection.SSHClientConnection):
    """Test getting back an unknown auth method from the SSH server"""

    def try_next_auth(self):
        """Attempt client authentication using an unknown method"""

        self._auth_methods = [b'unknown'] + self._auth_methods
        super().try_next_auth()


@unittest.skipUnless(gss_available, 'GSS not available')
@patch_gss
class _TestGSSAuth(ServerTestCase):
    """Unit tests for GSS authentication"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports GSS authentication"""

        return (yield from cls.create_server(_AsyncGSSServer, gss_host='1'))

    @asynctest
    def test_gss_kex_auth(self):
        """Test GSS key exchange authentication"""

        with (yield from self.connect(kex_algs=['gss-gex-sha1'],
                                      username='user', gss_host='1')) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_gss_mic_auth(self):
        """Test GSS MIC authentication"""

        with (yield from self.connect(kex_algs=['ecdh-sha2-nistp256'],
                                      username='user', gss_host='1')) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_gss_auth_unavailable(self):
        """Test GSS authentication being unavailable"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='user1', gss_host=())

    @asynctest
    def test_gss_client_error(self):
        """Test GSS client error"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(gss_host='1,init_error', username='user')


@unittest.skipUnless(gss_available, 'GSS not available')
@patch_gss
class _TestGSSServerError(ServerTestCase):
    """Unit tests for GSS server error"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which raises an error on GSS authentication"""

        return (yield from cls.create_server(gss_host='1,init_error'))

    @asynctest
    def test_gss_server_error(self):
        """Test GSS error on server"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='user')


@unittest.skipUnless(gss_available, 'GSS not available')
@patch_gss
class _TestGSSFQDN(ServerTestCase):
    """Unit tests for GSS server error"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which raises an error on GSS authentication"""

        def mock_gethostname():
            """Return a non-fully-qualified hostname"""

            return 'host'

        def mock_getfqdn():
            """Confirm getfqdn is called on relative hostnames"""

            return '1'

        with patch('socket.gethostname', mock_gethostname):
            with patch('socket.getfqdn', mock_getfqdn):
                return (yield from cls.create_server(gss_host=()))

    @asynctest
    def test_gss_fqdn_lookup(self):
        """Test GSS FQDN lookup"""

        with (yield from self.connect(username='user', gss_host=())) as conn:
            pass

        yield from conn.wait_closed()


@patch_getnameinfo
class _TestHostBasedAuth(ServerTestCase):
    """Unit tests for host-based authentication"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports host-based authentication"""

        return (yield from cls.create_server(
            _HostBasedServer, known_client_hosts='known_hosts'))

    @asynctest
    def test_client_host_auth(self):
        """Test connecting with host-based authentication"""

        with (yield from self.connect(username='user', client_host_keys='skey',
                                      client_username='user')) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_client_host_key_bytes(self):
        """Test client host key passed in as bytes"""

        with open('skey', 'rb') as f:
            skey = f.read()

        with (yield from self.connect(username='user', client_host_keys=[skey],
                                      client_username='user')) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_client_host_key_sshkey(self):
        """Test client host key passed in as an SSHKey"""

        skey = asyncssh.read_private_key('skey')

        with (yield from self.connect(username='user', client_host_keys=[skey],
                                      client_username='user')) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_client_host_key_keypairs(self):
        """Test client host keys passed in as a list of SSHKeyPairs"""

        keys = asyncssh.load_keypairs('skey')

        with (yield from self.connect(username='user', client_host_keys=keys,
                                      client_username='user')) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_client_host_signature_algs(self):
        """Test host based authentication with specific signature algorithms"""

        for alg in ('ssh-rsa', 'rsa-sha2-256', 'rsa-sha2-512'):
            with (yield from self.connect(username='user',
                                          client_host_keys='skey',
                                          client_username='user',
                                          signature_algs=[alg])) as conn:
                pass

            yield from conn.wait_closed()

    @asynctest
    def test_no_server_signature_algs(self):
        """Test a server which doesn't advertise signature algorithms"""

        def skip_ext_info(self):
            """Don't send extension information"""

            # pylint: disable=unused-argument

            return []

        with patch('asyncssh.connection.SSHConnection._get_ext_info_kex_alg',
                   skip_ext_info):
            with (yield from self.connect(username='user',
                                          client_host_keys='skey',
                                          client_username='user')) as conn:
                pass

        yield from conn.wait_closed()

    @asynctest
    def test_untrusted_client_host_key(self):
        """Test untrusted client host key"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='user', client_host_keys='ckey',
                                    client_username='user')

    @asynctest
    def test_missing_cert(self):
        """Test missing client host certificate"""

        with self.assertRaises(OSError):
            yield from self.connect(username='user',
                                    client_host_keys=[('skey', 'xxx')],
                                    client_username='user')

    @asynctest
    def test_invalid_client_host_signature(self):
        """Test invalid client host signature"""

        with patch('asyncssh.connection.SSHServerConnection',
                   _FailValidateHostSSHServerConnection):
            with self.assertRaises(asyncssh.DisconnectError):
                yield from self.connect(username='user',
                                        client_host_keys='skey',
                                        client_username='user')

    @asynctest
    def test_client_host_trailing_dot(self):
        """Test stripping of trailing dot from client host"""

        with (yield from self.connect(username='user', client_host_keys='skey',
                                      client_host='localhost.',
                                      client_username='user')) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_mismatched_client_host(self):
        """Test ignoring of mismatched client host due to canonicalization"""

        with (yield from self.connect(username='user', client_host_keys='skey',
                                      client_host='xxx',
                                      client_username='user')) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_mismatched_client_username(self):
        """Test mismatched client username"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='user', client_host_keys='skey',
                                    client_username='xxx')

    @asynctest
    def test_invalid_client_username(self):
        """Test invalid client username"""

        with patch('asyncssh.connection.SSHClientConnection',
                   _InvalidUsernameClientConnection):
            with self.assertRaises(asyncssh.DisconnectError):
                yield from self.connect(username='user',
                                        client_host_keys='skey')

    @asynctest
    def test_expired_cert(self):
        """Test expired certificate"""

        ckey = asyncssh.read_private_key('ckey')
        skey = asyncssh.read_private_key('skey')

        cert = make_certificate('ssh-rsa-cert-v01@openssh.com',
                                CERT_TYPE_HOST, ckey, skey, ['localhost'],
                                valid_before=1)

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='user',
                                    client_host_keys=[(ckey, cert)],
                                    client_username='user')

    @asynctest
    def test_untrusted_ca(self):
        """Test untrusted CA"""

        ckey = asyncssh.read_private_key('ckey')

        cert = make_certificate('ssh-rsa-cert-v01@openssh.com',
                                CERT_TYPE_HOST, ckey, ckey, ['localhost'])

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='user',
                                    client_host_keys=[(ckey, cert)],
                                    client_username='user')


@patch_getnameinfo
class _TestKeysignHostBasedAuth(ServerTestCase):
    """Unit tests for host-based authentication using ssh-keysign"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports host-based authentication"""

        return (yield from cls.create_server(
            _HostBasedServer, known_client_hosts='known_hosts'))

    @asyncio.coroutine
    def _connect_keysign(self, client_host_keysign=True,
                         client_host_keys=None, keysign_dirs=('.',)):
        """Open a connection to test host-based auth using ssh-keysign"""

        with patch('asyncio.create_subprocess_exec',
                   create_subprocess_exec_stub):
            with patch('asyncssh.keysign._DEFAULT_KEYSIGN_DIRS', keysign_dirs):
                with patch('asyncssh.public_key._DEFAULT_HOST_KEY_DIRS', ['.']):
                    with patch('asyncssh.public_key._DEFAULT_HOST_KEY_FILES',
                               ['skey', 'xxx']):
                        return (yield from self.connect(
                            username='user',
                            client_host_keysign=client_host_keysign,
                            client_host_keys=client_host_keys,
                            client_username='user'))

    @asynctest
    def test_keysign(self):
        """Test host-based authentication using ssh-keysign"""

        with (yield from self._connect_keysign()) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_explciit_keysign(self):
        """Test ssh-keysign with an explicit path"""

        with (yield from self._connect_keysign(
            client_host_keysign='.')) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_keysign_explicit_host_keys(self):
        """Test ssh-keysign with explicit host public keys"""

        with (yield from self._connect_keysign(
            client_host_keys='skey.pub')) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_invalid_keysign_response(self):
        """Test invalid ssh-keysign response"""

        with patch('asyncssh.keysign.KEYSIGN_VERSION', 0):
            with self.assertRaises(asyncssh.DisconnectError):
                yield from self._connect_keysign()

    @asynctest
    def test_keysign_error(self):
        """Test ssh-keysign error response"""

        with patch('asyncssh.keysign.KEYSIGN_VERSION', 1):
            with self.assertRaises(asyncssh.DisconnectError):
                yield from self._connect_keysign()

    @asynctest
    def test_invalid_keysign_version(self):
        """Test invalid version in ssh-keysign request"""

        with patch('asyncssh.keysign.KEYSIGN_VERSION', 99):
            with self.assertRaises(asyncssh.DisconnectError):
                yield from self._connect_keysign()

    @asynctest
    def test_keysign_not_found(self):
        """Test ssh-keysign executable not being found"""

        with self.assertRaises(ValueError):
            yield from self._connect_keysign(keysign_dirs=())

    @asynctest
    def test_explicit_keysign_not_found(self):
        """Test explicit ssh-keysign executable not being found"""

        with self.assertRaises(ValueError):
            yield from self._connect_keysign(client_host_keysign='xxx')

    @asynctest
    def test_keysign_dir_not_present(self):
        """Test ssh-keysign executable not in a keysign dir"""

        with self.assertRaises(ValueError):
            yield from self._connect_keysign(keysign_dirs=('xxx',))


@patch_getnameinfo
class _TestHostBasedAsyncServerAuth(_TestHostBasedAuth):
    """Unit tests for host-based authentication with async server callbacks"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports async host-based auth"""

        return (yield from cls.create_server(
            _AsyncHostBasedServer, known_client_hosts='known_hosts',
            trust_client_host=True))

    @asynctest
    def test_mismatched_client_host(self):
        """Test mismatch of trusted client host"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='user', client_host_keys='skey',
                                    client_host='xxx',
                                    client_username='user')


@patch_getnameinfo
class _TestLimitedHostBasedSignatureAlgs(ServerTestCase):
    """Unit tests for limited host key signature algorithms"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports host-based authentication"""

        return (yield from cls.create_server(
            _HostBasedServer, known_client_hosts='known_hosts',
            signature_algs=['ssh-rsa', 'rsa-sha2-512']))

    @asynctest
    def test_mismatched_host_signature_algs(self):
        """Test mismatched host key signature algorithms"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='ckey', client_host_keys='skey',
                                    client_username='user',
                                    signature_algs=['rsa-sha2-256'])

    @asynctest
    def test_host_signature_alg_fallback(self):
        """Test fall back to default host key signature algorithm"""

        with (yield from self.connect(username='ckey', client_host_keys='skey',
                                      client_username='user',
                                      signature_algs=['rsa-sha2-256',
                                                      'ssh-rsa'])) as conn:
            pass

        yield from conn.wait_closed()


class _TestPublicKeyAuth(ServerTestCase):
    """Unit tests for public key authentication"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports public key authentication"""

        return (yield from cls.create_server(
            _PublicKeyServer, authorized_client_keys='authorized_keys'))

    @asyncio.coroutine
    def _connect_publickey(self, keylist, test_async=False):
        """Open a connection to test public key auth"""

        def client_factory():
            """Return an SSHClient to use to do public key auth"""

            cls = _AsyncPublicKeyClient if test_async else _PublicKeyClient
            return cls(keylist)

        conn, _ = yield from self.create_connection(client_factory,
                                                    username='ckey',
                                                    client_keys=None)

        return conn

    @asynctest
    def test_agent_auth(self):
        """Test connecting with ssh-agent authentication"""

        if not self.agent_available(): # pragma: no cover
            self.skipTest('ssh-agent not available')

        with (yield from self.connect(username='ckey')) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_agent_signature_algs(self):
        """Test ssh-agent keys with specific signature algorithms"""

        if not self.agent_available(): # pragma: no cover
            self.skipTest('ssh-agent not available')

        for alg in ('ssh-rsa', 'rsa-sha2-256', 'rsa-sha2-512'):
            with (yield from self.connect(username='ckey',
                                          signature_algs=[alg])) as conn:
                pass

            yield from conn.wait_closed()

    @asynctest
    def test_agent_auth_failure(self):
        """Test failure connecting with ssh-agent authentication"""

        if not self.agent_available(): # pragma: no cover
            self.skipTest('ssh-agent not available')

        with patch.dict(os.environ, HOME='xxx'):
            with self.assertRaises(asyncssh.DisconnectError):
                yield from self.connect(username='ckey', agent_path='xxx',
                                        known_hosts='.ssh/known_hosts')

    @asynctest
    def test_agent_auth_unset(self):
        """Test connecting with no local keys and no ssh-agent configured"""

        with patch.dict(os.environ, HOME='xxx', SSH_AUTH_SOCK=''):
            with self.assertRaises(asyncssh.DisconnectError):
                yield from self.connect(username='ckey',
                                        known_hosts='.ssh/known_hosts')

    @asynctest
    def test_public_key_auth(self):
        """Test connecting with public key authentication"""

        with (yield from self.connect(username='ckey',
                                      client_keys='ckey')) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_public_key_signature_algs(self):
        """Test public key authentication with specific signature algorithms"""

        for alg in ('ssh-rsa', 'rsa-sha2-256', 'rsa-sha2-512'):
            with (yield from self.connect(username='ckey', client_keys='ckey',
                                          signature_algs=[alg])) as conn:
                pass

            yield from conn.wait_closed()

    @asynctest
    def test_no_server_signature_algs(self):
        """Test a server which doesn't advertise signature algorithms"""

        def skip_ext_info(self):
            """Don't send extension information"""

            # pylint: disable=unused-argument

            return []

        with patch('asyncssh.connection.SSHConnection._get_ext_info_kex_alg',
                   skip_ext_info):
            with (yield from self.connect(username='ckey', client_keys='ckey',
                                          agent_path=None)) as conn:
                pass

        yield from conn.wait_closed()

    @asynctest
    def test_default_public_key_auth(self):
        """Test connecting with default public key authentication"""

        with (yield from self.connect(username='ckey',
                                      agent_path=None)) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_invalid_default_key(self):
        """Test connecting with invalid default client key"""

        key_path = os.path.join('.ssh', 'id_dsa')
        with open(key_path, 'w') as f:
            f.write('-----XXX-----')

        with self.assertRaises(asyncssh.KeyImportError):
            yield from self.connect(username='ckey', agent_path=None)

        os.remove(key_path)

    @asynctest
    def test_client_key_bytes(self):
        """Test client key passed in as bytes"""

        with open('ckey', 'rb') as f:
            ckey = f.read()

        with (yield from self.connect(username='ckey',
                                      client_keys=[ckey])) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_client_key_sshkey(self):
        """Test client key passed in as an SSHKey"""

        ckey = asyncssh.read_private_key('ckey')

        with (yield from self.connect(username='ckey',
                                      client_keys=[ckey])) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_client_key_keypairs(self):
        """Test client keys passed in as a list of SSHKeyPairs"""

        keys = asyncssh.load_keypairs('ckey')

        with (yield from self.connect(username='ckey',
                                      client_keys=keys)) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_client_key_agent_keypairs(self):
        """Test client keys passed in as a list of SSHAgentKeyPairs"""

        if not self.agent_available(): # pragma: no cover
            self.skipTest('ssh-agent not available')

        agent = yield from asyncssh.connect_agent()

        for key in (yield from agent.get_keys()):
            with (yield from self.connect(username='ckey',
                                          client_keys=[key])) as conn:
                pass

        yield from conn.wait_closed()

        agent.close()

    @asynctest
    def test_untrusted_client_key(self):
        """Test untrusted client key"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='ckey', client_keys='skey')

    @asynctest
    def test_missing_cert(self):
        """Test missing client certificate"""

        with self.assertRaises(OSError):
            yield from self.connect(username='ckey',
                                    client_keys=[('ckey', 'xxx')])

    @asynctest
    def test_expired_cert(self):
        """Test expired certificate"""

        ckey = asyncssh.read_private_key('ckey')
        skey = asyncssh.read_private_key('skey')

        cert = make_certificate('ssh-rsa-cert-v01@openssh.com',
                                CERT_TYPE_USER, skey, ckey, ['ckey'],
                                valid_before=1)

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='ckey', client_keys=[(skey, cert)])

    @asynctest
    def test_allowed_address(self):
        """Test allowed address in certificate"""

        ckey = asyncssh.read_private_key('ckey')
        skey = asyncssh.read_private_key('skey')

        cert = make_certificate('ssh-rsa-cert-v01@openssh.com',
                                CERT_TYPE_USER, skey, ckey, ['ckey'],
                                options={'source-address':
                                         String('0.0.0.0/0,::/0')})

        with (yield from self.connect(username='ckey',
                                      client_keys=[(skey, cert)])) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_disallowed_address(self):
        """Test disallowed address in certificate"""

        ckey = asyncssh.read_private_key('ckey')
        skey = asyncssh.read_private_key('skey')

        cert = make_certificate('ssh-rsa-cert-v01@openssh.com',
                                CERT_TYPE_USER, skey, ckey, ['ckey'],
                                options={'source-address': String('0.0.0.0')})

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='ckey', client_keys=[(skey, cert)])

    @asynctest
    def test_untrusted_ca(self):
        """Test untrusted CA"""

        skey = asyncssh.read_private_key('skey')

        cert = make_certificate('ssh-rsa-cert-v01@openssh.com',
                                CERT_TYPE_USER, skey, skey, ['skey'])

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='ckey', client_keys=[(skey, cert)])

    @asynctest
    def test_mismatched_ca(self):
        """Test mismatched CA"""

        ckey = asyncssh.read_private_key('ckey')
        skey = asyncssh.read_private_key('skey')

        cert = make_certificate('ssh-rsa-cert-v01@openssh.com',
                                CERT_TYPE_USER, skey, skey, ['skey'])

        with self.assertRaises(ValueError):
            yield from self.connect(username='ckey',
                                    client_keys=[(ckey, cert)])

    @asynctest
    def test_callback(self):
        """Test connecting with public key authentication using callback"""

        with (yield from self._connect_publickey(['ckey'],
                                                 test_async=True)) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_callback_sshkeypair(self):
        """Test client key passed in as an SSHKeyPair by callback"""

        if not self.agent_available(): # pragma: no cover
            self.skipTest('ssh-agent not available')

        agent = yield from asyncssh.connect_agent()
        keylist = yield from agent.get_keys()

        with (yield from self._connect_publickey(keylist)) as conn:
            pass

        yield from conn.wait_closed()

        agent.close()

    @asynctest
    def test_callback_untrusted_client_key(self):
        """Test failure connecting with public key authentication callback"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self._connect_publickey(['skey'])

    @asynctest
    def test_unknown_auth(self):
        """Test server returning an unknown auth method before public key"""

        with patch('asyncssh.connection.SSHClientConnection',
                   _UnknownAuthClientConnection):
            with (yield from self.connect(username='ckey', client_keys='ckey',
                                          agent_path=None)) as conn:
                pass

        yield from conn.wait_closed()


class _TestPublicKeyAsyncServerAuth(_TestPublicKeyAuth):
    """Unit tests for public key authentication with async server callbacks"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports async public key auth"""

        def server_factory():
            """Return an SSH server which trusts specific client keys"""

            return _AsyncPublicKeyServer(client_keys=['ckey.pub',
                                                      'ckey_ecdsa.pub'])

        return (yield from cls.create_server(server_factory))


class _TestLimitedPublicKeySignatureAlgs(ServerTestCase):
    """Unit tests for limited public key signature algorithms"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports public key authentication"""

        return (yield from cls.create_server(
            _PublicKeyServer, authorized_client_keys='authorized_keys',
            signature_algs=['ssh-rsa', 'rsa-sha2-512']))

    @asynctest
    def test_mismatched_client_signature_algs(self):
        """Test mismatched client key signature algorithms"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='ckey', client_keys='ckey',
                                    signature_algs=['rsa-sha2-256'])

    @asynctest
    def test_client_signature_alg_fallback(self):
        """Test fall back to default client key signature algorithm"""

        with (yield from self.connect(username='ckey', client_keys='ckey',
                                      signature_algs=['rsa-sha2-256',
                                                      'ssh-rsa'])) as conn:
            pass

        yield from conn.wait_closed()


class _TestSetAuthorizedKeys(ServerTestCase):
    """Unit tests for public key authentication with set_authorized_keys"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports public key authentication"""

        def server_factory():
            """Return an SSH server which calls set_authorized_keys"""

            return _PublicKeyServer(authorized_keys='authorized_keys')

        return (yield from cls.create_server(server_factory))

    @asynctest
    def test_set_authorized_keys(self):
        """Test set_authorized_keys method on server"""

        with (yield from self.connect(username='ckey',
                                      client_keys='ckey')) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_cert_principals(self):
        """Test certificate principals check"""

        ckey = asyncssh.read_private_key('ckey')

        cert = make_certificate('ssh-rsa-cert-v01@openssh.com',
                                CERT_TYPE_USER, ckey, ckey, ['ckey'])

        with (yield from self.connect(username='ckey',
                                      client_keys=[(ckey, cert)])) as conn:
            pass

        yield from conn.wait_closed()


class _TestPreloadedAuthorizedKeys(ServerTestCase):
    """Unit tests for authentication with pre-loaded authorized keys"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports public key authentication"""

        def server_factory():
            """Return an SSH server which calls set_authorized_keys"""

            authorized_keys = asyncssh.read_authorized_keys('authorized_keys')
            return _PublicKeyServer(authorized_keys=authorized_keys)

        return (yield from cls.create_server(server_factory))

    @asynctest
    def test_pre_loaded_authorized_keys(self):
        """Test set_authorized_keys with pre-loaded authorized keys"""

        with (yield from self.connect(username='ckey',
                                      client_keys='ckey')) as conn:
            pass

        yield from conn.wait_closed()


@unittest.skipUnless(x509_available, 'X.509 not available')
class _TestX509Auth(ServerTestCase):
    """Unit tests for X.509 certificate authentication"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports public key authentication"""

        return (yield from cls.create_server(
            _PublicKeyServer, authorized_client_keys='authorized_keys_x509'))

    @asynctest
    def test_x509_self(self):
        """Test connecting with X.509 self-signed certificate"""

        with (yield from self.connect(username='ckey',
                                      client_keys=['ckey_x509_self'])) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_x509_chain(self):
        """Test connecting with X.509 certificate chain"""

        with (yield from self.connect(username='ckey',
                                      client_keys=['ckey_x509_chain'])) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_x509_incomplete_chain(self):
        """Test connecting with incomplete X.509 certificate chain"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='ckey',
                                    client_keys=[('ckey_x509_chain',
                                                  'ckey_x509_partial.pem')])

    @asynctest
    def test_x509_untrusted_cert(self):
        """Test connecting with untrusted X.509 certificate chain"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='ckey',
                                    client_keys=['skey_x509_chain'])

@unittest.skipUnless(x509_available, 'X.509 not available')
class _TestX509AuthDisabled(ServerTestCase):
    """Unit tests for disabled X.509 certificate authentication"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which doesn't support X.509 authentication"""

        return (yield from cls.create_server(
            _PublicKeyServer, x509_trusted_certs=None,
            authorized_client_keys='authorized_keys'))

    @asynctest
    def test_failed_x509_auth(self):
        """Test connect failure with X.509 certificate"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='ckey',
                                    client_keys=['ckey_x509_self'],
                                    signature_algs=['x509v3-ssh-rsa'])

    @asynctest
    def test_non_x509(self):
        """Test connecting without an X.509 certificate"""

        with (yield from self.connect(username='ckey',
                                      client_keys=['ckey'])) as conn:
            pass

        yield from conn.wait_closed()


@unittest.skipUnless(x509_available, 'X.509 not available')
class _TestX509Subject(ServerTestCase):
    """Unit tests for X.509 certificate authentication by subject name"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports public key authentication"""

        authorized_keys = asyncssh.import_authorized_keys(
            'x509v3-ssh-rsa subject=OU=name\n')

        return (yield from cls.create_server(
            _PublicKeyServer, authorized_client_keys=authorized_keys,
            x509_trusted_certs=['ckey_x509_self.pub']))

    @asynctest
    def test_x509_subject(self):
        """Test authenticating X.509 certificate by subject name"""

        with (yield from self.connect(username='ckey',
                                      client_keys=['ckey_x509_self'])) as conn:
            pass

        yield from conn.wait_closed()


@unittest.skipUnless(x509_available, 'X.509 not available')
class _TestX509Untrusted(ServerTestCase):
    """Unit tests for X.509 authentication with no trusted certificates"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports public key authentication"""

        return (yield from cls.create_server(
            _PublicKeyServer, authorized_client_keys=None))

    @asynctest
    def test_x509_untrusted(self):
        """Test untrusted X.509 self-signed certificate"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='ckey',
                                    client_keys=['ckey_x509_self'])


@unittest.skipUnless(x509_available, 'X.509 not available')
class _TestX509Disabled(ServerTestCase):
    """Unit tests for X.509 authentication with server support disabled"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server with X.509 authentication disabled"""

        return (yield from cls.create_server(_PublicKeyServer,
                                             x509_purposes=None))

    @asynctest
    def test_x509_disabled(self):
        """Test X.509 client certificate with server support disabled"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='ckey',
                                    client_keys='skey_x509_self')


class _TestPasswordAuth(ServerTestCase):
    """Unit tests for password authentication"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports password authentication"""

        return (yield from cls.create_server(_PasswordServer))

    @asyncio.coroutine
    def _connect_password(self, username, password, old_password='',
                          new_password='', test_async=False):
        """Open a connection to test password authentication"""

        def client_factory():
            """Return an SSHClient to use to do password change"""

            cls = _AsyncPasswordClient if test_async else _PasswordClient
            return cls(password, old_password, new_password)

        conn, _ = yield from self.create_connection(client_factory,
                                                    username=username,
                                                    client_keys=None)

        return conn

    @asynctest
    def test_password_auth(self):
        """Test connecting with password authentication"""

        with (yield from self.connect(username='pw', password='pw',
                                      client_keys=None)) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_password_auth_failure(self):
        """Test _failure connecting with password authentication"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='pw', password='badpw',
                                    client_keys=None)

    @asynctest
    def test_password_auth_callback(self):
        """Test connecting with password authentication callback"""

        with (yield from self._connect_password('pw', 'pw',
                                                test_async=True)) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_password_auth_callback_failure(self):
        """Test failure connecting with password authentication callback"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self._connect_password('pw', 'badpw')

    @asynctest
    def test_password_change(self):
        """Test password change"""

        with (yield from self._connect_password('pw', 'oldpw', 'oldpw', 'pw',
                                                test_async=True)) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_password_change_failure(self):
        """Test failure of password change"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self._connect_password('pw', 'oldpw', 'badpw', 'pw')


class _TestPasswordAsyncServerAuth(_TestPasswordAuth):
    """Unit tests for password authentication with async server callbacks"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports async password authentication"""

        return (yield from cls.create_server(_AsyncPasswordServer))


class _TestKbdintAuth(ServerTestCase):
    """Unit tests for keyboard-interactive authentication"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports keyboard-interactive auth"""

        return (yield from cls.create_server(_KbdintServer))

    @asyncio.coroutine
    def _connect_kbdint(self, username, responses, test_async=False):
        """Open a connection to test keyboard-interactive auth"""

        def client_factory():
            """Return an SSHClient to use to do keyboard-interactive auth"""

            cls = _AsyncKbdintClient if test_async else _KbdintClient
            return cls(responses)

        conn, _ = yield from self.create_connection(client_factory,
                                                    username=username,
                                                    client_keys=None)

        return conn

    @asynctest
    def test_kbdint_auth(self):
        """Test connecting with keyboard-interactive authentication"""

        with (yield from self.connect(username='kbdint', password='kbdint',
                                      client_keys=None)) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_kbdint_auth_failure(self):
        """Test failure connecting with keyboard-interactive authentication"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.connect(username='kbdint', password='badpw',
                                    client_keys=None)

    @asynctest
    def test_kbdint_auth_callback(self):
        """Test keyboard-interactive auth callback"""

        with (yield from self._connect_kbdint('kbdint', ['kbdint'],
                                              test_async=True)) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_kbdint_auth_callback_faliure(self):
        """Test failure connection with keyboard-interactive auth callback"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self._connect_kbdint('kbdint', ['badpw'])


class _TestKbdintAsyncServerAuth(_TestKbdintAuth):
    """Unit tests for keyboard-interactive auth with async server callbacks"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports async kbd-int auth"""

        return (yield from cls.create_server(_AsyncKbdintServer))


class _TestKbdintPasswordServerAuth(ServerTestCase):
    """Unit tests for keyboard-interactive auth with server password auth"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server which supports server password auth"""

        return (yield from cls.create_server(_PasswordServer))

    @asyncio.coroutine
    def _connect_kbdint(self, username, responses):
        """Open a connection to test keyboard-interactive auth"""

        def client_factory():
            """Return an SSHClient to use to do keyboard-interactive auth"""

            return _KbdintClient(responses)

        conn, _ = yield from self.create_connection(client_factory,
                                                    username=username,
                                                    client_keys=None)

        return conn

    @asynctest
    def test_kbdint_password_auth(self):
        """Test keyboard-interactive server password authentication"""

        with (yield from self._connect_kbdint('pw', ['pw'])) as conn:
            pass

        yield from conn.wait_closed()

    @asynctest
    def test_kbdint_password_auth_multiple_responses(self):
        """Test multiple responses to server password authentication"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self._connect_kbdint('pw', ['xxx', 'yyy'])

    @asynctest
    def test_kbdint_password_change(self):
        """Test keyboard-interactive server password change"""

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self._connect_kbdint('pw', ['oldpw'])


class _TestLoginTimeoutExceeded(ServerTestCase):
    """Unit test for login timeout"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server with a 1 second login timeout"""

        return (yield from cls.create_server(
            _PublicKeyServer, authorized_client_keys='authorized_keys',
            login_timeout=1))

    @asynctest
    def test_login_timeout_exceeded(self):
        """Test login timeout exceeded"""

        def client_factory():
            """Return an SSHClient that delays before providing a key"""

            return _PublicKeyClient(['ckey'], 2)

        with self.assertRaises(asyncssh.DisconnectError):
            yield from self.create_connection(client_factory, username='ckey',
                                              client_keys=None)


class _TestLoginTimeoutDisabled(ServerTestCase):
    """Unit test for disabled login timeout"""

    @classmethod
    @asyncio.coroutine
    def start_server(cls):
        """Start an SSH server with no login timeout"""

        return (yield from cls.create_server(
            _PublicKeyServer, authorized_client_keys='authorized_keys',
            login_timeout=None))

    @asynctest
    def test_login_timeout_disabled(self):
        """Test with login timeout disabled"""

        with (yield from self.connect(username='ckey',
                                      client_keys='ckey')) as conn:
            pass

        yield from conn.wait_closed()
