
from pypy.jit.backend import model
from pypy.jit.backend.llgraph import support
from pypy.jit.metainterp.history import Const, getkind, AbstractDescr
from pypy.jit.metainterp.history import INT, REF, FLOAT, VOID
from pypy.jit.metainterp.resoperation import rop
from pypy.jit.codewriter import longlong, heaptracker
from pypy.jit.codewriter.effectinfo import EffectInfo

from pypy.rpython.llinterp import LLInterpreter, LLException
from pypy.rpython.lltypesystem import lltype, llmemory, rffi, rclass, rstr

from pypy.rlib.rarithmetic import ovfcheck, r_uint, r_ulonglong
from pypy.rlib.rtimer import read_timestamp

class LLTrace(object):
    has_been_freed = False
    invalid = False

    def __init__(self, inputargs, operations):
        self.inputargs = inputargs
        self.operations = operations

class GuardFailed(Exception):
    def __init__(self, failargs, descr):
        self.failargs = failargs
        self.descr = descr

class ExecutionFinished(Exception):
    def __init__(self, descr, arg, failargs):
        self.descr = descr
        self.arg = arg
        self.failargs = failargs

class Jump(Exception):
    def __init__(self, descr, args):
        self.descr = descr
        self.args = args

class CallDescr(AbstractDescr):
    def __init__(self, RESULT, ARGS, extrainfo):
        self.RESULT = RESULT
        self.ARGS = ARGS
        self.extrainfo = extrainfo

    def __repr__(self):
        return 'CallDescr(%r, %r, %r)' % (self.RESULT, self.ARGS,
                                          self.extrainfo)

    def get_extra_info(self):
        return self.extrainfo

    def get_arg_types(self):
        return ''.join([getkind(ARG)[0] for ARG in self.ARGS])

    def get_result_type(self):
        return getkind(self.RESULT)[0]

class SizeDescr(AbstractDescr):
    def __init__(self, S):
        self.S = S

    def as_vtable_size_descr(self):
        return self

    def count_fields_if_immutable(self):
        return heaptracker.count_fields_if_immutable(self.S)

    def __repr__(self):
        return 'SizeDescr(%r)' % (self.S,)

class FieldDescr(AbstractDescr):
    def __init__(self, S, fieldname):
        self.S = S
        self.fieldname = fieldname
        self.FIELD = getattr(S, fieldname)

    def __repr__(self):
        return 'FieldDescr(%r, %r)' % (self.S, self.fieldname)

    def sort_key(self):
        return self.fieldname

    def is_pointer_field(self):
        return getkind(self.FIELD) == 'ref'

    def is_float_field(self):
        return getkind(self.FIELD) == 'float'

    def is_field_signed(self):
        return _is_signed_kind(self.FIELD)

def _is_signed_kind(TYPE):
    return (TYPE is not lltype.Bool and isinstance(TYPE, lltype.Number) and
            rffi.cast(TYPE, -1) == -1)

class ArrayDescr(AbstractDescr):
    def __init__(self, A):
        self.A = A

    def __repr__(self):
        return 'ArrayDescr(%r)' % (self.A,)

    def is_array_of_pointers(self):
        return getkind(self.A.OF) == 'ref'

    def is_array_of_floats(self):
        return getkind(self.A.OF) == 'float'

    def is_item_signed(self):
        return _is_signed_kind(self.A.OF)

    def is_array_of_structs(self):
        return isinstance(self.A.OF, lltype.Struct)

class InteriorFieldDescr(AbstractDescr):
    def __init__(self, A, fieldname):
        self.A = A
        self.fieldname = fieldname
        self.FIELD = getattr(A.OF, fieldname)

    def __repr__(self):
        return 'InteriorFieldDescr(%r, %r)' % (self.A, self.fieldname)

    def sort_key(self):
        return self.fieldname

    def is_pointer_field(self):
        return getkind(self.FIELD) == 'ref'

    def is_float_field(self):
        return getkind(self.FIELD) == 'float'

