# -*- coding: utf-8 -*-

import httplib
from flask import Flask, make_response, url_for
from flask_restful import Api, abort
from webargs.flaskparser import parser

from raiden.api.v1.encoding import (
    EventsListSchema,
    ChannelSchema,
    ChannelListSchema,
    TokensListSchema,
    PartnersPerTokenListSchema,
    HexAddressConverter,
)
from raiden.api.v1.resources import (
    create_blueprint,
    ChannelsResource,
    ChannelsResourceByChannelAddress,
    TokensResource,
    PartnersResourceByTokenAddress,
)
from raiden.api.objects import EventsList, ChannelList, TokensList, PartnersPerTokenList


class APIServer(object):
    """
    Runs the API-server that routes the endpoint to the resources.
    The API is wrapped in multiple layers, and the Server should be invoked this way:

    ```
    # instance of the raiden-api
    raiden_api = RaidenAPI(...)

    # wrap the raiden-api with rest-logic and encoding
    rest_api = RestAPI(raiden_api)

    # create the server and link the api-endpoints with flask / flask-restful middleware
    api_server = APIServer(rest_api)

    # run the server
    api_server.run(5001, debug=True)
    ```
    """

    # flask TypeConverter
    # links argument-placeholder in route (e.g. '/<hexaddress: channel_address>') to the Converter
    _type_converter_mapping = {
        'hexaddress': HexAddressConverter
    }

    def __init__(self, rest_api):
        self.rest_api = rest_api
        self.blueprint = create_blueprint()
        if self.rest_api.version == 1:
            self.flask_api_context = Api(
                self.blueprint,
                prefix="/api/1",
            )
        else:
            raise ValueError('Invalid api version: {}'.format(self.rest_api.version))

        self.flask_app = Flask(__name__)
        self._add_default_resources()
        self._register_type_converters()
        self.flask_app.register_blueprint(self.blueprint)

    def _add_default_resources(self):
        self.add_resource(ChannelsResource, '/channels')
        self.add_resource(
            ChannelsResourceByChannelAddress,
            '/channels/<hexaddress:channel_address>'
        )
        self.add_resource(TokensResource, '/tokens')
        self.add_resource(
            PartnersResourceByTokenAddress,
            '/tokens/<hexaddress:token_address>/partners'
        )

    def _register_type_converters(self, additional_mapping=None):
        # an additional mapping concats to class-mapping and will overwrite existing keys
        if additional_mapping:
            mapping = dict(self._type_converter_mapping, **additional_mapping)
        else:
            mapping = self._type_converter_mapping

        for key, value in mapping.items():
            self.flask_app.url_map.converters[key] = value

    def add_resource(self, resource_cls, route):
        self.flask_api_context.add_resource(
            resource_cls,
            route,
            resource_class_kwargs={'rest_api_object': self.rest_api}
        )

    def run(self, port, **kwargs):
        if 'host' in kwargs:
            raise ValueError('The server host is hardcoded, can\'t set it')
        self.flask_app.run(port=port, host='localhost', **kwargs)


