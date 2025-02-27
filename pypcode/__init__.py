#!/usr/bin/env python
"""
Pythonic interface to SLEIGH by way of the csleigh C API wrapper and CFFI.
"""

import sys
import os.path
import xml.etree.ElementTree as ET
from enum import Enum
from typing import Generator, Sequence, Optional, Mapping, Union, Literal

from ._csleigh import ffi
from ._csleigh.lib import *

# import pretty_traceback
# pretty_traceback.install()
# import sys, traceback

PKG_SRC_DIR = os.path.abspath(os.path.dirname(__file__))
SPECFILES_DIR = os.path.join(PKG_SRC_DIR, 'processors')


def gen_enum(pyname:str, cprefix:str):
  """
  Wrangle prefixed C enum names into a Python enum type, e.g.:

  enum csleigh_OpCode {           --> class OpCode(Enum):
      ...                         -->   ...
      csleigh_CPUI_BOOL_AND = 39, -->   BOOL_AND =39
      ...                         -->   ...
  };                              -->

  Provide destination type name in `pyname` and `cprefix` (e.g. csleigh_CPUI_).

  """
  return Enum(pyname, { n[len(cprefix):]:v
    for n,v in globals().items() if n.startswith(cprefix) })


OpCode = gen_enum('OpCode', 'csleigh_CPUI_')
SleighErrorType = gen_enum('SleighErrorType', 'csleigh_ERROR_TYPE_')


class ArchLanguage:

  __slots__ = (
    "archdir",
    "ldef"
    )

  archdir: str
  ldef: ET.Element

  def __init__(self, archdir:str, ldef:ET.Element):
    self.archdir = archdir
    self.ldef = ldef

  @property
  def pspec_path(self) -> str:
    return os.path.join(self.archdir, self.processorspec)

  @property
  def slafile_path(self) -> str:
    return os.path.join(self.archdir, self.slafile)

  @property
  def description(self) -> str:
    return self.ldef.find('description').text

  def __getattr__(self, key):
    if key in self.ldef.attrib:
      return self.ldef.attrib[key]
    raise AttributeError(key)

  def init_context_from_pspec(self, ctx:'csleigh_Context') -> None:
    pspec = ET.parse(self.pspec_path)
    cd = pspec.getroot().find('context_data')
    if cd is None: return
    cs = cd.find('context_set')
    if cs is None: return
    for e in cs:
      assert(e.tag == 'set')
      csleigh_setVariableDefault(ctx, e.attrib['name'].encode('utf-8'), int(e.attrib['val']))


class Arch:

  __slots__ = (
    "archpath",
    "archname",
    "ldefpath",
    "ldef",
    "languages",
    )

  archpath: str
  archname: str
  ldefpath: str
  ldef: ET.ElementTree
  languages: Sequence[ArchLanguage]

  def __init__(self, archname:str, ldefpath:str):
    self.archpath = os.path.dirname(ldefpath)
    self.archname = archname
    self.ldefpath = ldefpath
    self.ldef = ET.parse(ldefpath)
    self.languages = [ArchLanguage(self.archpath, e) for e in self.ldef.getroot()]

  @classmethod
  def enumerate(cls) -> Generator['Arch', None, None]:
    """
    Enumerate all available architectures and languages.

    Language definitions are sourced from definitions shipped with pypcode and
    can be found in processors/<architecture>/data/languages/<variant>.ldefs
    """
    for archname in os.listdir(SPECFILES_DIR):
      langdir = os.path.join(SPECFILES_DIR, archname, 'data', 'languages')
      if not (os.path.exists(langdir) and os.path.isdir(langdir)): continue
      for langname in os.listdir(langdir):
        if not langname.endswith('.ldefs'): continue
        ldefpath = os.path.join(langdir, langname)
        yield Arch(archname, ldefpath)


