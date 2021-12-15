import ast
import enum
import inspect
import re
import builtins
import torch
import warnings
import pdb
from .._jit_internal import List, Tuple, is_tuple, is_list, Dict, is_dict, Optional, \
    is_optional, _qualified_name, Any, Future, is_future, is_ignored_fn, Union, is_union
from .._jit_internal import BroadcastingList1, BroadcastingList2, BroadcastingList3  # type: ignore[attr-defined]
from ._state import _get_script_class

from torch._C import TensorType, TupleType, FloatType, IntType, ComplexType, \
    ListType, StringType, DictType, BoolType, OptionalType, InterfaceType, AnyType, \
    NoneType, DeviceObjType, StreamObjType, FutureType, EnumType, UnionType, NumberType

from textwrap import dedent
from torch._sources import get_source_lines_and_file
from typing import Type

if torch.distributed.rpc.is_available():
    from .._jit_internal import RRef, is_rref
    from torch._C import RRefType
from torch._ops import OpOverloadPacket

class Module(object):
    def __init__(self, name, members):
        self.name = name
        self.members = members

    def __getattr__(self, name):
        try:
            return self.members[name]
        except KeyError:
            raise RuntimeError(f"Module {self.name} has no member called {name}") from None


class EvalEnv(object):
    env = {
        'torch': Module('torch', {'Tensor': torch.Tensor}),
        'Tensor': torch.Tensor,
        'typing': Module('typing', {'Tuple': Tuple}),
        'Tuple': Tuple,
        'List': List,
        'Dict': Dict,
        'Optional': Optional,
        'Union': Union,
        'Future': Future
    }

    def __init__(self, rcb):
        self.rcb = rcb
        if torch.distributed.rpc.is_available():
            self.env['RRef'] = RRef

    def __getitem__(self, name):
        if name in self.env:
            return self.env[name]
        if self.rcb is not None:
            return self.rcb(name)
        return getattr(builtins, name, None)

def get_signature(fn, rcb, loc, is_method):
    if isinstance(fn, OpOverloadPacket):
        signature = try_real_annotations(fn.op, loc)
    else:
        signature = try_real_annotations(fn, loc)
    if signature is not None and is_method:
        # If this is a method, then the signature will include a type for
        # `self`, but type comments do not contain a `self`. So strip it
        # away here so everything is consistent (`inspect.ismethod` does
        # not work here since `fn` is unbound at this point)
        param_types, return_type = signature
        param_types = param_types[1:]
        signature = (param_types, return_type)

    if signature is None:
        type_line, source = None, None
        try:
            source = dedent(''.join(get_source_lines_and_file(fn)[0]))
            type_line = get_type_line(source)
        except TypeError:
            pass
        # This might happen both because we failed to get the source of fn, or
        # because it didn't have any annotations.
        if type_line is not None:
            signature = parse_type_line(type_line, rcb, loc)

    return signature


def is_function_or_method(the_callable):
    # A stricter version of `inspect.isroutine` that does not pass for built-in
    # functions
    return inspect.isfunction(the_callable) or inspect.ismethod(the_callable)


def is_vararg(the_callable):
    if not is_function_or_method(the_callable) and hasattr(the_callable, '__call__'):  # noqa: B004
        # If `the_callable` is a class, de-sugar the call so we can still get
        # the signature
        the_callable = the_callable.__call__

    if is_function_or_method(the_callable):
        return inspect.getfullargspec(the_callable).varargs is not None
    else:
        return False


def get_param_names(fn, n_args):
    if isinstance(fn, OpOverloadPacket):
        fn = fn.default

    if not is_function_or_method(fn) and hasattr(fn, '__call__') and is_function_or_method(fn.__call__):  # noqa: B004
        # De-sugar calls to classes
        fn = fn.__call__
    if is_function_or_method(fn):
        if is_ignored_fn(fn):
            fn = inspect.unwrap(fn)
        return inspect.getfullargspec(fn).args
    else:
        # The `fn` was not a method or function (maybe a class with a __call__
        # method, so use a default param name list)
        return [str(i) for i in range(n_args)]


