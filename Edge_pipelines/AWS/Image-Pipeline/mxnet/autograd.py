# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# coding: utf-8
"""Autograd for NDArray."""
from __future__ import absolute_import
from __future__ import division

from threading import Lock
import traceback
import ctypes
from ctypes import c_int, c_void_p, CFUNCTYPE, POINTER, cast
from .base import _LIB, check_call, string_types
from .base import mx_uint, NDArrayHandle, c_array, MXCallbackList, SymbolHandle
from .ndarray import NDArray
from .symbol import _GRAD_REQ_MAP, Symbol


def set_recording(is_recording): #pylint: disable=redefined-outer-name
    """Set status to recording/not recording. When recording, graph will be constructed
    for gradient computation.

    Parameters
    ----------
    is_recording: bool

    Returns
    -------
    previous state before this set.
    """
    prev = ctypes.c_int()
    check_call(_LIB.MXAutogradSetIsRecording(
        ctypes.c_int(is_recording), ctypes.byref(prev)))
    return bool(prev.value)

def set_training(train_mode): #pylint: disable=redefined-outer-name
    """Set status to training/predicting. This affects ctx.is_train in operator
    running context. For example, Dropout will drop inputs randomly when
    train_mode=True while simply passing through if train_mode=False.

    Parameters
    ----------
    train_mode: bool

    Returns
    -------
    previous state before this set.
    """
    prev = ctypes.c_int()
    check_call(_LIB.MXAutogradSetIsTraining(
        ctypes.c_int(train_mode), ctypes.byref(prev)))
    return bool(prev.value)

def is_recording():
    """Get status on recording/not recording.

    Returns
    -------
    Current state of recording.
    """
    curr = ctypes.c_bool()
    check_call(_LIB.MXAutogradIsRecording(ctypes.byref(curr)))
    return curr.value

def is_training():
    """Get status on training/predicting.

    Returns
    -------
    Current state of training/predicting.
    """
    curr = ctypes.c_bool()
    check_call(_LIB.MXAutogradIsTraining(ctypes.byref(curr)))
    return curr.value


class _RecordingStateScope(object):
    """Scope for managing training state.

    Example::

        with _RecordingStateScope(True, True):
            y = model(x)
            backward([y])

    """
    def __init__(self, is_record, train_mode): #pylint: disable=redefined-outer-name
        self._enter_is_record = is_record
        self._enter_train_mode = train_mode
        self._prev_is_record = None
        self._prev_train_mode = None

    def __enter__(self):
        if self._enter_is_record is not None:
            self._prev_is_record = set_recording(self._enter_is_record)
        if self._enter_train_mode is not None:
            self._prev_train_mode = set_training(self._enter_train_mode)

    def __exit__(self, ptype, value, trace):
        if self._enter_is_record is not None and self._prev_is_record != self._enter_is_record:
            set_recording(self._prev_is_record)
        if self._enter_train_mode is not None and self._prev_train_mode != self._enter_train_mode:
            set_training(self._prev_train_mode)


def record(train_mode=True): #pylint: disable=redefined-outer-name
    """Returns an autograd recording scope context to be used in 'with' statement
    and captures code that needs gradients to be calculated.

    .. note:: When forwarding with train_mode=False, the corresponding backward
              should also use train_mode=False, otherwise gradient is undefined.

    Example::

        with autograd.record():
            y = model(x)
            backward([y])
        metric.update(...)
        optim.step(...)

    Parameters
    ----------
    train_mode: bool, default True
        Whether the forward pass is in training or predicting mode. This controls the behavior
        of some layers such as Dropout, BatchNorm.
    """
    return _RecordingStateScope(True, train_mode)


def pause(train_mode=False): #pylint: disable=redefined-outer-name
    """Returns a scope context to be used in 'with' statement for codes that do not need
    gradients to be calculated.

    Example::

        with autograd.record():
            y = model(x)
            backward([y])
            with autograd.pause():
                # testing, IO, gradient updates...

    Parameters
    ----------
    train_mode: bool, default False
        Whether to do forward for training or predicting.
    """
    return _RecordingStateScope(False, train_mode)