_example_res = {'v': None,
                'r': lltype.nullptr(llmemory.GCREF.TO),
                'i': 0,
                'f': 0.0}

class LLGraphCPU(model.AbstractCPU):
    from pypy.jit.metainterp.typesystem import llhelper as ts
    supports_floats = True
    supports_longlong = r_uint is not r_ulonglong
    supports_singlefloats = True
    translate_support_code = False

    JITFRAMEPTR = llmemory.GCREF

    def __init__(self, rtyper, stats=None, *ignored_args, **ignored_kwds):
        model.AbstractCPU.__init__(self)
        self.rtyper = rtyper
        self.llinterp = LLInterpreter(rtyper)
        self.descrs = {}
        class MiniStats:
            pass
        self.stats = stats or MiniStats()

    def compile_loop(self, inputargs, operations, looptoken, log=True, name=''):
        clt = model.CompiledLoopToken(self, looptoken.number)
        looptoken.compiled_loop_token = clt
        lltrace = LLTrace(inputargs, operations)
        clt._llgraph_loop = lltrace
        clt._llgraph_alltraces = [lltrace]
        self._record_labels(lltrace)

    def compile_bridge(self, faildescr, inputargs, operations,
                       original_loop_token, log=True):
        clt = original_loop_token.compiled_loop_token
        clt.compiling_a_bridge()
        lltrace = LLTrace(inputargs, operations)
        faildescr._llgraph_bridge = lltrace
        clt._llgraph_alltraces.append(lltrace)
        self._record_labels(lltrace)

    def _record_labels(self, lltrace):
        # xxx pfff, we need to clone the list of operations because the
        # front-end will mutate them under our feet again
        # xXX pffffffff2 not enough to make sure things are freed
        lltrace.operations = [op.copy_and_change(op.getopnum())
                              for op in lltrace.operations]
        for i, op in enumerate(lltrace.operations):
            if op.getopnum() == rop.LABEL:
                op.getdescr()._llgraph_target = (lltrace, i)

    def invalidate_loop(self, looptoken):
        for trace in looptoken.compiled_loop_token._llgraph_alltraces:
            trace.invalid = True

    def redirect_call_assembler(self, oldlooptoken, newlooptoken):
        oldtrace = oldlooptoken.compiled_loop_token._llgraph_loop
        newtrace = newlooptoken.compiled_loop_token._llgraph_loop
        OLD = [box.type for box in oldtrace.inputargs]
        NEW = [box.type for box in newtrace.inputargs]
        assert OLD == NEW
        assert not hasattr(oldlooptoken, '_llgraph_redirected')
        oldlooptoken.compiled_loop_token._llgraph_redirected = True
        oldlooptoken.compiled_loop_token._llgraph_loop = newtrace
        alltraces = newlooptoken.compiled_loop_token._llgraph_alltraces
        oldlooptoken.compiled_loop_token._llgraph_alltraces = alltraces

    def free_loop_and_bridges(self, compiled_loop_token):
        for c in compiled_loop_token._llgraph_alltraces:
            c.has_been_freed = True
        compiled_loop_token._llgraph_alltraces = []
        compiled_loop_token._llgraph_loop = None
        model.AbstractCPU.free_loop_and_bridges(self, compiled_loop_token)

    def make_execute_token(self, *argtypes):
        return self._execute_token

    def _execute_token(self, loop_token, *args):
        lltrace = loop_token.compiled_loop_token._llgraph_loop
        frame = LLFrame(self, lltrace.inputargs, args)
        try:
            frame.execute(lltrace)
            assert False
        except ExecutionFinished, e:
            frame.finish_value = e.arg
            frame.latest_values = e.failargs
            frame.latest_descr = e.descr
            frame._execution_finished_normally = e.descr.fast_path_done
            return frame
        except GuardFailed, e:
            frame.latest_values = e.failargs
            frame.latest_descr = e.descr
            return frame

    def get_latest_value_int(self, frame, index):
        return frame.latest_values[index]
    get_latest_value_float = get_latest_value_int
    get_latest_value_ref   = get_latest_value_int

    def get_latest_value_count(self, frame):
        return len(frame.latest_values)

    def get_latest_descr(self, frame):
        return frame.latest_descr

    def get_finish_value_int(self, frame):
        res = frame.finish_value
        del frame.finish_value
        return res
    get_finish_value_float = get_finish_value_int
    get_finish_value_ref   = get_finish_value_int

    def grab_exc_value(self, frame):
        if frame.last_exception is not None:
            result = frame.last_exception.args[1]
            gcref = lltype.cast_opaque_ptr(llmemory.GCREF, result)
        else:
            gcref = lltype.nullptr(llmemory.GCREF.TO)
        frame.last_exception = None
        return gcref

    def force(self, frame):
        assert not frame._forced
        frame._forced = True
        call_op = frame.current_op
        if call_op.getopnum() == rop.FINISH:
            guard_op = call_op
        else:
            guard_op = frame.lltrace.operations[frame.current_index + 1]
        frame.latest_values = frame._getfailargs(guard_op, call_op.result)
        descr = guard_op.getdescr()
        frame.latest_descr = descr
        return descr

    def set_savedata_ref(self, frame, data):
        frame.saved_data = data

    def get_savedata_ref(self, frame):
        return frame.saved_data

    def calldescrof(self, FUNC, ARGS, RESULT, effect_info):
        key = ('call', getkind(RESULT),
               tuple([getkind(A) for A in ARGS]),
               effect_info)
        try:
            return self.descrs[key]
        except KeyError:
            descr = CallDescr(RESULT, ARGS, effect_info)
            self.descrs[key] = descr
            return descr

    def sizeof(self, S):
        key = ('size', S)
        try:
            return self.descrs[key]
        except KeyError:
            descr = SizeDescr(S)
            self.descrs[key] = descr
            return descr

    def fielddescrof(self, S, fieldname):
        key = ('field', S, fieldname)
        try:
            return self.descrs[key]
        except KeyError:
            descr = FieldDescr(S, fieldname)
            self.descrs[key] = descr
            return descr

    def arraydescrof(self, A):
        key = ('array', A)
        try:
            return self.descrs[key]
        except KeyError:
            descr = ArrayDescr(A)
            self.descrs[key] = descr
            return descr

    def interiorfielddescrof(self, A, fieldname):
        key = ('interiorfield', A, fieldname)
        try:
            return self.descrs[key]
        except KeyError:
            descr = InteriorFieldDescr(A, fieldname)
            self.descrs[key] = descr
            return descr        

    def _calldescr_dynamic_for_tests(self, atypes, rtype,
                                     abiname='FFI_DEFAULT_ABI'):
        # XXX WTF is that and why it breaks all abstractions?
        from pypy.jit.backend.llsupport import ffisupport
        return ffisupport.calldescr_dynamic_for_tests(self, atypes, rtype,
                                                      abiname)

    def calldescrof_dynamic(self, cif_description, extrainfo):
        # XXX WTF, this is happy nonsense
        from pypy.jit.backend.llsupport.ffisupport import get_ffi_type_kind
        from pypy.jit.backend.llsupport.ffisupport import UnsupportedKind
        ARGS = []
        try:
            for itp in range(cif_description.nargs):
                arg = cif_description.atypes[itp]
                kind = get_ffi_type_kind(self, arg)
                if kind != VOID:
                    ARGS.append(support.kind2TYPE[kind[0]])
            RESULT = support.kind2TYPE[get_ffi_type_kind(self, cif_description.rtype)[0]]
        except UnsupportedKind:
            return None
        key = ('call_dynamic', RESULT, tuple(ARGS),
               extrainfo, cif_description.abi)
        try:
            return self.descrs[key]
        except KeyError:
            descr = CallDescr(RESULT, ARGS, extrainfo)
            self.descrs[key] = descr
            return descr

    # ------------------------------------------------------------

    def maybe_on_top_of_llinterp(self, func, args, RESULT):
        ptr = llmemory.cast_int_to_adr(func).ptr
        if hasattr(ptr._obj, 'graph'):
            res = self.llinterp.eval_graph(ptr._obj.graph, args)
        else:
            res = ptr._obj._callable(*args)
        return support.cast_result(RESULT, res)

    def _do_call(self, func, args_i, args_r, args_f, calldescr):
        TP = llmemory.cast_int_to_adr(func).ptr._obj._TYPE
        args = support.cast_call_args(TP.ARGS, args_i, args_r, args_f)
        return self.maybe_on_top_of_llinterp(func, args, TP.RESULT)

    bh_call_i = _do_call
    bh_call_r = _do_call
    bh_call_f = _do_call
    bh_call_v = _do_call

    def bh_getfield_gc(self, p, descr):
        p = support.cast_arg(lltype.Ptr(descr.S), p)
        return support.cast_result(descr.FIELD, getattr(p, descr.fieldname))

    bh_getfield_gc_pure = bh_getfield_gc
    bh_getfield_gc_i = bh_getfield_gc
    bh_getfield_gc_r = bh_getfield_gc
    bh_getfield_gc_f = bh_getfield_gc

    bh_getfield_raw = bh_getfield_gc
    bh_getfield_raw_pure = bh_getfield_raw
    bh_getfield_raw_i = bh_getfield_raw
    bh_getfield_raw_r = bh_getfield_raw
    bh_getfield_raw_f = bh_getfield_raw

    def bh_setfield_gc(self, p, newvalue, descr):
        p = support.cast_arg(lltype.Ptr(descr.S), p)
        setattr(p, descr.fieldname, support.cast_arg(descr.FIELD, newvalue))

    bh_setfield_gc_i = bh_setfield_gc
    bh_setfield_gc_r = bh_setfield_gc
    bh_setfield_gc_f = bh_setfield_gc

    bh_setfield_raw   = bh_setfield_gc
    bh_setfield_raw_i = bh_setfield_raw
    bh_setfield_raw_r = bh_setfield_raw
    bh_setfield_raw_f = bh_setfield_raw

    def bh_arraylen_gc(self, a, descr):
        array = a._obj.container
        return array.getlength()

    def bh_getarrayitem_gc(self, a, index, descr):
        a = support.cast_arg(lltype.Ptr(descr.A), a)
        array = a._obj
        return support.cast_result(descr.A.OF, array.getitem(index))

    bh_getarrayitem_gc_pure = bh_getarrayitem_gc
    bh_getarrayitem_gc_i = bh_getarrayitem_gc
    bh_getarrayitem_gc_r = bh_getarrayitem_gc
    bh_getarrayitem_gc_f = bh_getarrayitem_gc

    bh_getarrayitem_raw = bh_getarrayitem_gc
    bh_getarrayitem_raw_pure = bh_getarrayitem_raw
    bh_getarrayitem_raw_i = bh_getarrayitem_raw
    bh_getarrayitem_raw_r = bh_getarrayitem_raw
    bh_getarrayitem_raw_f = bh_getarrayitem_raw

    def bh_setarrayitem_gc(self, a, index, item, descr):
        a = support.cast_arg(lltype.Ptr(descr.A), a)
        array = a._obj
        array.setitem(index, support.cast_arg(descr.A.OF, item))

    bh_setarrayitem_gc_i = bh_setarrayitem_gc
    bh_setarrayitem_gc_r = bh_setarrayitem_gc
    bh_setarrayitem_gc_f = bh_setarrayitem_gc

    bh_setarrayitem_raw   = bh_setarrayitem_gc
    bh_setarrayitem_raw_i = bh_setarrayitem_raw
    bh_setarrayitem_raw_r = bh_setarrayitem_raw
    bh_setarrayitem_raw_f = bh_setarrayitem_raw

    def bh_getinteriorfield_gc(self, a, index, descr):
        array = a._obj.container
        return support.cast_result(descr.FIELD,
                          getattr(array.getitem(index), descr.fieldname))

    bh_getinteriorfield_gc_i = bh_getinteriorfield_gc
    bh_getinteriorfield_gc_r = bh_getinteriorfield_gc
    bh_getinteriorfield_gc_f = bh_getinteriorfield_gc

    def bh_setinteriorfield_gc(self, a, index, item, descr):
        array = a._obj.container
        setattr(array.getitem(index), descr.fieldname,
                support.cast_arg(descr.FIELD, item))

    bh_setinteriorfield_gc_i = bh_setinteriorfield_gc
    bh_setinteriorfield_gc_r = bh_setinteriorfield_gc
    bh_setinteriorfield_gc_f = bh_setinteriorfield_gc

    def bh_raw_load_i(self, struct, offset, descr):
        ll_p = rffi.cast(rffi.CCHARP, struct)
        ll_p = rffi.cast(lltype.Ptr(descr.A), rffi.ptradd(ll_p, offset))
        value = ll_p[0]
        return support.cast_result(descr.A.OF, value)

    def bh_raw_load_f(self, struct, offset, descr):
        ll_p = rffi.cast(rffi.CCHARP, struct)
        ll_p = rffi.cast(rffi.CArrayPtr(longlong.FLOATSTORAGE),
                         rffi.ptradd(ll_p, offset))
        return ll_p[0]

    def bh_raw_load(self, struct, offset, descr):
        if descr.A.OF == lltype.Float:
            return self.bh_raw_load_f(struct, offset, descr)
        else:
            return self.bh_raw_load_i(struct, offset, descr)

    def bh_raw_store_i(self, struct, offset, newvalue, descr):
        ll_p = rffi.cast(rffi.CCHARP, struct)
        ll_p = rffi.cast(lltype.Ptr(descr.A), rffi.ptradd(ll_p, offset))
        ll_p[0] = rffi.cast(descr.A.OF, newvalue)

    def bh_raw_store_f(self, struct, offset, newvalue, descr):
        ll_p = rffi.cast(rffi.CCHARP, struct)
        ll_p = rffi.cast(rffi.CArrayPtr(longlong.FLOATSTORAGE),
                         rffi.ptradd(ll_p, offset))
        ll_p[0] = newvalue

    def bh_raw_store(self, struct, offset, newvalue, descr):
        if descr.A.OF == lltype.Float:
            self.bh_raw_store_f(struct, offset, newvalue, descr)
        else:
            self.bh_raw_store_i(struct, offset, newvalue, descr)

    def bh_newstr(self, length):
        return lltype.cast_opaque_ptr(llmemory.GCREF,
                                      lltype.malloc(rstr.STR, length,
                                                    zero=True))

    def bh_strlen(self, s):
        return s._obj.container.chars.getlength()

    def bh_strgetitem(self, s, item):
        return ord(s._obj.container.chars.getitem(item))

    def bh_strsetitem(self, s, item, v):
        s._obj.container.chars.setitem(item, chr(v))

    def bh_copystrcontent(self, src, dst, srcstart, dststart, length):
        src = lltype.cast_opaque_ptr(lltype.Ptr(rstr.STR), src)
        dst = lltype.cast_opaque_ptr(lltype.Ptr(rstr.STR), dst)
        assert 0 <= srcstart <= srcstart + length <= len(src.chars)
        assert 0 <= dststart <= dststart + length <= len(dst.chars)
        rstr.copy_string_contents(src, dst, srcstart, dststart, length)

    def bh_newunicode(self, length):
        return lltype.cast_opaque_ptr(llmemory.GCREF,
                                      lltype.malloc(rstr.UNICODE, length,
                                                    zero=True))

    def bh_unicodelen(self, string):
        return string._obj.container.chars.getlength()

    def bh_unicodegetitem(self, string, index):
        return ord(string._obj.container.chars.getitem(index))

    def bh_unicodesetitem(self, string, index, newvalue):
        string._obj.container.chars.setitem(index, unichr(newvalue))

    def bh_copyunicodecontent(self, src, dst, srcstart, dststart, length):
        src = lltype.cast_opaque_ptr(lltype.Ptr(rstr.UNICODE), src)
        dst = lltype.cast_opaque_ptr(lltype.Ptr(rstr.UNICODE), dst)
        assert 0 <= srcstart <= srcstart + length <= len(src.chars)
        assert 0 <= dststart <= dststart + length <= len(dst.chars)
        rstr.copy_unicode_contents(src, dst, srcstart, dststart, length)

    def bh_new(self, sizedescr):
        return lltype.cast_opaque_ptr(llmemory.GCREF,
                                      lltype.malloc(sizedescr.S, zero=True))

    def bh_new_with_vtable(self, vtable, descr):
        result = lltype.malloc(descr.S, zero=True)
        result_as_objptr = lltype.cast_pointer(rclass.OBJECTPTR, result)
        result_as_objptr.typeptr = support.cast_from_int(rclass.CLASSTYPE,
                                                         vtable)
        return lltype.cast_opaque_ptr(llmemory.GCREF, result)

    def bh_new_array(self, length, arraydescr):
        array = lltype.malloc(arraydescr.A, length, zero=True)
        return lltype.cast_opaque_ptr(llmemory.GCREF, array)

    def bh_classof(self, struct):
        struct = lltype.cast_opaque_ptr(rclass.OBJECTPTR, struct)
        result_adr = llmemory.cast_ptr_to_adr(struct.typeptr)
        return heaptracker.adr2int(result_adr)

    def bh_read_timestamp(self):
        return read_timestamp()

