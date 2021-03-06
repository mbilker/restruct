import os
from io import BytesIO
import math
import types
import struct
import enum
import collections
import itertools
from contextlib import contextmanager

from typing import Generic as G, Union, TypeVar, Any, Callable, Sequence, Mapping, Optional as O


## Helpers

def indent(s: str, count: int, start: bool = False) -> str:
    """ Indent all lines of a string. """
    lines = s.splitlines()
    for i in range(0 if start else 1, len(lines)):
        lines[i] = ' ' * count + lines[i]
    return '\n'.join(lines)

def format_value(value: Any, formatter: Callable[[Any], str], indentation: int = 0) -> str:
    """ Format containers to use the given formatter function instead of always repr(). """
    if isinstance(value, (dict, collections.Mapping)):
        if value:
            fmt = '{{\n{}\n}}'
            values = [indent(',\n'.join('{}: {}'.format(
                format_value(k, formatter),
                format_value(v, formatter)
            ) for k, v in value.items()), 2, True)]
        else:
            fmt = '{{}}'
            values = []
    elif isinstance(value, (list, set, frozenset)):
        l = len(value)
        is_set = isinstance(value, (set, frozenset))
        if l > 3:
            fmt = '{{\n{}\n}}' if is_set else '[\n{}\n]'
            values = [indent(',\n'.join(format_value(v, formatter) for v in value), 2, True)]
        elif l > 0:
            fmt = '{{{}}}' if is_set else '[{}]'
            values = [','.join(format_value(v, formatter) for v in value)]
        else:
            fmt = '{{}}' if is_set else '[]'
            values = []
    elif isinstance(value, (bytes, bytearray)):
        fmt = '{}'
        values = [format_bytes(value)]
    else:
        fmt = '{}'
        values = [formatter(value)]
    return indent(fmt.format(*values), indentation)

def format_bytes(bs: bytes) -> str:
    return '[' + ' '.join(hex(b)[2:].zfill(2) for b in bs) + ']'

def format_path(path: Sequence[str]) -> str:
    s = ''
    first = True
    for p in path:
        sep = '.'
        if isinstance(p, int):
            p = '[' + str(p) + ']'
            sep = ''
        if sep and not first:
            s += sep
        s += p
        first = False
    return s

def class_name(s: Any, module_whitelist: Sequence[str] = {'__main__', 'builtins', __name__}) -> str:
    module = s.__class__.__module__
    name = s.__class__.__qualname__
    if module in module_whitelist:
        return name
    return module + '.' + name

def friendly_name(s: Any) -> str:
    if hasattr(s, '__name__'):
        return s.__name__
    return str(s)


## Bases 

class IO:
    __slots__ = ()

    seekable = False
    readable = False
    writable = False

    def seek(self, n: int, whence: int = os.SEEK_SET):
        raise NotImplemented

    def read(self, n: int) -> bytes:
        raise NotImplemented

    def write(self, b: bytes):
        raise NotImplemented

class Context:
    __slots__ = ('root', 'value', 'path', 'user', 'size')

    def __init__(self, root, value=None):
        self.root = root
        self.value = value
        self.path = []
        self.user = types.SimpleNamespace()
        self.size = None

    @contextmanager
    def enter(self, name, parser):
        self.path.append((name, parser))
        yield
        self.path.pop()

    @contextmanager
    def add_ref(self, size):
        if self.size is None:
            self.size = sizeof(self.root, self.value)
        offset = self.size
        yield offset
        self.size += size

    def format_path(self):
        return format_path(name for name, parser in self.path)

class Error(Exception):
    __slots__ = ('path',)

    def __init__(self, context: Context, exception: Exception) -> None:
        path = '.'.join(str(p) for p, _ in context.path) if context.path else ''
        if not isinstance(exception, Exception):
            exception = ValueError(exception)
        super().__init__('{}{}: {}'.format(
            ('[' + path + '] ') if path else '', class_name(exception), str(exception)
        ))
        self.exception = exception
        self.path = context.path.copy()

class Type:
    __slots__ = ()

    def parse(self, io: IO, context: Context) -> Any:
        raise NotImplemented

    def emit(self, value: Any, io: IO, context: Context) -> None:
        raise NotImplemented

    def sizeof(self, value: O[Any], context: Context) -> O[int]:
        return None


## Type helpers

T = TypeVar('T', bound=Type)
T2 = TypeVar('T2', bound=Type)


## Base types

class Nothing(Type):
    __slots__ = ()

    def parse(self, io: IO, context: Context) -> None:
        return None

    def emit(self, value: None, io: IO, context: Context) -> None:
        pass

    def sizeof(self, value: O[None], context: Context) -> O[int]:
        return 0
    
    def __repr__(self):
        return '<{}>'.format(class_name(self))

