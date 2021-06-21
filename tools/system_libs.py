# Copyright 2014 The Emscripten Authors.  All rights reserved.
# Emscripten is available under two separate licenses, the MIT license and the
# University of Illinois/NCSA Open Source License.  Both these licenses can be
# found in the LICENSE file.

import glob
import hashlib
import itertools
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from glob import iglob

from . import shared, building, ports, config, utils
from . import deps_info, tempfiles
from . import diagnostics
from tools.shared import mangle_c_symbol_name, demangle_c_symbol_name

logger = logging.getLogger('system_libs')

# Files that are part of libsockets.a and so should be excluded from libc.a
LIBC_SOCKETS = ['socket.c', 'socketpair.c', 'shutdown.c', 'bind.c', 'connect.c',
                'listen.c', 'accept.c', 'getsockname.c', 'getpeername.c', 'send.c',
                'recv.c', 'sendto.c', 'recvfrom.c', 'sendmsg.c', 'recvmsg.c',
                'getsockopt.c', 'setsockopt.c', 'freeaddrinfo.c',
                'in6addr_any.c', 'in6addr_loopback.c']


def files_in_path(path_components, filenames):
  srcdir = shared.path_from_root(*path_components)
  return [os.path.join(srcdir, f) for f in filenames]


def glob_in_path(path_components, glob_pattern, excludes=()):
  srcdir = shared.path_from_root(*path_components)
  files = iglob(os.path.join(srcdir, glob_pattern), recursive=True)
  return [f for f in files if os.path.basename(f) not in excludes]


def get_all_files_under(dirname):
  for path, subdirs, files in os.walk(dirname):
    for name in files:
      yield os.path.join(path, name)


def dir_is_newer(dir_a, dir_b):
  assert os.path.exists(dir_a)
  assert os.path.exists(dir_b)
  newest_a = max([os.path.getmtime(x) for x in get_all_files_under(dir_a)])
  newest_b = max([os.path.getmtime(x) for x in get_all_files_under(dir_b)])
  return newest_a < newest_b


def get_base_cflags(force_object_files=False):
  # Always build system libraries with debug information.  Non-debug builds
  # will ignore this at link time because we link with `-strip-debug`.
  flags = ['-g']
  if shared.Settings.LTO and not force_object_files:
    flags += ['-flto=' + shared.Settings.LTO]
  if shared.Settings.RELOCATABLE:
    flags += ['-s', 'RELOCATABLE']
  if shared.Settings.MEMORY64:
    flags += ['-s', 'MEMORY64=' + str(shared.Settings.MEMORY64)]
  return flags


def run_one_command(cmd):
  # Helper function used by run_build_commands.
  if shared.EM_BUILD_VERBOSE:
    print(shared.shlex_join(cmd))
  # building system libraries and ports should be hermetic in that it is not
  # affected by things like EMMAKEN_CFLAGS which the user may have set
  safe_env = os.environ.copy()
  for opt in ['EMMAKEN_CFLAGS', 'EMMAKEN_JUST_CONFIGURE']:
    if opt in safe_env:
      del safe_env[opt]
  # TODO(sbc): Remove this one we remove the test_em_config_env_var test
  cmd.append('-Wno-deprecated')
  try:
    shared.run_process(cmd, env=safe_env)
  except subprocess.CalledProcessError as e:
    print("'%s' failed (%d)" % (shared.shlex_join(e.cmd), e.returncode))
    raise


def run_build_commands(commands):
  # Before running a set of build commands make sure the common sysroot
  # headers are installed.  This prevents each sub-process from attempting
  # to setup the sysroot itself.
  ensure_sysroot()
  cores = min(len(commands), building.get_num_cores())
  if cores <= 1 or shared.DEBUG:
    for command in commands:
      run_one_command(command)
  else:
    pool = building.get_multiprocessing_pool()
    # https://stackoverflow.com/questions/1408356/keyboard-interrupts-with-pythons-multiprocessing-pool
    # https://bugs.python.org/issue8296
    # 999999 seconds (about 11 days) is reasonably huge to not trigger actual timeout
    # and is smaller than the maximum timeout value 4294967.0 for Python 3 on Windows (threading.TIMEOUT_MAX)
    pool.map_async(run_one_command, commands, chunksize=1).get(999999)


def create_lib(libname, inputs):
  """Create a library from a set of input objects."""
  suffix = shared.suffix(libname)
  if suffix in ('.bc', '.o'):
    if len(inputs) == 1:
      if inputs[0] != libname:
        shutil.copyfile(inputs[0], libname)
    else:
      building.link_to_object(inputs, libname)
  elif suffix == '.a':
    building.emar('cr', libname, inputs)
  else:
    raise Exception('unknown suffix ' + libname)


def get_wasm_libc_rt_files():
  # Combining static linking with LTO is tricky under LLVM.  The codegen that
  # happens during LTO can generate references to new symbols that didn't exist
  # in the linker inputs themselves.
  # These symbols are called libcalls in LLVM and are the result of intrinsics
  # and builtins at the LLVM level.  These libcalls cannot themselves be part
  # of LTO because once the linker is running the LTO phase new bitcode objects
  # cannot be added to link.  Another way of putting it: by the time LTO happens
  # the decision about which bitcode symbols to compile has already been made.
  # See: https://bugs.llvm.org/show_bug.cgi?id=44353.
  # To solve this we put all such libcalls in a separate library that, like
  # compiler-rt, is never compiled as LTO/bitcode (see force_object_files in
  # CompilerRTLibrary).
  # Note that this also includes things that may be depended on by those
  # functions - fmin uses signbit, for example, so signbit must be here (so if
  # fmin is added by codegen, it will have all it needs).
  math_files = files_in_path(
    path_components=['system', 'lib', 'libc', 'musl', 'src', 'math'],
    filenames=[
      'fmin.c', 'fminf.c', 'fminl.c',
      'fmax.c', 'fmaxf.c', 'fmaxl.c',
      'fmod.c', 'fmodf.c', 'fmodl.c',
      'log2.c', 'log2f.c', 'log10.c', 'log10f.c',
      'exp2.c', 'exp2f.c', 'exp10.c', 'exp10f.c',
      'scalbn.c', '__fpclassifyl.c',
      '__signbitl.c', '__signbitf.c', '__signbit.c'
    ])
  other_files = files_in_path(
    path_components=['system', 'lib', 'libc'],
    filenames=['emscripten_memcpy.c', 'emscripten_memset.c',
               'emscripten_scan_stack.c',
               'emscripten_memmove.c'])
  # Calls to iprintf can be generated during codegen. Ideally we wouldn't
  # compile these with -O2 like we do the rest of compiler-rt since its
  # probably not performance sensitive.  However we don't currently have
  # a way to set per-file compiler flags.  And hopefully we should be able
  # move all this stuff back into libc once we it LTO compatible.
  iprintf_files = files_in_path(
    path_components=['system', 'lib', 'libc', 'musl', 'src', 'stdio'],
    filenames=['__towrite.c', '__overflow.c', 'fwrite.c', 'fputs.c',
               'printf.c', 'puts.c', '__lockfile.c'])
  iprintf_files += files_in_path(
    path_components=['system', 'lib', 'libc', 'musl', 'src', 'string'],
    filenames=['strlen.c'])
  return math_files + other_files + iprintf_files