def check_fn(fn, loc):
    # Make sure the function definition is not a class instantiation
    try:
        source = dedent(''.join(get_source_lines_and_file(fn)[0]))
    except (TypeError, IOError):
        return
    if source is None:
        return

    py_ast = ast.parse(source)
    if len(py_ast.body) == 1 and isinstance(py_ast.body[0], ast.ClassDef):
        raise torch.jit.frontend.FrontendError(
            loc, f"Cannot instantiate class '{py_ast.body[0].name}' in a script function")
    if len(py_ast.body) != 1 or not isinstance(py_ast.body[0], ast.FunctionDef):
        raise torch.jit.frontend.FrontendError(loc, "Expected a single top-level function")


def parse_type_line(type_line, rcb, loc):
    """Parses a type annotation specified as a comment.

    Example inputs:
        # type: (Tensor, torch.Tensor) -> Tuple[Tensor]
        # type: (Tensor, Tuple[Tensor, Tensor]) -> Tensor
    """
    arg_ann_str, ret_ann_str = split_type_line(type_line)

    try:
        arg_ann = eval(arg_ann_str, {}, EvalEnv(rcb))  # type: ignore[arg-type] # noqa: P204
    except (NameError, SyntaxError) as e:
        raise RuntimeError("Failed to parse the argument list of a type annotation") from e

    if not isinstance(arg_ann, tuple):
        arg_ann = (arg_ann,)

    try:
        ret_ann = eval(ret_ann_str, {}, EvalEnv(rcb))  # type: ignore[arg-type] # noqa: P204
    except (NameError, SyntaxError) as e:
        raise RuntimeError("Failed to parse the return type of a type annotation") from e

    arg_types = [ann_to_type(ann, loc) for ann in arg_ann]
    return arg_types, ann_to_type(ret_ann, loc)


def get_type_line(source):
    """Tries to find the line containing a comment with the type annotation."""
    type_comment = '# type:'

    lines = source.split('\n')
    lines = [(line_num, line) for line_num, line in enumerate(lines)]
    type_lines = list(filter(lambda line: type_comment in line[1], lines))
    # `type: ignore` comments may be needed in JIT'ed functions for mypy, due
    # to the hack in torch/_VF.py.

    # An ignore type comment can be of following format:
    #   1) type: ignore
    #   2) type: ignore[rule-code]
    # This ignore statement must be at the end of the line

    # adding an extra backslash before the space, to avoid triggering
    # one of the checks in .github/workflows/lint.yml
    type_pattern = re.compile("# type:\\ ignore(\\[[a-zA-Z-]+\\])?$")
    type_lines = list(filter(lambda line: not type_pattern.search(line[1]),
                             type_lines))

    if len(type_lines) == 0:
        # Catch common typo patterns like extra spaces, typo in 'ignore', etc.
        wrong_type_pattern = re.compile("#[\t ]*type[\t ]*(?!: ignore(\\[.*\\])?$):")
        wrong_type_lines = list(filter(lambda line: wrong_type_pattern.search(line[1]), lines))
        if len(wrong_type_lines) > 0:
            raise RuntimeError("The annotation prefix in line " + str(wrong_type_lines[0][0])
                               + " is probably invalid.\nIt must be '# type:'"
                               + "\nSee PEP 484 (https://www.python.org/dev/peps/pep-0484/#suggested-syntax-for-python-2-7-and-straddling-code)"  # noqa: B950
                               + "\nfor examples")
        return None
    elif len(type_lines) == 1:
        # Only 1 type line, quit now
        return type_lines[0][1].strip()

    # Parse split up argument types according to PEP 484
    # https://www.python.org/dev/peps/pep-0484/#suggested-syntax-for-python-2-7-and-straddling-code
    return_line = None
    parameter_type_lines = []
    for line_num, line in type_lines:
        if '# type: (...) -> ' in line:
            return_line = (line_num, line)
            break
        elif type_comment in line:
            parameter_type_lines.append(line)
    if return_line is None:
        raise RuntimeError(
            "Return type line '# type: (...) -> ...' not found on multiline "
            "type annotation\nfor type lines:\n" +
            '\n'.join([line[1] for line in type_lines]) +
            "\n(See PEP 484 https://www.python.org/dev/peps/pep-0484/#suggested-syntax-for-python-2-7-and-straddling-code)")

    def get_parameter_type(line):
        item_type = line[line.find(type_comment) + len(type_comment):]
        return item_type.strip()

    types = map(get_parameter_type, parameter_type_lines)
    parameter_types = ", ".join(types)

    return return_line[1].replace("...", parameter_types)


