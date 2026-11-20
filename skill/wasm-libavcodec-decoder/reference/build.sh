#!/bin/bash
# Builds web/vendor/tsdec/tsdec.{js,wasm} (the TS decoder of web/tsworker.js): FFmpeg's libavcodec with just
# the MPEG-2 video and AC-3 / E-AC-3 / MP2 / AAC decoders, plus libswresample, behind tsdec.c.
# Needs emsdk (https://emscripten.org, default ~/emsdk); FFmpeg's source is fetched into wasm/build/.
set -e
FFMPEG=7.1.1
EMSDK_VERSION=4.0.10
EMSDK=${EMSDK:-$HOME/emsdk}
CFLAGS="-O3 -msimd128"

cd "$(dirname "$0")"
. "$EMSDK/emsdk_env.sh" >/dev/null 2>&1
emcc --version | head -1 | grep -q "$EMSDK_VERSION" || echo "warning: built with emsdk $EMSDK_VERSION before" >&2
mkdir -p build
B=$(cd build && pwd)

if [ ! -f "$B/prefix/lib/libavcodec.a" ]; then
  [ -d "$B/ffmpeg-$FFMPEG" ] || curl -sfL "https://ffmpeg.org/releases/ffmpeg-$FFMPEG.tar.xz" | tar -xJ -C "$B"
  cd "$B/ffmpeg-$FFMPEG"
  emconfigure ./configure --prefix="$B/prefix" --target-os=none --arch=x86_32 --enable-cross-compile \
    --cc=emcc --cxx=em++ --ar=emar --ranlib=emranlib --nm=emnm \
    --disable-asm --disable-inline-asm --disable-x86asm --disable-stripping --disable-debug \
    --disable-programs --disable-doc --disable-autodetect --disable-pthreads --disable-network \
    --disable-everything --disable-avdevice --disable-avformat --disable-avfilter --disable-swscale \
    --enable-decoder=mpeg2video,ac3,eac3,mp2,aac --enable-parser=mpegvideo,ac3,mpegaudio,aac \
    --extra-cflags="$CFLAGS"
  emmake make -j"$(nproc)" install
  cd - >/dev/null
fi

mkdir -p ../web/vendor/tsdec
emcc $CFLAGS tsdec.c -I"$B/prefix/include" -L"$B/prefix/lib" -lavcodec -lswresample -lavutil \
  -o ../web/vendor/tsdec/tsdec.js \
  -sMODULARIZE -sEXPORT_NAME=createTsDec -sENVIRONMENT=worker -sALLOW_MEMORY_GROWTH -sINITIAL_MEMORY=64MB \
  -sEXPORTED_FUNCTIONS=_malloc,_free,_dec_open,_dec_run,_dec_out,_dec_len,_dec_w,_dec_h,_dec_close \
  -sEXPORTED_RUNTIME_METHODS=ccall,HEAPU8
ls -l ../web/vendor/tsdec
