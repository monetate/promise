from collections import namedtuple
from functools import partial
from sys import version_info
from threading import Event, RLock

from typing import (Any, Callable, Dict, Iterator, Optional,  # flake8: noqa
                    Union)

from .async_ import Async
from .compat import (Future, ensure_future, iscoroutine,  # type: ignore
                     iterate_promise)
from .utils import deprecated
from .context import Context

async = Async()

IS_PYTHON2 = version_info[0] == 2

MAX_LENGTH = 0xFFFF | 0
CALLBACK_SIZE = 3

CALLBACK_FULFILL_OFFSET = 0
CALLBACK_REJECT_OFFSET = 1
CALLBACK_PROMISE_OFFSET = 2


class States(object):
    # These are the potential states of a promise
    PENDING = -1
    REJECTED = 0
    FULFILLED = 1


def internal_executor(resolve, reject):
    pass


def make_self_resolution_error():
    return TypeError("Promise is self")


_error_obj = {
    'e': None
} # type: Dict[str, Union[None, Exception]]


def try_catch(handler, *args, **kwargs):
    try:
        return handler(*args, **kwargs)
    except Exception as e:
        _error_obj['e'] = e
        # print traceback.format_exc()
        return _error_obj


class CountdownLatch(object):
    __slots__ = ('_lock', 'count')

    def __init__(self, count):
        # type: (CountdownLatch, int) -> None
        assert count >= 0, "count needs to be greater or equals to 0. Got: %s" % count
        self._lock = RLock()
        self.count = count

    def dec(self):
        # type: (CountdownLatch) -> int
        with self._lock:
            assert self.count > 0, "count needs to be greater or equals to 0. Got: %s" % self.count
            self.count -= 1
            # Return inside lock to return the correct value,
            # otherwise an other thread could already have
            # decremented again.
            return self.count


