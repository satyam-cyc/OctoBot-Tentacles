#  Drakkar-Software OctoBot-Tentacles
#  Copyright (c) Drakkar-Software, All rights reserved.
#
#  This library is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 3.0 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library.
import logging
import os
import time
import flask
from flask import request, abort
from threading import Thread
from gevent.pywsgi import WSGIServer
from pyngrok import ngrok
from pyngrok.exception import PyngrokNgrokError

from octobot_commons.logging.logging_util import set_logging_level
from octobot_services.constants import CONFIG_WEBHOOK, CONFIG_CATEGORY_SERVICES, CONFIG_SERVICE_INSTANCE, \
    CONFIG_NGROK_TOKEN, ENV_WEBHOOK_ADDRESS, CONFIG_WEB_IP, ENV_WEBHOOK_PORT, CONFIG_WEB_PORT, \
    DEFAULT_WEBHOOK_SERVER_PORT, DEFAULT_WEBHOOK_SERVER_IP
from octobot_services.services.abstract_service import AbstractService


class WebHookService(AbstractService):
    CONNECTION_TIMEOUT = 3
    LOGGERS = ["pyngrok.ngrok", "werkzeug"]

    def get_fields_description(self):
        return {
            CONFIG_NGROK_TOKEN: "The ngrok token used to expose the webhook to the internet."
        }

    def get_default_value(self):
        return {
            CONFIG_NGROK_TOKEN: ""
        }

    def __init__(self):
        super().__init__()
        self.ngrok_public_url = ""
        self.webhook_public_url = ""

        self.service_feed_webhooks = {}
        self.service_feed_auth_callbacks = {}

        self.webhook_app = None
        self.webhook_host = None
        self.webhook_port = None
        self.webhook_server = None
        self.webhook_server_context = None
        self.webhook_server_thread = None
        self.connected = None

    @staticmethod
    def is_setup_correctly(config):
        return CONFIG_WEBHOOK in config[CONFIG_CATEGORY_SERVICES] \
            and CONFIG_SERVICE_INSTANCE in config[CONFIG_CATEGORY_SERVICES][CONFIG_WEBHOOK]

    @staticmethod
    def get_is_enabled(config):
        return True

    def has_required_configuration(self):
        return CONFIG_CATEGORY_SERVICES in self.config \
            and CONFIG_WEBHOOK in self.config[CONFIG_CATEGORY_SERVICES] \
            and self.check_required_config(self.config[CONFIG_CATEGORY_SERVICES][CONFIG_WEBHOOK])

    def get_required_config(self):
        return [CONFIG_NGROK_TOKEN]

    @classmethod
    def get_help_page(cls) -> str:
        return "https://github.com/Drakkar-Software/OctoBot/wiki/TradingView-webhook"

    def get_endpoint(self) -> None:
        return ngrok

    def get_type(self) -> None:
        return CONFIG_WEBHOOK

    def is_subscribed(self, feed_name):
        return feed_name in self.service_feed_webhooks

    @staticmethod
    def connect(port, protocol="http") -> str:
        """
        Create a new ngrok tunnel
        :param port: the tunnel local port
        :param protocol: the protocol to use
        :return: the ngrok url
        """
        return ngrok.connect(port, protocol)

    def subscribe_feed(self, service_feed_name, service_feed_callback, auth_callback) -> None:
        """
        Subscribe a service feed to the webhook
        :param service_feed_name: the service feed name
        :param service_feed_callback: the service feed callback reference
        :return: the service feed webhook url
        """
        if service_feed_name not in self.service_feed_webhooks:
            self.service_feed_webhooks[service_feed_name] = service_feed_callback
            self.service_feed_auth_callbacks[service_feed_name] = auth_callback
            return
        raise KeyError(f"Service feed has already subscribed to a webhook : {service_feed_name}")

    def get_subscribe_url(self, service_feed_name):
        return f"{self.webhook_public_url}/{service_feed_name}"

    def _prepare_webhook_server(self):
        try:
            self.webhook_server = WSGIServer((self.webhook_host, self.webhook_port),
                                             self.webhook_app,
                                             log=None)
            self.webhook_server_context = self.webhook_app.app_context()
            self.webhook_server_context.push()
        except OSError as e:
            self.webhook_server = None
            self.get_logger().exception(f"Fail to start webhook : {e}")

    def _load_webhook_routes(self) -> None:
        @self.webhook_app.route('/')
        def index():
            """
            Route to check if webhook server is online
            """
            return ''

        @self.webhook_app.route('/webhook/<webhook_name>', methods=['POST'])
        def webhook(webhook_name):
            if webhook_name in self.service_feed_webhooks:
                if request.method == 'POST':
                    data = request.get_data(as_text=True)
                    if self.service_feed_auth_callbacks[webhook_name](data):
                        self.service_feed_webhooks[webhook_name](data)
                    else:
                        self.logger.debug(f"Ignored feed (wrong token): {data}")
                    return '', 200
                abort(400)
            else:
                self.logger.warning(f"Received unknown request from {webhook_name}")
                abort(500)

    async def prepare(self) -> None:
        set_logging_level(self.LOGGERS, logging.WARNING)
        ngrok.set_auth_token(self.config[CONFIG_CATEGORY_SERVICES][CONFIG_WEBHOOK][CONFIG_NGROK_TOKEN])
        try:
            self.webhook_host = os.getenv(ENV_WEBHOOK_ADDRESS, self.config[CONFIG_CATEGORY_SERVICES]
                                          [CONFIG_WEBHOOK][CONFIG_WEB_IP])
        except KeyError:
            self.webhook_host = os.getenv(ENV_WEBHOOK_ADDRESS, DEFAULT_WEBHOOK_SERVER_IP)
        try:
            self.webhook_port = int(os.getenv(ENV_WEBHOOK_PORT, self.config[CONFIG_CATEGORY_SERVICES]
                                    [CONFIG_WEBHOOK][CONFIG_WEB_PORT]))
        except KeyError:
            self.webhook_port = int(os.getenv(ENV_WEBHOOK_PORT, DEFAULT_WEBHOOK_SERVER_PORT))

    def _start_server(self):
        try:
            self._prepare_webhook_server()
            self._load_webhook_routes()
            self.ngrok_public_url = self.connect(self.webhook_port, protocol="http")
            self.webhook_public_url = f"{self.ngrok_public_url}/webhook"
            if self.webhook_server:
                self.connected = True
                self.webhook_server.serve_forever()
        except PyngrokNgrokError as e:
            self.logger.error(f"Error when starting webhook service: Your ngrock.com token might be invalid. ({e})")
        except Exception as e:
            self.logger.exception(e, True, f"Error when running webhook service: ({e})")
        self.connected = False

    def start_webhooks(self) -> bool:
        if self.webhook_app is None:
            self.webhook_app = flask.Flask(__name__)
            # gevent WSGI server has to be created in the thread it is started: create everything in this thread
            self.webhook_server_thread = Thread(target=self._start_server, name=self.get_name())
            self.webhook_server_thread.start()
            start_time = time.time()
            timeout = False
            while self.connected is None and not timeout:
                time.sleep(0.01)
                timeout = time.time() - start_time > self.CONNECTION_TIMEOUT
            if timeout:
                self.logger.error("Webhook took too long to start, now stopping it.")
                self.stop()
                self.connected = False
            return self.connected is True
        return True

    def _is_healthy(self):
        return self.webhook_host is not None and self.webhook_port is not None

    def get_successful_startup_message(self):
        return f"Webhook configured on address: {self.webhook_host} and port: {self.webhook_port}", self._is_healthy()

    def stop(self):
        ngrok.kill()
        if self.webhook_server:
            self.webhook_server.stop()