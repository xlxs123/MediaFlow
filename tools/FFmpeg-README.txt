FFmpeg / ffprobe Windows x64 static GPL build
Provider: BtbN/FFmpeg-Builds
Version: n9.0.2-33-g4cf298e66a-20261022
Archive: https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-22-13-06/ffmpeg-n9.0.2-33-g4cf298e66a-win64-gpl-9.0.zip
Archive SHA-256: 5a8898e31f2c452062d1f1be475befd8ffba04db11e9f8251da3d10d9a1ae2e5
Build recipe commit: 937868360a21d711ec94a33f5591d345ad9b7754
Workflow run: https://github.com/BtbN/FFmpeg-Builds/actions/runs/39776938430
FFmpeg corresponding source: https://github.com/FFmpeg/FFmpeg/tree/4cf298e66a
Corresponding source cache, recipe snapshot and supplementary sources are documented next to the official MediaFlow release and in licenses/THIRD_PARTY_SOURCES.md. The download-cache alone omits FFmpeg itself, so the exact source snapshot is provided separately. Build scripts and source are supplied for rebuilding; bit-for-bit reproducibility is not asserted.

This GPLv3+ component is separate from the MIT application source; retain FFmpeg-LICENSE.

Actual supplied binary configuration:

  configuration:
    --prefix=/ffbuild/prefix
    --pkg-config-flags=--static
    --pkg-config=pkg-config --cross-prefix=x86_64-w64-mingw32-
    --arch=x86_64
    --target-os=mingw32
    --enable-gpl
    --enable-version3
    --disable-debug
    --disable-w32threads
    --enable-pthreads
    --enable-iconv
    --enable-zlib
    --enable-libxml2
    --enable-libvmaf
    --disable-libxcb
    --disable-xlib
    --enable-fontconfig
    --enable-libharfbuzz
    --enable-libfreetype
    --enable-libfribidi
    --enable-lcms2
    --enable-vulkan
    --enable-libdav1d
    --enable-libvorbis
    --enable-librav1e
    --enable-librsvg
    --disable-libpulse
    --enable-gmp
    --enable-lzma
    --enable-liblcevc-dec
    --enable-opencl
    --enable-amf
    --enable-libaom
    --enable-avisynth
    --enable-chromaprint
    --enable-libdavs2
    --enable-libdvdread
    --enable-libdvdnav
    --disable-libfdk-aac
    --enable-ffnvcodec
    --enable-cuda-llvm
    --enable-frei0r
    --enable-libgme
    --enable-libkvazaar
    --enable-libaribb24
    --enable-libaribcaption
    --enable-libass
    --enable-libbluray
    --enable-libjxl
    --enable-libmp3lame
    --enable-libopus
    --enable-libplacebo
    --enable-librist
    --enable-libssh
    --enable-libtheora
    --enable-libvpx
    --enable-libwebp
    --enable-libzmq
    --enable-lv2
    --enable-libvpl
    --enable-openal
    --enable-liboapv
    --enable-libopencore-amrnb
    --enable-libopencore-amrwb
    --enable-libopenh264
    --enable-libopenjpeg
    --enable-libopenmpt
    --enable-librubberband
    --enable-schannel
    --enable-sdl2
    --enable-libsnappy
    --enable-libsoxr
    --enable-libsrt
    --enable-libsvtav1
    --enable-libtwolame
    --enable-libuavs3d
    --disable-libdrm
    --enable-vaapi
    --enable-vapoursynth
    --enable-libvidstab
    --enable-libvvenc
    --disable-whisper
    --enable-libx264
    --enable-libx265
    --enable-libxavs2
    --enable-libxvid
    --enable-libzimg
    --enable-libzvbi
    --extra-cflags=-DLIBTWOLAME_STATIC
    --extra-cxxflags=
    --extra-libs=-lgomp
    --extra-ldflags=-pthread
    --extra-ldexeflags=
    --cc=x86_64-w64-mingw32-gcc
    --cxx=x86_64-w64-mingw32-g++
    --ar=x86_64-w64-mingw32-gcc-ar
    --ranlib=x86_64-w64-mingw32-gcc-ranlib
    --nm=x86_64-w64-mingw32-gcc-nm
    --extra-version=20261022

Exiting with exit code 0
ffmpeg version n9.0.2-33-g4cf298e66a-20261022 Copyright (c) 2000-2026 the FFmpeg developers
  built with gcc 16.2.0 (crosstool-NG 1.29.0.7_b1a94f6)
  configuration: --prefix=/ffbuild/prefix --pkg-config-flags=--static --pkg-config=pkg-config --cross-prefix=x86_64-w64-mingw32- --arch=x86_64 --target-os=mingw32 --enable-gpl --enable-version3 --disable-debug --disable-w32threads --enable-pthreads --enable-iconv --enable-zlib --enable-libxml2 --enable-libvmaf --disable-libxcb --disable-xlib --enable-fontconfig --enable-libharfbuzz --enable-libfreetype --enable-libfribidi --enable-lcms2 --enable-vulkan --enable-libdav1d --enable-libvorbis --enable-librav1e --enable-librsvg --disable-libpulse --enable-gmp --enable-lzma --enable-liblcevc-dec --enable-opencl --enable-amf --enable-libaom --enable-avisynth --enable-chromaprint --enable-libdavs2 --enable-libdvdread --enable-libdvdnav --disable-libfdk-aac --enable-ffnvcodec --enable-cuda-llvm --enable-frei0r --enable-libgme --enable-libkvazaar --enable-libaribb24 --enable-libaribcaption --enable-libass --enable-libbluray --enable-libjxl --enable-libmp3lame --enable-libopus --enable-libplacebo --enable-librist --enable-libssh --enable-libtheora --enable-libvpx --enable-libwebp --enable-libzmq --enable-lv2 --enable-libvpl --enable-openal --enable-liboapv --enable-libopencore-amrnb --enable-libopencore-amrwb --enable-libopenh264 --enable-libopenjpeg --enable-libopenmpt --enable-librubberband --enable-schannel --enable-sdl2 --enable-libsnappy --enable-libsoxr --enable-libsrt --enable-libsvtav1 --enable-libtwolame --enable-libuavs3d --disable-libdrm --enable-vaapi --enable-vapoursynth --enable-libvidstab --enable-libvvenc --disable-whisper --enable-libx264 --enable-libx265 --enable-libxavs2 --enable-libxvid --enable-libzimg --enable-libzvbi --extra-cflags=-DLIBTWOLAME_STATIC --extra-cxxflags= --extra-libs=-lgomp --extra-ldflags=-pthread --extra-ldexeflags= --cc=x86_64-w64-mingw32-gcc --cxx=x86_64-w64-mingw32-g++ --ar=x86_64-w64-mingw32-gcc-ar --ranlib=x86_64-w64-mingw32-gcc-ranlib --nm=x86_64-w64-mingw32-gcc-nm --extra-version=20261022
  libavutil      61.  1.102 / 61.  1.102
  libavcodec     63.  1.102 / 63.  1.102
  libavformat    63.  1.102 / 63.  1.102
  libavdevice    63.  1.102 / 63.  1.102
  libavfilter    12.  1.102 / 12.  1.102
  libswscale     10.  1.102 / 10.  1.102
  libswresample   7.  1.102 /  7.  1.102
