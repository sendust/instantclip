---
name: wasm-libavcodec-decoder
description: 브라우저(Web Worker)에서 ffmpeg.wasm(@ffmpeg/core)으로 GOP/프레임을 반복 디코딩하는 웹 플레이어의 메모리 문제와 그 근본 해결 — ffmpeg CLI 코어를 버리고 필요한 디코더만 넣은 libavcodec wasm(C API)을 emsdk 로 직접 빌드하는 방법. "memory access out of bounds", 몇 분 재생 후 화면/소리 멈춤(타임코드는 증가), 워커/탭 크래시("Target crashed"), core.exec 반복 호출, 32 MB ffmpeg-core.wasm 로딩이 느림, MXF/TS 웹플레이어(XDCAM50_MXF_webplayer, instantclip tsplayer) 디코더 개선 작업에 사용.
---

# ffmpeg.wasm 반복 디코딩의 메모리 문제와 libavcodec wasm 디코더

instantclip(tvheadend 브랜치)의 TS 플레이어에서 겪고 고친 내용이다. 같은 구조(워커마다 `@ffmpeg/core` 하나,
GOP 마다 `core.exec`)인 XDCAM50_MXF_webplayer 에도 그대로 해당한다. 작동하는 구현이 `reference/` 에 있다.

## 1. 증상

- 재생을 몇 분 하면 **타임코드는 계속 올라가는데 화면이 멈추고 소리가 안 난다.**
- 콘솔: `ts decode 99270 memory access out of bounds` (WebAssembly 트랩). 한 번 나면 그 워커의 이후 모든 디코딩이 실패한다.
- 플레이어가 실패한 GOP 를 "건너뛸 수 있는 것(error = ready)"으로 취급하면 시계는 멈추지 않으므로 위 증상이 된다.
- 헤드리스 크롬 soak 테스트에서는 트랩 대신 렌더러가 통째로 죽기도 한다(`Page.evaluate: Target crashed`).

## 2. 원인

`@ffmpeg/core`(0.12.x)는 ffmpeg **명령줄 프로그램**을 wasm 으로 만든 것이다. `core.exec()` 는 `main()` 을 다시 부르는데,
ffmpeg 는 끝날 때 `exit()` → emscripten 에서 예외(abort)로 빠져나가므로 **정리 코드(코덱/프레임풀/버퍼 해제)를 건너뛴다.**
게다가 인자 문자열도 `_malloc` 후 해제하지 않는다. 그래서 exec 마다 메모리가 샌다.

- 실측: 1080i MPEG-2 GOP(15프레임, Half=lowres 1) + AC-3 오디오 = GOP 당 exec 2회, **워커 1개가 약 79 GOP 에서 죽음**.
  워커 4개면 분담하므로 수 분 재생 후 발생.
- 주의: `core.HEAPU8.length` 로 힙을 재면 계속 32 MB 로 보인다(측정 수단으로 믿지 말 것). 누수 확인은 "같은 워커로 GOP N 개
  연속 디코딩 → 몇 번째에서 실패하는가" soak 테스트로 한다(§6).

부수 비용도 크다: exec 마다 인자 파싱·입력 열기·디코더 생성을 처음부터 하고, 데이터가 JS → MEMFS → wasm → MEMFS → JS 로
여러 번 복사되며, ffmpeg-core.wasm 이 32 MB 다.

## 3. 대안 검토 결과

| 방법 | 결론 |
|---|---|
| 코어를 N 회마다 / 오류 시 새로 만들기 | **임시방편**. 컴파일된 `WebAssembly.Module` 을 재사용하면 재생성은 싸다. 근본 해결 아님 |
| WebCodecs | MPEG-2, AC-3 미지원(크롬/엣지). H.264 만 되는 경우가 있으나 MXF 의 다른 코덱은 안 됨 |
| `@ffmpeg/core-mt` | SharedArrayBuffer → HTTPS + COOP/COEP 필요. `http://사내IP` 접속에선 불가. 누수도 그대로 |
| **필요한 디코더만 넣은 libavcodec wasm + C API** | **채택.** 누수 없음, 디코더 재사용, 복사 최소, 1 MB, SIMD |