class Context:

  __slots__ = (
    'lang',
    'ctx_c',
    'spaces',
    '_cached_addr_spaces',
    )

  lang: ArchLanguage
  ctx_c: 'csleigh_Context'
  _cached_addr_spaces: Mapping['csleigh_AddrSpace', 'AddrSpace']

  def __init__(self, lang:ArchLanguage):
    self._cached_addr_spaces = {}
    self.lang = lang
    self.ctx_c = csleigh_createContext(self.lang.slafile_path.encode('utf-8'))
    self.lang.init_context_from_pspec(self.ctx_c)
    self.spaces = {}
    num_spaces = csleigh_AddrSpaceManager_numSpaces(self.ctx_c)
    for i in range(num_spaces):
      space = AddrSpace.from_c(self, csleigh_AddrSpaceManager_getSpace(self.ctx_c, i))
      self.spaces[space.name] = space

  def __del__(self):
    csleigh_destroyContext(self.ctx_c)

  def get_cached_addr_space(self, cobj:'csleigh_AddrSpace') -> 'AddrSpace':
    """
    Used during translation to cache unchanging address space objects. Should
    not be called by pypcode users.
    """
    return self._cached_addr_spaces.get(cobj)

  def set_cached_addr_space(self, cobj:'csleigh_AddrSpace', pobj:'AddrSpace') -> None:
    """
    Used during translation to cache unchanging address space objects. Should
    not be called by pypcode users.
    """
    self._cached_addr_spaces[cobj] = pobj

  def translate(self, code:Union[bytes, bytearray, memoryview], base:int, max_inst:int = 0, max_bytes:int = 0, bb_terminating:bool = False, bb_nonlinear_terminating:bool = False) -> 'TranslationResult':
    """
    Disassemble and translate to p-code.

    :param code:           Buffer of machine code to translate
    :param base:           Base address of the machine code
    :param max_inst:       Maximum number of instructions to translate, or 0 for no limit
    :param max_bytes:      Maximum number of bytes to translate, or 0 for no limit
    :param bb_terminating: End translation at basic block boundaries
    """
    c_data = ffi.from_buffer(code)
    max_bytes = min(len(code), max_bytes) if (max_bytes > 0) else len(code)
    res_c = csleigh_translate(self.ctx_c, c_data, max_bytes, base, max_inst, bb_terminating, bb_nonlinear_terminating)
    res = TranslationResult.from_c(self, res_c)
    csleigh_freeResult(res_c)
    if res.error is None:
      for inst in res.instructions:
        if inst.length_delay:
          buf_off = inst.address.offset + inst.length - base
          binst = self.translate(code[buf_off:], base + buf_off, max_bytes=inst.length_delay)
          assert binst.error is None
          inst.delayslot_instructions = binst.instructions
    return res

  def get_register_name(self, space:'AddrSpace', offset:int, size:int) -> str:
    """
    Call SleighBase::getRegisterName in this context.
    """
    return ffi.string(csleigh_Sleigh_getRegisterName(
      self.ctx_c, space.to_c(), offset, size)).decode('utf-8')

  def get_register(self, name:str) -> 'Varnode':
    cres = csleigh_Sleigh_getRegister(self.ctx_c, name.encode('utf-8'))
    return Varnode.from_c(self, cres)

  def get_register_names(self) -> [str]:
    res = []
    cres = csleigh_Translate_getAllRegisterNames(self.ctx_c)
    cres = ffi.gc(cres, csleigh_free)
    p = cres
    while p[0]:
      sp = ffi.gc(p[0], csleigh_free)
      res.append(ffi.string(sp).decode('utf-8'))
      p += 1
    return res


class ContextObj:

  __slots__ = (
    'ctx',
    )

  ctx: Context

  def __init__(self, ctx:Context):
    self.ctx = ctx


class AddrSpace(ContextObj):

  __slots__ = (
    'cobj',
    'name',
    )

  cobj: 'csleigh_AddrSpace'
  name: str

  def __init__(self, ctx:Context, cobj:'csleigh_AddrSpace'):
    super().__init__(ctx)
    self.cobj = cobj
    self.name = ffi.string(csleigh_AddrSpace_getName(cobj)).decode('utf-8')

  @classmethod
  def from_c_uncached(cls, ctx:Context, cobj:'csleigh_AddrSpace') -> 'AddrSpace':
    return cls(ctx, cobj)

  @classmethod
  def from_c(cls, ctx:Context, cobj:'csleigh_AddrSpace') -> 'AddrSpace':
    obj = ctx.get_cached_addr_space(cobj)
    if obj is None:
      obj = cls.from_c_uncached(ctx, cobj)
      ctx.set_cached_addr_space(cobj, obj)
    return obj

  def to_c(self) -> 'csleigh_AddrSpace':
    return self.cobj

  @property
  def endianness(self) -> Literal['big', 'little']:
    return 'big' if csleigh_AddrSpace_isBigEndian(self.to_c()) else 'little'