class Library(object):
  """
  `Library` is the base class of all system libraries.

  There are two types of libraries: abstract and concrete.
    * An abstract library, e.g. MTLibrary, is a subclass of `Library` that
      implements certain behaviour common to multiple libraries. The features
      of multiple abstract libraries can be used through multiple inheritance.
    * A concrete library, e.g. libc, is a subclass of `Library` that describes
      how to build a particular library, and its properties, such as name and
      dependencies.

  This library system is meant to handle having many versions of the same library,
  which we call *variations*. For example, some libraries (those that inherit
  from MTLibrary), have both single-threaded and multi-threaded versions.

  An instance of a `Library` subclass represents a specific variation of the
  library. Instance methods perform operations relating to this variation.
  For example, `get_cflags()` would return the emcc flags needed to build this
  variation, and `build()` would generate the library file for this variation.
  The constructor takes keyword arguments that defines the variation.

  Class methods perform tasks relating to all variations. For example,
  `variations()` returns a list of all variations that exists for this library,
  and `get_default_variation()` returns the variation suitable for the current
  environment.

  Other class methods act upon a group of libraries. For example,
  `Library.get_all_variations()` returns a mapping of all variations of
  existing libraries.

  To add a new type of variation, you must add an parameter to `__init__` that
  selects the variant. Then, override one of `vary_on` or `variations`, as well
  as `get_default_variation`.

  If the parameter is boolean, overriding `vary_on` to add the parameter name
  to the returned list is sufficient:

    @classmethod
    def vary_on(cls):
      return super().vary_on() + ['my_parameter']

  Otherwise, you must override `variations`:

    @classmethod
    def variations(cls):
      return [{'my_parameter': value, **other} for value, other in
              itertools.product([1, 2, 3], super().variations())]

  Overriding either `vary_on` or `variations` allows `embuilder.py` to know all
  possible variations so it can build all of them.

  You then need to modify `get_default_variation` to detect the correct value
  for your new parameter based on the settings:

    @classmethod
    def get_default_variation(cls, **kwargs):
      return super().get_default_variation(my_parameter=shared.Settings.MY_PARAMETER, **kwargs)

  This allows the correct variation of the library to be selected when building
  code with Emscripten.
  """

  # The simple name of the library. When linking, this is the name to use to
  # automatically get the correct version of the library.
  # This should only be overridden in a concrete library class, e.g. libc,
  # and left as None in an abstract library class, e.g. MTLibrary.
  name = None

  # Set to true to prevent EMCC_FORCE_STDLIBS from linking this library.
  never_force = False

  # A list of flags to pass to emcc.
  # The flags for the parent class is automatically inherited.
  # TODO: Investigate whether perf gains from loop unrolling would be worth the
  # extra code size. The -fno-unroll-loops flags was added here when loop
  # unrolling landed upstream in LLVM to avoid changing behavior but was not
  # specifically evaluated.
  cflags = ['-Werror', '-fno-unroll-loops']

  # A list of directories to put in the include path when building.
  # This is a list of tuples of path components.
  # For example, to put system/lib/a and system/lib/b under the emscripten
  # directory into the include path, you would write:
  #    includes = [('system', 'lib', 'a'), ('system', 'lib', 'b')]
  # The include path of the parent class is automatically inherited.
  includes = []

  # By default, `get_files` look for source files for this library under `src_dir`.
  # It will either use the files listed in `src_files`, or use the glob pattern in
  # `src_glob`. You may not specify both `src_files` and `src_glob`.
  # When using `src_glob`, you can specify a list of files in `src_glob_exclude`
  # to be excluded from the library.
  # Alternatively, you can override `get_files` to use your own logic.
  src_dir = None
  src_files = None
  src_glob = None
  src_glob_exclude = None

  # Whether to always generate WASM object files, even when LTO is set
  force_object_files = False

  def __init__(self):
    """
    Creates a variation of this library.

    A variation is a specific combination of settings a library can have.
    For example, libc++-mt-noexcept is a variation of libc++.
    There might be only one variation of a library.

    The constructor keyword arguments will define what variation to use.

    Use the `variations` classmethod to get the list of all possible constructor
    arguments for this library.

    Use the `get_default_variation` classmethod to construct the variation
    suitable for the current invocation of emscripten.
    """
    if not self.name:
      raise NotImplementedError('Cannot instantiate an abstract library')

  def can_use(self):
    """
    Whether this library can be used in the current environment.

    For example, libmalloc would override this and return False
    if the user requested no malloc.
    """
    return True

  def can_build(self):
    """
    Whether this library can be built in the current environment.

    Override this if, for example, the library can only be built on WASM backend.
    """
    return True

  def erase(self):
    shared.Cache.erase_file(shared.Cache.get_lib_name(self.get_filename()))

  def get_path(self):
    """
    Gets the cached path of this library.

    This will trigger a build if this library is not in the cache.
    """
    return shared.Cache.get_lib(self.get_filename(), self.build)

  def get_files(self):
    """
    Gets a list of source files for this library.

    Typically, you will use `src_dir`, `src_files`, `src_glob` and `src_glob_exclude`.
    If those are insufficient to describe the files needed, you can override this method.
    """
    if self.src_dir:
      if self.src_files and self.src_glob:
        raise Exception('Cannot use src_files and src_glob together')

      if self.src_files:
        return files_in_path(self.src_dir, self.src_files)
      elif self.src_glob:
        return glob_in_path(self.src_dir, self.src_glob, self.src_glob_exclude or ())

    raise NotImplementedError()

  def build_objects(self, build_dir):
    """
    Returns a list of compiled object files for this library.

    By default, this builds all the source files returned by `self.get_files()`,
    with the `cflags` returned by `self.get_cflags()`.
    """
    commands = []
    objects = []
    cflags = self.get_cflags()
    base_flags = get_base_cflags()
    for src in self.get_files():
      o = os.path.join(build_dir, shared.unsuffixed_basename(src) + '.o')
      ext = shared.suffix(src)
      if ext in ('.s', '.S', '.c'):
        cmd = [shared.EMCC]
      else:
        cmd = [shared.EMXX]
      if ext in ('.s', '.S'):
        cmd += base_flags
        # TODO(sbc) There is an llvm bug that causes a crash when `-g` is used with
        # assembly files that define wasm globals.
        cmd.remove('-g')
      else:
        cmd += cflags
      commands.append(cmd + ['-c', src, '-o', o])
      objects.append(o)
    run_build_commands(commands)
    return objects

  def build(self, out_filename):
    """Builds the library and returns the path to the file."""
    build_dir = shared.Cache.get_path(os.path.join('build', self.get_base_name()))
    utils.safe_ensure_dirs(build_dir)
    create_lib(out_filename, self.build_objects(build_dir))
    if not shared.DEBUG:
      tempfiles.try_delete(build_dir)

  @classmethod
  def _inherit_list(cls, attr):
    # Some properties, like cflags and includes, makes more sense to inherit
    # via concatenation than replacement.
    result = []
    for item in cls.__mro__[::-1]:
      # Using  __dict__ to avoid inheritance
      result += item.__dict__.get(attr, [])
    return result

  def get_cflags(self):
    """
    Returns the list of flags to pass to emcc when building this variation
    of the library.

    Override and add any flags as needed to handle new variations.
    """
    cflags = self._inherit_list('cflags')
    cflags += get_base_cflags(force_object_files=self.force_object_files)

    if self.includes:
      cflags += ['-I' + shared.path_from_root(*path) for path in self._inherit_list('includes')]

    return cflags

  def get_base_name_prefix(self):
    """
    Returns the base name of the library without any suffixes.
    """
    return self.name

  def get_base_name(self):
    """
    Returns the base name of the library file.

    This will include suffixes such as -mt, but will not include a file extension.
    """
    return self.get_base_name_prefix()

  def get_ext(self):
    """
    Return the appropriate file extension for this library.
    """
    return '.a'

  def get_filename(self):
    """
    Return the full name of the library file, including the file extension.
    """
    return self.get_base_name() + self.get_ext()

  @classmethod
  def vary_on(cls):
    """
    Returns a list of strings that are the names of boolean constructor
    arguments that defines the variations of this library.

    This is used by the default implementation of `cls.variations()` to generate
    every possible combination of boolean values to pass to these arguments.
    """
    return []

  @classmethod
  def variations(cls):
    """
    Returns a list of keyword arguments to pass to the constructor to create
    every possible variation of this library.

    By default, this is every possible combination of boolean values to pass
    to the list of arguments returned by `vary_on`, but you can override
    the behaviour.
    """
    vary_on = cls.vary_on()
    return [dict(zip(vary_on, toggles)) for toggles in
            itertools.product([False, True], repeat=len(vary_on))]

  @classmethod
  def get_default_variation(cls, **kwargs):
    """
    Construct the variation suitable for the current invocation of emscripten.

    Subclasses should pass the keyword arguments they introduce to the
    superclass version, and propagate **kwargs. The base class collects
    all the keyword arguments and creates the instance.
    """
    return cls(**kwargs)

  @classmethod
  def get_inheritance_tree(cls):
    """Returns all the classes in the inheritance tree of the current class."""
    yield cls
    for subclass in cls.__subclasses__():
      for subclass in subclass.get_inheritance_tree():
        yield subclass

  @classmethod
  def get_all_variations(cls):
    """
    Gets all the variations of libraries in the inheritance tree of the current
    library.

    Calling Library.get_all_variations() returns the variations of ALL libraries
    that can be built as a dictionary of variation names to Library objects.
    """
    result = {}
    for library in cls.get_inheritance_tree():
      if library.name:
        for flags in library.variations():
          variation = library(**flags)
          if variation.can_build():
            result[variation.get_base_name()] = variation
    return result

  @classmethod
  def get_usable_variations(cls):
    """
    Gets all libraries suitable for the current invocation of emscripten.

    This returns a dictionary of simple names to Library objects.
    """
    result = {}
    for subclass in cls.get_inheritance_tree():
      if subclass.name:
        library = subclass.get_default_variation()
        if library.can_build() and library.can_use():
          result[subclass.name] = library
    return result


class MTLibrary(Library):
  def __init__(self, **kwargs):
    self.is_mt = kwargs.pop('is_mt')
    super(MTLibrary, self).__init__(**kwargs)

  def get_cflags(self):
    cflags = super(MTLibrary, self).get_cflags()
    if self.is_mt:
      cflags += ['-s', 'USE_PTHREADS']
    return cflags

  def get_base_name(self):
    name = super(MTLibrary, self).get_base_name()
    if self.is_mt:
      name += '-mt'
    return name

  @classmethod
  def vary_on(cls):
    return super(MTLibrary, cls).vary_on() + ['is_mt']

  @classmethod
  def get_default_variation(cls, **kwargs):
    return super(MTLibrary, cls).get_default_variation(is_mt=shared.Settings.USE_PTHREADS, **kwargs)


