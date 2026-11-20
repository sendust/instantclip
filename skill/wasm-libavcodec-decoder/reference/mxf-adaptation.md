# XDCAM50_MXF_webplayer 에 적용하기

기준: `public/js/decoder-worker.js` (2026-11-17, 922e3a8). 구조는 instantclip 과 같다 — 워커마다 `createFFmpegCore`,
GOP 마다 `runFfmpeg(['-f', info.fmt, '-i', '/in.es', ... '-f', 'rawvideo', '-pix_fmt', pix, '/out.yuv'])`,
lowres 실패 시 `-vf scale=...` 로 다시 exec. 오디오(PCM, AES3)는 이미 JS 로 직접 풀고 있으므로 **wasm 은 비디오만** 필요하다.

## 1. 넣을 디코더

`probeEs()` 가 고르는 포맷 → libavcodec 디코더 이름:

| probeEs fmt | 디코더 | lowres | 비고 |
|---|---|---|---|
| mpegvideo | `mpeg2video` | 0–3 | XDCAM HD 422 50 = 4:2:2 8-bit, IMX(D-10) 도 여기 |
| dnxhd | `dnxhd` | 없음 | 8/10-bit 4:2:2 |
| j2k_pipe | `jpeg2000` | 있음(`lowres`) | 느림 |
| mjpeg | `mjpeg` | 0–3 | |
| h264 | `h264` | 없음 | AVC-Intra/XAVC 는 10-bit 4:2:2 가 많음 |

configure: `--enable-decoder=mpeg2video,dnxhd,jpeg2000,mjpeg,h264` (+ swscale 를 쓸 경우 `--enable-swscale`, 아래 4절).
오디오 디코더와 swresample 은 빼도 된다.

## 2. 파서 대신 프레임 단위 입력 (권장)

MXF 는 frame wrapping 이라 KLV 하나 = 그림 하나다(`demux()` 의 `pics[]`). 그러니 파서 없이 각 조각을 **패킷 하나로 바로**
`avcodec_send_packet` 하는 게 정확하고 빠르다(h264/jpeg2000 파서 불필요, 파서의 한 프레임 지연도 없음).

API 를 이렇게 바꾼다:

```c
// sizes[i] 바이트씩 연속으로 놓인 count 개의 패킷
EMSCRIPTEN_KEEPALIVE int dec_run_packets(Dec *d, uint8_t *data, int32_t *sizes, int count, int max);
```

- 내부: flush_buffers → 각 패킷 send(+drain) → `max` 채우면 반환 → 아니면 NULL 로 EOF → drain.
- JS: `pics` 를 힙에 이어 쓰고, `Int32Array` 크기 배열도 `_malloc` 해서 `M.HEAP32.set(sizes, sizesPtr >> 2)`.
  (`EXPORTED_RUNTIME_METHODS` 에 `HEAP32` 추가)
- 패킷에 pts 가 필요 없다: 출력 순서 = 표시 순서, 개수로 자른다(GOP 의 B-frame 처리 규칙은 기존 MXF 코드와 동일하게 유지).

## 3. 픽셀 포맷 일반화

reference `tsdec.c` 의 `take_video` 는 `AV_PIX_FMT_YUV420P` 만 받는다. MXF 용으로는:

```c
const AVPixFmtDescriptor *pd = av_pix_fmt_desc_get(f->format);
int cw = AV_CEIL_RSHIFT(w, pd->log2_chroma_w), ch = AV_CEIL_RSHIFT(h, pd->log2_chroma_h);
int bps = pd->comp[0].depth > 8 ? 2 : 1;           // 10-bit 은 16비트 little-endian 그대로 복사
// 행 복사 폭 = pw * bps
```

그리고 `dec_fmt(d)` 로 `{log2_chroma_w, log2_chroma_h, depth}` 를 JS 에 알려 준다. 렌더러 선택지:

- 8-bit: 지금 렌더러(yuv420p/422p/444p) 그대로.
- 10-bit: (a) WebGL2 `R16UI`/`R16F` 텍스처로 올려 셰이더에서 `/1023.0` — 정확하고 빠름. 또는 (b) C 에서 `>> 2` 로 8-bit 로
  내려 기존 렌더러 재사용 — 간단. 미리보기 용도면 (b) 로 충분.

## 4. lowres 가 없는 코덱(dnxhd, h264)의 Half/Quarter

기존 코드는 `-vf scale=w:h:flags=fast_bilinear` 로 exec 를 한 번 더 했다. 선택지:

1. **풀해상도 그대로 넘기고 WebGL 이 축소해서 그린다** — 가장 간단. 대신 전송/캐시 메모리가 4배.
2. C 에서 2x2(또는 4x4) 박스 평균으로 줄여 출력 — swscale 없이 수십 줄, 빠름. **권장.**
3. `--enable-swscale` 로 `sws_scale` — 크기 증가(~300 KB) 대비 이득 적음.

## 5. 이행 순서

1. `reference/build.sh` 복사 → 디코더 목록/파서 제거/swresample 제거로 수정, `-sEXPORT_NAME=createMxfDec`.
2. `tsdec.c` → `mxfdec.c`: `dec_run_packets`, 픽셀 포맷 일반화, 2x 축소 추가.
3. `decoder-worker.js`: `runFfmpeg`/`core.FS` 제거, `run()` 패턴(SKILL.md §5)으로 교체. 오디오 코드는 그대로.
4. `public/vendor/ffmpeg-core.*` 삭제, 페이지의 wasm URL 교체.
5. SKILL.md §6 검증: soak(워커 1, GOP 수백 개), 네이티브 ffmpeg 와 PSNR(코덱별 샘플 파일 하나씩), 처리량.