class Address(ContextObj):

  __slots__ = (
    'space',
    'offset',
    )

  space: AddrSpace
  offset: int

  def __init__(self, ctx:Context, space:AddrSpace, offset:int):
    super().__init__(ctx)
    self.space = space
    self.offset = offset

  @classmethod
  def from_c(cls, ctx:Context, cobj:'csleigh_Address') -> 'Address':
    return cls(ctx, AddrSpace.from_c(ctx, cobj.space), cobj.offset)

  def to_c(self) -> 'csleigh_Address':
    # XXX: The boxing in AddrSpace/Address is a little excessive, but lets us
    # access the API as provided by SLEIGH, and should still be fast.
    cobj = ffi.new('csleigh_Address *')
    cobj.space = self.space.to_c()
    cobj.offset = self.offset
    return cobj

  @property
  def is_constant(self) -> bool:
    """
    Return True if this address is in the constant space.
    """
    return csleigh_Addr_isConstant(self.to_c())

  def get_space_from_const(self) -> AddrSpace:
    """
    For LOAD/STORE, the address space which is being accessed is encoded as
    a pointer stored in a constant space offset. See Address::getSpaceFromConst.
    """
    return AddrSpace.from_c(self.ctx, csleigh_Addr_getSpaceFromConst(self.to_c()))

  @property
  def endianness(self) -> Literal['big', 'little']:
    return 'big' if csleigh_AddrSpace_isBigEndian(self.space.to_c()) else 'little'


class Varnode(ContextObj):

  __slots__ = (
    'space',
    'offset',
    'size',
    )

  space: AddrSpace
  offset: int
  size: int

  def __init__(self, ctx:Context, space:AddrSpace, offset:int, size:int):
    super().__init__(ctx)
    self.space = space
    self.offset = offset
    self.size = size

  @classmethod
  def from_c(cls, ctx:Context, cobj:'csleigh_Varnode') -> 'Varnode':
    res = cls(ctx, AddrSpace.from_c(ctx, cobj.space), cobj.offset, cobj.size)
    # if res.offset in (0x7E80, 0x8100,):
    #   print(f"Varnode.from_c(ctx: {cobj}) = space: {res.space} off: {res.offset:#x} sz: {res.size}")
    #   traceback.print_stack()
    return res

  def __str__(self):
    return '%s[%#x:%d]' % (self.space.name, self.offset, self.size)

  def __hash__(self) -> int:
    return hash((self.space.name, self.offset, self.size))

  def get_addr(self) -> Address:
    return Address(self.ctx, self.space, self.offset)

  def get_register_name(self) -> str:
    """
    Convenience function to call SleighBase::getRegisterName with current
    Varnode attributes.
    """
    return self.ctx.get_register_name(self.space, self.offset, self.size)

  def get_space_from_const(self) -> AddrSpace:
    """
    Convenience function to call Address::getSpaceFromConst with current Varnode
    attributes.
    """
    return self.get_addr().get_space_from_const()


class SeqNum(ContextObj):

  __slots__ = (
    'pc',
    'uniq',
    'order',
    )

  pc: Address
  uniq: int
  order: int

  def __init__(self, ctx:Context, pc:Address, uniq:int, order:int):
    super().__init__(ctx)
    self.pc = pc
    self.uniq = uniq
    self.order = order

  @classmethod
  def from_c(cls, ctx:Context, cobj:'csleigh_SeqNum') -> 'SeqNum':
    return cls(ctx, Address.from_c(ctx, cobj.pc), cobj.uniq, cobj.order)


class PcodeOp(ContextObj):

  __slots__ = (
    'seq',
    'opcode',
    'output',
    'inputs',
    'da', 'd',
    'aa', 'a',
    'ba', 'b',
    )

  seq: SeqNum
  opcode: OpCode
  output: Optional[Varnode]
  inputs: Sequence[Varnode]
  da: Optional[Varnode]
  d: Optional
  aa: Optional[Varnode]
  a: Optional
  ba: Optional[Varnode]
  b: Optional

  def __init__(self, ctx:Context, seq:int, opcode:OpCode, inputs:Sequence[Varnode], output:Optional[Varnode] = None):
    super().__init__(ctx)
    self.seq = seq
    self.opcode = opcode
    self.output = output
    self.inputs = inputs

  @classmethod
  def from_c(cls, ctx:Context, cobj:'csleigh_PcodeOp') -> 'PcodeOp':
    return cls(ctx, SeqNum.from_c(ctx, cobj.seq), OpCode(cobj.opcode),
      [Varnode.from_c(ctx, cobj.inputs[i]) for i in range(cobj.inputs_count)],
      Varnode.from_c(ctx, cobj.output) if cobj.output else None)

  @property
  def address(self) -> int:
    return self.seq.pc.offset
  

  def __str__(self):
    s = ''
    if self.output:
      s += str(self.output) + ' = '
    s += f'{self.opcode.name} {", ".join(map(str, self.inputs))}'
    return s