class exceptions(object):
  """
  This represents exception handling mode of Emscripten. Currently there are
  three modes of exception handling:
  - None: Does not handle exceptions. This includes -fno-exceptions, which
    prevents both throwing and catching, and -fignore-exceptions, which only
    allows throwing, but library-wise they use the same version.
  - Emscripten: Emscripten provides exception handling capability using JS
    emulation. This causes code size increase and performance degradation.
  - Wasm: Wasm native exception handling support uses Wasm EH instructions and
    is meant to be fast. You need to use a VM that has the EH support to use
    this. This is not fully working yet and still experimental.
  """
  none = 0
  emscripten = 1
  wasm = 2


class NoExceptLibrary(Library):
  def __init__(self, **kwargs):
    self.eh_mode = kwargs.pop('eh_mode')
    super(NoExceptLibrary, self).__init__(**kwargs)

  def get_cflags(self):
    cflags = super(NoExceptLibrary, self).get_cflags()
    if self.eh_mode == exceptions.none:
      cflags += ['-fno-exceptions']
    elif self.eh_mode == exceptions.emscripten:
      cflags += ['-s', 'DISABLE_EXCEPTION_CATCHING=0']
    elif self.eh_mode == exceptions.wasm:
      cflags += ['-fwasm-exceptions']
    return cflags

  def get_base_name(self):
    name = super(NoExceptLibrary, self).get_base_name()
    # TODO Currently emscripten-based exception is the default mode, thus no
    # suffixes. Change the default to wasm exception later.
    if self.eh_mode == exceptions.none:
      name += '-noexcept'
    elif self.eh_mode == exceptions.wasm:
      name += '-except'
    return name

  @classmethod
  def variations(cls, **kwargs):
    combos = super(NoExceptLibrary, cls).variations()
    return ([dict(eh_mode=exceptions.none, **combo) for combo in combos] +
            [dict(eh_mode=exceptions.emscripten, **combo) for combo in combos] +
            [dict(eh_mode=exceptions.wasm, **combo) for combo in combos])

  @classmethod
  def get_default_variation(cls, **kwargs):
    if shared.Settings.EXCEPTION_HANDLING:
      eh_mode = exceptions.wasm
    elif shared.Settings.DISABLE_EXCEPTION_CATCHING == 1:
      eh_mode = exceptions.none
    else:
      eh_mode = exceptions.emscripten
    return super(NoExceptLibrary, cls).get_default_variation(eh_mode=eh_mode, **kwargs)


class MuslInternalLibrary(Library):
  includes = [
    ['system', 'lib', 'libc', 'musl', 'src', 'internal'],
  ]

  cflags = [
    '-D_XOPEN_SOURCE=700',
    '-Wno-unused-result',  # system call results are often ignored in musl, and in wasi that warns
  ]


class AsanInstrumentedLibrary(Library):
  def __init__(self, **kwargs):
    self.is_asan = kwargs.pop('is_asan', False)
    super(AsanInstrumentedLibrary, self).__init__(**kwargs)

  def get_cflags(self):
    cflags = super(AsanInstrumentedLibrary, self).get_cflags()
    if self.is_asan:
      cflags += ['-fsanitize=address']
    return cflags

  def get_base_name(self):
    name = super(AsanInstrumentedLibrary, self).get_base_name()
    if self.is_asan:
      name += '-asan'
    return name

  @classmethod
  def vary_on(cls):
    return super(AsanInstrumentedLibrary, cls).vary_on() + ['is_asan']

  @classmethod
  def get_default_variation(cls, **kwargs):
    return super(AsanInstrumentedLibrary, cls).get_default_variation(is_asan=shared.Settings.USE_ASAN, **kwargs)


class libcompiler_rt(MTLibrary):
  name = 'libcompiler_rt'
  # compiler_rt files can't currently be part of LTO although we are hoping to remove this
  # restriction soon: https://reviews.llvm.org/D71738
  force_object_files = True

  cflags = ['-O2', '-fno-builtin']
  src_dir = ['system', 'lib', 'compiler-rt', 'lib', 'builtins']
  src_files = glob_in_path(src_dir, '*.c')
  src_files.append(shared.path_from_root('system', 'lib', 'compiler-rt', 'stack_ops.S'))
  src_files.append(shared.path_from_root('system', 'lib', 'compiler-rt', 'stack_limits.S'))
  src_files.append(shared.path_from_root('system', 'lib', 'compiler-rt', 'emscripten_setjmp.c'))
  src_files.append(shared.path_from_root('system', 'lib', 'compiler-rt', 'emscripten_exception_builtins.c'))


