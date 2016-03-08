"""
yubistack.wsgi
~~~~~~~~~~~~~~

WSGI wrappers around the yubistack functions
to support backward compatibility.
"""

import json
import logging
import re
import time

from yubistack.config import (
    settings,
    TOKEN_LEN
)
from yubistack.exceptions import (
    YKAuthError,
    YKValError,
    YKSyncError,
    YKKSMError,
)
from yubistack.utils import (
    parse_querystring,
    wsgi_response,
    sign,
)
from yubistack.sync import Sync
from yubistack.ykauth import Client
from yubistack.ykksm import Decryptor
from yubistack.ykval import Validator

if settings['SYSLOG_WSGI_AUTH']:
    import syslog
    syslog.openlog(ident='yubistack', facility=syslog.LOG_AUTH)

logger = logging.getLogger(__name__)

HTTP_STATUS_CODES = {
    200: '200 OK',
    400: '400 Bad Request',
    401: '401 Unauthorized',
    404: '404 Not Found',
    500: '500 Internal Server Error',
}
URI_REGEX = re.compile(r"""
    ^/?                     # Leading slash might not be present
    (                       # Intersection: yubiauth and yubikey-val + yubikey-ksm differences
        wsapi(/\d+\.\d+)?   # YKVal and YKKsm uses wsapi prefix + protocol version
      | yubiauth/client     # Yubiclient authentication URI
    )
    /
    (?P<resource>           # The resource should map to a function name
        (
            decrypt
          | verify
          | sync
          | resync
          | authenticate
        )
    )
    /?                      # Trailing clash might not be present
""", re.VERBOSE)

PERSISTENT_OBJECTS = {}

REQUIRED_AUTH_PARAMS = ['username', 'password', 'otp']
def authenticate(environ, start_response):
    """
    Handle authentications
    """
    start_time = time.time()

    if 'client' not in PERSISTENT_OBJECTS:
        PERSISTENT_OBJECTS['client'] = Client()
    client = PERSISTENT_OBJECTS['client']
    _format = 'json' if environ.get('HTTP_ACCEPT') == 'application/json' else 'text'
    params = {}

    try:
        # Parse POST request
        try:
            request_body_size = int(environ.get('CONTENT_LENGTH', 0))
        except ValueError:
            request_body_size = 0
        request_body = environ['wsgi.input'].read(request_body_size)
        params = parse_querystring(request_body.decode())
        _params = params.copy()
        _params['password'] = '*' * 8
        logger.debug('PROCESSED QUERYSTRING: %s', _params)
        # Checking parameters
        for req_param in REQUIRED_AUTH_PARAMS:
            if req_param not in params:
                raise YKAuthError('MISSING_PARAMETER')
        client.authenticate(params['username'], params['password'], params['otp'])
        status_code = 200
        output = {'status': 'OK',
                  'message': 'Successful authentication'}
    except (YKAuthError, YKValError, YKSyncError, YKKSMError) as err:
        status_code = 400
        output = {'status': err.error_code,
                  'message': str(err)}
    except Exception as err:
        status_code = 500
        output = {'status': 'BACKEND_ERROR',
                  'message': 'Backend error: %s' % err}
        logger.exception('Backend error: %s', err)
    finally:
        content_type = 'application/json' if _format == 'json' else 'text/plain'
        start_response(HTTP_STATUS_CODES[status_code], [('Content-Type', content_type)])
        output['username'] = params.get('username', '')
        if len(params.get('otp', '')) >= TOKEN_LEN:
            output['token_id'] = params['otp'][:-TOKEN_LEN]
        else:
            output['token_id'] = ''
        output['latency'] = round(time.time() - start_time, 3)
        output['src_ip'] = environ.get('REMOTE_ADDR')
        response = json.dumps(output if _format == 'json' else (status_code == 200))
        if settings['SYSLOG_WSGI_AUTH']:
            if status_code == 200:
                severity = syslog.LOG_INFO
            elif status_code == 400:
                severity = syslog.LOG_WARNING
            else:
                severity = syslog.LOG_ERR
            syslog.syslog(severity, response if _format == 'json' else json.dumps(output))
        return [response.encode()]

def decrypt(environ, start_response):
    """
    Handle OTP decryptions
    """
    _format = 'text'
    try:
        params = parse_querystring(environ['QUERY_STRING'])
        if params.get('format') == 'json' or environ['HTTP_ACCEPT'] == 'application/json':
            _format = 'json'
        logger.debug('PROCESSED QUERYSTRING: %s', params)
        decryptor = Decryptor()
        output = decryptor.decrypt(params.get('otp'))
        if _format != 'json':
            output = 'OK counter=%(counter)s low=%(low)s high=%(high)s use=%(use)s\n' % output
        status_code = 200
    except YKKSMError as err:
        logger.exception('Decryption error: %s', err.error_code)
        output = '%s' % str(err)
        status_code = 400
    except Exception as err:
        logger.exception('Backend error: %s', err)
        output = 'Backend failure\n'
        status_code = 500
    finally:
        content_type = 'application/json' if _format == 'json' else 'text/plain'
        if _format == 'json':
            if isinstance(output, str):
                output = {'error': output}
            output = json.dumps(output)
        elif status_code != 200:
            output = 'ERR %s\n' % output
        start_response(HTTP_STATUS_CODES[status_code], [('Content-Type', content_type)])
        return [output.encode()]