class OpFormat:

  @staticmethod
  def fmt_vn(vn:Varnode) -> str:
    if vn.space.name == 'const':
      return '%#x' % vn.offset
    elif vn.space.name == 'register':
      name = vn.get_register_name()
      if name:
        return name
    return str(vn)

  def fmt(self, op:PcodeOp) -> str:
    return f'{op.opcode.name} {", ".join(map(self.fmt_vn, op.inputs))}'


class OpFormatUnary(OpFormat):

  __slots__ = (
    'operator',
    )

  def __init__(self, operator:str):
    super().__init__()
    self.operator = operator

  def fmt(self, op:PcodeOp) -> str:
    return f'{self.operator}{self.fmt_vn(op.inputs[0])}'


class OpFormatBinary(OpFormat):

  __slots__ = (
    'operator',
    )

  def __init__(self, operator:str):
    super().__init__()
    self.operator = operator

  def fmt(self, op:PcodeOp) -> str:
    return f'{self.fmt_vn(op.inputs[0])} {self.operator} {self.fmt_vn(op.inputs[1])}'


class OpFormatFunc(OpFormat):

  __slots__ = (
    'operator',
    )

  def __init__(self, operator:str):
    super().__init__()
    self.operator = operator

  def fmt(self, op:PcodeOp) -> str:
    return f'{self.operator}({", ".join(map(self.fmt_vn, op.inputs))})'


class OpFormatSpecial(OpFormat):

  def fmt_BRANCH(self, op:PcodeOp) -> str:
    return f'goto {self.fmt_vn(op.inputs[0])}'

  def fmt_BRANCHIND(self, op:PcodeOp) -> str:
    return f'goto [{self.fmt_vn(op.inputs[0])}]'

  def fmt_CALL(self, op:PcodeOp) -> str:
    return f'call {self.fmt_vn(op.inputs[0])}'

  def fmt_CALLIND(self, op:PcodeOp) -> str:
    return f'call [{self.fmt_vn(op.inputs[0])}]'

  def fmt_CBRANCH(self, op:PcodeOp) -> str:
    return f'if ({self.fmt_vn(op.inputs[1])}) goto {self.fmt_vn(op.inputs[0])}'

  def fmt_LOAD(self, op:PcodeOp) -> str:
    return f'*[{op.inputs[0].get_space_from_const().name}]{self.fmt_vn(op.inputs[1])}'

  def fmt_RETURN(self, op:PcodeOp) -> str:
    return f'return {self.fmt_vn(op.inputs[0])}'

  def fmt_STORE(self, op:PcodeOp) -> str:
    return f'*[{op.inputs[0].get_space_from_const().name}]{self.fmt_vn(op.inputs[1])} = {op.inputs[2]}'

  def fmt(self, op:PcodeOp) -> str:
    return {
      OpCode.BRANCH:    self.fmt_BRANCH,
      OpCode.BRANCHIND: self.fmt_BRANCHIND,
      OpCode.CALL:      self.fmt_CALL,
      OpCode.CALLIND:   self.fmt_CALLIND,
      OpCode.CBRANCH:   self.fmt_CBRANCH,
      OpCode.LOAD:      self.fmt_LOAD,
      OpCode.RETURN:    self.fmt_RETURN,
      OpCode.STORE:     self.fmt_STORE,
    }.get(op.opcode)(op)