class libc(AsanInstrumentedLibrary, MuslInternalLibrary, MTLibrary):
  name = 'libc'

  # Without -fno-builtin, LLVM can optimize away or convert calls to library
  # functions to something else based on assumptions that they behave exactly
  # like the standard library. This can cause unexpected bugs when we use our
  # custom standard library. The same for other libc/libm builds.
  cflags = ['-Os', '-fno-builtin']

  # Disable certain warnings for code patterns that are contained in upstream musl
  cflags += ['-Wno-ignored-attributes',
             '-Wno-dangling-else',
             '-Wno-unknown-pragmas',
             '-Wno-shift-op-parentheses',
             '-Wno-string-plus-int',
             '-Wno-pointer-sign']

  def get_files(self):
    libc_files = []
    musl_srcdir = shared.path_from_root('system', 'lib', 'libc', 'musl', 'src')

    # musl modules
    ignore = [
        'ipc', 'passwd', 'thread', 'signal', 'sched', 'ipc', 'time', 'linux',
        'aio', 'exit', 'legacy', 'mq', 'search', 'setjmp', 'env',
        'ldso', 'conf'
    ]

    # individual files
    ignore += [
        'memcpy.c', 'memset.c', 'memmove.c', 'getaddrinfo.c', 'getnameinfo.c',
        'res_query.c', 'res_querydomain.c', 'gai_strerror.c',
        'proto.c', 'gethostbyaddr.c', 'gethostbyaddr_r.c', 'gethostbyname.c',
        'gethostbyname2_r.c', 'gethostbyname_r.c', 'gethostbyname2.c',
        'alarm.c', 'syscall.c', '_exit.c', 'popen.c',
        'getgrouplist.c', 'initgroups.c', 'wordexp.c', 'timer_create.c',
        'faccessat.c',
        # 'process' exclusion
        'fork.c', 'vfork.c', 'posix_spawn.c', 'execve.c', 'waitid.c', 'system.c'
    ]

    ignore += LIBC_SOCKETS

    if self.is_asan:
      # With ASan, we need to use specialized implementations of certain libc
      # functions that do not rely on undefined behavior, for example, reading
      # multiple bytes at once as an int and overflowing a buffer.
      # Otherwise, ASan will catch these errors and terminate the program.
      ignore += ['strcpy.c', 'memchr.c', 'strchrnul.c', 'strlen.c',
                 'aligned_alloc.c', 'fcntl.c']
      libc_files += [
        shared.path_from_root('system', 'lib', 'libc', 'emscripten_asan_strcpy.c'),
        shared.path_from_root('system', 'lib', 'libc', 'emscripten_asan_memchr.c'),
        shared.path_from_root('system', 'lib', 'libc', 'emscripten_asan_strchrnul.c'),
        shared.path_from_root('system', 'lib', 'libc', 'emscripten_asan_strlen.c'),
        shared.path_from_root('system', 'lib', 'libc', 'emscripten_asan_fcntl.c'),
      ]

    # These are included in wasm_libc_rt instead
    ignore += [os.path.basename(f) for f in get_wasm_libc_rt_files()]

    ignore = set(ignore)
    # TODO: consider using more math code from musl, doing so makes box2d faster
    for dirpath, dirnames, filenames in os.walk(musl_srcdir):
      # Don't recurse into ingored directories
      remove = [d for d in dirnames if d in ignore]
      for r in remove:
        dirnames.remove(r)

      for f in filenames:
        if f.endswith('.c') and f not in ignore:
          libc_files.append(os.path.join(musl_srcdir, dirpath, f))

    # Allowed files from ignored modules
    libc_files += files_in_path(
        path_components=['system', 'lib', 'libc', 'musl', 'src', 'time'],
        filenames=['clock_settime.c', 'asctime.c', 'ctime.c', 'gmtime.c', 'localtime.c', 'nanosleep.c'])
    libc_files += files_in_path(
        path_components=['system', 'lib', 'libc', 'musl', 'src', 'legacy'],
        filenames=['getpagesize.c', 'err.c'])

    libc_files += files_in_path(
        path_components=['system', 'lib', 'libc', 'musl', 'src', 'env'],
        filenames=['__environ.c', 'getenv.c', 'putenv.c', 'setenv.c', 'unsetenv.c'])

    libc_files += files_in_path(
        path_components=['system', 'lib', 'libc', 'musl', 'src', 'sched'],
        filenames=['sched_yield.c'])

    libc_files += files_in_path(
        path_components=['system', 'lib', 'libc'],
        filenames=['extras.c', 'wasi-helpers.c', 'emscripten_pthread.c'])

    libc_files += files_in_path(
        path_components=['system', 'lib', 'pthread'],
        filenames=['emscripten_atomic.c'])

    libc_files += files_in_path(
        path_components=['system', 'lib', 'libc', 'musl', 'src', 'thread'],
        filenames=[
          'pthread_self.c',
          # C11 thread library functions
          'thrd_create.c',
          'thrd_exit.c',
          'thrd_join.c',
          'thrd_sleep.c',
          'thrd_yield.c',
          'call_once.c',
        ])

    libc_files += glob_in_path(['system', 'lib', 'libc', 'compat'], '*.c')

    if self.is_mt:
      libc_files += files_in_path(
        path_components=['system', 'lib', 'libc', 'musl', 'src', 'thread'],
        filenames=[
          'pthread_attr_destroy.c', 'pthread_condattr_setpshared.c',
          'pthread_mutex_lock.c', 'pthread_spin_destroy.c', 'pthread_attr_get.c',
          'pthread_cond_broadcast.c', 'pthread_mutex_setprioceiling.c',
          'pthread_spin_init.c', 'pthread_attr_init.c', 'pthread_cond_destroy.c',
          'pthread_mutex_timedlock.c', 'pthread_spin_lock.c',
          'pthread_attr_setdetachstate.c', 'pthread_cond_init.c',
          'pthread_mutex_trylock.c', 'pthread_spin_trylock.c',
          'pthread_attr_setguardsize.c', 'pthread_cond_signal.c',
          'pthread_mutex_unlock.c', 'pthread_spin_unlock.c',
          'pthread_attr_setinheritsched.c', 'pthread_cond_timedwait.c',
          'pthread_once.c', 'sem_destroy.c', 'pthread_attr_setschedparam.c',
          'pthread_cond_wait.c', 'pthread_rwlockattr_destroy.c', 'sem_getvalue.c',
          'pthread_attr_setschedpolicy.c', 'pthread_equal.c', 'pthread_rwlockattr_init.c',
          'sem_init.c', 'pthread_attr_setscope.c', 'pthread_getspecific.c',
          'pthread_rwlockattr_setpshared.c', 'sem_open.c', 'pthread_attr_setstack.c',
          'pthread_key_create.c', 'pthread_rwlock_destroy.c', 'sem_post.c',
          'pthread_attr_setstacksize.c', 'pthread_mutexattr_destroy.c',
          'pthread_rwlock_init.c', 'sem_timedwait.c', 'pthread_barrierattr_destroy.c',
          'pthread_mutexattr_init.c', 'pthread_rwlock_rdlock.c', 'sem_trywait.c',
          'pthread_barrierattr_init.c', 'pthread_mutexattr_setprotocol.c',
          'pthread_rwlock_timedrdlock.c', 'sem_unlink.c',
          'pthread_barrierattr_setpshared.c', 'pthread_mutexattr_setpshared.c',
          'pthread_rwlock_timedwrlock.c', 'sem_wait.c', 'pthread_barrier_destroy.c',
          'pthread_mutexattr_setrobust.c', 'pthread_rwlock_tryrdlock.c',
          '__timedwait.c', 'pthread_barrier_init.c', 'pthread_mutexattr_settype.c',
          'pthread_rwlock_trywrlock.c', 'vmlock.c', 'pthread_barrier_wait.c',
          'pthread_mutex_consistent.c', 'pthread_rwlock_unlock.c', '__wait.c',
          'pthread_condattr_destroy.c', 'pthread_mutex_destroy.c',
          'pthread_rwlock_wrlock.c', 'pthread_condattr_init.c',
          'pthread_mutex_getprioceiling.c', 'pthread_setcanceltype.c',
          'pthread_condattr_setclock.c', 'pthread_mutex_init.c',
          'pthread_setspecific.c', 'pthread_setcancelstate.c',
          'pthread_getconcurrency.c', 'pthread_setconcurrency.c',
          'pthread_getschedparam.c', 'pthread_setschedparam.c',
          'pthread_setschedprio.c', 'pthread_atfork.c',
          'pthread_getcpuclockid.c',
        ])
      libc_files += files_in_path(
        path_components=['system', 'lib', 'pthread'],
        filenames=[
          'library_pthread.c',
          'emscripten_tls_init.c',
          'emscripten_thread_state.s',
        ])
    else:
      libc_files += [shared.path_from_root('system', 'lib', 'pthread', 'library_pthread_stub.c')]

    return libc_files


class libprintf_long_double(libc):
  name = 'libprintf_long_double'

  cflags = ['-DEMSCRIPTEN_PRINTF_LONG_DOUBLE']

  def get_files(self):
    return files_in_path(
        path_components=['system', 'lib', 'libc', 'musl', 'src', 'stdio'],
        filenames=['vfprintf.c'])


class libsockets(MuslInternalLibrary, MTLibrary):
  name = 'libsockets'

  cflags = ['-Os', '-fno-builtin']

  def get_files(self):
    network_dir = shared.path_from_root('system', 'lib', 'libc', 'musl', 'src', 'network')
    return [os.path.join(network_dir, x) for x in LIBC_SOCKETS]


class libsockets_proxy(MTLibrary):
  name = 'libsockets_proxy'

  cflags = ['-Os']

  def get_files(self):
    return [shared.path_from_root('system', 'lib', 'websocket', 'websocket_to_posix_socket.cpp')]


class crt1(MuslInternalLibrary):
  name = 'crt1'
  cflags = ['-O2']
  src_dir = ['system', 'lib', 'libc']
  src_files = ['crt1.c']

  force_object_files = True

  def get_ext(self):
    return '.o'

  def can_use(self):
    return super(crt1, self).can_use() and shared.Settings.STANDALONE_WASM


class crt1_reactor(MuslInternalLibrary):
  name = 'crt1_reactor'
  cflags = ['-O2']
  src_dir = ['system', 'lib', 'libc']
  src_files = ['crt1_reactor.c']

  force_object_files = True

  def get_ext(self):
    return '.o'

  def can_use(self):
    return super(crt1_reactor, self).can_use() and shared.Settings.STANDALONE_WASM


class libcxxabi(NoExceptLibrary, MTLibrary):
  name = 'libc++abi'
  cflags = [
      '-Oz',
      '-D_LIBCPP_DISABLE_VISIBILITY_ANNOTATIONS',
      # Remove this once we update to include this llvm
      # revision: https://reviews.llvm.org/D64961
      '-D_LIBCXXABI_GUARD_ABI_ARM',
    ]

  def get_cflags(self):
    cflags = super(libcxxabi, self).get_cflags()
    cflags.append('-DNDEBUG')
    if not self.is_mt:
      cflags.append('-D_LIBCXXABI_HAS_NO_THREADS')
    if self.eh_mode == exceptions.none:
      cflags.append('-D_LIBCXXABI_NO_EXCEPTIONS')
    elif self.eh_mode == exceptions.emscripten:
      cflags.append('-D__USING_EMSCRIPTEN_EXCEPTIONS__')
    elif self.eh_mode == exceptions.wasm:
      cflags.append('-D__USING_WASM_EXCEPTIONS__')
    return cflags

  def get_files(self):
    filenames = [
      'abort_message.cpp',
      'cxa_aux_runtime.cpp',
      'cxa_default_handlers.cpp',
      'cxa_demangle.cpp',
      'cxa_exception_storage.cpp',
      'cxa_guard.cpp',
      'cxa_handlers.cpp',
      'cxa_virtual.cpp',
      'fallback_malloc.cpp',
      'stdlib_new_delete.cpp',
      'stdlib_exception.cpp',
      'stdlib_stdexcept.cpp',
      'stdlib_typeinfo.cpp',
      'private_typeinfo.cpp'
    ]
    if self.eh_mode == exceptions.none:
      filenames += ['cxa_noexception.cpp']
    elif self.eh_mode == exceptions.wasm:
      filenames += [
        'cxa_exception.cpp',
        'cxa_personality.cpp'
      ]

    return files_in_path(
        path_components=['system', 'lib', 'libcxxabi', 'src'],
        filenames=filenames)


class libcxx(NoExceptLibrary, MTLibrary):
  name = 'libc++'

  cflags = ['-DLIBCXX_BUILDING_LIBCXXABI=1', '-D_LIBCPP_BUILDING_LIBRARY', '-Oz',
            '-D_LIBCPP_DISABLE_VISIBILITY_ANNOTATIONS']

  src_dir = ['system', 'lib', 'libcxx', 'src']
  src_glob = '**/*.cpp'
  src_glob_exclude = ['locale_win32.cpp', 'thread_win32.cpp', 'support.cpp', 'int128_builtins.cpp']


