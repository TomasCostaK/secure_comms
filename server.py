import asyncio
import json
import base64
import argparse
import coloredlogs, logging
import re
import os
from aio_tcpserver import tcp_server

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding, ParameterFormat, PublicFormat, load_pem_public_key
from cryptography.hazmat.primitives.asymmetric import dh
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger('root')

STATE_CONNECT = 0
STATE_OPEN = 1
STATE_DATA = 2
STATE_CLOSE= 3

#GLOBAL
storage_dir = 'files'

class ClientHandler(asyncio.Protocol):
    def __init__(self, signal):
        """
        Default constructor
        """
        self.signal = signal
        self.state = 0
        self.file = None
        self.file_name = None
        self.file_path = None
        self.storage_dir = storage_dir
        self.buffer = ''
        self.peername = ''
        self.key = None

        self.parameters = None
        self.private_key = None
        #Arrays of possible ciphers to take from
        self.ciphers = ['AES','3DES','Salsa20']
        self.modes = ['CBC','GCM','EBC']
        self.sinteses = ['SHA-256','SHA-384','SHA-512']

        #Chosen cipher by server
        self.cipher = None
        self.mode = None
        self.hash_function = None

    def connection_made(self, transport) -> None:
        """
        Called when a client connects

        :param transport: The transport stream to use with this client
        :return:
        """
        self.peername = transport.get_extra_info('peername')
        logger.info('\n\nConnection from {}'.format(self.peername))

        self.transport = transport
        self.state = STATE_CONNECT

        if not self.parameters:
            self.diffie_hellman_init()
        if not self.private_key:
            self.diffie_hellman_gen_Y()

    def data_received(self, data: bytes) -> None:
        """
        Called when data is received from the client.
        Stores the data in the buffer

        :param data: The data that was received. This may not be a complete JSON message
        :return:
        """
        logger.debug('Received: {}'.format(data))
        try:
            self.buffer += data.decode()
        except:
            logger.exception('Could not decode data from client')

        idx = self.buffer.find('\r\n')

        while idx >= 0:  # While there are separators
            frame = self.buffer[:idx + 2].strip()  # Extract the JSON object
            self.buffer = self.buffer[idx + 2:]  # Removes the JSON object from the buffer

            self.on_frame(frame)  # Process the frame
            idx = self.buffer.find('\r\n')

        if len(self.buffer) > 4096 * 1024 * 1024:  # If buffer is larger than 4M
            logger.warning('Buffer to large')
            self.buffer = ''
            self.transport.close()


    def on_frame(self, frame: str) -> None:
        """
        Called when a frame (JSON Object) is extracted

        :param frame: The JSON object to process
        :return:
        """
        #logger.debug("Frame: {}".format(frame))

        try:
            message = json.loads(frame)
        except:
            logger.exception("Could not decode JSON message: {}".format(frame))
            self.transport.close()
            return

        mtype = message.get('type', "").upper()


        if mtype == 'DH_KEY_EXCHANGE':
            ret = self.get_key(message.get('data').get('pub_key'))
        elif mtype == 'OPEN':
            ret = self.process_open(message)
        elif mtype == 'DATA':
            ret = self.process_data(message)
        elif mtype == 'CLOSE':
            ret = self.process_close(message)
        elif mtype == 'NEGOTIATE':
            ret = self.process_negotiate(message)
        else:
            logger.warning("Invalid message type: {}".format(message['type']))
            ret = False
        if not ret:
            try:
                self._send({'type': 'ERROR', 'message': 'See server'})
            except:
                pass # Silently ignore

            logger.info("Closing transport")
            if self.file is not None:
                self.file.close()
                self.file = None

            self.state = STATE_CLOSE
            self.transport.close()


    def process_open(self, message: str) -> bool:
        """
        Processes an OPEN message from the client
        This message should contain the filename

        :param message: The message to process
        :return: Boolean indicating the success of the operation
        """
        logger.debug("Process Open: {}".format(message))

        if self.state != STATE_CONNECT:
            logger.warning("Invalid state. Discarding")
            return False

        if not 'file_name' in message:
            logger.warning("No filename in Open")
            return False

        # Only chars and letters in the filename
        file_name = re.sub(r'[^\w\.]', '', message['file_name'])
        file_path = os.path.join(self.storage_dir, file_name)
        if not os.path.exists("files"):
            try:
                os.mkdir("files")
            except:
                logger.exception("Unable to create storage directory")
                return False

        try:
            self.file = open(file_path, "wb")
            logger.info("File open")
        except Exception:
            logger.exception("Unable to open file")
            return False

        self._send({'type': 'OK'})

        self.file_name = file_name
        self.file_path = file_path
        self.state = STATE_OPEN
        return True


    def process_data(self, message: str) -> bool:
        """
        Processes a DATA message from the client
        This message should contain a chunk of the file

        :param message: The message to process
        :return: Boolean indicating the success of the operation
        """
        logger.debug("Process Data: {}".format(message))

        if self.state == STATE_OPEN:
            self.state = STATE_DATA
            # First Packet

        elif self.state == STATE_DATA:
            # Next packets
            pass

        else:
            logger.warning("Invalid state. Discarding")
            return False

        try:
            data = message.get('data', None)
            if data is None:
                    logger.debug("Invalid message. No data found")
                    return False

            bdata = base64.b64decode(message['data'])
        except:
            logger.exception("Could not decode base64 content from message.data")
            return False

        try:
            self.file.write(bdata)
            self.file.flush()
        except:
            logger.exception("Could not write to file")
            return False

        return True


    def process_close(self, message: str) -> bool:
        """
        Processes a CLOSE message from the client.
        This message will trigger the termination of this session

        :param message: The message to process
        :return: Boolean indicating the success of the operation
        """
        logger.debug("Process Close: {}".format(message))

        self.transport.close()
        if self.file is not None:
            self.file.close()
            self.file = None

        self.state = STATE_CLOSE

        return True


    def _send(self, message: str, dump=True) -> None:
        """
        Effectively encodes and sends a message
        :param message:
        :return:
        """
        logger.debug("Send: {}".format(message))

        message_b = (json.dumps(message) + '\r\n').encode()
        self.transport.write(message_b)

    def diffie_hellman_init(self):
        logger.debug("Init Diffie-Hellman")
        self.parameters = dh.generate_parameters(generator=2, key_size=1024,
                                            backend=default_backend())
        parameter_numbers = self.parameters.parameter_numbers()
        # msg = { 'type' : 'DH_INIT', 'data' : { 'params' : parameters.parameter_bytes(Encoding.PEM, ParameterFormat.PKCS3).decode() }}
        msg = { 'type' : 'DH_INIT', 'data' : { 'p' : parameter_numbers.p, 'g' : parameter_numbers.g }}
        self._send(msg)
        return True

    def diffie_hellman_gen_Y(self):
        logger.debug("Starting Diffie-Hellman key exchange.")

        # Generate a private key for use in the exchange.
        self.private_key = self.parameters.generate_private_key()
        self.public_key = self.private_key.public_key()

        msg = { 'type' : 'DH_KEY_EXCHANGE', 
                'data' : { 
                    'pub_key' : self.public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
                    }
                }
        self._send(msg)

    def get_key(self, client_pub_key_b):
        # q = DIFFIE_HELLMAN_AGREED_PRIME
        # self.key = (Y**self.a) % q

        logger.debug("Getting shared key")

        client_pub_key = load_pem_public_key(client_pub_key_b.encode(), default_backend())

        shared_key = self.private_key.exchange(client_pub_key)

        # Perform key derivation.
        derived_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b'handshake data',
            backend=default_backend()
        ).derive(shared_key)

        self.key = derived_key

        logger.debug(f'Got key {self.key}')
        return True

    def process_negotiate(self, message: str) -> bool:
        """
        Processes a NEGOTIATE message from the client
        This message should contain the filename

        :param message: The message to process
        :return: Boolean indicating the success of the operation
        """
        logger.debug("Process Open: {}".format(message))

        if self.state != STATE_CONNECT:
            logger.warning("Invalid state. Discarding")
            return False

        #verificar esta condiçao pq podemos ter cifras que nao precisem dos modos ou assim
        if (not 'ciphers' in message 
                or not 'modes' in message 
                or not 'sinteses' in message
            ):
                logger.warning("Negotiation impossible, ciphers or modes not allowed or inexistent.")
                return False

        #Aqui fazer uma escolha hardcoded por ordem de melhor para pior cifra a usar
        logger.info("Cipher chosen from message: %s" % (message))

        ret = self.choose_algo(message.get('ciphers'), 
                message.get('modes'),
                message.get('sinteses'))

        self._send(
                {
                    'type': 'CIPHER_CHOSEN', 
                    'cipher': self.cipher, 
                    'mode': self.mode, 
                    'sintese': self.hash_function
                }
            )

        return ret

    def choose_algo(self, ciphers, modes, hash_functions):
        # choose cipher
        if 'ChaCha20' in ciphers:
            self.cipher =  'ChaCha20'
        elif 'AES' in ciphers:
            self.cipher = 'AES'
        else:
            logger.error("Algo not supported")
            return False

        # choose mode
        if 'GCM' in modes:
            self.mode = 'GCM'
        elif 'CBC' in modes:
            self.mode = 'CBC'
        else:
            logger.error("Algo not supported")
            return False

        # choose hash_function
        if 'SHA-512' in hash_functions:
            self.hash_function = 'SHA-512'
        elif 'SHA-256' in hash_functions:
            self.hash_function = 'SHA-256'
        else:
            logger.error("Algo not supported")
            return False

        return True


def main():
    global storage_dir

    parser = argparse.ArgumentParser(description='Receives files from clients.')
    parser.add_argument('-v', action='count', dest='verbose',
                                            help='Shows debug messages (default=False)',
                                            default=0)
    parser.add_argument('-p', type=int, nargs=1,
                                            dest='port', default=5000,
                                            help='TCP Port to use (default=5000)')

    parser.add_argument('-d', type=str, required=False, dest='storage_dir',
                                            default='files',
                                            help='Where to store files (default=./files)')

    args = parser.parse_args()
    storage_dir = os.path.abspath(args.storage_dir)
    level = logging.DEBUG if args.verbose > 0 else logging.INFO
    port = args.port
    if port <= 0 or port > 65535:
            logger.error("Invalid port")
            return

    if port < 1024 and not os.geteuid() == 0:
            logger.error("Ports below 1024 require eUID=0 (root)")
            return

    coloredlogs.install(level)
    logger.setLevel(level)

    logger.info("Port: {} LogLevel: {} Storage: {}".format(port, level, storage_dir))
    tcp_server(ClientHandler, worker=2, port=port, reuse_port=True)


if __name__ == '__main__':
    main()