class Promise(object):
    """
    This is the Promise class that complies
    Promises/A+ specification and test suite:
    http://promises-aplus.github.io/promises-spec/
    """

    __slots__ = ('_state', '_is_final', '_is_bound', '_is_following', '_is_async_guaranteed',
                 '_length', '_handlers', '_fulfillment_handler0', '_rejection_handler0', '_promise0',
                 '_is_waiting', '_future', '_trace'
                 )

    def __init__(self, executor=None):
        # type: (Promise, Union[Callable, partial]) -> None
        """
        Initialize the Promise into a pending state.
        """
        self._state = States.PENDING  # type: int
        self._is_final = False
        self._is_bound = False
        self._is_following = False
        self._is_async_guaranteed = False
        self._length = 0
        self._handlers = {}  # type: Dict[int, Union[Callable, None]]
        self._fulfillment_handler0 = None  # type: Union[Callable, partial]
        self._rejection_handler0 = None  # type: Union[Callable, partial]
        self._promise0 = None  # type: Promise
        self._future = None  # type: Future
        self._trace = Context.peek_context()

        self._is_waiting = False
        if executor is not None and executor != internal_executor:
            self._resolve_from_executor(executor)

        # For compatibility reasons
        # self.reject = self._deprecated_reject
        # self.resolve = self._deprecated_resolve

    @property
    def future(self):
        # type: (Promise) -> Future
        if not self._future:
            self._future = Future()
            self._then(self._future.set_result, self._future.set_exception)
        return self._future

    def __iter__(self):
        return iterate_promise(self)

    __await__ = __iter__

    @deprecated(
        "Rejecting directly in a Promise instance is deprecated, as Promise.reject() is now a class method. "
        "Please use promise.do_reject() instead.",
        name='reject'
    )
    def _deprecated_reject(self, e):
        self.do_reject(e)

    @deprecated(
        "Resolving directly in a Promise instance is deprecated, as Promise.resolve() is now a class method. "
        "Please use promise.do_resolve() instead.",
        name='resolve'
    )
    def _deprecated_resolve(self, value):
        self.do_resolve(value)

    def _resolve_callback(self, value):
        if value is self:
            return self._reject_callback(make_self_resolution_error(), False)

        maybe_promise = self._try_convert_to_promise(value, self)
        if not isinstance(maybe_promise, Promise):
            return self._fulfill(value)

        promise = maybe_promise._target()
        if promise == self:
            self._reject(make_self_resolution_error())
            return

        if promise.is_pending:
            len = self._length
            if len > 0:
                promise._migrate_callback0(self)
            for i in range(1, len):
                promise._migrate_callback_at(self, i)

            self._is_following = True
            self._length = 0
            self._set_followee(promise)
        elif promise.is_fulfilled:
            self._fulfill(promise._value())
        elif promise.is_rejected:
            self._reject(promise._reason())

    def _settled_value(self, _raise=False):
        assert not self._is_following

        if self._state == States.FULFILLED:
            return self._rejection_handler0
        elif self._state == States.REJECTED:
            if _raise:
                raise self._fulfillment_handler0
            return self._fulfillment_handler0

    def _fulfill(self, value):
        if value is self:
            err = make_self_resolution_error()
            # self._attach_extratrace(err)
            return self._reject(err)
        self._state = States.FULFILLED
        self._rejection_handler0 = value

        if self._length > 0:
            if self._is_async_guaranteed:
                self._settle_promises()
            else:
                async.settle_promises(self)

    def _reject(self, reason):
        self._state = States.REJECTED
        self._fulfillment_handler0 = reason

        if self._is_final:
            assert self._length == 0
            return async.fatal_error(reason)

        if self._length > 0:
            async.settle_promises(self)
        else:
            self._ensure_possible_rejection_handled()

        if self._is_async_guaranteed:
            self._settle_promises()
        else:
            async.settle_promises(self)

    def _ensure_possible_rejection_handled(self):
        # self._rejection_is_unhandled = True
        # async.invoke_later(self._notify_unhandled_rejection, self)
        pass

    def _reject_callback(self, reason, synchronous=False):
        assert isinstance(reason, Exception), "A promise was rejected with a non-error: {}".format(reason)
        # trace = ensure_error_object(reason)
        # has_stack = trace is reason
        # self._attach_extratrace(trace, synchronous and has_stack)
        self._reject(reason)

    def _clear_callback_data_index_at(self, index):
        assert not self._is_following
        assert index > 0
        base = index * CALLBACK_SIZE - CALLBACK_SIZE
        self._handlers[base + CALLBACK_PROMISE_OFFSET] = None
        self._handlers[base + CALLBACK_FULFILL_OFFSET] = None
        self._handlers[base + CALLBACK_REJECT_OFFSET] = None

    def _fulfill_promises(self, length, value):
        for i in range(1, length):
            handler = self._fulfillment_handler_at(i)
            promise = self._promise_at(i)
            self._clear_callback_data_index_at(i)
            self._settle_promise(promise, handler, value)

    def _reject_promises(self, length, reason):
        for i in range(1, length):
            handler = self._rejection_handler_at(i)
            promise = self._promise_at(i)
            self._clear_callback_data_index_at(i)
            self._settle_promise(promise, handler, reason)

    def _settle_promise(self, promise, handler, value):
        assert not self._is_following
        is_promise = isinstance(promise, self.__class__)
        async_guaranteed = self._is_async_guaranteed
        if callable(handler):
            if not is_promise:
                handler(value)  # , promise
            else:
                if async_guaranteed:
                    promise._is_async_guaranteed = True
                self._settle_promise_from_handler(handler, value, promise)
        elif is_promise:
            if async_guaranteed:
                promise._is_async_guaranteed = True
            if self.is_fulfilled:
                promise._fulfill(value)
            else:
                promise._reject(value)

    def _settle_promise0(self, handler, value):
        promise = self._promise0
        self._promise0 = None
        self._settle_promise(promise, handler, value)

    def _settle_promise_from_handler(self, handler, value, promise):
        # promise._push_context()
        with Context():
            x = try_catch(handler, value)  # , promise
        # promise_created = promise._pop_context()

        if x == _error_obj:
            promise._reject_callback(x['e'], False)
        # if isinstance(x, PromiseError):
        #     promise._reject_callback(x.e, False)
        else:
            promise._resolve_callback(x)

    def _promise_at(self, index):
        assert index > 0
        assert not self._is_following
        return self._handlers[index * CALLBACK_SIZE - CALLBACK_SIZE + CALLBACK_PROMISE_OFFSET]

    def _fulfillment_handler_at(self, index):
        assert not self._is_following
        assert index > 0
        return self._handlers[index * CALLBACK_SIZE - CALLBACK_SIZE + CALLBACK_FULFILL_OFFSET]

    def _rejection_handler_at(self, index):
        assert not self._is_following
        assert index > 0
        return self._handlers[index * CALLBACK_SIZE - CALLBACK_SIZE + CALLBACK_REJECT_OFFSET]

    def _migrate_callback0(self, follower):
        self._add_callbacks(
            follower._fulfillment_handler0,
            follower._rejection_handler0,
            follower._promise0,
        )

    def _migrate_callback_at(self, follower, index):
        self._add_callbacks(
            follower._fulfillment_handler_at(index),
            follower._rejection_handler_at(index),
            follower._promise_at(index),
        )

    def _add_callbacks(self, fulfill, reject, promise):
        assert not self._is_following
        index = self._length
        if index > MAX_LENGTH - CALLBACK_SIZE:
            index = 0
            self._length = 0

        if index == 0:
            assert not self._promise0
            assert not self._fulfillment_handler0
            assert not self._rejection_handler0

            self._promise0 = promise
            if callable(fulfill):
                self._fulfillment_handler0 = fulfill
            if callable(reject):
                self._rejection_handler0 = reject

        else:
            base = index * CALLBACK_SIZE - CALLBACK_SIZE

            assert (base + CALLBACK_PROMISE_OFFSET) not in self._handlers
            assert (base + CALLBACK_FULFILL_OFFSET) not in self._handlers
            assert (base + CALLBACK_REJECT_OFFSET) not in self._handlers

            self._handlers[base + CALLBACK_PROMISE_OFFSET] = promise
            if callable(fulfill):
                self._handlers[base + CALLBACK_FULFILL_OFFSET] = fulfill
            if callable(reject):
                self._handlers[base + CALLBACK_REJECT_OFFSET] = reject

        self._length = index + 1
        return index

    def _target(self):
        ret = self
        while (ret._is_following):
            ret = ret._followee()
        return ret

    def _followee(self):
        assert self._is_following
        assert isinstance(self._rejection_handler0, Promise)
        return self._rejection_handler0

    def _set_followee(self, promise):
        assert self._is_following
        assert not isinstance(self._rejection_handler0, Promise)
        self._rejection_handler0 = promise

    def _settle_promises(self):
        length = self._length
        if length > 0:
            if self.is_rejected:
                reason = self._fulfillment_handler0
                self._settle_promise0(self._rejection_handler0, reason)
                self._reject_promises(length, reason)
            else:
                value = self._rejection_handler0
                self._settle_promise0(self._fulfillment_handler0, value)
                self._fulfill_promises(length, value)

            self._length = 0

    def _resolve_from_executor(self, executor):
        # self._capture_stacktrace()
        # self._push_context()
        with Context():
            synchronous = True

            def resolve(value):
                self._resolve_callback(value)

            def reject(reason):
                self._reject_callback(reason, synchronous)

            error = None
            try:
                executor(resolve, reject)
            except Exception as e:
                error = e
                # print traceback.format_exc()

            synchronous = False
        # self._pop_context()

        if error is not None:
            self._reject_callback(error, True)

    def _wait(self, timeout=None):
        target = self._target()

        if self._state == States.PENDING and not self._is_waiting:
            event = Event()

            # Simpler way of doing it, not working with following promises

            # self._add_callbacks(
            #     lambda result: event.set(),
            #     lambda error: event.set(),
            #     None,
            # )

            self._is_following = False

            def on_result(result):
                event.set()
                self._resolve_callback(result)

            def on_error(error):
                event.set()
                self._reject_callback(error)

            target._then(
                on_result,
                on_error,
            )

            if self._trace:
                # If we wait, we drain the queue of the
                # callbacks waiting on the context exit
                # so we avoid a blocking state
                self._trace.drain_queue()

            self._is_waiting = True

            if timeout is None and IS_PYTHON2:
                float("Inf")

            waited = event.wait(timeout)
            if not waited:
                self._is_waiting = False

    def get(self, wait=True, timeout=None):
        if wait or timeout:
            self._wait(timeout)

        return self._settled_value(_raise=True)

    _value = _reason = _settled_value
    value = reason = property(_settled_value)

    def __repr__(self):
        hex_id = hex(id(self))
        if self._state == States.PENDING:
            return "<Promise at {} pending>".format(hex_id)
        elif self._state == States.FULFILLED:
            return "<Promise at {} fulfilled with {}>".format(
                hex_id,
                repr(self._rejection_handler0)
            )
        elif self._state == States.REJECTED:
            return "<Promise at {} rejected with {}>".format(
                hex_id,
                repr(self._fulfillment_handler0)
            )

    @property
    def is_pending(self):
        # type: (Promise) -> bool
        """Indicate whether the Promise is still pending. Could be wrong the moment the function returns."""
        return self._state == States.PENDING

    @property
    def is_fulfilled(self):
        # type: (Promise) -> bool
        """Indicate whether the Promise has been fulfilled. Could be wrong the moment the function returns."""
        return self._state == States.FULFILLED

    @property
    def is_rejected(self):
        # type: (Promise) -> bool
        """Indicate whether the Promise has been rejected. Could be wrong the moment the function returns."""
        return self._state == States.REJECTED

    def catch(self, on_rejection):
        # type: (Promise, Union[Callable, partial]) -> Promise
        """
        This method returns a Promise and deals with rejected cases only.
        It behaves the same as calling Promise.then(None, on_rejection).
        """
        return self.then(None, on_rejection)

    def _then(self, did_fulfill=None, did_reject=None):
        promise = self.__class__(internal_executor)
        target = self._target()

        if not target.is_pending:
            if target.is_fulfilled:
                value = target._rejection_handler0
                handler = did_fulfill
            elif target.is_rejected:
                value = target._fulfillment_handler0
                handler = did_reject
                # target._rejection_is_unhandled = False
            async.invoke(
                partial(self._settle_promise, promise, handler, value)
                # target._settle_promise instead?
                # settler,
                # target,
                # Context(handler, promise, value),
            )
        else:
            target._add_callbacks(did_fulfill, did_reject, promise)

        return promise

    fulfill = _resolve_callback
    do_resolve = _resolve_callback
    do_reject = _reject_callback

    def then(self, did_fulfill=None, did_reject=None):
        # type: (Promise, Union[Callable, partial], Union[Callable, partial]) -> Promise
        """
        This method takes two optional arguments.  The first argument
        is used if the "self promise" is fulfilled and the other is
        used if the "self promise" is rejected.  In either case, this
        method returns another promise that effectively represents
        the result of either the first of the second argument (in the
        case that the "self promise" is fulfilled or rejected,
        respectively).
        Each argument can be either:
          * None - Meaning no action is taken
          * A function - which will be called with either the value
            of the "self promise" or the reason for rejection of
            the "self promise".  The function may return:
            * A value - which will be used to fulfill the promise
              returned by this method.
            * A promise - which, when fulfilled or rejected, will
              cascade its value or reason to the promise returned
              by this method.
          * A value - which will be assigned as either the value
            or the reason for the promise returned by this method
            when the "self promise" is either fulfilled or rejected,
            respectively.
        :type success: (Any) -> object
        :type failure: (Any) -> object
        :rtype : Promise
        """
        return self._then(did_fulfill, did_reject)

    def done(self, did_fulfill=None, did_reject=None):
        promise = self._then(did_fulfill, did_reject)
        promise._is_final = True

    def done_all(self, handlers=None):
        # type: (Promise, List[Callable]) -> List[Promise]
        """
        :type handlers: list[(Any) -> object] | list[((Any) -> object, (Any) -> object)]
        """
        if not handlers:
            return []

        for handler in handlers:
            if isinstance(handler, tuple):
                s, f = handler

                self.done(s, f)
            elif isinstance(handler, dict):
                s = handler.get('success')
                f = handler.get('failure')

                self.done(s, f)
            else:
                self.done(handler)

    def then_all(self, handlers=None):
        # type: (Promise, List[Callable]) -> List[Promise]
        """
        Utility function which calls 'then' for each handler provided. Handler can either
        be a function in which case it is used as success handler, or a tuple containing
        the success and the failure handler, where each of them could be None.
        :type handlers: list[(Any) -> object] | list[((Any) -> object, (Any) -> object)]
        :param handlers
        :rtype : list[Promise]
        """
        if not handlers:
            return []

        promises = []  # type: List[Promise]

        for handler in handlers:
            if isinstance(handler, tuple):
                s, f = handler

                promises.append(self.then(s, f))
            elif isinstance(handler, dict):
                s = handler.get('success')
                f = handler.get('failure')

                promises.append(self.then(s, f))
            else:
                promises.append(self.then(handler))

        return promises

    @classmethod
    def _try_convert_to_promise(cls, obj, context=None):
        if isinstance(obj, cls):
            return obj

        add_done_callback = get_done_callback(obj)  # type: Optional[Callable]
        if callable(add_done_callback):
            def executor(resolve, reject):
                # if obj.done():
                #     _process_future_result(resolve, reject)(obj)
                # else:
                #     add_done_callback(_process_future_result(resolve, reject))
                _process_future_result(resolve, reject)(obj)
            promise = cls(executor)
            promise._future = obj
            return promise

        done = getattr(obj, "done", None)  # type: Optional[Callable]
        if callable(done):
            def executor(resolve, reject):
                done(resolve, reject)
            return cls(executor)

        then = getattr(obj, "then", None)  # type: Optional[Callable]
        if callable(then):
            def executor(resolve, reject):
                then(resolve, reject)
            return cls(executor)

        if iscoroutine(obj):
            return cls._try_convert_to_promise(ensure_future(obj))

        return obj

    @classmethod
    def reject(cls, reason):
        ret = Promise(internal_executor)
        # ret._capture_stacktrace();
        # ret._rejectCallback(reason, true);
        ret._reject_callback(reason, True)
        return ret

    rejected = reject

    @classmethod
    def resolve(cls, obj):
        # type: (Any) -> Promise
        ret = cls._try_convert_to_promise(obj)
        if not isinstance(ret, cls):
            ret = cls(internal_executor)
            # ret._capture_stacktrace()
            ret._state = States.FULFILLED
            ret._rejection_handler0 = obj

        return ret

    cast = resolve
    promisify = cast
    fulfilled = cast

    @classmethod
    def safe(cls, fn):
        from functools import wraps
        @wraps(fn)
        def wrapper(*args, **kwargs):
            with Context():
                return fn(*args, **kwargs)

        return wrapper

    @classmethod
    def all(cls, values_or_promises):
        # Type: (Iterable[Promise, Any]) -> Promise
        """
        A special function that takes a bunch of promises
        and turns them into a promise for a vector of values.
        In other words, this turns an list of promises for values
        into a promise for a list of values.
        """
        _len = len(values_or_promises)
        if _len == 0:
            return cls.resolve(values_or_promises)

        promises = (
            cls.promisify(v_or_p)
            if is_thenable(v_or_p) else cls.resolve(v_or_p)
            for v_or_p in values_or_promises)  # type: Iterator[Promise]

        all_promise = cls()  # type: Promise
        counter = CountdownLatch(_len)
        values = [None] * _len  # type: List[Any]

        def handle_success(original_position):
            # type: (int) -> Callable
            def ret(value):
                values[original_position] = value
                if counter.dec() == 0:
                    all_promise.fulfill(values)

            return ret

        for i, p in enumerate(promises):
            p.done(handle_success(i), all_promise.reject)  # type: ignore

        return all_promise

    @classmethod
    def for_dict(cls, m):
        # type: (Dict[Any, Promise]) -> Promise
        """
        A special function that takes a dictionary of promises
        and turns them into a promise for a dictionary of values.
        In other words, this turns an dictionary of promises for values
        into a promise for a dictionary of values.
        """
        dict_type = type(m)

        if not m:
            return cls.resolve(dict_type())

        def handle_success(resolved_values):
            return dict_type(zip(m.keys(), resolved_values))

        return cls.all(m.values()).then(handle_success)


promisify = Promise.promisify
promise_for_dict = Promise.for_dict


def _process_future_result(resolve, reject):
    def handle_future_result(future):
        exception = future.exception()
        if exception:
            reject(exception)
        else:
            resolve(future.result())
    return handle_future_result


def is_future(obj):
    # type: (Any) -> bool
    return callable(get_done_callback(obj))


def get_done_callback(obj):
    # type: (Any) -> Callable
    return getattr(obj, "add_done_callback", None)


def is_thenable(obj):
    # type: (Any) -> bool
    """
    A utility function to determine if the specified
    object is a promise using "duck typing".
    """
    return isinstance(obj, Promise) or is_future(obj) or (
        hasattr(obj, "done") and callable(getattr(obj, "done"))) or (
            hasattr(obj, "then") and callable(getattr(obj, "then"))) or (
                iscoroutine(obj))