class libunwind(NoExceptLibrary, MTLibrary):
  name = 'libunwind'
  cflags = ['-Oz', '-D_LIBUNWIND_DISABLE_VISIBILITY_ANNOTATIONS']
  src_dir = ['system', 'lib', 'libunwind', 'src']
  # Without this we can't build libunwind since it will pickup the unwind.h
  # that is part of llvm (which is not compatible for some reason).
  includes = [['system', 'lib', 'libunwind', 'include']]
  src_files = ['Unwind-wasm.cpp']

  def __init__(self, **kwargs):
    super(libunwind, self).__init__(**kwargs)

  def can_use(self):
    return super(libunwind, self).can_use() and self.eh_mode == exceptions.wasm

  def get_cflags(self):
    cflags = super(libunwind, self).get_cflags()
    cflags.append('-DNDEBUG')
    if not self.is_mt:
      cflags.append('-D_LIBUNWIND_HAS_NO_THREADS')
    if self.eh_mode == exceptions.none:
      cflags.append('-D_LIBUNWIND_HAS_NO_EXCEPTIONS')
    elif self.eh_mode == exceptions.emscripten:
      cflags.append('-D__USING_EMSCRIPTEN_EXCEPTIONS__')
    elif self.eh_mode == exceptions.wasm:
      cflags.append('-D__USING_WASM_EXCEPTIONS__')
    return cflags


class libmalloc(MTLibrary):
  name = 'libmalloc'

  cflags = ['-O2', '-fno-builtin']

  def __init__(self, **kwargs):
    self.malloc = kwargs.pop('malloc')
    if self.malloc not in ('dlmalloc', 'emmalloc', 'emmalloc-debug', 'emmalloc-memvalidate', 'emmalloc-verbose', 'emmalloc-memvalidate-verbose', 'none'):
      raise Exception('malloc must be one of "emmalloc[-debug|-memvalidate][-verbose]", "dlmalloc" or "none", see settings.js')

    self.use_errno = kwargs.pop('use_errno')
    self.is_tracing = kwargs.pop('is_tracing')
    self.memvalidate = kwargs.pop('memvalidate')
    self.verbose = kwargs.pop('verbose')
    self.is_debug = kwargs.pop('is_debug') or self.memvalidate or self.verbose

    super(libmalloc, self).__init__(**kwargs)

  def get_files(self):
    malloc_base = self.malloc.replace('-memvalidate', '').replace('-verbose', '').replace('-debug', '')
    malloc = shared.path_from_root('system', 'lib', {
      'dlmalloc': 'dlmalloc.c', 'emmalloc': 'emmalloc.cpp',
    }[malloc_base])
    sbrk = shared.path_from_root('system', 'lib', 'sbrk.c')
    return [malloc, sbrk]

  def get_cflags(self):
    cflags = super(libmalloc, self).get_cflags()
    if self.memvalidate:
      cflags += ['-DEMMALLOC_MEMVALIDATE']
    if self.verbose:
      cflags += ['-DEMMALLOC_VERBOSE']
    if self.is_debug:
      cflags += ['-UNDEBUG', '-DDLMALLOC_DEBUG']
    else:
      cflags += ['-DNDEBUG']
    if not self.use_errno:
      cflags += ['-DMALLOC_FAILURE_ACTION=', '-DEMSCRIPTEN_NO_ERRNO']
    if self.is_tracing:
      cflags += ['--tracing']
    return cflags

  def get_base_name_prefix(self):
    return 'lib%s' % self.malloc

  def get_base_name(self):
    name = super(libmalloc, self).get_base_name()
    if self.is_debug and not self.memvalidate and not self.verbose:
      name += '-debug'
    if not self.use_errno:
      # emmalloc doesn't actually use errno, but it's easier to build it again
      name += '-noerrno'
    if self.is_tracing:
      name += '-tracing'
    return name

  def can_use(self):
    return super(libmalloc, self).can_use() and shared.Settings.MALLOC != 'none'

  @classmethod
  def vary_on(cls):
    return super(libmalloc, cls).vary_on() + ['is_debug', 'use_errno', 'is_tracing', 'memvalidate', 'verbose']

  @classmethod
  def get_default_variation(cls, **kwargs):
    return super(libmalloc, cls).get_default_variation(
      malloc=shared.Settings.MALLOC,
      is_debug=shared.Settings.ASSERTIONS >= 2,
      use_errno=shared.Settings.SUPPORT_ERRNO,
      is_tracing=shared.Settings.EMSCRIPTEN_TRACING,
      memvalidate='memvalidate' in shared.Settings.MALLOC,
      verbose='verbose' in shared.Settings.MALLOC,
      **kwargs
    )

  @classmethod
  def variations(cls):
    combos = super(libmalloc, cls).variations()
    return ([dict(malloc='dlmalloc', **combo) for combo in combos if not combo['memvalidate'] and not combo['verbose']] +
            [dict(malloc='emmalloc', **combo) for combo in combos if not combo['memvalidate'] and not combo['verbose']] +
            [dict(malloc='emmalloc-memvalidate-verbose', **combo) for combo in combos if combo['memvalidate'] and combo['verbose']] +
            [dict(malloc='emmalloc-memvalidate', **combo) for combo in combos if combo['memvalidate'] and not combo['verbose']] +
            [dict(malloc='emmalloc-verbose', **combo) for combo in combos if combo['verbose'] and not combo['memvalidate']])


class libal(Library):
  name = 'libal'

  cflags = ['-Os']
  src_dir = ['system', 'lib']
  src_files = ['al.c']


class libgl(MTLibrary):
  name = 'libgl'

  src_dir = ['system', 'lib', 'gl']
  src_files = ['gl.c', 'webgl1.c', 'libprocaddr.c']

  cflags = ['-Oz']

  def __init__(self, **kwargs):
    self.is_legacy = kwargs.pop('is_legacy')
    self.is_webgl2 = kwargs.pop('is_webgl2')
    self.is_ofb = kwargs.pop('is_ofb')
    self.is_full_es3 = kwargs.pop('is_full_es3')
    if self.is_webgl2 or self.is_full_es3:
      # Don't use append or += here, otherwise we end up adding to
      # the class member.
      self.src_files = self.src_files + ['webgl2.c']
    super(libgl, self).__init__(**kwargs)

  def get_base_name(self):
    name = super(libgl, self).get_base_name()
    if self.is_legacy:
      name += '-emu'
    if self.is_webgl2:
      name += '-webgl2'
    if self.is_ofb:
      name += '-ofb'
    if self.is_full_es3:
      name += '-full_es3'
    return name

  def get_cflags(self):
    cflags = super(libgl, self).get_cflags()
    if self.is_legacy:
      cflags += ['-DLEGACY_GL_EMULATION=1']
    if self.is_webgl2:
      cflags += ['-DMAX_WEBGL_VERSION=2', '-s', 'MAX_WEBGL_VERSION=2']
    if self.is_ofb:
      cflags += ['-D__EMSCRIPTEN_OFFSCREEN_FRAMEBUFFER__']
    if self.is_full_es3:
      cflags += ['-D__EMSCRIPTEN_FULL_ES3__']
    return cflags

  @classmethod
  def vary_on(cls):
    return super(libgl, cls).vary_on() + ['is_legacy', 'is_webgl2', 'is_ofb', 'is_full_es3']

  @classmethod
  def get_default_variation(cls, **kwargs):
    return super(libgl, cls).get_default_variation(
      is_legacy=shared.Settings.LEGACY_GL_EMULATION,
      is_webgl2=shared.Settings.MAX_WEBGL_VERSION >= 2,
      is_ofb=shared.Settings.OFFSCREEN_FRAMEBUFFER,
      is_full_es3=shared.Settings.FULL_ES3,
      **kwargs
    )


class libwebgpu_cpp(MTLibrary):
  name = 'libwebgpu_cpp'

  cflags = ['-std=c++11', '-O2']
  src_dir = ['system', 'lib', 'webgpu']
  src_files = ['webgpu_cpp.cpp']

class libwebnn_cpp(MTLibrary):
  name = 'libwebnn_cpp'

  cflags = ['-std=c++11', '-O2']
  src_dir = ['system', 'lib', 'webnn']
  src_files = ['webnn_cpp.cpp']


class libembind(Library):
  name = 'libembind'
  never_force = True

  def __init__(self, **kwargs):
    self.with_rtti = kwargs.pop('with_rtti', False)
    super(libembind, self).__init__(**kwargs)

  def get_cflags(self):
    cflags = super(libembind, self).get_cflags()
    if not self.with_rtti:
      cflags += ['-fno-rtti', '-DEMSCRIPTEN_HAS_UNBOUND_TYPE_NAMES=0']
    return cflags

  @classmethod
  def vary_on(cls):
    return super(libembind, cls).vary_on() + ['with_rtti']

  def get_base_name(self):
    name = super(libembind, self).get_base_name()
    if self.with_rtti:
      name += '-rtti'
    return name

  def get_files(self):
    return [shared.path_from_root('system', 'lib', 'embind', 'bind.cpp')]

  @classmethod
  def get_default_variation(cls, **kwargs):
    return super(libembind, cls).get_default_variation(with_rtti=shared.Settings.USE_RTTI, **kwargs)