def train_mode():
    """Returns a scope context to be used in 'with' statement
    in which forward pass behavior is set to training mode,
    without changing the recording states.

    Example::

        y = model(x)
        with autograd.train_mode():
            y = dropout(y)

    """
    return _RecordingStateScope(None, True)


def predict_mode():
    """Returns a scope context to be used in 'with' statement
    in which forward pass behavior is set to inference mode,
    without changing the recording states.

    Example::

        with autograd.record():
            y = model(x)
            with autograd.predict_mode():
                y = sampling(y)
            backward([y])
    """
    return _RecordingStateScope(None, False)


def mark_variables(variables, gradients, grad_reqs='write'):
    """Mark NDArrays as variables to compute gradient for autograd.

    Parameters
    ----------
    variables: NDArray or list of NDArray
    gradients: NDArray or list of NDArray
    grad_reqs: str or list of str
    """
    if isinstance(variables, NDArray):
        assert isinstance(gradients, NDArray)
        variables = [variables]
        gradients = [gradients]

    variable_handles = []
    gradient_handles = []
    for var, gradvar in zip(variables, gradients):
        variable_handles.append(var.handle)
        gradient_handles.append(gradvar.handle)
    if isinstance(grad_reqs, string_types):
        grad_reqs = [_GRAD_REQ_MAP[grad_reqs]]*len(variables)
    else:
        grad_reqs = [_GRAD_REQ_MAP[i] for i in grad_reqs]

    check_call(_LIB.MXAutogradMarkVariables(
        len(variable_handles),
        c_array(NDArrayHandle, variable_handles),
        c_array(mx_uint, grad_reqs),
        c_array(NDArrayHandle, gradient_handles)))


def backward(heads, head_grads=None, retain_graph=False, train_mode=True): #pylint: disable=redefined-outer-name
    """Compute the gradients of heads w.r.t previously marked variables.

    Parameters
    ----------
    heads: NDArray or list of NDArray
        Output NDArray(s)
    head_grads: NDArray or list of NDArray or None
        Gradients with respect to heads.
    train_mode: bool, optional
        Whether to do backward for training or predicting.
    """
    if isinstance(heads, NDArray):
        assert head_grads is None or isinstance(head_grads, NDArray)
        heads = [heads]
        head_grads = [head_grads] if head_grads is not None else None

    output_handles = []
    for arr in heads:
        output_handles.append(arr.handle)

    if head_grads is None:
        check_call(_LIB.MXAutogradBackwardEx(
            len(output_handles),
            c_array(NDArrayHandle, output_handles),
            ctypes.c_void_p(0),
            ctypes.c_int(retain_graph),
            ctypes.c_int(train_mode)))
        return

    ograd_handles = []
    for arr in head_grads:
        if arr is not None:
            ograd_handles.append(arr.handle)
        else:
            ograd_handles.append(NDArrayHandle(0))
    assert len(ograd_handles) == len(output_handles), \
        "heads and head_grads must have the same length"

    check_call(_LIB.MXAutogradBackwardEx(
        len(output_handles),
        c_array(NDArrayHandle, output_handles),
        c_array(NDArrayHandle, ograd_handles),
        ctypes.c_int(retain_graph),
        ctypes.c_int(train_mode)))


def get_symbol(x):
    """Retrieve recorded computation history as `Symbol`.

    Parameters
    ----------
    x : NDArray
        Array representing the head of computation graph.

    Returns
    -------
    Symbol
        The retrieved Symbol.
    """
    hdl = SymbolHandle()
    check_call(_LIB.MXAutogradGetSymbol(x.handle, ctypes.byref(hdl)))
    return Symbol(hdl)


