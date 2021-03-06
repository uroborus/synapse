# Copyright 2014 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from twisted.internet import ssl
from OpenSSL import SSL
from twisted.internet._sslverify import _OpenSSLECCurve, _defaultCurveName


class ServerContextFactory(ssl.ContextFactory):
    """Factory for PyOpenSSL SSL contexts that are used to handle incoming
    connections and to make connections to remote servers."""

    def __init__(self, config):
        self._context = SSL.Context(SSL.SSLv23_METHOD)
        self.configure_context(self._context, config)

    @staticmethod
    def configure_context(context, config):
        try:
            _ecCurve = _OpenSSLECCurve(_defaultCurveName)
            _ecCurve.addECKeyToContext(context)
        except:
            pass
        context.set_options(SSL.OP_NO_SSLv2 | SSL.OP_NO_SSLv3)
        context.use_certificate(config.tls_certificate)
        context.use_privatekey(config.tls_private_key)
        context.load_tmp_dh(config.tls_dh_params_path)
        context.set_cipher_list("!ADH:HIGH+kEDH:!AECDH:HIGH+kEECDH")

    def getContext(self):
        return self._context