## 4. 해결: libavcodec 디코더 wasm (reference/tsdec.c, reference/build.sh)

API (C, `EMSCRIPTEN_KEEPALIVE`):

```
Dec *dec_open(const char *codec_name, int lowres);  // avcodec_find_decoder_by_name, lowres 는 open 전에 설정
int  dec_run(Dec *d, uint8_t *es, int len, int max); // 한 덩어리를 처음부터 디코딩. 반환 = 비디오 프레임 수 / 오디오 샘플 수
uint8_t *dec_out(d); int dec_len(d), dec_w(d), dec_h(d);  // 다음 dec_run 까지 유효
void dec_close(d);
```

핵심 설계:
- 디코더 컨텍스트는 **코덱+lowres 별로 한 번 열고 계속 재사용**한다. 덩어리 사이엔 `avcodec_flush_buffers` 와 파서 재생성만.
- 비디오는 출력 프레임(표시 순서)을 packed planar 로 한 버퍼에 이어 붙인다. `max` 프레임 채우면 바로 반환(keyOnly 미리보기).
- 오디오는 swresample 로 **interleaved stereo float 48 kHz** 로 변환(5.1 다운믹스 포함), 끝에 swr flush.
- 패킷 분리는 `av_parser_parse2`(ES 를 그대로 넣음). 끝에서 파서를 NULL 로 flush → 디코더에 NULL(EOF) → drain.
- `avcodec_send_packet` 이 EAGAIN 이면 drain 후 재시도, `AVERROR_INVALIDDATA` 는 건너뜀(잘린 GOP 끝은 정상적으로 깨져 있다).
- `av_log_set_level(AV_LOG_QUIET)` — 안 하면 잘린 GOP 의 concealment 경고가 콘솔에 쏟아진다.

빌드 (build.sh 요약):
- emsdk 4.0.10 (`~/emsdk`, root 불필요): `git clone https://github.com/emscripten-core/emsdk && ./emsdk install 4.0.10 && ./emsdk activate 4.0.10`
- **스크립트는 bash 여야 한다**(`emsdk_env.sh` 가 dash 에서 경로를 못 찾아 `emcc: not found`).
- FFmpeg 7.1.1 configure: `--target-os=none --arch=x86_32 --enable-cross-compile --cc=emcc ... --disable-asm --disable-everything
  --disable-avformat --disable-avfilter --disable-swscale --enable-decoder=... --enable-parser=... --extra-cflags="-O3 -msimd128"`
- 링크: `emcc -O3 -msimd128 tsdec.c -lavcodec -lswresample -lavutil -sMODULARIZE -sEXPORT_NAME=createTsDec -sENVIRONMENT=worker
  -sALLOW_MEMORY_GROWTH -sINITIAL_MEMORY=64MB -sEXPORTED_FUNCTIONS=_malloc,_free,_dec_open,... -sEXPORTED_RUNTIME_METHODS=ccall,HEAPU8`
- 40코어 기준 FFmpeg 빌드 1–2분, 결과 `tsdec.js`(60 KB) + `tsdec.wasm`(1 MB). 결과물을 저장소에 커밋하면 운영엔 emsdk 불필요.
  FFmpeg 소스/빌드 디렉터리(`wasm/build/`)는 gitignore.

## 5. 워커 쪽 사용법 (reference 의 web/tsworker.js 와 동일한 패턴)