class RestAPI(object):
    """
    This wraps around the actual RaidenAPI in raiden_service.
    It will provide the additional, neccessary RESTful logic and
    the proper JSON-encoding of the Objects provided by the RaidenAPI
    """
    version = 1

    def __init__(self, raiden_api):
        self.raiden_api = raiden_api
        self.channel_schema = ChannelSchema()
        self.channel_list_schema = ChannelListSchema()
        self.events_list_schema = EventsListSchema()
        self.tokens_list_schema = TokensListSchema()
        self.partner_per_token_list_schema = PartnersPerTokenListSchema()

    def open(self, partner_address, token_address, settle_timeout, balance=None):
        raiden_service_result = self.raiden_api.open(
            token_address,
            partner_address,
            settle_timeout
        )

        if balance:
            # make initial deposit
            raiden_service_result = self.raiden_api.deposit(
                token_address,
                partner_address,
                balance
            )

        result = self.channel_schema.dumps(raiden_service_result)
        return result

    def deposit(self, token_address, partner_address, amount):

        raiden_service_result = self.raiden_api.deposit(
            token_address,
            partner_address,
            amount
        )

        result = self.channel_schema.dumps(raiden_service_result)
        return result

    def close(self, token_address, partner_address):

        raiden_service_result = self.raiden_api.close(
            token_address,
            partner_address
        )

        result = self.channel_schema.dumps(raiden_service_result)
        return result

    def get_channel_list(self, token_address=None, partner_address=None):
        raiden_service_result = self.raiden_api.get_channel_list(token_address, partner_address)
        assert isinstance(raiden_service_result, list)

        channel_list = ChannelList(raiden_service_result)
        result = self.channel_list_schema.dumps(channel_list)
        return result

    def get_tokens_list(self):
        raiden_service_result = self.raiden_api.get_tokens_list()
        assert isinstance(raiden_service_result, list)

        new_list = []
        for result in raiden_service_result:
            new_list.append({'address': result})

        tokens_list = TokensList(new_list)
        result = self.tokens_list_schema.dumps(tokens_list)
        return result

    def get_new_events(self):
        raiden_service_result = self.get_new_events()
        assert isinstance(raiden_service_result, list)

        events_list = EventsList(raiden_service_result)
        result = self.events_list_schema.dumps(events_list)
        return result

    def get_channel(self, channel_address):
        channel = self.raiden_api.get_channel(channel_address)
        return self.channel_schema.dumps(channel)

    def get_partners_by_token(self, token_address):
        return_list = []
        raiden_service_result = self.raiden_api.get_channel_list(token_address)
        for result in raiden_service_result:
            return_list.append({
                'partner_address': result.partner_address,
                'channel': url_for(
                    # TODO: Somehow nicely parameterize this for future versions
                    'v1_resources.channelsresourcebychanneladdress',
                    channel_address=result.channel_address
                ),
            })

        schema_list = PartnersPerTokenList(return_list)
        result = self.partner_per_token_list_schema.dumps(schema_list)
        return result

    def patch_channel(self, channel_address, balance=None, state=None):
        if balance is not None and state is not None:
            return make_response(
                'Can not update balance and change channel state at the same time',
                httplib.CONFLICT,
            )
        elif balance is None and state is None:
            return make_response(
                'Nothing to do. Should either provide \'balance\' or \'state\' argument',
                httplib.BAD_REQUEST,
            )

        # find the channel
        channel = self.raiden_api.get_channel(channel_address)
        current_state = channel.state
        # if we patch with `balance` it's a deposit
        if balance is not None:
            if current_state != 'open':
                return make_response(
                    'Can\'t deposit on a closed channel',
                    httplib.CONFLICT,
                )
            raiden_service_result = self.raiden_api.deposit(
                channel.token_address,
                channel.partner_address,
                balance
            )
            return self.channel_schema.dumps(raiden_service_result)

        else:
            if state == 'closed':
                if current_state != 'open':
                    return make_response(
                        httplib.CONFLICT,
                        'Attempted to close an already closed channel'
                    )
                raiden_service_result = self.raiden_api.close(
                    channel.token_address,
                    channel.partner_address
                )
                return self.channel_schema.dumps(raiden_service_result)
            elif state == 'settled':
                if current_state == 'settled' or current_state == 'open':
                    return make_response(
                        'Attempted to settle a channel at its {} state'.format(current_state),
                        httplib.CONFLICT,
                    )
                raiden_service_result = self.raiden_api.settle(
                    channel.token_address,
                    channel.partner_address
                )
                return self.channel_schema.dumps(raiden_service_result)
            else:
                return make_response(
                    'Provided invalid channel state {}'.format(state),
                    httplib.BAD_REQUEST,
                )


@parser.error_handler
def handle_request_parsing_error(err):
    """ This handles request parsing errors generated for example by schema
    field validation failing."""
    abort(httplib.BAD_REQUEST, errors=err.messages)