class Implied(Type):
    __slots__ = ('value',)

    def __init__(self, value: Any):
        self.value = value

    def parse(self, io: IO, context: Context) -> Any:
        return to_value(self.value, context)

    def emit(self, value: Any, io: IO, context: Context) -> None:
        pass

    def sizeof(self, value: O[Any], context: Context) -> int:
        return 0

    def __repr__(self) -> str:
        return '<{}({!r})>'.format(class_name(self), self.value)

class Fixed(Type):
    __slots__ = ('pattern',)

    def __init__(self, pattern: bytes) -> None:
        self.pattern = pattern

    def parse(self, io: IO, context: Context) -> bytes:
        data = io.read(len(self.pattern))
        if data != self.pattern:
            raise Error(context, 'Fixed mismatch!\n  wanted: {}\n  found:  {}'.format(
                format_bytes(self.pattern), format_bytes(data)
            ))
        return data

    def emit(self, value: bytes, io: IO, context: Context) -> None:
        io.write(value)

    def sizeof(self, value: O[bytes], context: Context) -> O[int]:
        return len(self.pattern)

    def __repr__(self) -> str:
        return '<{}({})>'.format(class_name(self), self.value)

class Pad(Type):
    __slots__ = ('size', 'value',)

    def __init__(self, size: O[int], value: bytes = b'\x00') -> None:
        self.size = size
        self.value = value

    def parse(self, io: IO, context: Context) -> None:
        io.read(self.size)

    def emit(self, value: None, io: IO, context: Context) -> None:
        value = to_value(self.value, context)
        value *= to_value(self.size, context) // len(value)
        value += value[:self.size - len(value)]
        io.write(value)

    def sizeof(self, value: O[None], context: Context) -> O[int]:
        return to_value(self.size, context)

    def __repr__(self) -> str:
        return '<{}({}){})>'.format(
            class_name(self), self.size,
            format_bytes(self.value) if isinstance(self.value, bytes) else self.value,
        )

class Data(Type):
    __slots__ = ('size',)

    def __init__(self, size: O[int]) -> None:
        self.size = size

    def parse(self, io: IO, context: Context) -> bytes:
        if self.size is None:
            size = -1
        else:
            size = self.size
        data = io.read(size)
        if size >= 0 and len(data) != size:
            raise Error(context, 'Size mismatch!\n  wanted {} bytes\n  found  {} bytes'.format(
                size, len(data)
            ))
        return data

    def emit(self, value: bytes, io: IO, context: Context) -> None:
        io.write(value)

    def sizeof(self, value: O[bytes], context: Context) -> O[int]:
        if value is not None:
            return len(value)
        if self.size is not None:
            return self.size
        return None

    def __repr__(self) -> str:
        return '<{}{}>'.format(
            class_name(self),
            ('[' + str(self.size) + ']') if self.size is not None else '',
        )

E_co = TypeVar('E_co', bound=enum.Enum)

class Enum(Type, G[E_co, T]):
    __slots__ = ('type', 'cls', 'exhaustive')

    def __init__(self, cls: E_co, type: T, exhaustive: bool = True):
        self.type = type
        self.cls = cls
        self.exhaustive = exhaustive

    def parse(self, io: IO, context: Context) -> Union[E_co, T]:
        value = parse(self.type, io, context)
        try:
            return self.cls(value)
        except ValueError:
            if self.exhaustive:
                raise
            return value

    def emit(self, value: Union[E_co, T], io: IO, context: Context) -> None:
        if isinstance(value, self.cls):
            value = value.value
        return emit(self.type, value, io, context)

    def sizeof(self, value: O[Union[E_co, T]], context: Context):
        if value is not None and isinstance(value, self.cls):
            value = value.value
        return sizeof(self.type, value, context)

    def __repr__(self):
        return '<{}: {}>'.format(class_name(self), self.type)



## Modifier types

@contextmanager
def seeking(fd, pos, whence=os.SEEK_SET):
    oldpos = fd.tell()
    fd.seek(pos, whence)
    try:
        yield fd
    finally:
        fd.seek(oldpos, os.SEEK_SET)

class AtOffset(Type, G[T]):
    __slots__ = ('type', 'point', 'reference')

    def __init__(self, type: T, point: O[int] = None, reference: int = os.SEEK_SET) -> None:
        self.type = type
        self.point = point
        self.reference = reference

    def parse(self, io: IO, context: Context) -> T:
        point = to_value(self.point, context)

        with seeking(io, point, self.reference) as f:
            return parse(self.type, f, context)

    def emit(self, value: T, io: IO, context: Context) -> None:
        point = to_value(self.point, context)

        with seeking(io, point, self.reference) as f:
            return emit(self.type, value, f, context)

    def sizeof(self, value: O[T], context: Context) -> O[int]:
        return 0 # sizeof(self.type, value, context)

    def __repr__(self):
        return '<~{!r}({}{})>'.format(
            self.type, {os.SEEK_SET: '', os.SEEK_CUR: '+', os.SEEK_END: '-'}[self.reference], self.point,
        )

