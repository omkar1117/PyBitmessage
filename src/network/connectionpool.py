import errno
import re
import socket
import time

import asyncore_pollchoose as asyncore
import helper_bootstrap
import helper_random
import knownnodes
import protocol
import state
from bmconfigparser import BMConfigParser
from connectionchooser import chooseConnection
from debug import logger
from proxy import Proxy
from singleton import Singleton
from tcp import (
    TCPServer, Socks5BMConnection, Socks4aBMConnection, TCPConnection)
from udp import UDPSocket


@Singleton
class BMConnectionPool(object):
    """Pool of all existing connections"""
    def __init__(self):
        asyncore.set_rates(
            BMConfigParser().safeGetInt(
                "bitmessagesettings", "maxdownloadrate"),
            BMConfigParser().safeGetInt(
                "bitmessagesettings", "maxuploadrate")
        )
        self.outboundConnections = {}
        self.inboundConnections = {}
        self.listeningSockets = {}
        self.udpSockets = {}
        self.streams = []
        self.lastSpawned = 0
        self.spawnWait = 2
        self.bootstrapped = False

    def connectToStream(self, streamNumber):
        """Connect to a bitmessage stream"""
        self.streams.append(streamNumber)

    def getConnectionByAddr(self, addr):
        """
        Return an (existing) connection object based on a `Peer` object
        (IP and port)
        """
        try:
            return self.inboundConnections[addr]
        except KeyError:
            pass
        try:
            return self.inboundConnections[addr.host]
        except (KeyError, AttributeError):
            pass
        try:
            return self.outboundConnections[addr]
        except KeyError:
            pass
        try:
            return self.udpSockets[addr.host]
        except (KeyError, AttributeError):
            pass
        raise KeyError

    def isAlreadyConnected(self, nodeid):
        """Check if we're already connected to this peer"""
        for i in (
            self.inboundConnections.values() +
            self.outboundConnections.values()
        ):
            try:
                if nodeid == i.nodeid:
                    return True
            except AttributeError:
                pass
        return False

    def addConnection(self, connection):
        """Add a connection object to our internal dict"""
        if isinstance(connection, UDPSocket):
            return
        if connection.isOutbound:
            self.outboundConnections[connection.destination] = connection
        else:
            if connection.destination.host in self.inboundConnections:
                self.inboundConnections[connection.destination] = connection
            else:
                self.inboundConnections[connection.destination.host] = \
                    connection

    def removeConnection(self, connection):
        """Remove a connection from our internal dict"""
        if isinstance(connection, UDPSocket):
            del self.udpSockets[connection.listening.host]
        elif isinstance(connection, TCPServer):
            del self.listeningSockets[state.Peer(
                connection.destination.host, connection.destination.port)]
        elif connection.isOutbound:
            try:
                del self.outboundConnections[connection.destination]
            except KeyError:
                pass
        else:
            try:
                del self.inboundConnections[connection.destination]
            except KeyError:
                try:
                    del self.inboundConnections[connection.destination.host]
                except KeyError:
                    pass
        connection.handle_close()

    def getListeningIP(self):
        """What IP are we supposed to be listening on?"""
        if BMConfigParser().safeGet(
                "bitmessagesettings", "onionhostname").endswith(".onion"):
            host = BMConfigParser().safeGet(
                "bitmessagesettings", "onionbindip")
        else:
            host = '127.0.0.1'
        if (BMConfigParser().safeGetBoolean(
                "bitmessagesettings", "sockslisten") or
            BMConfigParser().safeGet(
                "bitmessagesettings", "socksproxytype") == "none"):
            # python doesn't like bind + INADDR_ANY?
            # host = socket.INADDR_ANY
            host = BMConfigParser().get("network", "bind")
        return host

    def startListening(self, bind=None):
        """Open a listening socket and start accepting connections on it"""
        if bind is None:
            bind = self.getListeningIP()
        port = BMConfigParser().safeGetInt("bitmessagesettings", "port")
        # correct port even if it changed
        ls = TCPServer(host=bind, port=port)
        self.listeningSockets[ls.destination] = ls

    def startUDPSocket(self, bind=None):
        """
        Open an UDP socket. Depending on settings, it can either only
        accept incoming UDP packets, or also be able to send them.
        """
        if bind is None:
            host = self.getListeningIP()
            udpSocket = UDPSocket(host=host, announcing=True)
        else:
            if bind is False:
                udpSocket = UDPSocket(announcing=False)
            else:
                udpSocket = UDPSocket(host=bind, announcing=True)
        self.udpSockets[udpSocket.listening.host] = udpSocket

    def loop(self):
        """Main Connectionpool's loop"""
        # defaults to empty loop if outbound connections are maxed
        spawnConnections = False
        acceptConnections = True
        if BMConfigParser().safeGetBoolean(
                'bitmessagesettings', 'dontconnect'):
            acceptConnections = False
        elif BMConfigParser().safeGetBoolean(
                'bitmessagesettings', 'sendoutgoingconnections'):
            spawnConnections = True
        socksproxytype = BMConfigParser().safeGet(
            'bitmessagesettings', 'socksproxytype', '')
        onionsocksproxytype = BMConfigParser().safeGet(
            'bitmessagesettings', 'onionsocksproxytype', '')
        if (socksproxytype[:5] == 'SOCKS' and
            not BMConfigParser().safeGetBoolean(
                'bitmessagesettings', 'sockslisten') and
            '.onion' not in BMConfigParser().safeGet(
                'bitmessagesettings', 'onionhostname', '')):
            acceptConnections = False

        if spawnConnections:
            if not knownnodes.knownNodesActual:
                helper_bootstrap.dns()
            if not self.bootstrapped:
                self.bootstrapped = True
                Proxy.proxy = (
                    BMConfigParser().safeGet(
                        'bitmessagesettings', 'sockshostname'),
                    BMConfigParser().safeGetInt(
                        'bitmessagesettings', 'socksport')
                )
                # TODO AUTH
                # TODO reset based on GUI settings changes
                try:
                    if not onionsocksproxytype.startswith("SOCKS"):
                        raise ValueError
                    Proxy.onion_proxy = (
                        BMConfigParser().safeGet(
                            'network', 'onionsockshostname', None),
                        BMConfigParser().safeGet(
                            'network', 'onionsocksport', None)
                    )
                except ValueError:
                    Proxy.onion_proxy = None
            established = sum(
                1 for c in self.outboundConnections.values()
                if (c.connected and c.fullyEstablished))
            pending = len(self.outboundConnections) - established
            if established < BMConfigParser().safeGetInt(
                    'bitmessagesettings', 'maxoutboundconnections'):
                for i in range(
                        state.maximumNumberOfHalfOpenConnections - pending):
                    try:
                        chosen = chooseConnection(
                            helper_random.randomchoice(self.streams))
                    except ValueError:
                        continue
                    if chosen in self.outboundConnections:
                        continue
                    if chosen.host in self.inboundConnections:
                        continue
                    # don't connect to self
                    if chosen in state.ownAddresses:
                        continue

                    try:
                        if (chosen.host.endswith(".onion") and
                                Proxy.onion_proxy is not None):
                            if onionsocksproxytype == "SOCKS5":
                                self.addConnection(Socks5BMConnection(chosen))
                            elif onionsocksproxytype == "SOCKS4a":
                                self.addConnection(Socks4aBMConnection(chosen))
                        elif socksproxytype == "SOCKS5":
                            self.addConnection(Socks5BMConnection(chosen))
                        elif socksproxytype == "SOCKS4a":
                            self.addConnection(Socks4aBMConnection(chosen))
                        else:
                            self.addConnection(TCPConnection(chosen))
                    except socket.error as e:
                        if e.errno == errno.ENETUNREACH:
                            continue

                    self.lastSpawned = time.time()
        else:
            for i in (
                self.inboundConnections.values() +
                self.outboundConnections.values()
            ):
                # FIXME: rating will be increased after next connection
                i.handle_close()

        if acceptConnections:
            if not self.listeningSockets:
                if BMConfigParser().safeGet('network', 'bind') == '':
                    self.startListening()
                else:
                    for bind in re.sub(
                        "[^\w.]+", " ",
                        BMConfigParser().safeGet('network', 'bind')
                    ).split():
                        self.startListening(bind)
                logger.info('Listening for incoming connections.')
            if not self.udpSockets:
                if BMConfigParser().safeGet('network', 'bind') == '':
                    self.startUDPSocket()
                else:
                    for bind in re.sub(
                        "[^\w.]+", " ",
                        BMConfigParser().safeGet('network', 'bind')
                    ).split():
                        self.startUDPSocket(bind)
                    self.startUDPSocket(False)
                logger.info('Starting UDP socket(s).')
        else:
            if self.listeningSockets:
                for i in self.listeningSockets.values():
                    i.close_reason = "Stopping listening"
                    i.accepting = i.connecting = i.connected = False
                logger.info('Stopped listening for incoming connections.')
            if self.udpSockets:
                for i in self.udpSockets.values():
                    i.close_reason = "Stopping UDP socket"
                    i.accepting = i.connecting = i.connected = False
                logger.info('Stopped udp sockets.')

        loopTime = float(self.spawnWait)
        if self.lastSpawned < time.time() - self.spawnWait:
            loopTime = 2.0
        asyncore.loop(timeout=loopTime, count=1000)

        reaper = []
        for i in (
            self.inboundConnections.values() +
            self.outboundConnections.values()
        ):
            minTx = time.time() - 20
            if i.fullyEstablished:
                minTx -= 300 - 20
            if i.lastTx < minTx:
                if i.fullyEstablished:
                    i.append_write_buf(protocol.CreatePacket('ping'))
                else:
                    i.close_reason = "Timeout (%is)" % (
                        time.time() - i.lastTx)
                    i.set_state("close")
        for i in (
            self.inboundConnections.values() +
            self.outboundConnections.values() +
            self.listeningSockets.values() +
            self.udpSockets.values()
        ):
            if not (i.accepting or i.connecting or i.connected):
                reaper.append(i)
            else:
                try:
                    if i.state == "close":
                        reaper.append(i)
                except AttributeError:
                    pass
        for i in reaper:
            self.removeConnection(i)