class PcodePrettyPrinter:

  DEFAULT_OP_FORMAT = OpFormat()

  OP_FORMATS = {
    OpCode.BOOL_AND          : OpFormatBinary('&&'),
    OpCode.BOOL_NEGATE       : OpFormatUnary('!'),
    OpCode.BOOL_OR           : OpFormatBinary('||'),
    OpCode.BOOL_XOR          : OpFormatBinary('^^'),
    OpCode.BRANCH            : OpFormatSpecial(),
    OpCode.BRANCHIND         : OpFormatSpecial(),
    OpCode.CALL              : OpFormatSpecial(),
    OpCode.CALLIND           : OpFormatSpecial(),
    OpCode.CBRANCH           : OpFormatSpecial(),
    OpCode.COPY              : OpFormatUnary(''),
    OpCode.CPOOLREF          : OpFormatFunc('cpool'),
    OpCode.FLOAT_ABS         : OpFormatFunc('abs'),
    OpCode.FLOAT_ADD         : OpFormatBinary('f+'),
    OpCode.FLOAT_CEIL        : OpFormatFunc('ceil'),
    OpCode.FLOAT_DIV         : OpFormatBinary('f/'),
    OpCode.FLOAT_EQUAL       : OpFormatBinary('f=='),
    OpCode.FLOAT_FLOAT2FLOAT : OpFormatFunc('float2float'),
    OpCode.FLOAT_FLOOR       : OpFormatFunc('floor'),
    OpCode.FLOAT_INT2FLOAT   : OpFormatFunc('int2float'),
    OpCode.FLOAT_LESS        : OpFormatBinary('f<'),
    OpCode.FLOAT_LESSEQUAL   : OpFormatBinary('f<='),
    OpCode.FLOAT_MULT        : OpFormatBinary('f*'),
    OpCode.FLOAT_NAN         : OpFormatFunc('nan'),
    OpCode.FLOAT_NEG         : OpFormatUnary('f- '),
    OpCode.FLOAT_NOTEQUAL    : OpFormatBinary('f!='),
    OpCode.FLOAT_ROUND       : OpFormatFunc('round'),
    OpCode.FLOAT_SQRT        : OpFormatFunc('sqrt'),
    OpCode.FLOAT_SUB         : OpFormatBinary('f-'),
    OpCode.FLOAT_TRUNC       : OpFormatFunc('trunc'),
    OpCode.INT_2COMP         : OpFormatUnary('-'),
    OpCode.INT_ADD           : OpFormatBinary('+'),
    OpCode.INT_AND           : OpFormatBinary('&'),
    OpCode.INT_CARRY         : OpFormatFunc('carry'),
    OpCode.INT_DIV           : OpFormatBinary('/'),
    OpCode.INT_EQUAL         : OpFormatBinary('=='),
    OpCode.INT_LEFT          : OpFormatBinary('<<'),
    OpCode.INT_LESS          : OpFormatBinary('<'),
    OpCode.INT_LESSEQUAL     : OpFormatBinary('<='),
    OpCode.INT_MULT          : OpFormatBinary('*'),
    OpCode.INT_NEGATE        : OpFormatUnary('~'),
    OpCode.INT_NOTEQUAL      : OpFormatBinary('!='),
    OpCode.INT_OR            : OpFormatBinary('|'),
    OpCode.INT_REM           : OpFormatBinary('%'),
    OpCode.INT_RIGHT         : OpFormatBinary('>>'),
    OpCode.INT_SBORROW       : OpFormatFunc('sborrow'),
    OpCode.INT_SCARRY        : OpFormatFunc('scarry'),
    OpCode.INT_SDIV          : OpFormatBinary('s/'),
    OpCode.INT_SEXT          : OpFormatFunc('sext'),
    OpCode.INT_SLESS         : OpFormatBinary('s<'),
    OpCode.INT_SLESSEQUAL    : OpFormatBinary('s<='),
    OpCode.INT_SREM          : OpFormatBinary('s%'),
    OpCode.INT_SRIGHT        : OpFormatBinary('s>>'),
    OpCode.INT_SUB           : OpFormatBinary('-'),
    OpCode.INT_XOR           : OpFormatBinary('^'),
    OpCode.INT_ZEXT          : OpFormatFunc('zext'),
    OpCode.LOAD              : OpFormatSpecial(),
    OpCode.NEW               : OpFormatFunc('newobject'),
    OpCode.POPCOUNT          : OpFormatFunc('popcount'),
    OpCode.RETURN            : OpFormatSpecial(),
    OpCode.STORE             : OpFormatSpecial(),
  }

  @classmethod
  def fmt_op(cls, op:PcodeOp) -> str:
    fmt = cls.OP_FORMATS.get(op.opcode, cls.DEFAULT_OP_FORMAT)
    return (f'{fmt.fmt_vn(op.output)} = ' if op.output else '') + fmt.fmt(op)