class libfetch(MTLibrary):
  name = 'libfetch'
  never_force = True

  def get_files(self):
    return [shared.path_from_root('system', 'lib', 'fetch', 'emscripten_fetch.cpp')]


class libasmfs(MTLibrary):
  name = 'libasmfs'
  never_force = True

  def get_files(self):
    return [shared.path_from_root('system', 'lib', 'fetch', 'asmfs.cpp')]

  def can_build(self):
    # ASMFS is looking for a maintainer
    # https://github.com/emscripten-core/emscripten/issues/9534
    return True


class libhtml5(Library):
  name = 'libhtml5'

  cflags = ['-Oz']
  src_dir = ['system', 'lib', 'html5']
  src_glob = '*.c'


class CompilerRTLibrary(Library):
  cflags = ['-O2', '-fno-builtin']
  # compiler_rt files can't currently be part of LTO although we are hoping to remove this
  # restriction soon: https://reviews.llvm.org/D71738
  force_object_files = True


class libc_rt_wasm(AsanInstrumentedLibrary, CompilerRTLibrary, MuslInternalLibrary):
  name = 'libc_rt_wasm'

  def get_files(self):
    return get_wasm_libc_rt_files()


class libubsan_minimal_rt_wasm(CompilerRTLibrary, MTLibrary):
  name = 'libubsan_minimal_rt_wasm'
  never_force = True

  includes = [['system', 'lib', 'compiler-rt', 'lib']]
  src_dir = ['system', 'lib', 'compiler-rt', 'lib', 'ubsan_minimal']
  src_files = ['ubsan_minimal_handlers.cpp']


class libsanitizer_common_rt(CompilerRTLibrary, MTLibrary):
  name = 'libsanitizer_common_rt'
  # TODO(sbc): We should not need musl-internal headers here.
  includes = [['system', 'lib', 'libc', 'musl', 'src', 'internal'],
              ['system', 'lib', 'compiler-rt', 'lib']]
  never_force = True

  src_dir = ['system', 'lib', 'compiler-rt', 'lib', 'sanitizer_common']
  src_glob = '*.cpp'
  src_glob_exclude = ['sanitizer_common_nolibc.cpp']


class SanitizerLibrary(CompilerRTLibrary, MTLibrary):
  never_force = True

  includes = [['system', 'lib', 'compiler-rt', 'lib']]
  src_glob = '*.cpp'


class libubsan_rt(SanitizerLibrary):
  name = 'libubsan_rt'

  cflags = ['-DUBSAN_CAN_USE_CXXABI']
  src_dir = ['system', 'lib', 'compiler-rt', 'lib', 'ubsan']


class liblsan_common_rt(SanitizerLibrary):
  name = 'liblsan_common_rt'

  src_dir = ['system', 'lib', 'compiler-rt', 'lib', 'lsan']
  src_glob = 'lsan_common*.cpp'


class liblsan_rt(SanitizerLibrary):
  name = 'liblsan_rt'

  src_dir = ['system', 'lib', 'compiler-rt', 'lib', 'lsan']
  src_glob_exclude = ['lsan_common.cpp', 'lsan_common_mac.cpp', 'lsan_common_linux.cpp',
                      'lsan_common_emscripten.cpp']


class libasan_rt(SanitizerLibrary):
  name = 'libasan_rt'

  src_dir = ['system', 'lib', 'compiler-rt', 'lib', 'asan']


class libasan_js(Library):
  name = 'libasan_js'

  cflags = ['-fsanitize=address']

  src_dir = ['system', 'lib']
  src_files = ['asan_js.c']


# This library is used when STANDALONE_WASM is set. In that mode, we don't
# want to depend on JS, and so this library contains implementations of
# things that we'd normally do in JS. That includes some general things
# as well as some additional musl components (that normally we reimplement
# in JS as it's more efficient that way).
class libstandalonewasm(MuslInternalLibrary):
  name = 'libstandalonewasm'
  # LTO defeats the weak linking trick used in __original_main.c
  force_object_files = True

  cflags = ['-Os']
  src_dir = ['system', 'lib']

  def __init__(self, **kwargs):
    self.is_mem_grow = kwargs.pop('is_mem_grow')
    super(libstandalonewasm, self).__init__(**kwargs)

  def get_base_name(self):
    name = super(libstandalonewasm, self).get_base_name()
    if self.is_mem_grow:
      name += '-memgrow'
    return name

  def get_cflags(self):
    cflags = super(libstandalonewasm, self).get_cflags()
    cflags += ['-DNDEBUG']
    if self.is_mem_grow:
      cflags += ['-D__EMSCRIPTEN_MEMORY_GROWTH__=1']
    return cflags

  @classmethod
  def vary_on(cls):
    return super(libstandalonewasm, cls).vary_on() + ['is_mem_grow']

  @classmethod
  def get_default_variation(cls, **kwargs):
    return super(libstandalonewasm, cls).get_default_variation(
      is_mem_grow=shared.Settings.ALLOW_MEMORY_GROWTH,
      **kwargs
    )

  def get_files(self):
    base_files = files_in_path(
        path_components=['system', 'lib', 'standalone'],
        filenames=['standalone.c', 'standalone_wasm_stdio.c', '__original_main.c',
                   '__main_void.c', '__main_argc_argv.c'])
    # It is more efficient to use JS methods for time, normally.
    time_files = files_in_path(
        path_components=['system', 'lib', 'libc', 'musl', 'src', 'time'],
        filenames=['strftime.c',
                   '__month_to_secs.c',
                   '__secs_to_tm.c',
                   '__tm_to_secs.c',
                   '__tz.c',
                   '__year_to_secs.c',
                   'asctime_r.c',
                   'clock.c',
                   'clock_gettime.c',
                   'ctime_r.c',
                   'difftime.c',
                   'gettimeofday.c',
                   'gmtime_r.c',
                   'localtime_r.c',
                   'mktime.c',
                   'time.c'])
    # It is more efficient to use JS for __assert_fail, as it avoids always
    # including fprintf etc.
    exit_files = files_in_path(
        path_components=['system', 'lib', 'libc', 'musl', 'src', 'exit'],
        filenames=['assert.c', 'atexit.c', 'exit.c']) + files_in_path(
        path_components=['system', 'lib', 'libc', 'musl', 'src', 'unistd'],
        filenames=['_exit.c'])
    conf_files = files_in_path(
        path_components=['system', 'lib', 'libc', 'musl', 'src', 'conf'],
        filenames=['sysconf.c'])
    return base_files + time_files + exit_files + conf_files


class libjsmath(Library):
  name = 'libjsmath'
  cflags = ['-Os']
  src_dir = ['system', 'lib']
  src_files = ['jsmath.c']


# If main() is not in EXPORTED_FUNCTIONS, it may be dce'd out. This can be
# confusing, so issue a warning.
def warn_on_unexported_main(symbolses):
  # In STANDALONE_WASM we don't expect main to be explictly exported
  if shared.Settings.STANDALONE_WASM:
    return
  if '_main' not in shared.Settings.EXPORTED_FUNCTIONS:
    for symbols in symbolses:
      if 'main' in symbols.defs:
        logger.warning('main() is in the input files, but "_main" is not in EXPORTED_FUNCTIONS, which means it may be eliminated as dead code. Export it if you want main() to run.')
        return


def handle_reverse_deps(input_files):
  if shared.Settings.REVERSE_DEPS == 'none':
    return
  elif shared.Settings.REVERSE_DEPS == 'all':
    # When not optimzing we add all possible reverse dependencies rather
    # than scanning the input files
    for symbols in deps_info.deps_info.values():
      for symbol in symbols:
        shared.Settings.EXPORTED_FUNCTIONS.append(mangle_c_symbol_name(symbol))
    return

  if shared.Settings.REVERSE_DEPS != 'auto':
    shared.exit_with_error(f'invalid values for REVERSE_DEPS: {shared.Settings.REVERSE_DEPS}')

  added = set()

  def add_reverse_deps(need):
    more = False
    for ident, deps in deps_info.deps_info.items():
      if ident in need.undefs and ident not in added:
        added.add(ident)
        more = True
        for dep in deps:
          need.undefs.add(dep)
          logger.debug('adding dependency on %s due to deps-info on %s' % (dep, ident))
          shared.Settings.EXPORTED_FUNCTIONS.append(mangle_c_symbol_name(dep))
    if more:
      add_reverse_deps(need) # recurse to get deps of deps

  # Scan symbols
  symbolses = building.llvm_nm_multiple([os.path.abspath(t) for t in input_files])

  warn_on_unexported_main(symbolses)

  if len(symbolses) == 0:
    class Dummy(object):
      defs = set()
      undefs = set()
    symbolses.append(Dummy())

  # depend on exported functions
  for export in shared.Settings.EXPORTED_FUNCTIONS:
    if shared.Settings.VERBOSE:
      logger.debug('adding dependency on export %s' % export)
    symbolses[0].undefs.add(demangle_c_symbol_name(export))

  for symbols in symbolses:
    add_reverse_deps(symbols)


