#!/usr/bin/env python3
import os.path
import platform
import struct
import subprocess
import platform
from distutils.command.build_ext import build_ext

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
LIB_SRC_DIR = os.path.join(ROOT_DIR, 'pypcode', 'native')
SLEIGH_BUILD_DIR = os.path.join(LIB_SRC_DIR, 'build')
# SAN_FLAGS = "-fsanitize=address"
SAN_FLAGS = ""

class FfiPreBuildExtension(build_ext):
    def pre_run(self, ext, ffi):
        try:
            out = subprocess.check_output(['cmake', '--version'])
        except OSError:
            raise RuntimeError('Please install CMake to build')

        cmake_config_args = [
            '-DCMAKE_VERBOSE_MAKEFILE:BOOL=ON',
            '-DCMAKE_WINDOWS_EXPORT_ALL_SYMBOLS=TRUE',
            f'-DCMAKE_CXX_FLAGS=-stdlib=libc++ {SAN_FLAGS}',
            f'-DCMAKE_EXE_LINKER_FLAGS=-stdlib=libc++ {SAN_FLAGS}',
            f'-DCMAKE_MODULE_LINKER_FLAGS=-stdlib=libc++ {SAN_FLAGS}',
            '-DCMAKE_C_COMPILER=clang-14',
            '-DCMAKE_CXX_COMPILER=clang++-14',
            ]
        cmake_build_args = []
        if platform.system() == 'Windows':
            is_64b = (struct.calcsize("P")*8 == 64)
            cmake_config_args += ['-A', 'x64' if is_64b else 'Win32']
            cmake_build_args += ['--config', 'Release']

        cmake_build_args += ['--config', 'Debug']

        # Build sleigh and csleigh library
        subprocess.check_call(['cmake', '-S', '.', '-B', 'build'] + cmake_config_args, cwd=LIB_SRC_DIR)
        subprocess.check_call(['cmake', '--build', 'build', '--parallel', '--verbose'] + cmake_build_args, cwd=LIB_SRC_DIR)

        # Build sla files
        bin_ext = {
          'Windows' : '.exe',
          'Linux'   : '',
          'Darwin'  : ''
        }[platform.system()]
        sleigh_bin = os.path.join(SLEIGH_BUILD_DIR, 'bin', 'sleigh' + bin_ext)
        specfiles_dir = os.path.join(self.build_lib, 'pypcode', 'processors')
        subprocess.check_call([sleigh_bin, '-a', specfiles_dir])

        ffi.cdef(open(os.path.join(SLEIGH_BUILD_DIR, "csleigh.i")).read())

def ffibuilder():
    from cffi import FFI
    ffi = FFI()
    LIBS = {
        'Windows': [],
        'Darwin': ['c++'],
        'Linux': ['c++']
    }[platform.system()]
    ffi.set_source("pypcode._csleigh",
        """
        #include "build/csleigh.i"
        """,
        libraries=['csleigh'] + LIBS,
        include_dirs=[LIB_SRC_DIR],
        extra_compile_args=["-O0", "-ggdb3", f"{SAN_FLAGS if SAN_FLAGS else ''}"],
        extra_link_args=[f"{SAN_FLAGS if SAN_FLAGS else ''}"],
        library_dirs=[os.path.join(SLEIGH_BUILD_DIR, 'lib')])
    return ffi

if __name__ == "__main__":
    ffibuilder().compile(verbose=True, debug=True)