PARAM_MAP = {
    'id': 'client_id',
    'sl': 'sync_level',
}
def verify(environ, start_response):
    """
    Handle OTP Validation
    """
    apikey = ''.encode()
    try:
        params = parse_querystring(environ['QUERY_STRING'])
        public_id = params.get('otp', '?' * 12)[:12]
        logger.debug('%s: PROCESSED QUERYSTRING: %s', public_id, params)
        validator = Validator()
        apikey = validator.get_client_apikey(params.get('id'))
        client_signature = params.pop('h')
        server_signature = sign(params, apikey)
        if client_signature != server_signature:
            logger.error('Client hmac=%s != Server hmac=%s',
                         client_signature, server_signature)
            raise YKValError('BAD_SIGNATURE')
        for old_key, new_key in PARAM_MAP.items():
            if old_key in params:
                params[new_key] = params[old_key]
                params.pop(old_key)
        extra = validator.verify(**params)
        output = 'OK'
    except YKValError as err:
        logger.exception('%s: Validation error: %s', public_id, err)
        output = '%s' % err
    except Exception as err:
        logger.exception('%s: Backend error: %s', public_id, err)
        output = 'BACKEND_ERROR'
    finally:
        logger.info('%s: Verified', public_id)
        return wsgi_response(output, start_response, apikey=apikey, extra=None)

def sync(environ, start_response):
    """
    Handle Sync requests
    """
    local_params = None
    try:
        # Validate caller address
        logger.debug('Received request from %(REMOTE_ADDR)s', environ)
        if environ['REMOTE_ADDR'] not in settings['SYNC_POOL']:
            logger.info('Operation not permitted from IP %(REMOTE_ADDR)s', environ)
            logger.debug('Remote IP %s is not in allowed sync pool: %s',
                         environ['REMOTE_ADDR'], settings['SYNC_POOL'])
            raise YKSyncError('OPERATION_NOT_ALLOWED',
                              'Remote IP %(REMOTE_ADDR)s it not in sync pool' % environ)
        sync_params = parse_querystring(environ['QUERY_STRING'])
        logger.info('Received: %s', sync_params)
        synclib = Sync()
        local_params = synclib.sync_local(sync_params)
        output = 'OK'
        status_code = 200
    except YKSyncError as err:
        output = str(err)
        status_code = 401
    except Exception as err:
        logger.exception('ERROR: %s', err)
        output = 'BACKEND_ERROR'
        status_code = 500
    finally:
        return wsgi_response(output, start_response, apikey=''.encode(),
                             extra=local_params, status=status_code)

def resync(environ, start_response):
    """
    Handle Re-sync requests
    """
    try:
        # Validate caller address
        if environ['REMOTE_ADDR'] not in settings['SYNC_POOL']:
            logger.info('Operation not permitted from IP %(REMOTE_ADDR)s', environ)
            raise YKSyncError('ERROR Authorization failed for %(REMOTE_ADDR)s)' % environ)
        # Parse query and check values
        resync_params = parse_querystring(environ['QUERY_STRING'])
        synclib = Sync()
        output = synclib.resync_local(resync_params)
        status_code = 200
    except YKSyncError as err:
        output = str(err)
        status_code = 401
    except Exception as err:
        logger.exception('ERROR: %s', err)
        output = ''
        status_code = 500
    finally:
        start_response(HTTP_STATUS_CODES[status_code], [('Content-Type', 'text/plain')])
        return [output.encode()]

def router(environ, start_response):
    """ Simple WSGI router """
    path = environ.get('PATH_INFO', '')
    match = re.match(URI_REGEX, path)
    if not match or not match.groupdict()['resource']:
        start_response(HTTP_STATUS_CODES[404], [('Content-Type', 'text/plain')])
        return ['Invalid URI'.encode()]
    func = globals()[match.groupdict()['resource']]
    return func(environ, start_response)

def main():
    """ Run a web server to test the application """
    from wsgiref.simple_server import make_server
    logging.basicConfig(format='%(asctime)s %(levelname)s %(module)s %(funcName)s: %(message)s',
                        level=logging.DEBUG)
    srv = make_server('0.0.0.0', 8080, router)
    srv.serve_forever()

if __name__ == '__main__':
    main()