class LLFrame(object):
    _TYPE = llmemory.GCREF

    # some obscure hacks to support comparison with llmemory.GCREF
    def __ne__(self, other):
        return not self == other
    def __eq__(self, other):
        return isinstance(other, LLFrame) and self is other
    
    _forced = False
    _execution_finished_normally = False
    finish_value = None
    
    def __init__(self, cpu, argboxes, args):
        self.env = {}
        self.cpu = cpu
        assert len(argboxes) == len(args)
        for box, arg in zip(argboxes, args):
            self.env[box] = arg
        self.overflow_flag = False
        self.last_exception = None

    def lookup(self, arg):
        if isinstance(arg, Const):
            return arg.value
        return self.env[arg]

    def execute(self, lltrace):
        self.lltrace = lltrace
        del lltrace
        i = 0
        while True:
            assert not self.lltrace.has_been_freed
            op = self.lltrace.operations[i]
            if op.getopnum() == -124:      # force_spill, for tests
                i += 1
                continue
            args = [self.lookup(arg) for arg in op.getarglist()]
            self.current_op = op # for label
            self.current_index = i
            try:
                resval = getattr(self, 'execute_' + op.getopname())(op.getdescr(),
                                                                    *args)
            except Jump, j:
                self.lltrace, i = j.descr._llgraph_target
                label_op = self.lltrace.operations[i]
                self.do_renaming(label_op.getarglist(), j.args)
                i += 1
                continue
            except GuardFailed, gf:
                if hasattr(gf.descr, '_llgraph_bridge'):
                    i = 0
                    self.lltrace = gf.descr._llgraph_bridge
                    newargs = [self.env[arg] for arg in
                               self.current_op.getfailargs() if arg is not None]
                    self.do_renaming(self.lltrace.inputargs, newargs)
                    continue
                raise
            if op.result is not None:
                # typecheck the result
                if op.result.type == INT:
                    if isinstance(resval, bool):
                        resval = int(resval)
                    assert lltype.typeOf(resval) == lltype.Signed
                elif op.result.type == REF:
                    assert lltype.typeOf(resval) == llmemory.GCREF
                elif op.result.type == FLOAT:
                    assert lltype.typeOf(resval) == longlong.FLOATSTORAGE
                else:
                    raise AssertionError(op.result.type)
                #
                self.env[op.result] = resval
            else:
                assert resval is None
            i += 1

    def _getfailargs(self, op=None, skip=None):
        if op is None:
            op = self.current_op
        r = []
        for arg in op.getfailargs():
            if arg is None:
                r.append(None)
            elif arg is skip:
                r.append(_example_res[skip.type])
            else:
                r.append(self.env[arg])
        return r

    def do_renaming(self, newargs, oldargs):
        assert len(newargs) == len(oldargs)
        newenv = {}
        for new, old in zip(newargs, oldargs):
            newenv[new] = old
        self.env = newenv

    # -----------------------------------------------------

    def fail_guard(self, descr):
        raise GuardFailed(self._getfailargs(), descr)

    def execute_finish(self, descr, arg=None):
        if self.current_op.getfailargs() is not None:
            failargs = self._getfailargs()
        else:
            failargs = None   # compatibility
        raise ExecutionFinished(descr, arg, failargs)

    def execute_label(self, descr, *args):
        argboxes = self.current_op.getarglist()
        self.do_renaming(argboxes, args)

    def execute_guard_true(self, descr, arg):
        if not arg:
            self.fail_guard(descr)

    def execute_guard_false(self, descr, arg):
        if arg:
            self.fail_guard(descr)

    def execute_guard_value(self, descr, arg1, arg2):
        if arg1 != arg2:
            self.fail_guard(descr)

    def execute_guard_nonnull(self, descr, arg):
        if not arg:
            self.fail_guard(descr)

    def execute_guard_isnull(self, descr, arg):
        if arg:
            self.fail_guard(descr)

    def execute_guard_class(self, descr, arg, klass):
        value = lltype.cast_opaque_ptr(rclass.OBJECTPTR, arg)
        expected_class = llmemory.cast_adr_to_ptr(
            llmemory.cast_int_to_adr(klass),
            rclass.CLASSTYPE)
        if value.typeptr != expected_class:
            self.fail_guard(descr)

    def execute_guard_nonnull_class(self, descr, arg, klass):
        self.execute_guard_nonnull(descr, arg)
        self.execute_guard_class(descr, arg, klass)

    def execute_guard_no_exception(self, descr):
        if self.last_exception is not None:
            self.fail_guard(descr)

    def execute_guard_exception(self, descr, excklass):
        lle = self.last_exception
        if lle is None:
            gotklass = lltype.nullptr(rclass.CLASSTYPE.TO)
        else:
            gotklass = lle.args[0]
        excklass = llmemory.cast_adr_to_ptr(
            llmemory.cast_int_to_adr(excklass),
            rclass.CLASSTYPE)
        if gotklass != excklass:
            self.fail_guard(descr)
        #
        res = lle.args[1]
        self.last_exception = None
        return support.cast_to_ptr(res)

    def execute_guard_not_forced(self, descr):
        if self._forced:
            self.fail_guard(descr)

    def execute_guard_not_invalidated(self, descr):
        if self.lltrace.invalid:
            self.fail_guard(descr)

    def execute_int_add_ovf(self, _, x, y):
        try:
            z = ovfcheck(x + y)
        except OverflowError:
            ovf = True
            z = 0
        else:
            ovf = False
        self.overflow_flag = ovf
        return z

    def execute_int_sub_ovf(self, _, x, y):
        try:
            z = ovfcheck(x - y)
        except OverflowError:
            ovf = True
            z = 0
        else:
            ovf = False
        self.overflow_flag = ovf
        return z

    def execute_int_mul_ovf(self, _, x, y):
        try:
            z = ovfcheck(x * y)
        except OverflowError:
            ovf = True
            z = 0
        else:
            ovf = False
        self.overflow_flag = ovf
        return z        

    def execute_guard_no_overflow(self, descr):
        if self.overflow_flag:
            self.fail_guard(descr)

    def execute_guard_overflow(self, descr):
        if not self.overflow_flag:
            self.fail_guard(descr)

    def execute_jump(self, descr, *args):
        assert descr is not None
        raise Jump(descr, args)

    def _do_math_sqrt(self, value):
        import math
        y = support.cast_from_floatstorage(lltype.Float, value)
        x = math.sqrt(y)
        return support.cast_to_floatstorage(x)

    def execute_call(self, calldescr, func, *args):
        effectinfo = calldescr.get_extra_info()
        if effectinfo is not None and hasattr(effectinfo, 'oopspecindex'):
            oopspecindex = effectinfo.oopspecindex
            if oopspecindex == EffectInfo.OS_MATH_SQRT:
                return self._do_math_sqrt(args[0])
        TP = llmemory.cast_int_to_adr(func).ptr._obj._TYPE
        call_args = support.cast_call_args_in_order(TP.ARGS, args)
        try:
            res = self.cpu.maybe_on_top_of_llinterp(func, call_args, TP.RESULT)
            self.last_exception = None
        except LLException, lle:
            self.last_exception = lle
            res = _example_res[getkind(TP.RESULT)[0]]
        return res

    execute_call_may_force = execute_call

    def execute_call_release_gil(self, descr, func, *args):
        call_args = support.cast_call_args_in_order(descr.ARGS, args)
        FUNC = lltype.FuncType(descr.ARGS, descr.RESULT)
        func_to_call = rffi.cast(lltype.Ptr(FUNC), func)
        result = func_to_call(*call_args)
        return support.cast_result(descr.RESULT, result)

    def execute_call_assembler(self, descr, *args):
        frame = self.cpu._execute_token(descr, *args)
        if frame._execution_finished_normally:    # fast path
            return frame.finish_value
        jd = descr.outermost_jitdriver_sd
        assembler_helper_ptr = jd.assembler_helper_adr.ptr  # fish
        try:
            result = assembler_helper_ptr(frame)
        except LLException, lle:
            assert self.last_exception is None, "exception left behind"
            self.last_exception = lle
            if self.current_op.result is not None:
                return _example_res[self.current_op.result.type]
            return None
        return support.cast_result(lltype.typeOf(result), result)

    def execute_same_as(self, _, x):
        return x

    def execute_debug_merge_point(self, descr, *args):
        from pypy.jit.metainterp.warmspot import get_stats
        try:
            stats = get_stats()
        except AttributeError:
            pass
        else:
            stats.add_merge_point_location(args[1:])

    def execute_new_with_vtable(self, _, vtable):
        descr = heaptracker.vtable2descr(self.cpu, vtable)
        return self.cpu.bh_new_with_vtable(vtable, descr)

    def execute_jit_frame(self, _):
        return self

def _setup():
    def _make_impl_from_blackhole_interp(opname):
        from pypy.jit.metainterp.blackhole import BlackholeInterpreter
        name = 'bhimpl_' + opname.lower()
        try:
            func = BlackholeInterpreter.__dict__[name]
        except KeyError:
            return
        for argtype in func.argtypes:
            if argtype not in ('i', 'r', 'f'):
                return
        #
        def _op_default_implementation(self, descr, *args):
            # for all operations implemented in the blackhole interpreter
            return func(*args)
        #
        _op_default_implementation.func_name = 'execute_' + opname
        return _op_default_implementation

    def _new_execute(opname):
        def execute(self, descr, *args):
            if descr is not None:
                new_args = args + (descr,)
            else:
                new_args = args
            return getattr(self.cpu, 'bh_' + opname)(*new_args)
        execute.func_name = 'execute_' + opname
        return execute

    for k, v in rop.__dict__.iteritems():
        if not k.startswith("_"):
            fname = 'execute_' + k.lower()
            if not hasattr(LLFrame, fname):
                func = _make_impl_from_blackhole_interp(k)
                if func is None:
                    func = _new_execute(k.lower())
                setattr(LLFrame, fname, func)

_setup()
