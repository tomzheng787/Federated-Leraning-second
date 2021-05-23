"""
The base class for all federated learning clients on edge devices or edge servers.
"""

import asyncio
import logging
import random
import os
import pickle
import sys
from multiprocessing import Process

from abc import abstractmethod
from dataclasses import dataclass
from typing import List

import websockets
from plato.config import Config


@dataclass
class Report:
    """Client report, to be sent to the federated learning server."""
    num_samples: int
    accuracy: float


class Client:
    """ A basic federated learning client. """
    def __init__(self) -> None:
        self.client_id = Config().args.id
        self.data_loaded = False  # is training data already loaded from the disk?

        if hasattr(Config().algorithm,
                   'cross_silo') and not Config().is_edge_server():
            # Contact one of the edge servers
            self.edge_server_id = int(Config().clients.total_clients) + (
                self.client_id - 1) % int(Config().algorithm.total_silos) + 1

            assert hasattr(Config().algorithm, 'total_silos')

            self.server_port = Config().server.port + self.edge_server_id
        else:
            self.server_port = Config().server.port

    @staticmethod
    async def heartbeat(client_id):
        """ Sending client heartbeats. """
        try:
            uri = 'ws://{}:{}'.format(Config().server.address,
                                      Config().server.port)

            while True:
                async with websockets.connect(uri) as websocket:
                    logging.info(
                        "[Client #%d] Sending a heartbeat to the server.",
                        client_id)
                    await websocket.send(pickle.dumps({'id': client_id}))

                await websocket.close()

                heartbeat_max_interval = Config(
                ).clients.heartbeat_max_interval if hasattr(
                    Config().server, 'heartbeat_max_interval') else 60
                await asyncio.sleep(heartbeat_max_interval * random.random())

        except OSError as exception:
            logging.info(
                "[Client #%d] Connection to the server failed while sending heartbeats.",
                client_id)
            logging.error(exception)

    @staticmethod
    def heartbeat_process(client_id):
        """ Starting an asyncio loop for sending client heartbeats. """
        asyncio.run(Client.heartbeat(client_id))

    async def start_client(self) -> None:
        """ Startup function for a client. """

        if hasattr(Config().algorithm,
                   'cross_silo') and not Config().is_edge_server():
            # Contact one of the edge servers
            logging.info("[Client #%d] Contacting Edge server #%d.",
                         self.client_id, self.edge_server_id)
        else:
            logging.info("[Client #%d] Contacting the central server.",
                         self.client_id)
        uri = 'ws://{}:{}'.format(Config().server.address, self.server_port)

        try:
            async with websockets.connect(uri,
                                          ping_interval=None,
                                          max_size=2**30) as websocket:
                logging.info("[Client #%d] Signing in at the server.",
                             self.client_id)

                await websocket.send(pickle.dumps({'id': self.client_id}))

                while True:
                    logging.info("[Client #%d] Waiting to be selected.",
                                 self.client_id)
                    server_response = await websocket.recv()
                    data = pickle.loads(server_response)

                    if data['id'] == self.client_id:
                        self.process_server_response(data)
                        logging.info("[Client #%d] Selected by the server.",
                                     self.client_id)

                        if not self.data_loaded:
                            self.load_data()

                        if 'payload' in data:
                            server_payload = await self.recv(
                                self.client_id, data, websocket)
                            self.load_payload(server_payload)

                        heartbeat_proc = Process(
                            target=Client.heartbeat_process,
                            args=(self.client_id, ))
                        heartbeat_proc.start()
                        report, payload = await self.train()
                        heartbeat_proc.terminate()

                        if Config().is_edge_server():
                            logging.info(
                                "[Server #%d] Model aggregated on edge server (client #%d).",
                                os.getpid(), self.client_id)
                        else:
                            logging.info("[Client #%d] Model trained.",
                                         self.client_id)

                        # Sending the client report as metadata to the server (payload to follow)
                        client_report = {
                            'id': self.client_id,
                            'report': report,
                            'payload': True
                        }
                        await websocket.send(pickle.dumps(client_report))

                        # Sending the client training payload to the server
                        await self.send(websocket, payload)

        except OSError as exception:
            logging.info("[Client #%d] Connection to the server failed.",
                         self.client_id)
            logging.error(exception)

    async def recv(self, client_id, data, websocket) -> List:
        """Receiving the payload from the server using WebSockets."""

        logging.info("[Client #%d] Receiving payload data from the server.",
                     client_id)

        if 'payload_length' in data:
            server_payload = []
            payload_size = 0

            for __ in range(0, data['payload_length']):
                _data = await websocket.recv()
                payload = pickle.loads(_data)
                server_payload.append(payload)
                payload_size += sys.getsizeof(_data)
        else:
            _data = await websocket.recv()
            server_payload = pickle.loads(_data)
            payload_size = sys.getsizeof(_data)

        logging.info(
            "[Client #%d] Received %s MB of payload data from the server.",
            client_id, round(payload_size / 1024**2, 2))

        return server_payload

    async def send(self, websocket, payload) -> None:
        """Sending the client payload to the server using WebSockets."""
        if isinstance(payload, list):
            data_size: int = 0

            for data in payload:
                _data = pickle.dumps(data)
                await websocket.send(_data)
                data_size += sys.getsizeof(_data)
        else:
            _data = pickle.dumps(payload)
            await websocket.send(_data)
            data_size = sys.getsizeof(_data)

        logging.info("[Client #%d] Sent %s MB of payload data to the server.",
                     self.client_id, round(data_size / 1024**2, 2))

    def process_server_response(self, server_response):
        """Additional client-specific processing on the server response."""

    @abstractmethod
    def configure(self) -> None:
        """Prepare this client for training."""

    @abstractmethod
    def load_data(self) -> None:
        """Generating data and loading them onto this client."""

    @abstractmethod
    def load_payload(self, server_payload) -> None:
        """Loading the payload onto this client."""

    @abstractmethod
    async def train(self):
        """The machine learning training workload on a client."""