class Translation(ContextObj):

  __slots__ = (
    'address',
    'length',
    'length_delay',
    'delayslot_instructions',
    'asm_mnem',
    'asm_body',
    'ops',
    )

  address: Address
  length: int
  length_delay: int
  delayslot_instructions: Sequence['Translation']
  asm_mnem: str
  asm_body: str
  ops: Sequence[PcodeOp]

  def __init__(self, ctx:Context, address:Address, length:int, length_delay:int, asm_mnem:str, asm_body:str, ops:Sequence[PcodeOp]):
    super().__init__(ctx)
    self.address = address
    self.length = length
    self.length_delay = length_delay
    self.asm_mnem = asm_mnem
    self.asm_body = asm_body
    self.ops = ops
    self.delayslot_instructions = []

  @classmethod
  def from_c(cls, ctx:Context, cobj:'csleigh_Translation') -> 'Translation':
    addr = Address.from_c(ctx, cobj.address)
    ops = []
    for i in range(cobj.ops_count):
      cop = cobj.ops[i]
      op = PcodeOp.from_c(ctx, cop)
      ops.append(op)
    return cls(ctx, addr,
               cobj.length,
               cobj.length_delay,
               ffi.string(cobj.asm_mnem).decode('utf-8'),
               ffi.string(cobj.asm_body).decode('utf-8'),
               ops)


class SleighError(Exception):

  __slots__ = (
    'ctx',
    )

  ctx: Context

  def __init__(self, ctx:Context, explain:str):
    super().__init__(explain)
    self.ctx = ctx


class UnimplError(SleighError):

  __slots__ = (
    'explain',
    'address',
    'instruction_length',
    )

  explain: str
  address: Address
  instruction_length: int

  def __init__(self, ctx:Context, explain:str, address:Address, instruction_length:int):
    super().__init__(ctx, explain)
    self.explain = explain
    self.address = address
    self.instruction_length = instruction_length

  @classmethod
  def from_c(cls, ctx:Context, cobj:'csleigh_Error') -> 'UnimplError':
    assert(SleighErrorType(cobj.type) == SleighErrorType.UNIMPL)
    return cls(
      ctx,
      ffi.string(cobj.explain).decode('utf-8'),
      Address.from_c(ctx, cobj.unimpl.address),
      cobj.unimpl.instruction_length
      )


class BadDataError(SleighError):

  __slots__ = (
    'explain',
    'address',
    )

  explain: str
  address: Address

  def __init__(self, ctx:Context, explain:str, address:Address):
    super().__init__(ctx, explain)
    self.explain = explain
    self.address = address

  @classmethod
  def from_c(cls, ctx:Context, cobj:'csleigh_Error') -> 'BadDataError':
    assert(SleighErrorType(cobj.type) == SleighErrorType.BADDATA)
    return cls(
      ctx,
      ffi.string(cobj.explain).decode('utf-8'),
      Address.from_c(ctx, cobj.unimpl.address)
      )


class SleighErrorFactory:
  @classmethod
  def from_c(cls, ctx:Context, cobj:'csleigh_Error') -> Union[None, UnimplError, BadDataError]:
    t = SleighErrorType(cobj.type)
    if t == SleighErrorType.NOERROR:
      return None
    else:
      return {
        SleighErrorType.UNIMPL: UnimplError,
        SleighErrorType.BADDATA: BadDataError,
      }[t].from_c(ctx, cobj)


class TranslationResult(ContextObj):

  __slots__ = (
    'instructions',
    'error',
    )

  instructions: Sequence[Translation]
  error: SleighError

  def __init__(self, ctx:Context, instructions:Sequence[Translation], error:Optional[SleighError] = None):
    super().__init__(ctx)
    self.instructions = instructions
    self.error = error

  @classmethod
  def from_c(cls, ctx:Context, cobj:'csleigh_TranslationResult') -> 'TranslationResult':
    insts = []
    for i in range(cobj.instructions_count):
      cinst = cobj.instructions[i]
      inst = Translation.from_c(ctx, cinst)
      insts.append(inst)
    return cls(ctx,
      insts,
      SleighErrorFactory.from_c(ctx, cobj.error)
      )