def split_type_line(type_line):
    """Splits the comment with the type annotation into parts for argument and return types.

    For example, for an input of:
        # type: (Tensor, torch.Tensor) -> Tuple[Tensor, Tensor]

    This function will return:
        ("(Tensor, torch.Tensor)", "Tuple[Tensor, Tensor]")

    """
    start_offset = len('# type:')
    try:
        arrow_pos = type_line.index('->')
    except ValueError:
        raise RuntimeError("Syntax error in type annotation (cound't find `->`)") from None
    return type_line[start_offset:arrow_pos].strip(), type_line[arrow_pos + 2:].strip()


def try_real_annotations(fn, loc):
    """Tries to use the Py3.5+ annotation syntax to get the type."""
    try:
        # Note: anything annotated as `Optional[T]` will automatically
        # be returned as `Union[T, None]` per
        # https://github.com/python/typing/blob/master/src/typing.py#L850
        sig = inspect.signature(fn)
    except ValueError:
        return None

    all_annots = [sig.return_annotation] + [p.annotation for p in sig.parameters.values()]
    if all(ann is sig.empty for ann in all_annots):
        return None

    arg_types = [ann_to_type(p.annotation, loc)
                 for p in sig.parameters.values()]
    return_type = ann_to_type(sig.return_annotation, loc)
    return arg_types, return_type


# Finds common type for enum values belonging to an Enum class. If not all
# values have the same type, AnyType is returned.
def get_enum_value_type(e: Type[enum.Enum], loc):
    enum_values: List[enum.Enum] = list(e)
    if not enum_values:
        raise ValueError(f"No enum values defined for: '{e.__class__}'")

    types = {type(v.value) for v in enum_values}
    ir_types = [try_ann_to_type(t, loc) for t in types]

    # If Enum values are of different types, an exception will be raised here.
    # Even though Python supports this case, we chose to not implement it to
    # avoid overcomplicate logic here for a rare use case. Please report a
    # feature request if you find it necessary.
    return torch._C.unify_type_list(ir_types)

def is_tensor(ann):
    if issubclass(ann, torch.Tensor):
        return True

    if issubclass(ann, (torch.LongTensor, torch.DoubleTensor, torch.FloatTensor,
                        torch.IntTensor, torch.ShortTensor, torch.HalfTensor,
                        torch.CharTensor, torch.ByteTensor, torch.BoolTensor)):
        warnings.warn("TorchScript will treat type annotations of Tensor "
                      "dtype-specific subtypes as if they are normal Tensors. "
                      "dtype constraints are not enforced in compilation either.")
        return True

    return False



