# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
from base64 import urlsafe_b64encode as b64encode
import binascii
import json
import os
import math
import time
import urllib.parse as urlparse

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tokenlib.utils import decode_token_bytes
from webtest import TestApp

DEFAULT_OAUTH_SCOPE = 'https://identity.mozilla.com/apps/oldsync'


class TestCase:
    FXA_EMAIL_DOMAIN = 'api-accounts.stage.mozaws.net'
    FXA_METRICS_HASH_SECRET = os.environ.get("SYNC_MASTER_SECRET", 'secret0')
    NODE_ID = 800
    NODE_URL = 'https://example.com'
    TOKEN_SIGNING_SECRET = os.environ.get("SYNC_MASTER_SECRET", 'secret0')
    TOKENSERVER_HOST = os.environ['TOKENSERVER_HOST']

    @classmethod
    def setUpClass(cls):
        cls._build_auth_headers = cls._build_oauth_headers

    def setUp(self):
        self._db_connect()

        host_url = urlparse.urlparse(self.TOKENSERVER_HOST)
        self.app = TestApp(self.TOKENSERVER_HOST, extra_environ={
            'HTTP_HOST': host_url.netloc,
            'wsgi.url_scheme': host_url.scheme or 'http',
            'SERVER_NAME': host_url.hostname,
            'REMOTE_ADDR': '127.0.0.1',
            'SCRIPT_NAME': host_url.path,
        })

        # Start each test with a blank slate.
        cursor = self._execute_sql(('DELETE FROM users'), ())
        cursor.close()

        cursor = self._execute_sql(('DELETE FROM nodes'), ())
        cursor.close()

        self.service_id = self._add_service('sync-1.5', r'{node}/1.5/{uid}')

        # Ensure we have a node with enough capacity to run the tests.
        self._add_node(capacity=100, node=self.NODE_URL, id=self.NODE_ID)
        self.database.close()

    def tearDown(self):
        self._db_connect()

        # And clean up at the end, for good measure.
        cursor = self._execute_sql(('DELETE FROM users'), ())
        cursor.close()

        cursor = self._execute_sql(('DELETE FROM nodes'), ())
        cursor.close()

        cursor = self._execute_sql(('DELETE FROM services'), ())
        cursor.close()

        self.database.close()

    def _build_oauth_headers(self, generation=None, user='test',
                             keys_changed_at=None, client_state=None,
                             status=200, **additional_headers):
        claims = {
            'user': user,
            'generation': generation,
            'client_id': 'fake client id',
            'scope': [DEFAULT_OAUTH_SCOPE],
        }

        if generation is not None:
            claims['generation'] = generation

        body = {
            'body': claims,
            'status': status
        }

        headers = {}
        headers['Authorization'] = 'Bearer %s' % json.dumps(body)
        client_state = binascii.unhexlify(client_state)
        client_state = b64encode(client_state).strip(b'=').decode('utf-8')
        headers['X-KeyID'] = '%s-%s' % (keys_changed_at, client_state)
        headers.update(additional_headers)

        return headers

    def _add_node(self, capacity=100, available=100, node=NODE_URL, id=None,
                  current_load=0, backoff=0, downed=0):
        self._db_connect()
        query = 'INSERT INTO nodes (service, node, available, capacity, \
            current_load, backoff, downed'
        data = (self.service_id, node, available, capacity, current_load,
                backoff, downed)

        if id:
            query += ', id) VALUES(%s, %s, %s, %s, %s, %s, %s, %s)'
            data += (id,)
        else:
            query += ') VALUES(%s, %s, %s, %s, %s, %s, %s)'

        cursor = self._execute_sql(query, data)
        cursor.close()
        self.database.close()

        return self._last_insert_id()

    def _get_node(self, id):
        self._db_connect()
        query = 'SELECT * FROM nodes WHERE id=%s'
        cursor = self._execute_sql(query, (id,))
        (id, service, node, available, current_load, capacity, downed,
         backoff) = cursor.fetchone()
        cursor.close()
        self.database.close()

        return {
            'id': id,
            'service': service,
            'node': node,
            'available': available,
            'current_load': current_load,
            'capacity': capacity,
            'downed': downed,
            'backoff': backoff
        }

    def _last_insert_id(self):
        self._db_connect()
        cursor = self._execute_sql('SELECT LAST_INSERT_ID()', ())
        (id,) = cursor.fetchone()
        cursor.close()
        self.database.close()

        return id

    def _add_service(self, service_name, pattern):
        self._db_connect()
        query = 'INSERT INTO services (service, pattern) \
            VALUES(%s, %s)'
        cursor = self._execute_sql(query, (service_name, pattern))
        cursor.close()
        self.database.close()

        return self._last_insert_id()

    def _add_user(self, email=None, generation=1234, client_state='aaaa',
                  created_at=None, nodeid=NODE_ID, keys_changed_at=1234,
                  replaced_at=None):
        self._db_connect()
        query = '''
            INSERT INTO users (service, email, generation, client_state, \
                created_at, nodeid, keys_changed_at, replaced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
        '''
        created_at = created_at or math.trunc(time.time() * 1000)
        cursor = self._execute_sql(query,
                                   (self.service_id,
                                    email or 'test@%s' % self.FXA_EMAIL_DOMAIN,
                                    generation, client_state,
                                    created_at, nodeid, keys_changed_at,
                                    replaced_at))
        cursor.close()
        self.database.close()

        return self._last_insert_id()

    def _get_user(self, uid):
        self._db_connect()
        query = 'SELECT * FROM users WHERE uid = %s'
        cursor = self._execute_sql(query, (uid,))

        (uid, service, email, generation, client_state, created_at,
         replaced_at, nodeid, keys_changed_at) = cursor.fetchone()
        cursor.close()
        self.database.close()

        return {
            'uid': uid,
            'service': service,
            'email': email,
            'generation': generation,
            'client_state': client_state,
            'created_at': created_at,
            'replaced_at': replaced_at,
            'nodeid': nodeid,
            'keys_changed_at': keys_changed_at
        }

    def _get_replaced_users(self, service_id, email):
        self._db_connect()
        query = 'SELECT * FROM users WHERE service = %s AND email = %s AND \
            replaced_at IS NOT NULL'
        cursor = self._execute_sql(query, (service_id, email))

        users = []
        for user in cursor.fetchall():
            (uid, service, email, generation, client_state, created_at,
             replaced_at, nodeid, keys_changed_at) = user

            user_dict = {
                'uid': uid,
                'service': service,
                'email': email,
                'generation': generation,
                'client_state': client_state,
                'created_at': created_at,
                'replaced_at': replaced_at,
                'nodeid': nodeid,
                'keys_changed_at': keys_changed_at
            }
            users.append(user_dict)

        cursor.close()
        self.database.close()
        return users

    def _get_service_id(self, service):
        self._db_connect()
        query = 'SELECT id FROM services WHERE service = %s'
        cursor = self._execute_sql(query, (service,))
        (service_id,) = cursor.fetchone()
        cursor.close()
        self.database.close()

        return service_id

    def _count_users(self):
        self._db_connect()
        query = 'SELECT COUNT(DISTINCT(uid)) FROM users'
        cursor = self._execute_sql(query, ())
        (count,) = cursor.fetchone()
        cursor.close()
        self.database.close()

        return count

    def _execute_sql(self, query, args):
        cursor = self.database.execute(query, args)

        return cursor

    def _db_connect(self):
        time.sleep(2)  # FIXME: Don't waste time like that
        engine = create_engine(os.environ['SYNC_TOKENSERVER__DATABASE_URL'], poolclass=NullPool)
        self.database = engine. \
            execution_options(isolation_level='AUTOCOMMIT'). \
            connect()

    def unsafelyParseToken(self, token):
        # For testing purposes, don't check HMAC or anything...
        return json.loads(decode_token_bytes(token)[:-32].decode('utf8'))
