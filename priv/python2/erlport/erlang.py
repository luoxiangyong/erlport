# Copyright (c) 2009-2013, Dmitry Vasiliev <dima@hlabs.org>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#  * Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#  * Neither the name of the copyright holders nor the names of its
#    contributors may be used to endorse or promote products derived from this
#    software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import sys
from sys import exc_info
from traceback import extract_tb
from inspect import getargspec

from erlport import Atom

class Error(Exception):
    """ErlPort Error."""

class InvalidMessage(Error):
    """Invalid message."""

class UnknownMessage(Error):
    """Unknown message."""

class UnexpectedMessage(Error):
    """Unexpected message."""

class UnexpectedResponses(UnexpectedMessage):
    """Unexpected responses."""

class DuplicateMessageId(Error):
    """Duplicate message ID."""

class CallError(Error):
    """Call error."""

    def __init__(self, value):
        if type(value) != tuple or len(value) != 4:
            value = None, None, value, []
        self.language, self.type, self.value, self.stacktrace = value
        Error.__init__(self, value)


class MessageHandler(object):

    def __init__(self, port):
        self.port = port
        self.set_encoder(None)
        self.set_decoder(None)
        self.set_message_handler(None)
        self._self = None
        self.responses = {}
        self.calls = set()

    def set_encoder(self, encoder):
        if encoder:
            self.check_handler(encoder)
            self.encoder = self.object_iterator(encoder)
        else:
            self.encoder = lambda o: o

    def set_decoder(self, decoder):
        if decoder:
            self.check_handler(decoder)
            self.decoder = self.object_iterator(decoder)
        else:
            self.decoder = lambda o: o

    def set_message_handler(self, handler):
        if handler:
            self.check_handler(handler)
            self.handler = handler
        else:
            self.handler = lambda o: None

    def check_handler(self, handler):
        # getargspec will raise TypeError if handler is not a function
        args, varargs, _keywords, defaults = getargspec(handler)
        largs = len(args)
        too_much = largs > 1 and largs - len(default) > 1
        too_few = largs == 0 and varargs is None
        if too_much or too_few:
            raise ValueError("expected single argument function: %r"
                % (handler,))

    def object_iterator(self, handler,
            isinstance=isinstance, list=list, tuple=tuple, map=map):
        def iterator(obj):
            obj = handler(obj)
            if isinstance(obj, (list, tuple)):
                return obj.__class__(map(iterator, obj))
            return obj
        return iterator

    def start(self):
        try:
            self.receive()
        except EOFError:
            pass

    def receive(self, expect_id=None, expect_message=False):
        if expect_id is None and self.responses:
            raise UnexpectedResponses(self.responses)
        while True:
            if expect_id is not None and expect_id in self.responses:
                return self.responses.pop(expect_id)
            message = self.port.read()
            try:
                mtype = message[0]
            except (IndexError, TypeError):
                raise InvalidMessage(message)

            if mtype == "C":
                try:
                    mid, module, function, args = message[1:]
                except ValueError:
                    raise InvalidMessage(message)
                self.port.write(self.call_with_error_handler(
                    mid, module, function, args))
            elif mtype == "M":
                if expect_message:
                    return message
                try:
                    payload, = message[1:]
                except ValueError:
                    raise InvalidMessage(message)
                try:
                    self.handler(payload)
                except:
                    # TODO: Return formatted exception
                    pass
            elif mtype == "r" or mtype == "e":
                if expect_id is not None:
                    try:
                        mid = message[1]
                    except IndexError:
                        raise InvalidMessage(message)
                    if mid == expect_id:
                        return message
                    else:
                        # TODO: Should we lock all self.responses and
                        # self.calls operations?
                        if mid in self.responses:
                            raise DuplicateMessageId(message)
                        self.responses[mid] = message
                else:
                    raise UnexpectedMessage(message)
            else:
                raise UnknownMessage(message)

    def cast(self, pid, message):
        # It's safe to call it from multiple threads because port.write will be
        # locked
        self.port.write((Atom('M'), pid, message))

    def call(self, module, function, args):
        return self._call(module, function, args, Atom('N'))

    def self(self):
        if self._self is None:
            self._self = self._call(Atom('erlang'), Atom('self'), [], Atom('L'))
        return self._self

    def make_ref(self):
        return self._call(Atom('erlang'), Atom('make_ref'), [], Atom('L'))

    def _call(self, module, function, args, context):
        if not isinstance(module, Atom):
            raise ValueError(module)
        if not isinstance(function, Atom):
            raise ValueError(function)
        if not isinstance(args, list):
            raise ValueError(args)

        # TODO: Should we lock all self.responses and self.calls operations?
        if self.calls:
            mid = max(self.calls) + 1
        else:
            mid = 1
        self.calls.add(mid)
        self.port.write((Atom('C'), mid, module, function,
            map(self.encoder, args), context))
        response = self.receive(expect_id=mid)
        self.calls.remove(mid)

        try:
            mtype, _mid, value = response
        except ValueError:
            raise InvalidMessage(response)

        if mtype != "r":
            if mtype == "e":
                raise CallError(value)
            raise UnknownMessage(response)
        return self.decoder(value)

    def call_with_error_handler(self, mid, module, function, args):
        try:
            objects = function.split(".")
            f = sys.modules.get(module)
            if not f:
                f = __import__(module, {}, {}, [objects[0]])
            for o in objects:
                f = getattr(f, o)
            result = Atom("r"), mid, self.encoder(f(*map(self.decoder, args)))
        except:
            t, val, tb = exc_info()
            exc = Atom("%s.%s" % (t.__module__, t.__name__))
            exc_tb = extract_tb(tb)
            exc_tb.reverse()
            result = Atom("e"), mid, (Atom("python"), exc, unicode(val), exc_tb)
        return result

class RedirectedStdin(object):

    def __getattr__(self, name):
        def closed(*args, **kwargs):
            raise RuntimeError("STDIN is closed for ErlPort connected process")
        return closed

class RedirectedStdout(object):

    def __init__(self, port):
        self.__port = port

    def write(self, data):
        if not isinstance(data, (str, unicode, buffer)):
            raise TypeError("expected a characer buffer object")
        return self.__port.write((Atom("P"), data))

    def writelines(self, lst):
        for data in lst:
            if not isinstance(data, (str, unicode, buffer)):
                raise TypeError("expected a character buffer object")
        return self.write("".join(lst))

    def __getattr__(self, name):
        def unsupported(*args, **kwargs):
            raise RuntimeError("unsupported STDOUT operation for ErlPort"
                " connected process")
        return unsupported


def setup_stdin_stdout(port):
    global RedirectedStdin, RedirectedStdout
    sys.stdin = RedirectedStdin()
    sys.stdout = RedirectedStdout(port)
    del RedirectedStdin, RedirectedStdout

def setup_api_functions(handler):
    global call, cast, self, make_ref
    global set_encoder, set_decoder, set_message_handler
    call = handler.call
    cast = handler.cast
    self = handler.self
    make_ref = handler.make_ref
    set_encoder = handler.set_encoder
    set_decoder = handler.set_decoder
    set_message_handler = handler.set_message_handler

def setup(port):
    global MessageHandler, setup
    handler = MessageHandler(port)
    setup_api_functions(handler)
    setup_stdin_stdout(port)
    del MessageHandler, setup
    handler.start()