```js
// 페이지: WebAssembly.compileStreaming(fetch(WASM)) 한 번 → 모든 워커에 Module 전달 (기존 ffmpeg.wasm 방식 그대로)
let M = null; const decs = new Map();
async function instance() {
  if (M) return M;
  M = await createTsDec({ instantiateWasm(imports, cb) { WebAssembly.instantiate(wasmModule, imports).then(cb); return {}; } });
  decs.clear(); return M;
}
function run(codec, lowres, chunks, max) {            // chunks: Uint8Array[] (PES/KLV payload 조각)
  const k = codec + '/' + lowres;
  let d = decs.get(k);
  if (!d) { d = M.ccall('dec_open', 'number', ['string', 'number'], [codec, lowres]); decs.set(k, d); }
  let n = 0; for (const a of chunks) n += a.length;
  const ptr = M._malloc(n || 1); let o = ptr;
  for (const a of chunks) { M.HEAPU8.set(a, o); o += a.length; }   // concat 없이 힙으로 직접
  const r = M._dec_run(d, ptr, n, max); M._free(ptr);
  if (r < 0) throw new Error(codec + ' error ' + r);
  const out = M._dec_out(d);
  return { n: r, out: M.HEAPU8.slice(out, out + M._dec_len(d)), w: M._dec_w(d), h: M._dec_h(d) };
}
// 작업 실패 시(네트워크 오류 제외) M = null → 다음 작업에서 새 인스턴스 (트랩 후 인스턴스 상태를 믿지 않는다)
```

주의점:
- `M.HEAPU8` 는 메모리가 커지면 새 뷰로 바뀐다 → **변수에 잡아 두지 말고 매번 `M.HEAPU8`** 로 접근.
- 결과는 `slice()`(복사본)로 꺼내야 `postMessage` transfer 가능하고, 다음 `dec_run` 에 덮이지 않는다.
- 출력 크기는 C 가 알려 주는 `w/h` 로 계산(lowres 의 `AV_CEIL_RSHIFT` 와 일치: 1920x1080 lowres1 = 960x540).
- 플레이어 쪽: 실패한 GOP 를 영구히 "error" 로 두지 말고 일정 시간(2초) 후 재시도하게 할 것.
- HTTP/1.1 은 호스트당 연결 6개. 워커 수만큼 Range fetch 가 동시에 열리므로 **워커는 4개 이하**로 해서 API/페이지용 연결을 남긴다
  (멈춘 탭이 6개를 다 물고 있으면 새 탭에서 페이지가 아예 안 열린다).

## 6. 검증 방법 (반드시 할 것)

1. **soak**: 워커 1개로 연속 GOP 600개 디코딩 → 오류 0 (ffmpeg.wasm 은 79번째에서 실패).
2. **비트 정확도**: GOP 시작/중간/끝 프레임, 파일 첫/마지막 프레임의 Y 평면을 네이티브 `ffmpeg -ss … -copyts -vf select=…` 출력과
   PSNR 비교 → 해당 프레임 99(동일), 이웃 프레임보다 높아야 함.
3. **오디오**: 연속 3 GOP 를 이어 붙여 네이티브 `-ac 2 -ar 48000 f32le` 와 상호상관 → lag 0 샘플.
4. **처리량/탐색 시간**: 워커 4개 연속 40 GOP, 임의 위치 8회 median.

instantclip 실측(1080i MPEG-2 15–19 Mbps, CIFS 원본):

| | ffmpeg.wasm | libavcodec wasm |
|---|---|---|
| soak (워커 1) | 79 GOP 에서 실패 | 600 GOP 무오류 |
| 임의 탐색 Half / Full | 0.51 / 0.77 s | 0.25 / 0.39 s |
| 처리량 4워커 Half / Full | 185 / 84 fps | 258 / 197 fps |
| wasm 크기 | 32 MB | 1 MB |

## 7. MXF 웹플레이어에 적용할 때

`reference/mxf-adaptation.md` 를 볼 것 — 코덱 목록, 4:2:2 / 10-bit 출력, 파서 없이 KLV 프레임 단위로 넣는 API, lowres 가 없는
코덱의 축소 처리.