class Ref(Type, G[T, T2]):
    __slots__ = ('value_type', 'offset_type', 'reference')

    def __init__(self, value_type: T, offset_type: T2, reference: int = os.SEEK_SET) -> None:
        self.value_type = value_type
        self.offset_type = offset_type
        self.reference = reference

    def parse(self, io: IO, context: Context) -> T:
        offset = parse(self.offset_type, io, context)
        return parse(AtOffset(self.value_type, offset, reference), io, context)

    def emit(self, value: T, io: IO, context: Context) -> None:
        raise NotImplemented

    def sizeof(self, value: O[T], context: Context) -> O[int]:
        return sizeof(self.value_type, value, context)

    def __repr__(self):
        return '<~{!r}({}{!r})>'.format(
            self.value_type, {os.SEEK_SET: '', os.SEEK_CUR: '+', os.SEEK_END: '-'}[self.reference], self.offset_type,
        )

class SizedFile:
    def __init__(self, file: IO, limit: int, exact: bool = False):
        self._file = file
        self._pos = 0
        self._limit = limit
        self._start = file.tell()

    def read(self, n: int = -1) -> bytes:
        remaining = self._limit - self._pos
        if n < 0:
            n = remaining
        n = min(n, remaining)
        self._pos += n
        return self._file.read(n)

    def write(self, data: bytes) -> None:
        remaining = self._limit - self._pos
        if len(data) > remaining:
            raise ValueError('trying to write past limit by {} bytes'.format(len(data) - remaining))
        self._pos += len(data)
        return self._file.write(data)

    def seek(self, offset: int, whence: int) -> None:
        if whence == os.SEEK_SET:
            pos = offset
        elif whence == os.SEEK_CUR:
            pos = self._start + self._pos + offset
        elif whence == os.SEEK_SET:
            pos = self._start + self._limit - offset
        if pos < self._start:
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), offset)
        self._pos = pos - self._start
        return self._file.seek(pos, os.SEEK_SET)

    def tell(self) -> int:
        return self._start + self._pos

    def __getattr__(self, n):
        return getattr(self._file, n)

class WithSize(Type, G[T]):
    __slots__ = ('type', 'limit', 'exact')

    def __init__(self, type: Type, limit: O[int] = None, exact: bool = False) -> None:
        self.type = type
        self.limit = limit
        self.exact = exact

    def parse(self, io: IO, context: Context) -> T:
        start = io.tell()
        limit = to_value(self.limit, context)
        capped = SizedFile(io, limit)
        value = parse(self.type, capped, context)
        if self.exact:
            io.seek(start + limit, os.SEEK_SET)
        return value

    def emit(self, value: T, io: IO, context: Context):
        start = io.tell()
        limit = to_value(self.limit, context)
        capped = SizedFile(io, limit)
        ret = emit(self.type, value, capped, context)
        if self.exact:
            output.seek(start + limit, os.SEEK_SET)
        return ret

    def sizeof(self, value: O[T], context: Context) -> O[int]:
        limit = to_value(self.limit, context)
        if self.exact:
            return limit
        size = sizeof(self.child, value, context)
        if size is None:
            return limit
        if limit is None:
            return size
        return min(size, limit)

    def __repr__(self):
        return '<{}: {!r} (limit={})>'.format(class_name(self), self.type, self.limit)

class AlignTo(Type, G[T]):
    __slots__ = ('type', 'alignment', 'value')

    def __init__(self, type: T, alignment: int, value: bytes = b'\x00') -> None:
        self.type = type
        self.alignment = alignment
        self.value = value

    def parse(self, io: IO, context: Context) -> T:
        value = parse(self.type, io, context)
        adjustment = io.tell() % self.alignment
        if adjustment:
            io.seek(self.alignment - adjustment, os.SEEK_CUR)
        return value

    def emit(self, value: T, io: IO, context: Context) -> None:
        emit(self.type, value, io, context)
        adjustment = io.tell() % self.alignment
        if adjustment:
            io.write(self.value * (self.alignment - adjustment))

    def sizeof(self, value: O[T], context: Context) -> O[int]:
        return None # TODO

    def __repr__(self):
        return '<{}: {!r} (n={})>'.format(class_name(self), self.type, self.alignment)