def calculate(input_files, cxx, forced):
  # Setting this will only use the forced libs in EMCC_FORCE_STDLIBS. This avoids spending time checking
  # for unresolved symbols in your project files, which can speed up linking, but if you do not have
  # the proper list of actually needed libraries, errors can occur. See below for how we must
  # export all the symbols in deps_info when using this option.
  only_forced = os.environ.get('EMCC_ONLY_FORCED_STDLIBS')
  if only_forced:
    # One of the purposes EMCC_ONLY_FORCED_STDLIBS was to skip the scanning
    # of the input files for reverse dependencies.
    diagnostics.warning('deprecated', 'EMCC_ONLY_FORCED_STDLIBS is deprecated.  Use `-nostdlib` and/or `-s REVERSE_DEPS=none` depending on the desired result')
    shared.Settings.REVERSE_DEPS = 'all'

  handle_reverse_deps(input_files)

  libs_to_link = []
  already_included = set()
  system_libs_map = Library.get_usable_variations()

  # Setting this in the environment will avoid checking dependencies and make
  # building big projects a little faster 1 means include everything; otherwise
  # it can be the name of a lib (libc++, etc.).
  # You can provide 1 to include everything, or a comma-separated list with the
  # ones you want
  force = os.environ.get('EMCC_FORCE_STDLIBS')
  if force == '1':
    force = ','.join(name for name, lib in system_libs_map.items() if not lib.never_force)
  force_include = set((force.split(',') if force else []) + forced)
  if force_include:
    logger.debug('forcing stdlibs: ' + str(force_include))

  for lib in force_include:
    if lib not in system_libs_map:
      shared.exit_with_error('invalid forced library: %s', lib)

  def add_library(lib):
    if lib.name in already_included:
      return
    already_included.add(lib.name)

    logger.debug('including %s (%s)' % (lib.name, lib.get_filename()))

    need_whole_archive = lib.name in force_include and lib.get_ext() == '.a'
    libs_to_link.append((lib.get_path(), need_whole_archive))

  if shared.Settings.STANDALONE_WASM:
    if shared.Settings.EXPECT_MAIN:
      add_library(system_libs_map['crt1'])
    else:
      add_library(system_libs_map['crt1_reactor'])

  for forced in force_include:
    add_library(system_libs_map[forced])

  if only_forced:
    add_library(system_libs_map['libc_rt_wasm'])
    add_library(system_libs_map['libcompiler_rt'])
  else:
    if shared.Settings.AUTO_NATIVE_LIBRARIES:
      add_library(system_libs_map['libgl'])
      add_library(system_libs_map['libal'])
      add_library(system_libs_map['libhtml5'])

    sanitize = shared.Settings.USE_LSAN or shared.Settings.USE_ASAN or shared.Settings.UBSAN_RUNTIME

    # JS math must come before anything else, so that it overrides the normal
    # libc math.
    if shared.Settings.JS_MATH:
      add_library(system_libs_map['libjsmath'])

    # to override the normal libc printf, we must come before it
    if shared.Settings.PRINTF_LONG_DOUBLE:
      add_library(system_libs_map['libprintf_long_double'])

    add_library(system_libs_map['libc'])
    add_library(system_libs_map['libcompiler_rt'])
    if cxx:
      add_library(system_libs_map['libc++'])
    if cxx or sanitize:
      add_library(system_libs_map['libc++abi'])
      if shared.Settings.EXCEPTION_HANDLING:
        add_library(system_libs_map['libunwind'])
    if shared.Settings.MALLOC != 'none':
      add_library(system_libs_map['libmalloc'])
    if shared.Settings.STANDALONE_WASM:
      add_library(system_libs_map['libstandalonewasm'])
    add_library(system_libs_map['libc_rt_wasm'])

    if shared.Settings.USE_LSAN:
      force_include.add('liblsan_rt')
      add_library(system_libs_map['liblsan_rt'])

    if shared.Settings.USE_ASAN:
      force_include.add('libasan_rt')
      add_library(system_libs_map['libasan_rt'])
      add_library(system_libs_map['libasan_js'])

    if shared.Settings.UBSAN_RUNTIME == 1:
      add_library(system_libs_map['libubsan_minimal_rt_wasm'])
    elif shared.Settings.UBSAN_RUNTIME == 2:
      add_library(system_libs_map['libubsan_rt'])

    if shared.Settings.USE_LSAN or shared.Settings.USE_ASAN:
      add_library(system_libs_map['liblsan_common_rt'])

    if sanitize:
      add_library(system_libs_map['libsanitizer_common_rt'])

    # the sanitizer runtimes may call mmap, which will need a few things. sadly
    # the usual deps_info mechanism does not work since we scan only user files
    # for things, and not libraries (to be able to scan libraries, we'd need to
    # somehow figure out which of their object files will actually be linked in -
    # but only lld knows that). so just directly handle that here.
    if sanitize:
      shared.Settings.EXPORTED_FUNCTIONS.append(mangle_c_symbol_name('memset'))

    if shared.Settings.PROXY_POSIX_SOCKETS:
      add_library(system_libs_map['libsockets_proxy'])
    else:
      add_library(system_libs_map['libsockets'])

    if shared.Settings.USE_WEBGPU:
      add_library(system_libs_map['libwebgpu_cpp'])

    if shared.Settings.USE_WEBNN:
      add_library(system_libs_map['libwebnn_cpp'])

  # When LINKABLE is set the entire link command line is wrapped in --whole-archive by
  # building.link_ldd.  And since --whole-archive/--no-whole-archive processing does not nest we
  # shouldn't add any extra `--no-whole-archive` or we will undo the intent of building.link_ldd.
  if shared.Settings.LINKABLE:
    return [l[0] for l in libs_to_link]

  # Wrap libraries in --whole-archive, as needed.  We need to do this last
  # since otherwise the abort sorting won't make sense.
  ret = []
  in_group = False
  for name, need_whole_archive in libs_to_link:
    if need_whole_archive and not in_group:
      ret.append('--whole-archive')
      in_group = True
    if in_group and not need_whole_archive:
      ret.append('--no-whole-archive')
      in_group = False
    ret.append(name)
  if in_group:
    ret.append('--no-whole-archive')

  return ret


