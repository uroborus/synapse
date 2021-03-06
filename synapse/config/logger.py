# -*- coding: utf-8 -*-
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

from ._base import Config

from twisted.python.log import PythonLoggingObserver
import logging
import logging.config

class LoggingConfig(Config):
    def __init__(self, args):
        super(LoggingConfig, self).__init__(args)
        self.verbosity = int(args.verbose) if args.verbose else None
        self.log_config = self.abspath(args.log_config)
        self.log_file = self.abspath(args.log_file)

    @classmethod
    def add_arguments(cls, parser):
        super(LoggingConfig, cls).add_arguments(parser)
        logging_group = parser.add_argument_group("logging")
        logging_group.add_argument(
            '-v', '--verbose', dest="verbose", action='count',
            help="The verbosity level."
        )
        logging_group.add_argument(
            '-f', '--log-file', dest="log_file", default=None,
            help="File to log to."
        )
        logging_group.add_argument(
            '--log-config', dest="log_config", default=None,
            help="Python logging config file"
        )

    def setup_logging(self):
        log_format = (
            '%(asctime)s - %(name)s - %(lineno)d - %(levelname)s - %(message)s'
        )
        if self.log_config is None:

            level = logging.INFO
            if self.verbosity:
               level = logging.DEBUG

               # FIXME: we need a logging.WARN for a -q quiet option

            logging.basicConfig(
                level=level,
                filename=self.log_file,
                format=log_format
            )
        else:
            logging.config.fileConfig(self.log_config)

        observer = PythonLoggingObserver()
        observer.start()