class AlignedTo(Type, G[T]):
    __slots__ = ('child', 'alignment', 'value')

    def __init__(self, child: T, alignment: int, value: bytes = b'\x00') -> None:
        self.child = child
        self.alignment = alignment
        self.value = value

    def parse(self, io: IO, context: Context) -> T:
        adjustment = io.tell() % self.alignment
        if adjustment:
            io.seek(self.alignment - adjustment, os.SEEK_CUR)
        value = parse(self.child, io, context)
        return value

    def emit(self, value: T, io: IO, context: Context) -> None:
        adjustment = io.tell() % self.alignment
        if adjustment:
            io.write(self.value * (self.alignment - adjustment))
        emit(self.child, value, io, context)

    def sizeof(self, value: O[T], context: Context) -> O[int]:
        return None # TODO

    def __repr__(self):
        return '<{}: {!r} (n={})>'.format(class_name(self), self.child, self.alignment)

class LazyEntry(G[T]):
    __slots__ = ('type', 'io', 'pos', 'context', 'parsed')

    def __init__(self, type: T, io: IO, context: Context) -> None:
        self.type = type
        self.io = io
        self.pos = self.io.tell()
        self.context = context
        self.parsed: O[T] = None

    def __call__(self) -> None:
        if self.parsed is None:
            with seeking(self.io, self.pos) as f:
                self.parsed = parse(self.type, f, self.context)
        return self.parsed

    def __str__(self):
        return '~~{}'.format(self.type)

    def __repr__(self):
        return '<{}: {!r}>'.format(class_name(self), self.type)

class Lazy(Type, G[T]):
    __slots__ = ('type', 'size')

    def __init__(self, type: T, size: O[int] = None) -> None:
        self.type = type
        self.size = size

    def parse(self, io: IO, context: Context) -> T:
        size = self.sizeof(None, context)
        if size is None:
            raise ValueError('lazy type size must be known at parse-time')
        entry = LazyEntry(to_parser(self.type), io, context)
        io.seek(size, os.SEEK_CUR)
        return entry
    
    def emit(self, value: O[T], io: IO, context: Context) -> None:
        emit(self.type, value(), io, context)

    def sizeof(self, value: O[T], context: Context) -> O[int]:
        length = to_value(self.length, context)
        if length is not None:
            return length
        if value is not None:
            value = value()
        return sizeof(self.type, value, context)

    def __str__(self) -> str:
        return '~{}'.format(self.child)

    def __repr__(self) -> str:
        return '<{}: {!r}>'.format(class_name(self), self.child)

class Processed(Type, G[T]):
    __slots__ = ('type', 'do_parse', 'do_emit')

    def __init__(self, type: T, parse: Callable[[T], T2], emit: Callable[[T2], T]) -> None:
        self.type = type
        self.do_parse = parse
        self.do_emit = emit

    def parse(self, io: IO, context: Context) -> T2:
        value = parse(self.type, io, context)
        return self.do_parse(value)

    def emit(self, value: T2, io: IO, context: Context) -> None:
        emit(self.type, self.do_emit(value), io, context)

    def sizeof(self, value: O[T2], context: Context) -> O[int]:
        if value is not None:
            value = self.do_emit(value)
        return sizeof(self.type, value, context)

    def __repr__(self) -> str:
        return '<λ{!r} ->{} <-{}>'.format(
            self.type, self.do_parse.__name__, self.do_emit.__name__
        )

class Mapped(Type, G[T]):
    def __new__(self, type: T, mapping: Mapping[T, T2], default: O[Any] = None) -> Processed:
        reverse = {v: k for k, v in mapping.items()}
        if default is not None:
            mapping = collections.defaultdict(lambda: default, mapping)
            reverse = collections.defaultdict(lambda: default, reverse)
        return Processed(type, mapping.__getitem__, reverse.__getitem__)


## Compound types

class Generic(Type):
    __slots__ = ('stack',)

    def __init__(self) -> None:
        self.stack = []

    def resolve(self, value) -> None:
        if isinstance(value, Generic):
            self.stack.append(value.stack[-1])
        else:
            self.stack.append(value)

    def pop(self) -> None:
        self.stack.pop()

    def __get_restruct_type__(self, ident: Any) -> Type:
        return to_type(self.stack[-1])

    def parse(self, io: IO, context: Context) -> Any:
        if not self.stack:
            raise Error(context, 'unresolved generic')
        return parse(self.stack[-1], io, context)

    def emit(self, value: O[Any], io: IO, context: Context) -> None:
        if not self.stack:
            raise Error(context, 'unresolved generic')
        return emit(self.stack[-1], value, io, context)

    def sizeof(self, value: O[Any], context: Context) -> O[int]:
        if not self.stack:
            return None
        return sizeof(self.stack[-1], value, context)

    def to_value(self):
        return self.stack[-1]

    def __repr__(self):
        if self.stack:
            return '<{} @ 0x{:x}: {!r}>'.format(class_name(self), id(self), self.stack[-1])
        return '<{} @ 0x{:x}: unresolved>'.format(class_name(self), id(self))

    def __deepcopy__(self, memo):
        return self