class Ports(object):
  """emscripten-ports library management (https://github.com/emscripten-ports).
  """

  @staticmethod
  def get_include_dir():
    dirname = shared.Cache.get_include_dir()
    shared.safe_ensure_dirs(dirname)
    return dirname

  @staticmethod
  def install_header_dir(src_dir, target=None):
    if not target:
      target = os.path.basename(src_dir)
    dest = os.path.join(Ports.get_include_dir(), target)
    shared.try_delete(dest)
    logger.debug('installing headers: ' + dest)
    shutil.copytree(src_dir, dest)

  @staticmethod
  def install_headers(src_dir, pattern="*.h", target=None):
    logger.debug("install_headers")
    dest = Ports.get_include_dir()
    if target:
      dest = os.path.join(dest, target)
      shared.safe_ensure_dirs(dest)
    matches = glob.glob(os.path.join(src_dir, pattern))
    assert matches, "no headers found to install in %s" % src_dir
    for f in matches:
      logger.debug('installing: ' + os.path.join(dest, os.path.basename(f)))
      shutil.copyfile(f, os.path.join(dest, os.path.basename(f)))

  @staticmethod
  def build_port(src_path, output_path, includes=[], flags=[], exclude_files=[], exclude_dirs=[]):
    srcs = []
    for root, dirs, files in os.walk(src_path, topdown=False):
      if any((excluded in root) for excluded in exclude_dirs):
        continue
      for f in files:
        ext = shared.suffix(f)
        if ext in ('.c', '.cpp') and not any((excluded in f) for excluded in exclude_files):
          srcs.append(os.path.join(root, f))
    include_commands = ['-I' + src_path]
    for include in includes:
      include_commands.append('-I' + include)

    commands = []
    objects = []
    for src in srcs:
      obj = src + '.o'
      commands.append([shared.EMCC, '-c', src, '-O2', '-o', obj, '-w'] + include_commands + flags)
      objects.append(obj)

    Ports.run_commands(commands)
    create_lib(output_path, objects)
    return output_path

  @staticmethod
  def run_commands(commands):
    # Runs a sequence of compiler commands, adding importand cflags as defined by get_cflags() so
    # that the ports are built in the correct configuration.
    def add_args(cmd):
      # this must only be called on a standard build command
      assert cmd[0] in (shared.EMCC, shared.EMXX)
      # add standard cflags, but also allow the cmd to override them
      return cmd[:1] + get_base_cflags() + cmd[1:]
    run_build_commands([add_args(c) for c in commands])

  @staticmethod
  def create_lib(libname, inputs): # make easily available for port objects
    create_lib(libname, inputs)

  @staticmethod
  def get_dir():
    dirname = config.PORTS
    shared.safe_ensure_dirs(dirname)
    return dirname

  @staticmethod
  def erase():
    dirname = Ports.get_dir()
    shared.try_delete(dirname)
    if os.path.exists(dirname):
      logger.warning('could not delete ports dir %s - try to delete it manually' % dirname)

  @staticmethod
  def get_build_dir():
    return shared.Cache.get_path('ports-builds')

  name_cache = set()

  @staticmethod
  def fetch_project(name, url, subdir, is_tarbz2=False, sha512hash=None):
    # To compute the sha512 hash, run `curl URL | sha512sum`.
    fullname = os.path.join(Ports.get_dir(), name)

    # EMCC_LOCAL_PORTS: A hacky way to use a local directory for a port. This
    #                   is not tested but can be useful for debugging
    #                   changes to a port.
    #
    # if EMCC_LOCAL_PORTS is set, we use a local directory as our ports. This is useful
    # for testing. This env var should be in format
    #     name=dir,name=dir
    # e.g.
    #     sdl2=/home/username/dev/ports/SDL2
    # so you could run
    #     EMCC_LOCAL_PORTS="sdl2=/home/alon/Dev/ports/SDL2" ./tests/runner.py browser.test_sdl2_mouse
    # this will simply copy that directory into the ports directory for sdl2, and use that. It also
    # clears the build, so that it is rebuilt from that source.
    local_ports = os.environ.get('EMCC_LOCAL_PORTS')
    if local_ports:
      logger.warning('using local ports: %s' % local_ports)
      local_ports = [pair.split('=', 1) for pair in local_ports.split(',')]
      with shared.Cache.lock():
        for local in local_ports:
          if name == local[0]:
            path = local[1]
            if name not in ports.ports_by_name:
              shared.exit_with_error('%s is not a known port' % name)
            port = ports.ports_by_name[name]
            if not hasattr(port, 'SUBDIR'):
              logger.error('port %s lacks .SUBDIR attribute, which we need in order to override it locally, please update it' % name)
              sys.exit(1)
            subdir = port.SUBDIR
            target = os.path.join(fullname, subdir)
            if os.path.exists(target) and not dir_is_newer(path, target):
              logger.warning('not grabbing local port: ' + name + ' from ' + path + ' to ' + fullname + ' (subdir: ' + subdir + ') as the destination ' + target + ' is newer (run emcc --clear-ports if that is incorrect)')
            else:
              logger.warning('grabbing local port: ' + name + ' from ' + path + ' to ' + fullname + ' (subdir: ' + subdir + ')')
              shared.try_delete(fullname)
              shutil.copytree(path, target)
              Ports.clear_project_build(name)
            return

    if is_tarbz2:
      fullpath = fullname + '.tar.bz2'
    elif url.endswith('.tar.gz'):
      fullpath = fullname + '.tar.gz'
    else:
      fullpath = fullname + '.zip'

    if name not in Ports.name_cache: # only mention each port once in log
      logger.debug('including port: ' + name)
      logger.debug('    (at ' + fullname + ')')
      Ports.name_cache.add(name)

    def retrieve():
      # retrieve from remote server
      logger.info('retrieving port: ' + name + ' from ' + url)
      try:
        import requests
        response = requests.get(url)
        data = response.content
      except ImportError:
        from urllib.request import urlopen
        f = urlopen(url)
        data = f.read()

      if sha512hash:
        actual_hash = hashlib.sha512(data).hexdigest()
        if actual_hash != sha512hash:
          shared.exit_with_error('Unexpected hash: ' + actual_hash + '\n'
                                 'If you are updating the port, please update the hash in the port module.')
      open(fullpath, 'wb').write(data)

    def check_tag():
      if is_tarbz2:
        names = tarfile.open(fullpath, 'r:bz2').getnames()
      elif url.endswith('.tar.gz'):
        names = tarfile.open(fullpath, 'r:gz').getnames()
      else:
        names = zipfile.ZipFile(fullpath, 'r').namelist()

      # check if first entry of the archive is prefixed with the same
      # tag as we need so no longer download and recompile if so
      return bool(re.match(subdir + r'(\\|/|$)', names[0]))

    def unpack():
      logger.info('unpacking port: ' + name)
      shared.safe_ensure_dirs(fullname)
      shutil.unpack_archive(filename=fullpath, extract_dir=fullname)

    # before acquiring the lock we have an early out if the port already exists
    if os.path.exists(fullpath) and check_tag():
      return

    # main logic. do this under a cache lock, since we don't want multiple jobs to
    # retrieve the same port at once
    with shared.Cache.lock():
      if os.path.exists(fullpath):
        # Another early out in case another process build the library while we were
        # waiting for the lock
        if check_tag():
          return
        # file exists but tag is bad
        logger.warning('local copy of port is not correct, retrieving from remote server')
        shared.try_delete(fullname)
        shared.try_delete(fullpath)

      retrieve()
      unpack()

      # we unpacked a new version, clear the build in the cache
      Ports.clear_project_build(name)

  @staticmethod
  def clear_project_build(name):
    port = ports.ports_by_name[name]
    port.clear(Ports, shared.Settings, shared)
    shared.try_delete(os.path.join(Ports.get_build_dir(), name))


# get all ports
def get_ports(settings):
  ret = []
  needed = get_needed_ports(settings)

  for port in dependency_order(needed):
    if port.needed(settings):
      try:
        # ports return their output files, which will be linked, or a txt file
        ret += [f for f in port.get(Ports, settings, shared) if not f.endswith('.txt')]
      except Exception:
        logger.error('a problem occurred when using an emscripten-ports library.  try to run `emcc --clear-ports` and then run this command again')
        raise

  ret.reverse()
  return ret


def dependency_order(port_list):
  # Perform topological sort of ports according to the dependency DAG
  port_map = {p.name: p for p in port_list}

  # Perform depth first search of dependecy graph adding nodes to
  # the stack only after all children have been explored.
  stack = []
  unsorted = set(port_list)

  def dfs(node):
    for dep in node.deps:
      child = port_map[dep]
      if child in unsorted:
        unsorted.remove(child)
        dfs(child)
    stack.append(node)

  while unsorted:
    dfs(unsorted.pop())

  return stack


def resolve_dependencies(port_set, settings):
  def add_deps(node):
    node.process_dependencies(settings)
    for d in node.deps:
      dep = ports.ports_by_name[d]
      if dep not in port_set:
        port_set.add(dep)
        add_deps(dep)

  for port in list(port_set):
    add_deps(port)


def get_needed_ports(settings):
  # Start with directly needed ports, and transitively add dependencies
  needed = set(p for p in ports.ports if p.needed(settings))
  resolve_dependencies(needed, settings)
  return needed


def build_port(port_name, settings):
  port = ports.ports_by_name[port_name]
  port_set = set((port,))
  resolve_dependencies(port_set, settings)
  for port in dependency_order(port_set):
    port.get(Ports, settings, shared)


def add_ports_cflags(args, settings):
  # Legacy SDL1 port is not actually a port at all but builtin
  if settings.USE_SDL == 1:
    args += ['-Xclang', '-iwithsysroot/include/SDL']

  needed = get_needed_ports(settings)

  # Now get (i.e. build) the ports independency order.  This is important because the
  # headers from one ports might be needed before we can build the next.
  for port in dependency_order(needed):
    port.get(Ports, settings, shared)
    args += port.process_args(Ports)

  return args


def show_ports():
  print('Available ports:')
  for port in ports.ports:
    print('   ', port.show())


# Once we require python 3.8 we can use shutil.copytree with
# dirs_exist_ok=True and remove this function.
def copytree_exist_ok(src, dest):
  with utils.chdir(src):
    for dirname, dirs, files in os.walk('.'):
      destdir = os.path.join(dest, dirname)
      utils.safe_ensure_dirs(destdir)
      for f in files:
        shared.safe_copy(os.path.join(src, dirname, f), os.path.join(destdir, f))


def install_system_headers(stamp):
  install_dirs = {
    ('include',): '',
    ('lib', 'compiler-rt', 'include'): '',
    ('lib', 'libunwind', 'include'): '',
    # Copy the generic arch files first then
    ('lib', 'libc', 'musl', 'arch', 'generic'): '',
    # Then overlay the emscripten directory on top.
    # This mimicks how musl itself installs its headers.
    ('lib', 'libc', 'musl', 'arch', 'emscripten'): '',
    ('lib', 'libc', 'musl', 'include'): '',
    ('lib', 'libcxx', 'include'): os.path.join('c++', 'v1'),
    ('lib', 'libcxxabi', 'include'): os.path.join('c++', 'v1'),
  }

  target_include_dir = shared.Cache.get_include_dir()
  for src, dest in install_dirs.items():
    src = shared.path_from_root('system', *src)
    dest = os.path.join(target_include_dir, dest)
    copytree_exist_ok(src, dest)

  # Create a stamp file that signal the the header have been installed
  # Removing this file, or running `emcc --clear-cache` or running
  # `./embuilder build sysroot --force` will cause the re-installation of
  # the system headers.
  with open(stamp, 'w') as f:
    f.write('x')
  return stamp


def ensure_sysroot():
  shared.Cache.get('sysroot_install.stamp', install_system_headers, what='system headers')