class Function(object):
    """User-defined differentiable function.

    Function allows defining both forward and backward computation for
    custom operators. During gradient computation, the used-defined
    backward function will be used instead of the default chain-rule.
    You can also cast to numpy array and back for some operations in
    forward and backward.

    For example, a stable sigmoid function can be defined as::

        class sigmoid(Function):
            def forward(self, x):
                y = 1 / (1 + mx.nd.exp(-x))
                self.save_for_backward(y)
                return y

            def backward(self, dy):
                # backward takes as many inputs as forward's return value,
                # and returns as many NDArrays as forward's arguments.
                y, = self.saved_tensors
                return y * (1-y)
    """
    _bwd_functype = CFUNCTYPE(c_int, c_int, c_int, POINTER(c_void_p),
                              POINTER(c_int), c_int, c_void_p)
    _del_functype = CFUNCTYPE(c_int, c_void_p)
    class _Registry(object):
        """CustomOp registry."""
        def __init__(self):
            self.ref_holder = {}
            self.counter = 0
            self.lock = Lock()

        def inc(self):
            """Get index for new entry."""
            self.lock.acquire()
            cur = self.counter
            self.counter += 1
            self.lock.release()
            return cur

    _registry = _Registry()

    def __init__(self):
        self._used = False
        self.saved_tensors = ()

    def save_for_backward(self, *args):
        self.saved_tensors = args

    def __call__(self, *inputs):
        assert not self._used, \
            "Each Function instance can only be called once. "\
            "Please create another instance."
        self._used = True

        prev_recording = set_recording(False)
        outputs = self.forward(*inputs)
        set_recording(prev_recording)

        if not prev_recording:
            return outputs

        ret_outputs = outputs
        if isinstance(outputs, NDArray):
            outputs = (outputs,)

        key = Function._registry.inc()

        def backward_entry(num_ograds, num_igrads, ptrs, reqs, is_train, _):
            """entry point for backward."""
            # pylint: disable=W0613
            try:
                output_grads = [NDArray(ctypes.cast(i, NDArrayHandle), writable=False) \
                                for i in ptrs[:num_ograds]]
                input_grads = [NDArray(ctypes.cast(i, NDArrayHandle), writable=True) \
                               for i in ptrs[num_ograds:num_ograds+num_igrads]]
                reqs = [reqs[i] for i in range(num_igrads)]
                rets = self.backward(*output_grads)
                if isinstance(rets, NDArray):
                    rets = (rets,)
                assert len(rets) == len(input_grads), \
                    "%s.backward must return exactly the same number " \
                    "of NDArrays as the number of NDArrays arguments to forward." \
                    "Expecting %d got %d"%(self.__class__.name, len(input_grads), len(rets))
                for igrad, ret, req in zip(input_grads, rets, reqs):
                    assert isinstance(ret, NDArray), \
                        "autograd.Function.backward must return NDArrays, not %s"%type(ret)
                    if req == 0:  # null
                        return
                    elif req == 1 or req == 2:  # write or inplace
                        igrad[:] = ret
                    elif req == 'add':
                        igrad[:] += ret
            except Exception:  # pylint: disable=broad-except
                print('Error in Function.backward: %s' % traceback.format_exc())
                return False
            return True

        def delete_entry(_):
            """C Callback for CustomFunction::delete"""
            try:
                del Function._registry.ref_holder[key]
            except Exception:  # pylint: disable=broad-except
                print('Error in autograd.Function.delete: %s' % traceback.format_exc())
                return False
            return True

        input_handles = [x.handle for x in inputs]
        output_handles = [x.handle for x in outputs]
        callbacks = [Function._bwd_functype(backward_entry),
                     Function._del_functype(delete_entry)]
        callbacks = [cast(i, CFUNCTYPE(c_int)) for i in callbacks]
        context = MXCallbackList(c_int(len(callbacks)),
                                 cast(c_array(CFUNCTYPE(c_int), callbacks),
                                      POINTER(CFUNCTYPE(c_int))),
                                 cast(c_array(c_void_p, [None]*len(callbacks)),
                                      POINTER(c_void_p)))
        check_call(_LIB.MXCustomFunctionRecord(
            c_int(len(inputs)),
            c_array(NDArrayHandle, input_handles),
            c_int(len(outputs)),
            c_array(NDArrayHandle, output_handles),
            ctypes.byref(context)))

        Function._registry.ref_holder[key] = context

        return ret_outputs

    def forward(self, *inputs):
        """Forward computation."""
        raise NotImplementedError

    def backward(self, *output_grads):
        """Backward computation.

        Takes as many inputs as forward's outputs,
        and returns as many NDArrays as forward's inputs.
        """
        raise NotImplementedError