def try_ann_to_type(ann, loc):
    if ann is inspect.Signature.empty:
        return TensorType.getInferred()
    if ann is None:
        return NoneType.get()
    if inspect.isclass(ann) and is_tensor(ann):
        return TensorType.get()
    if is_tuple(ann):
        # Special case for the empty Tuple type annotation `Tuple[()]`
        if len(ann.__args__) == 1 and ann.__args__[0] == ():
            return TupleType([])
        return TupleType([try_ann_to_type(a, loc) for a in ann.__args__])
    if is_list(ann):
        elem_type = try_ann_to_type(ann.__args__[0], loc)
        if elem_type:
            return ListType(elem_type)
    if is_dict(ann):
        key = try_ann_to_type(ann.__args__[0], loc)
        value = try_ann_to_type(ann.__args__[1], loc)
        # Raise error if key or value is None
        if key is None:
            raise ValueError(f"Unknown type annotation: '{ann.__args__[0]}' at {loc.highlight()}")
        if value is None:
            raise ValueError(f"Unknown type annotation: '{ann.__args__[1]}' at {loc.highlight()}")
        return DictType(key, value)
    if is_optional(ann):
        if issubclass(ann.__args__[1], type(None)):
            contained = ann.__args__[0]
        else:
            contained = ann.__args__[1]
        valid_type = try_ann_to_type(contained, loc)
        msg = "Unsupported annotation {} could not be resolved because {} could not be resolved."
        assert valid_type, msg.format(repr(ann), repr(contained))
        return OptionalType(valid_type)
    if is_union(ann):
        # TODO: this is hack to recognize NumberType
        if set(ann.__args__) == set([int, float, complex]):
            return NumberType.get()
        inner: List = []
        # We need these extra checks because both `None` and invalid
        # values will return `None`
        # TODO: Determine if the other cases need to be fixed as well
        for a in ann.__args__:
            if a is None:
                inner.append(NoneType.get())
            maybe_type = try_ann_to_type(a, loc)
            msg = "Unsupported annotation {} could not be resolved because {} could not be resolved."
            assert maybe_type, msg.format(repr(ann), repr(maybe_type))
            inner.append(maybe_type)
        return UnionType(inner)    # type: ignore[arg-type]
    if torch.distributed.rpc.is_available() and is_rref(ann):
        return RRefType(try_ann_to_type(ann.__args__[0], loc))
    if is_future(ann):
        return FutureType(try_ann_to_type(ann.__args__[0], loc))
    if ann is float:
        return FloatType.get()
    if ann is complex:
        return ComplexType.get()
    if ann is int:
        return IntType.get()
    if ann is str:
        return StringType.get()
    if ann is bool:
        return BoolType.get()
    if ann is Any:
        return AnyType.get()
    if ann is type(None):
        return NoneType.get()
    if inspect.isclass(ann) and hasattr(ann, "__torch_script_interface__"):
        return InterfaceType(ann.__torch_script_interface__)
    if ann is torch.device:
        return DeviceObjType.get()
    if ann is torch.Stream:
        return StreamObjType.get()
    if ann is torch.dtype:
        return IntType.get()  # dtype not yet bound in as its own type
    if inspect.isclass(ann) and issubclass(ann, enum.Enum):
        if _get_script_class(ann) is None:
            scripted_class = torch.jit._script._recursive_compile_class(ann, loc)
            name = scripted_class.qualified_name()
        else:
            name = _qualified_name(ann)
        return EnumType(name, get_enum_value_type(ann, loc), list(ann))
    if inspect.isclass(ann):
        maybe_script_class = _get_script_class(ann)
        if maybe_script_class is not None:
            return maybe_script_class
        if torch._jit_internal.can_compile_class(ann):
            return torch.jit._script._recursive_compile_class(ann, loc)

    # Maybe resolve a NamedTuple to a Tuple Type
    def fake_rcb(key):
        return None
    return torch._C._resolve_type_from_object(ann, loc, fake_rcb)


def ann_to_type(ann, loc):
    the_type = try_ann_to_type(ann, loc)
    if the_type is not None:
        return the_type
    raise ValueError(f"Unknown type annotation: '{ann}' at {loc.highlight()}")


__all__ = [
    'Any',
    'List',
    'BroadcastingList1',
    'BroadcastingList2',
    'BroadcastingList3',
    'Tuple',
    'is_tuple',
    'is_list',
    'Dict',
    'is_dict',
    'is_optional',
    'is_union',
    'TensorType',
    'TupleType',
    'FloatType',
    'ComplexType',
    'IntType',
    'ListType',
    'StringType',
    'DictType',
    'AnyType',
    'Module',
    # TODO: Consider not exporting these during wildcard import (reserve
    # that for the types; for idiomatic typing code.)
    'get_signature',
    'check_fn',
    'get_param_names',
    'parse_type_line',
    'get_type_line',
    'split_type_line',
    'try_real_annotations',
    'try_ann_to_type',
    'ann_to_type',
]