class MetaSpec(collections.OrderedDict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            raise AttributeError(item)

    def __setattr__(self, item, value):
        if '__' in item:
            super().__setattr__(item, value)
        else:
            self[item] = value

class StructType(Type):
    __slots__ = ('fields', 'cls', 'generics', 'union', 'partial', 'bound')

    def __init__(self, fields: Mapping[str, Type], cls: type, generics: Sequence[Generic] = [], union: bool = False, partial: bool = False, bound: Sequence[Any] = []) -> None:
        self.fields = MetaSpec(fields)
        self.cls = cls
        self.generics = generics
        self.union = union
        self.partial = partial
        self.bound = bound or []

    def __getitem__(self, ty):
        if not isinstance(ty, tuple):
            ty = (ty,)

        bound = self.bound[:]
        bound.extend(ty)
        if len(bound) > len(self.generics):
            raise TypeError('too many generics arguments for {}: {}'.format(
                self.__class__.__name__, len(bound)
            ))
        return self.__class__(self.fields, self.cls, self.generics, self.union, self.partial, bound=bound)

    def parse(self, io: IO, context: Context) -> Any:
        n = 0
        pos = io.tell()

        for g, child in zip(self.generics, self.bound):
            g.resolve(child)

        c = self.cls()
        try:
            for name, type in self.fields.items():
                with context.enter(name, type):
                    if type is None:
                        continue
                    if self.union:
                        io.seek(pos, os.SEEK_SET)

                    val = parse(type, io, context)

                    nbytes = io.tell() - pos
                    if self.union:
                        n = max(n, nbytes)
                    else:
                        n = nbytes

                    setattr(c, name, val)
                    hook = 'on_' + name
                    if hasattr(c, hook):
                        getattr(c, hook)(self.fields, context)
        except Exception as e:
            # Check EOF and allow if partial.
            b = io.read(1)
            if not self.partial or b:
                if b:
                    io.seek(-1, os.SEEK_CUR)
                raise
            # allow EOF if partial

        for g in self.generics:
            g.pop()

        io.seek(pos + n, os.SEEK_SET)
        return c

    def emit(self, value: Any, io: IO, context: Context) -> None:
        n = 0
        pos = io.tell()

        for g, child in zip(self.generics, self.bound):
            g.resolve(child)
    
        for name, type in self.fields.items():
            with context.enter(name, type):
                if self.union:
                    io.seek(pos, os.SEEK_SET)

                field = getattr(value, name)
                emit(type, field, io, context)

                nbytes = io.tell() - pos
                if self.union:
                    n = max(n, nbytes)
                else:
                    n = nbytes

                hook = 'on_' + name
                if hasattr(value, hook):
                    getattr(value, hook)(self.fields, context)

        for g in self.generics:
            g.pop()

        io.seek(pos + n, os.SEEK_SET)

    def sizeof(self, value: O[Any], context: Context) -> O[int]:
        n = 0

        for g, child in zip(self.generics, self.bound):
            g.resolve(child)

        for name, type in self.fields.items():
            with context.enter(name, type):
                if value:
                    field = getattr(value, name)
                else:
                    field = None

                nbytes = sizeof(type, field, context)
                if nbytes is None:
                    n = None
                    break

                if self.union:
                    n = max(n, nbytes)
                else:
                    n += nbytes

        return n

class MetaStruct(type):
    @classmethod
    def __prepare__(mcls, name: str, bases: Sequence[Any], generics: Sequence[str] = [], inject: bool = True, **kwargs) -> dict:
        attrs = collections.OrderedDict()
        attrs.update({g: Generic() for g in generics})
        if inject:
            attrs.update({n: globals()[n] for n in __all__})
        return attrs

    def __new__(mcls, name: str, bases: Sequence[Any], attrs: Mapping[str, Any], inject: bool = True, generics: Sequence[str] = [], **kwargs) -> Any:
        if inject:
            for n in __all__:
                del attrs[n]
        
        # Inherit some properties from base types
        gs = []
        bound = []
        fields = {}
        for b in bases:
            fields.update(getattr(b, '__annotations__', {}))
            type = b.__restruct_type__
            gs.extend(type.generics)
            bound.extend(type.bound)
            if type.union:
                kwargs['union'] = True

        fields.update(attrs.get('__annotations__', {}))
        for g in generics:
            gs.append(attrs.pop(g))

        attrs['__slots__'] = attrs.get('__slots__', ()) + tuple(fields)

        c = super().__new__(mcls, name, bases, attrs)
        type = StructType(fields, c, gs, bound=bound, **kwargs)
        c.__restruct_type__ = type
        return c

    def __init__(cls, *args, **kwargs) -> Any:
        return super().__init__(*args)

    def __getitem__(cls, ty) -> Any:
        if not isinstance(ty, tuple):
            ty = (ty,)
        subtype = cls.__restruct_type__[ty]
        new_name = '{}[{}]'.format(cls.__name__, ', '.join(friendly_name(r).strip('<>') for r in subtype.bound))
        new = type(new_name, (cls,), cls.__class__.__prepare__(new_name, (cls,)))
        new.__restruct_type__ = subtype
        new.__slots__ = cls.__slots__
        subtype.cls = new
        return new

class Struct(metaclass=MetaStruct, inject=False):
    def __init__(self, **kwargs):
        super().__init__()
        for k in self:
            setattr(self, k, None)
        for k, v in kwargs.items():
            setattr(self, k ,v)

    def __iter__(self):
        return iter(self.__slots__)

    def __hash__(self):
        return hash(tuple((k, getattr(self, k)) for k in self))

    def __eq__(self, other):
        if type(self) != type(other):
            return False
        if self.__slots__ != other.__slots__:
            return False
        for k in self:
            ov = getattr(self, k)
            tv = getattr(other, k)
            if ov != tv:
                return False
        return True

    def __fmt__(self, fieldfunc):
        args = []
        for k in self:
            if k.startswith('_'):
                continue
            val = getattr(self, k)
            val = format_value(val, fieldfunc, 2)
            args.append('  {}: {}'.format(k, val))
        args = ',\n'.join(args)
        # Format final value.
        if args:
            return '{} {{\n{}\n}}'.format(class_name(self), args)
        else:
            return '{} {{}}'.format(class_name(self))

    def __str__(self):
        return self.__fmt__(str)

    def __repr__(self):
        return self.__fmt__(repr)

class Union(Struct, metaclass=MetaStruct, union=True, inject=False):
    pass

class Arr(Type, G[T]):
    __slots__ = ('type', 'count', 'size', 'stop_value')

    def __init__(self, type: T, count: O[int] = None, size: O[int] = None, stop_value: O[Any] = None) -> None:
        self.type = type
        self.count = count
        self.size = size
        self.stop_value = stop_value

    def parse(self, io: IO, context: Context) -> Sequence[T]:
        value = []

        count = to_value(self.count, context)
        size = to_value(self.size, context)
        stop_value = to_value(self.stop_value, context)

        i = 0
        start = io.tell()
        while count is None or i < count:
            if size is not None and io.tell() - start >= size:
                break
            
            if isinstance(self.type, list):
                type = to_type(self.type[i], i)
            else:
                type = to_type(self.type, i)

            with context.enter(i, type):
                try:
                    elem = parse(type, io, context)
                except Exception:
                    # Check EOF.
                    if not io.read(1):
                        break
                    io.seek(-1, os.SEEK_CUR)
                    raise

                if elem == stop_value:
                    break
            
            value.append(elem)
            i += 1

        return value

    def emit(self, value: Sequence[T], io: IO, context: Context) -> None:
        count = to_value(self.count, context)
        size = to_value(self.size, context)
        stop_value = to_value(self.stop_value, context)

        if stop_value is not None:
            value = value + [stop_value]

        start = io.tell()
        for i, elem in enumerate(value):
            if size is not None and io.tell() - start >= size:
                raise ValueError('oversized array, maximum size {}'.format(size))

            if isinstance(self.type, list):
                type = to_type(self.type[i], i)
            else:
                type = to_type(self.type, i)

            with context.enter(i, type):
                emit(type, elem, io, context)

    def sizeof(self, value: O[Sequence[T]], context: Context) -> int:
        count = to_value(self.count, context)
        size = to_value(self.size, context)
        stop_value = to_value(self.stop_value, context)

        if size is not None:
            return size
        if count is None:
            return None

        l = 0
        for i in range(count):
            if isinstance(self.type, list):
                type = to_type(self.type[i], i)
            else:
                type = to_type(self.type, i)
            l += sizeof(type, value[i] if value is not None else None, context)

        if stop_value is not None:
            if isinstance(self.type, list):
                type = to_type(self.type[count], count)
            else:
                type = to_type(self.type, count)
            l += sizeof(type, stop_value, context)

        return l

    def __repr__(self) -> str:
        return '<{}({!r}{}{}{})>'.format(
            class_name(self), self.type,
            ('[' + str(self.count) + ']') if self.count is not None else '',
            (', size: ' + str(self.size)) if self.size is not None else '',
            (', stop: ' + repr(self.stop_value)) if self.stop_value is not None else '',
        )

class Switch(Type):
    __slots__ = ('options', 'selector', 'fallback')

    def __init__(self, default: O[Any] = None, fallback: O[T] = None, options: Mapping[Any, T] = None):
        self.options = options or {}
        self.selector = default
        self.fallback = fallback

    @property
    def current(self) -> T:
        if self.selector is None and not self.fallback:
            raise ValueError('Selector not set!')
        if self.selector not in self.options and not self.fallback:
            raise ValueError('Selector {} is invalid! [options: {}]'.format(
                self.selector, ', '.join(repr(x) for x in self.options.keys())
            ))
        if self.selector is not None and self.selector in self.options:
            return self.options[self.selector]
        return self.fallback

    def parse(self, io: IO, context: Context) -> Any:
        return parse(self.current, io, context)

    def emit(self, value: Any, io: IO, context: Context) -> None:
        return emit(self.current, value, io, context)

    def sizeof(self, value: O[Any], context: Context) -> O[int]:
        return sizeof(self.current, value, context)

    def __repr__(self) -> str:
        return '<{}: {}>'.format(class_name(self), ', '.join(repr(k) + ': ' + repr(v) for k, v in self.options.items()))


## Primitive types

class Int(Type):
    __slots__ = ('bits', 'signed', 'order')

    def __init__(self, bits: int, order: str = 'le', signed: bool = True) -> None:
        self.bits = bits
        self.signed = signed
        self.order = order
    
    def parse(self, io: IO, context: Context) -> int:
        bits = to_value(self.bits, context)
        order = to_value(self.order, context)
        signed = to_value(self.signed, context)
        bs = io.read(bits // 8)
        if len(bs) != bits // 8:
            raise ValueError('short read')
        return int.from_bytes(bs, byteorder='little' if order == 'le' else 'big', signed=signed)

    def emit(self, value: int, io: IO, context: Context) -> None:
        bits = to_value(self.bits, context)
        order = to_value(self.order, context)
        signed = to_value(self.signed, context)
        bs = value.to_bytes(bits // 8, byteorder='little' if order == 'le' else 'big', signed=signed)
        io.write(bs)

    def sizeof(self, value: O[int], context: Context) -> int:
        bits = to_value(self.bits, context)
        return bits // 8

    def __repr__(self) -> str:
        return '<{}{}({}, {})>'.format(
            'U' if not self.signed else '',
            class_name(self), self.bits, self.order
        )

class UInt(Type):
    def __new__(cls, *args, **kwargs):
        return Int(*args, signed=False, **kwargs)

class Bool(Type, G[T]):
    def __new__(self, type: T = UInt(8), true_value: T = 1, false_value: T = 0) -> Mapped:
        return Mapped(type, {true_value: True, false_value: False})

class Float(Type):
    __slots__ = ('bits',)

    FORMATS = {
        32: 'f',
        64: 'd',
    }

    def __init__(self, bits: int = 32) -> None:
        self.bits = bits
        if self.bits not in self.FORMATS:
            raise ValueError('unsupported bit count for float: {}'.format(bits))

    def parse(self, io: IO, context: Context) -> float:
        bits = to_value(self.bits, context)
        bs = io.read(bits // 8)
        return struct.unpack(self.FORMAT[bits], bs)[0]

    def emit(self, value: float, io: IO, context: Context) -> None:
        bits = to_value(self.bits, context)
        bs = struct.pack(self.FORMAT[bits], value)
        io.write(bs)

    def sizeof(self, value: O[int], context: Context) -> int:
        bits = to_value(self.bits, context)
        return to_value(bits, context) // 8

    def __repr__(self) -> str:
        return '<{}({})>'.format(class_name(self), self.bits)

class Str(Type):
    __slots__ = ('length', 'type', 'encoding', 'terminator', 'exact', 'length_type', 'length_unit')

    def __init__(self, length: O[int] = None, type: str = 'c', encoding: str = 'utf-8', terminator: O[bytes] = None, exact: bool = False, length_unit: int = 1, length_type: Type = UInt(8)) -> None:
        self.length = length
        self.type = type
        self.encoding = encoding
        self.terminator = terminator or b'\x00' * length_unit
        self.exact = exact
        self.length_unit = length_unit
        self.length_type = length_type

        if self.type not in ('raw', 'c', 'pascal'):
            raise ValueError('string type must be any of [raw, c, pascal]')

    def parse(self, io: IO, context: Context) -> str:
        length = to_value(self.length, context)
        length_unit = to_value(self.length_unit, context)
        type = to_value(self.type, context)
        exact = to_value(self.exact, context)
        encoding = to_value(self.encoding,  context)
        terminator = to_value(self.terminator, context)

        if type == 'pascal':
            read_length = parse(self.length_type, io, context)
            if length is not None:
                read_length = min(read_length, length)
            raw = io.read(read_length * length_unit)
        elif type in ('raw', 'c'):
            read_length = 0
            raw = bytearray()
            for i in itertools.count(start=1):
                if length is not None and i > length:
                    break
                c = io.read(length_unit)
                read_length += 1
                if not c or (type == 'c' and c == terminator):
                    break
                raw.extend(c)

        if exact and length is not None:
            if read_length > length:
                raise ValueError('exact length specified but read length ({}) > given length ({})'.format(read_length, length))
            left = length - read_length
            if exact and left:
                io.read(left * length_unit)

        return raw.decode(encoding)

    def emit(self, value: str, io: IO, context: Context) -> None:
        length = to_value(self.length, context)
        length_unit = to_value(self.length_unit, context)
        type = to_value(self.type, context)
        exact = to_value(self.exact, context)
        encoding = to_value(self.encoding,  context)
        terminator = to_value(self.terminator, context)

        raw = value.encode(encoding)

        write_length = (len(value) + len(terminator)) // length_unit
        if type == 'pascal':
            emit(self.length_type, write_length, io, context)
            io.write(raw)
        elif type in ('c', 'pascal'):
            io.write(raw)
            if type == 'c':
                io.write(terminator)
        
        if exact and length is not None:
            if write_length > length:
                raise ValueError('exact length specified but write length ({}) > given length ({})'.format(write_length, length))
            left = length - write_length
            if exact and left:
                io.write(b'\x00' * (left * length_unit))

    def sizeof(self, value: O[str], context: Context) -> O[int]:
        length = to_value(self.length, context)
        length_unit = to_value(self.length_unit, context)
        type = to_value(self.type, context)
        exact = to_value(self.exact, context)
        encoding = to_value(self.encoding,  context)
        terminator = to_value(self.terminator, context)

        if exact and length is not None:
            l = length * length_unit
        elif value is not None:
            l = len(value.encode(encoding))
            if type == 'c':
                l += len(terminator)
        else:
            return None

        if type == 'pascal':
            size_len = sizeof(self.length_type, l, context)
            if size_len is None:
                return None
            l += size_len

        return l

    def __repr__(self) -> str:
        return '<{}{}({}{})>'.format(self.type.capitalize(), class_name(self), '=' if self.exact else '', self.length)



## Main functions

def to_io(value: Any) -> IO:
    if value is None:
        return BytesIO()
    if isinstance(value, (bytes, bytearray)):
        return BytesIO(value)
    return value

def to_type(spec: Any, ident: O[Any] = None) -> Type:
    if isinstance(spec, Type):
        return spec
    if isinstance(spec, (list, tuple)):
        return Tuple(spec)
    elif hasattr(spec, '__restruct_type__'):
        return spec.__restruct_type__
    elif hasattr(spec, '__get_restruct_type__'):
        return spec.__get_restruct_type__(ident)
    elif callable(spec):
        return spec(ident)

    raise ValueError('Could not figure out specification from argument {}.'.format(spec))

def to_value(p, context):
    if isinstance(p, Generic):
        return p.to_value()
    return p

def parse(spec: Any, io: IO, context: O[Context] = None) -> Any:
    type = to_type(spec)
    io = to_io(io)
    context = context or Context(type)
    at_start = not context.path
    try:
        return type.parse(io, context)
    except Error:
        raise
    except Exception as e:
        if at_start:
            raise Error(context, e)
        else:
            raise

def emit(spec: Any, value: Any, io: O[IO] = None, context: O[Context] = None) -> None:
    type = to_type(spec)
    io = to_io(io)
    ctx = context or Context(type, value)
    try:
        type.emit(value, io, ctx)
        return io
    except Error:
        raise
    except Exception as e:
        if not context:
            raise Error(ctx, e)
        else:
            raise

def sizeof(spec: Any, value: O[Any] = None, context: O[Context] = None) -> O[int]:
    type = to_type(spec)
    ctx = context or Context(type, value)
    try:
        return type.sizeof(value, ctx)
    except Error:
        raise
    except Exception as e:
        if not context:
            raise Error(ctx, e)
        else:
            raise


__all__ = [c.__name__ for c in {
    # Bases
    IO, Context, Error, Type,
    # Base types
    Nothing, Implied, Fixed, Pad, Data, Enum,
    # Modifier types
    AtOffset, Ref, WithSize, AlignTo, AlignedTo, Lazy, Processed, Mapped,
    # Compound types
    StructType, MetaStruct, Struct, Union, Arr, Switch,
    # Primitive types
    Bool, Int, UInt, Float, Str,
    # Functions
    parse, emit, sizeof,
}]
