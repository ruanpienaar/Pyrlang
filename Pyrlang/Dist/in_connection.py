""" The module implements incoming TCP distribution connection (i.e. initiated
    by another node with the help of EPMD).
"""
from __future__ import print_function
import random
import struct
from hashlib import md5
from typing import Union

from gevent.queue import Queue

from Pyrlang import term, logger
from Pyrlang.Dist import epmd, util, etf
from Pyrlang.Dist.node_opts import NodeOpts

# First element of control term in a 'p' message defines what it is
CONTROL_TERM_SEND = 2
CONTROL_TERM_REG_SEND = 6
CONTROL_TERM_MONITOR_P = 19
CONTROL_TERM_DEMONITOR_P = 20
CONTROL_TERM_MONITOR_P_EXIT = 21

LOG = logger.nothing
ERROR = logger.tty


class DistributionError(Exception):
    pass


class InConnection:
    """ Handles incoming connections from other nodes.

        Behaves like a ``Greenlet`` but the actual Greenlet run procedure and
        the recv loop around this protocol are located in the
        ``util.make_handler`` helper function.
    """
    DISCONNECTED = 0
    RECV_NAME = 1
    WAIT_CHALLENGE_REPLY = 2
    CONNECTED = 3

    def __init__(self, dist, node_opts: NodeOpts):
        self.state_ = self.DISCONNECTED
        self.packet_len_size_ = 2
        """ Packet size header is variable, 2 bytes before handshake is finished
            and 4 bytes afterwards.
        """
        self.socket_ = None
        self.addr_ = None

        self.dist_ = dist  # reference to distribution object
        self.node_opts_ = node_opts
        self.inbox_ = Queue()  # refer to util.make_handler which reads this
        """ Inbox is used to ask the connection to do something. """

        self.peer_distr_version_ = (None, None)
        """ Protocol version range supported by the remote peer. Erlang/OTP 
            versions 19-20 supports protocol version 7, older Erlangs down to 
            R6B support version 5. """
        self.peer_flags_ = 0
        self.peer_name_ = None
        self.my_challenge_ = None

    def on_connected(self, sockt, address):
        """ Handler invoked from the recv loop (in ``util.make_handler``)
            when the connection has been accepted and established.
        """
        self.state_ = self.RECV_NAME
        self.socket_ = sockt
        self.addr_ = address

    def consume(self, data: bytes) -> Union[bytes, None]:
        """ Attempt to consume first part of data as a packet

            :param data: The accumulated data from the socket which we try to
                partially or fully consume
            :return: Unconsumed data, incomplete following packet maybe or
                nothing. Returning None requests to close the connection
        """
        if len(data) < self.packet_len_size_:
            # Not ready yet, keep reading
            return data

        # Dist protocol switches from 2 byte packet length to 4 at some point
        if self.packet_len_size_ == 2:
            pkt_size = util.u16(data, 0)
            offset = 2
        else:
            pkt_size = util.u32(data, 0)
            offset = 4

        if len(data) < self.packet_len_size_ + pkt_size:
            # Length is already visible but the data is not here yet
            return data

        packet = data[offset:(offset + pkt_size)]
        # LOG('Dist packet:', util.hex_bytes(packet))

        if self.on_packet(packet):
            return data[(offset + pkt_size):]

        # Protocol error has occured and instead we return None to request
        # connection close (see util.py)
        return None

    def on_connection_lost(self):
        """ Handler is called when the client has disconnected
        """
        self.state_ = self.DISCONNECTED

        from Pyrlang.node import Node
        Node.singleton.inbox_.put(
            ('node_disconnected', self.peer_name_))

    def on_packet(self, data) -> bool:
        """ Handle incoming distribution packet

            :param data: The packet after the header with length has been
                removed.
        """
        if self.state_ == self.RECV_NAME:
            return self.on_packet_recvname(data)

        elif self.state_ == self.WAIT_CHALLENGE_REPLY:
            return self.on_packet_challengereply(data)

        elif self.state_ == self.CONNECTED:
            return self.on_packet_connected(data)

    @staticmethod
    def _error(msg) -> False:
        ERROR("Distribution protocol error:", msg)
        return False

    @staticmethod
    def _dist_version_check(pdv: tuple):
        """ Check peer version against our version
            :type pdv: (MAX,MIN) - peer dist version, supported by the peer
        """
        return pdv[0] >= epmd.DIST_VSN >= pdv[1]

    def on_packet_recvname(self, data) -> bool:
        """ Handle RECV_NAME command, the first packet in a new connection. """
        if data[0] != ord('n'):
            return self._error("Unexpected packet (expecting RECV_NAME)")

        # Read peer distribution version and compare to ours
        pdv = (data[1], data[2])
        if self._dist_version_check(pdv):
            return self._error("Dist protocol version have: %s got: %s"
                               % (str(epmd.DIST_VSN_PAIR), str(pdv)))
        self.peer_distr_version_ = pdv

        self.peer_flags_ = util.u32(data[3:7])
        self.peer_name_ = data[7:].decode("latin1")
        LOG("RECV_NAME:", self.peer_distr_version_, self.peer_name_)

        # Maybe too early here? Actual connection is established moments later
        from Pyrlang.node import Node
        Node.singleton.inbox_.put(
            ('node_connected', self.peer_name_, self))

        # Report
        self._send_packet2(b"sok")

        self.my_challenge_ = int(random.random() * 0x7fffffff)
        self._send_challenge(self.my_challenge_)

        self.state_ = self.WAIT_CHALLENGE_REPLY

        return True

    def on_packet_challengereply(self, data):
        if data[0] != ord('r'):
            return self._error("Unexpected packet (expecting CHALLENGE_REPLY)")

        peers_challenge = util.u32(data, 1)
        peer_digest = data[5:]
        LOG("challengereply: peer's challenge", peers_challenge)

        my_cookie = self.node_opts_.cookie_
        if not self._check_digest(peer_digest, self.my_challenge_, my_cookie):
            return self._error("Disallowed node connection (check the cookie)")

        self._send_challenge_ack(peers_challenge, my_cookie)
        self.packet_len_size_ = 4
        self.state_ = self.CONNECTED

        # TODO: start timer with node_opts_.network_tick_time_

        LOG("Connection established with %s" % self.peer_name_)
        return True

    def on_packet_connected(self, data):
        # TODO: Update timeout timer, that we have connectivity still
        if data == b'':
            self._send_packet4(b'')
            return True  # this was a keepalive

        msg_type = chr(data[0])

        if msg_type == "p":
            (control_term, tail) = etf.binary_to_term(data[1:])

            if tail != b'':
                (msg_term, tail) = etf.binary_to_term(tail)
            else:
                msg_term = None
            self.on_passthrough_message(control_term, msg_term)

        else:
            return self._error("Unexpected dist message type: %s" % msg_type)

        return True

    def _send_packet2(self, content: bytes):
        """ Send a handshake-time status message with a 2 byte length prefix
        """
        msg = struct.pack(">H", len(content)) + content
        self.socket_.sendall(msg)

    def _send_packet4(self, content: bytes):
        """ Send a connection-time status message with a 4 byte length prefix
        """
        msg = struct.pack(">I", len(content)) + content
        self.socket_.sendall(msg)

    def _send_challenge(self, my_challenge):
        LOG("Sending challenge (our number is %d)" % my_challenge,
            self.dist_.name_)
        msg = b'n' \
              + struct.pack(">HII",
                            epmd.DIST_VSN,
                            self.node_opts_.dflags_,
                            my_challenge) \
              + bytes(self.dist_.name_, "latin1")
        self._send_packet2(msg)

    @staticmethod
    def _check_digest(peer_digest: bytes, peer_challenge: int,
                      cookie: str) -> bool:
        """ Hash cookie + the challenge together producing a verification hash
        """
        expected_digest = InConnection.make_digest(peer_challenge, cookie)
        LOG("Check digest: expected digest", expected_digest,
            "peer digest", peer_digest)
        return peer_digest == expected_digest

    @staticmethod
    def make_digest(challenge: int, cookie: str) -> bytes:
        result = md5(bytes(cookie, "ascii")
                     + bytes(str(challenge), "ascii")).digest()
        return result

    def _send_challenge_ack(self, peers_challenge: int, cookie: str):
        """ After cookie has been verified, send the confirmation by digesting
            our cookie with the remote challenge
        """
        digest = InConnection.make_digest(peers_challenge, cookie)
        self._send_packet2(b'a' + digest)

    @staticmethod
    def on_passthrough_message(control_term, msg_term):
        """ On incoming 'p' message with control and data, handle it """
        LOG("Passthrough msg %s\n%s" % (control_term, msg_term))

        if type(control_term) != tuple:
            raise DistributionError("In a 'p' message control term must be a "
                                    "tuple")

        ctrl_msg_type = control_term[0]

        from Pyrlang import node
        the_node = node.Node.singleton

        if ctrl_msg_type in [CONTROL_TERM_SEND, CONTROL_TERM_REG_SEND]:
            return the_node.send(sender=control_term[1],
                                 receiver=control_term[3],
                                 message=msg_term)

        elif ctrl_msg_type == CONTROL_TERM_MONITOR_P:
            (_, sender, target, ref) = control_term
            return the_node.monitor_process(origin=sender,
                                            target=target)

        elif ctrl_msg_type == CONTROL_TERM_DEMONITOR_P:
            (_, sender, target, ref) = control_term
            return the_node.demonitor_process(origin=sender,
                                              target=target)

        else:
            ERROR("Unhandled 'p' message: %s\n%s" % (control_term, msg_term))

    def handle_one_inbox_message(self, m):
        # Send a ('send', Dst, Msg) to deliver a message to the other side
        if m[0] == 'send':
            (_, dst, msg) = m
            ctrl = self._control_term_send(dst)
            LOG("Connection: control msg %s; %s" % (ctrl, msg))
            return self._control_message(ctrl, msg)

        elif m[0] == 'monitor_p_exit':
            (_, from_pid, to_pid, ref, reason) = m
            ctrl = (CONTROL_TERM_MONITOR_P_EXIT,
                    from_pid, to_pid, ref, reason)
            LOG("Monitor proc exit: %s with %s" % (from_pid, reason))
            return self._control_message(ctrl, None)

        ERROR("Connection: Unhandled message to InConnection %s" % m)

    @staticmethod
    def _control_term_send(dst):
        return CONTROL_TERM_SEND, term.Atom(''), dst

    def _control_message(self, ctrl, msg):
        """ Pack a control message and a regular message (can be None) together
            and send them over the connection
        """
        if msg is None:
            packet = b'p' + etf.term_to_binary(ctrl)
        else:
            packet = b'p' + etf.term_to_binary(ctrl) + etf.term_to_binary(msg)

        self._send_packet4(packet)


__all__ = ['InConnection']
