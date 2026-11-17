# instantclip

DeckLink 로 녹화 중인(또는 녹화가 끝난) 클립을 원격 웹브라우저에서 보면서 mark in / mark out 을 찍어
subclip 을 만드는 시스템.

![녹화 중인 클립 편집 — Subclip 코덱 창을 위로 접고 시스템 상태·인코더 큐를 펼친 화면](instantclip_wwwUI.png)

![같은 화면에서 Subclip 코덱 설정을 펼친 상태](instantclip_wwwUI2.png)

```
SDI 1080i59.94 2ch ─ DeckLink ─ ffmpeg (1 process / 채널)
                                 ├─ bwdif_cuda → hires 1080p59.94 / 29.97 4:2:0 MPEG-2 35 Mbps CBR (CPU)  <record>/<이름>.ts
                                 ├─ 480p29.97 H.264 4 Mbps HLS(EVENT)                    <proxy>/<이름>/index.m3u8
                                 ├─ 썸네일 160x90, 2초 간격                               <thumbnail>/<이름>/NNNNNN.jpg
                                 └─ live 640x360 H.264 + Opus → mediamtx RTSP → WebRTC(WHEP) → 녹화제어 모니터
브라우저 편집기 ─ proxy HLS(hls.js) 재생 → mark in/out → Enter
                                         → 인코더 큐 → hires 에서 frame-accurate 재인코딩 → <subclip>/
```

- 편집은 저해상도 proxy 로, subclip 은 고해상도 hires 로 만든다. 두 파일은 같은 ffmpeg 의 같은 필터
  그래프에서 나오므로 같은 장면에 같은 timestamp 가 붙는다. 인코더는 mark 를 편집기가 그 자리에서 보여 준
  프레임의 timestamp 로 바꾸고, 5초 앞에서 입력 seek 한 뒤 `trim`/`atrim` 으로 그 timestamp 에서 정확히 자른다
  (in 프레임부터 out 바로 앞 프레임까지 = 편집기에서 본 그대로, 오디오·비디오 모두 0 에서 시작).
  입력 `-ss`/`-t` 만으로 자르면 seek 지점 다음의 디코딩 가능한 프레임(최대 GOP 하나, 0.5초)부터 시작해 subclip 전체가 밀린다.
  59.94 원본에서 편집기 한 프레임은 원본 두 프레임이고 그중 뒤의 것을 보여 준다. 컷이 그 두 프레임 사이(field 경계)에
  있으면 in 을 원본 한 프레임 뒤로 옮겨, 이전 장면 한 프레임이 subclip 맨 앞에 끼지 않게 한다.
- 녹화 중인 클립도 proxy 가 HLS EVENT playlist 라서 바로 불러와 편집할 수 있고, 길이는 자동으로 늘어난다.
  녹화 중인 소스의 subclip 은 out 지점이 hires 에 기록될 때까지 `WAITING` 으로 기다렸다가 인코딩한다.
- 이 시스템으로 녹화하지 않은 파일(외부에서 복사한 mp4/mov/mxf 등)은 플레이어로 보내면 proxy 와 썸네일을 자동 생성한다.

## 실행

```bash
./run.sh                    # 처음 실행 시 .venv 생성 + requirements 설치
# http://<host>:8090/
```

서비스로 등록 (sudo 필요):

```bash
sudo cp deploy/instantclip.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now instantclip
journalctl -u instantclip -f
```

CPU 전력(W) 표시 — RAPL 은 기본적으로 root 만 읽을 수 있다:

```bash
sudo cp deploy/rapl-read.conf /etc/tmpfiles.d/ && sudo systemd-tmpfiles --create /etc/tmpfiles.d/rapl-read.conf
```

필요한 것: ffmpeg (`--enable-decklink --enable-ffnvcodec --enable-cuda-nvcc --enable-libopus --enable-libx264`),
mediamtx (RTSP :8554, WebRTC :8889 + UDP :8189 — 원격 브라우저에서 live 모니터를 보려면 방화벽 개방), NVIDIA 드라이버.

## 환경설정 `config.json`

`config.json` 은 이 서버의 운영 설정이라 git 으로 관리하지 않는다 (`.gitignore`). 저장소에는 견본 `config.example.json` 만 있고,
`config.json` 이 없으면 처음 시작할 때 견본을 바탕으로 만들어진다 (견본에도 없는 키는 코드의 기본값으로 채워 넣음).
UI 의 subclip 코덱 설정은 바꾸는 즉시 이 파일에 저장된다. 나머지는 파일을 고친 뒤 재시작.

| 섹션 | 내용 |
|---|---|
| `http.port` | 웹 UI 포트 (기본 8090) |
| `paths.record / proxy / subclip / thumbnail` | 녹화, proxy, subclip, 썸네일 경로 |
| `capture` | DeckLink `format_code`(Hi59 = 1080i59.94), 오디오 채널, 디인터레이스 필터, 기본 녹화 프레임레이트 `fps`(60000/1001 또는 30000/1001), 녹화 파일 이름 규칙 (`{prefix}` + strftime), 자동 종료 `auto_stop_sec`(7200 = 2시간), 연장 단위 `extend_sec`(600), `device_nodes` 장치 이름 → `/dev/blackmagic/ioN` (다른 프로세스 사용 감지용, 이 시스템이 캡처할 때마다 자동으로 고쳐짐) |
| `record_hires` | 고해상도 녹화 코덱. 기본 `mpeg2video` 35M CBR (minrate=maxrate=bitrate), GOP 30, B 2, `threads`, `extra_args`(intra_vlc, non_linear_quant, qmin/qmax). `h264_nvenc`/`libx264` 로 바꿀 수도 있음 |
| `record_proxy` | 저해상도 녹화 코덱 (854x480, 29.97, 4M, HLS segment 길이) |
| `live` | WebRTC 모니터용 인코딩 |
| `editor` | 화살표 키 seek 시간(초): `seek_lr` ←/→ (기본 10), `seek_ud` ↓/↑ (기본 60). 버튼 표시도 이 값을 따름. scene 검출 기본값 `scene_range` (±초, 5), `scene_threshold` (0.3). 프록시 없는 TS 를 브라우저에서 디코딩 `browser_decode` (true), 그 화질 `ts_quality` (`full` / `half` / `quarter`, 기본 full — half 이하는 MPEG-2 lowres 디코딩이라 작은 자막이 깍두기로 뭉개진다), 디코딩 워커 수 `ts_workers` (0 = 자동, CPU 코어 − 2, 2~4) |
| `thumbnail` | 썸네일 간격/크기 |
| `subclip` | subclip 코덱 (h264_nvenc / hevc_nvenc / libx264, preset, rc, bitrate, GOP, faststart, 오디오, gain), `name_template`, 동시 인코딩 수 `workers`(UI 에서 변경, 즉시 적용), 인터레이스 원본 디인터레이스 `deinterlace` (`auto` / `off`) |
| `mediamtx` | RTSP 주소, WebRTC 주소/포트 (`webrtc_url` 이 비어 있으면 `브라우저 host:webrtc_port`) |
| `log` | 로그 경로/레벨/회전 |

subclip 이름: `name_template` = `{src}_[{n:03d}]_{start:.2f}_{end:.2f}_{dur:.2f}`
→ `소스클립이름_[N]_시작초_종료초_길이.mp4` (예 `REC_20261113_090000_[001]_125.40_190.20_64.80.mp4`).
N 은 소스 클립별 카운터로 `state.json` 에 저장된다. 카운터가 없는 소스는 subclip 폴더의 기존 번호(괄호 없는 예전 이름 포함) 최댓값 + 1 에서 시작.
편집제어의 RESET 으로 그 소스의 카운터를 [001] 부터 다시 시작한다 (카운터는 모든 사용자가 공유하며, 누가 번호를 쓰거나 리셋하면 모든 화면의 다음 번호가 1초 안에 갱신된다). 같은 이름의 파일이 이미 있으면 덮어쓰지 않고 `-2`, `-3` … 을 붙인다.

MPEG-2 는 NVENC 가 지원하지 않아 CPU 로 인코딩한다 (실측: 1080p59.94 35 Mbps 실시간 60 fps, drop 0).
1080p59.94 는 MPEG-2 Main@High 의 표준 샘플레이트 한도를 넘지만 ffmpeg/NVDEC 등 일반 디코더는 문제없이 재생한다.

## 화면

왼쪽 위 로고 옆 `[YYYY-MM-DD HH:MM]` 은 서버가 시작할 때 체크아웃되어 있던 코드의 마지막 커밋 일시다.

```
┌ 녹화제어 ────┐┌ 편집제어 ───────────────┐┌ Subclip 코덱 ┐
│              ││                          ││              │
├╌╌╌(드래그)╌╌╌┤│                          │├ 시스템 상태 ─┤
│ 녹화 클립    │├ 미디어 브라우저: Subclip ┤│  + 인코더 큐 │
└──────────────┘└──────────────────────────┘└──────────────┘
```

1. **녹화제어** — DeckLink 선택, 접두어, 녹화 프레임레이트, REC/STOP, live 모니터(400px, WebRTC), dBFS 레벨미터.
   프레임레이트: 1080p 59.94 (필드마다 한 프레임, bwdif mode=1) 또는 1080p 29.97 (프레임마다 한 프레임, mode=0).
   녹화마다 고르며 브라우저에 기억된다 (처음엔 `capture.fps`). 녹화 중에는 바꿀 수 없고 클립 이름 옆에 표시된다.
   제목 오른쪽 `«` 로 왼쪽 열(녹화제어 + 녹화 클립)을 가장자리의 얇은 띠로 접고, 띠를 누르면 다시 펼친다
   (편집제어가 그만큼 넓어짐, 브라우저에 기억, 화면 폭 1050px 이하에서는 없음). 접혀 있어도 녹화는 계속되고 띠에 `● REC` 가 보인다.
   REC 를 누르면 2시간 뒤 자동 종료(녹화만 걸어 두고 자리를 비우는 경우 방지). `+10분` 으로 연장하며,
   녹화 시간과 자동 종료까지 남은 시간을 같은 크기로 표시한다 (10분 미만 노랑, 3분 미만 빨강 깜빡임).
   녹화제어와 그 아래 녹화 클립 목록 사이의 가로선은 끌어서 높이를 바꿀 수 있다 (더블클릭: 기본값, 브라우저에 기억).
   소리는 MUTE 로 시작하고 버튼을 눌러야 나온다. ▾ 를 누르면 접히고 WebRTC 연결도 끊는다.
   `입력 모니터` 를 켜면 녹화 전에도 입력을 볼 수 있다 (켜 둔 동안 DeckLink 장치를 점유).
   다른 프로세스(mediarouter 등)가 쓰고 있는 장치는 목록에서 회색 `— 사용 중` 으로 표시되어 고를 수 없고,
   REC/입력 모니터도 거부된다 (2초마다 `/proc/*/fd` 로 확인; 같은 사용자 계정의 프로세스만 보임).
   Duo 2 는 장치 노드 순서가 목록 순서와 다르다 (Duo (2)=io2, Duo (3)=io1).
2. **편집제어** — 미디어 브라우저에서 클립을 끌어다 놓거나 더블클릭. 첫 프레임에서 정지 상태로 열린다.
   제목 오른쪽 `⏏ EJECT` 로 클립을 내리고 빈 플레이어로 돌아간다 (그 클립의 mark 는 기억되어 다시 열면 복원).
   타임라인 hover = 썸네일, IN/OUT 구간 표시, mark 시간은 클립마다 브라우저에 기억된다.
   SUBCLIP 번호 칸에 다음 번호가 보이고 RESET 으로 [001] 부터 다시 시작한다.
3. **Subclip 코덱** — 바꾸는 즉시 `config.json` 에 저장. 동시 인코딩 수도 여기서 설정 (진행 중인 작업에 바로 적용).
   프레임레이트는 59.94 / 29.97. 편집제어에 29.97 녹화 원본을 올리면 29.97 로 고정된다
   (인코더도 원본보다 높은 프레임레이트로는 변환하지 않으므로, 설정이 59.94 라도 29.97 원본의 subclip 은 29.97).
   제목 오른쪽 `»` 로 오른쪽 열(코덱 + 시스템 상태)을 가장자리의 얇은 띠로 접고, 띠를 누르면 다시 펼친다 (편집제어·브라우저가 그만큼 넓어짐, 브라우저에 기억).
   `▾` 는 설정을 제목줄만 남기고 위로 접는다 (Live 모니터처럼; 그만큼 시스템 상태가 넓어짐, 브라우저에 기억). 화면 폭 1500px 이하에서는 `▾` 만 있다.
4. **미디어 브라우저** — 녹화 클립(REC = 녹화 중)은 녹화제어 아래, subclip 은 편집제어 아래 넓은 목록. 폴더 생성, 이름변경(F2), 삭제(Del),
   Ctrl/Shift 다중선택, subclip 다운로드. 새 subclip 은 오른쪽 패널의 현재 폴더에 저장된다.
   정렬: 이름 / 생성일시(파일 수정 시각), ↓↑ 버튼으로 역순 (패널별로 브라우저에 기억).
   이동: 항목을 (여러 개 선택해서) 폴더 행에 끌어다 놓으면 그 폴더로, `↑` 버튼에 놓으면 상위 폴더로 이동.
   대상에 같은 이름이 하나라도 있으면 아무것도 옮기지 않는다. 인코딩 중인 subclip 은 옮기거나 지울 수 없다.
   녹화 클립의 이름변경/삭제는 proxy·썸네일도 함께 처리하며, 녹화 중인 클립과 대기/인코딩 중인 subclip 작업의 원본은 바꾸거나 지울 수 없다.
5. **시스템 상태** (코덱 아래) — CPU(코어별), RAM, GPU/NVENC/NVDEC/VRAM, NIC 사용률, 온도, 전력, 디스크 남은 용량,
   인코더 큐(가장 최근에 등록한 작업이 맨 위, 진행률·취소).

### 단축키 (편집제어 창 안 어디든 포커스가 있을 때 — 단축키 설명 줄 옆 초록 "단축키 사용 가능", 창 밖으로 포커스가 나가면 빨간 "단축키 꺼짐")

| 키 | 동작 | 키 | 동작 |
|---|---|---|---|
| `I` | mark in | `O` | mark out |
| `Ctrl+I` | mark in 으로 이동 후 정지 | `Ctrl+O` | mark out 으로 이동 후 정지 |
| `Enter` | subclip 인코더 큐 등록 | `Space` / `K` | 재생/일시정지 |
| `←` `→` | ∓`editor.seek_lr` 초 (기본 10) | `↓` `↑` | ∓`editor.seek_ud` 초 (기본 60) |
| `J` `L` | -0.5 / +0.5 초 | `,` `.` | -1 / +1 프레임 |

키는 `KeyboardEvent.code` 로 판별하므로 한글 입력 상태에서도 동작한다.
mark in 프레임은 subclip 에 포함되고 mark out 프레임은 포함되지 않는다 (duration = out − in 프레임, 마지막 프레임은 out 바로 앞 프레임).
녹화 중인 소스의 subclip 은 대기(WAITING) 중에 인코딩 슬롯을 차지하지 않는다.

### Scene change 검출

타임라인 아래 `✂ SCENE 검출` 을 누르면 playhead 앞뒤 `범위 ±N초`(기본 5) 안의 장면 전환(컷)을 찾아,
그 구간만 확대한 띠에 프레임마다의 변화량(막대)과 threshold(점선), 컷(파란 세로선), mark in/out, playhead 를 그린다.
- 컷 = threshold(기본 0.3, 0~1) 이상인 프레임 = **새 장면의 첫 프레임**. threshold 는 바꾸는 즉시 다시 그린다 (재검출 없음).
- 띠를 클릭하면 그 프레임으로 이동하고 정지한다 (컷 근처를 누르면 컷에 맞춰짐). 그 자리에서
  `I` = 새 장면부터 subclip 시작, `O` = 이전 장면의 마지막 프레임까지 (out 프레임은 포함되지 않으므로).
- 버튼을 눌렀을 때만 서버가 계산한다 (proxy 를 ffmpeg `scene` 점수로 분석, 10초 구간 약 0.5초).
  편집기가 보여 주는 proxy 자체를 프레임 단위로 세어 분석하므로 표시되는 프레임 번호가 편집기와 정확히 일치한다.
- 범위·threshold 는 브라우저에 기억되고, 처음 값은 `config.json` `editor.scene_range` / `editor.scene_threshold`.
- 입력 칸에 숫자를 칠 때는 단축키가 동작하지 않는다.

### 외부 녹화 파일 (tvheadend 등) — 브라우저 디코딩

이 시스템이 녹화하지 않은 파일도 녹화 폴더(`paths.record`) 아래에 두거나 **심볼릭 링크**로 연결하면 편집할 수 있다.
```
ln -s /mnt/tvheadend /mnt/raid0_old/capture/tvheadend
```
- 링크로 연결한 폴더는 읽기 전용이다 (UI 에서 이름변경/이동/삭제/폴더 생성 거부 — 원본 보호).
- 처음 여는 폴더의 길이(duration)는 백그라운드 ffprobe 가 끝나면 채워진다 (CIFS 의 tvheadend 776개 약 1분 반, 서버 재시작 전까지 기억).
- 프록시가 없는 MPEG-2 TS(목록의 `TS` 표시)는 프록시를 만들지 않고 **브라우저가 원본을 직접 디코딩**한다
  (`web/tsplayer.js`, wasm 디코더 — [XDCAM50_MXF_webplayer](https://github.com/sendust/XDCAM50_MXF_webplayer) 방식):
  - 서버(`app/tsindex.py`)는 파일 앞 4 MB 와 끝 2 MB 만 읽어 PAT/PMT, 첫 I-프레임(= 프레임 0), 프레임 수를 알아내고,
    프레임 번호를 주면 그 GOP 의 바이트 범위를 보간 탐색(작은 읽기 몇 번, 평균 0.1~0.2 초)으로 찾는다. 파일 전체를 훑지 않는다.
    B-frame / open GOP 도 처리한다 (GOP 범위 = 그 I-프레임부터 다음 I-프레임 뒤 B-frame 까지).
  - 브라우저 워커(`web/tsworker.js`, 여러 개)가 그 범위를 HTTP Range 로 원본에서 받아 TS 를 풀고, MPEG-2 비디오(기본 Full, Half 는 lowres 1)와
    첫 번째 오디오 스트림(AC-3 → 48 kHz 스테레오)을 디코딩한다. 오디오는 프레임 0 기준 샘플 격자로 잘라 GOP 끼리 빈틈 없이 이어진다.
  - 인터레이스 프레임은 첫 필드를 보여준다(WebGL bob). 편집기 프레임 k = 원본의 k 번째 표시 프레임이라
    scene 검출(원본에서 첫 필드로 직접 계산)과 subclip 인코딩도 녹화 클립과 똑같이 프레임 단위로 맞는다.
  - 디코더 `web/vendor/tsdec/tsdec.wasm`(1 MB)은 FFmpeg 7.1.1 의 libavcodec 에서 MPEG-2 / AC-3 / E-AC-3 / MP2 / AAC 디코더만 넣어
    `wasm/build.sh` 로 빌드한다(emsdk 4.0.10, `wasm/tsdec.c`). 디코더를 GOP 사이에 재사용하고 메모리 누수가 없다
    (ffmpeg.wasm 명령줄 코어는 실행마다 메모리가 새서 몇 분 재생하면 디코딩이 멈췄다). 결과는 네이티브 ffmpeg 와 비트 단위로 같다.
  - 측정(서버 CPU, 1080i 15~19 Mbps): 처음 열기 약 0.5 초, 임의 위치 탐색 약 0.25 초(Half) / 0.4 초(Full),
    연속 디코딩 워커 4개 Half 258 fps / Full 197 fps.
    썸네일은 없다. 브라우저에 WebGL2 가 필요하다.
  - 29.97 이 아닌 TS 나 MPEG-2 가 아닌 파일은 지금처럼 프록시를 만들어 편집한다.
- 인코더는 인터레이스 원본(`field_order` tt/bb)을 자동으로 디인터레이스한다: 59.94 설정이면 bwdif `mode=1`(필드마다 한 프레임, 1080p59.94),
  29.97 이면 `mode=0`. 그래서 1080i 원본도 코덱 설정에서 59.94 / 29.97 을 고를 수 있다.
  59.94 로 만들 때 마지막 프레임의 두 필드 사이에 컷이 있으면(편집기에는 안 보이는 다음 장면 한 필드) 그 필드를 뺀다.
- HEVC NVENC 가 B-frame 을 지원하지 않는 GPU(Quadro P2200 등 Pascal)는 처음 인코딩할 때 감지해서 `-bf 0` 으로 인코딩한다.

### 세션 복원 (클라이언트 IP 별)

편집제어 상태(불러온 클립, 플레이헤드 위치, 재생/정지, mark in/out)를 서버가 접속 IP 별로 기억한다.
새로고침하거나 같은 IP 에서 다시 접속하면 그 클립을 다시 불러와 같은 위치로 가고, 재생 중이었으면 이어서 재생한다
(브라우저 자동재생 정책에 막히면 위치만 복원하고 ▶ 안내). EJECT 하면 빈 상태가 기억된다.
`sessions.json` 에 저장 (30일 지난 항목은 시작할 때 정리). 같은 IP 의 여러 브라우저는 하나의 세션을 공유한다.

## 로그

`logs/instantclip.log` (앱 이벤트), `logs/ffmpeg.log` (모든 ffmpeg 명령줄과 stderr). 10 MB × 10 회전.

## 구조

```
app/main.py       FastAPI: REST, /ws 상태 push(1초), 정적 파일, /media/proxy, /media/thumb, /api/ts
app/config.py     config.json 로드(없으면 config.example.json 에서 시작)/기본값/원자적 저장
app/logger.py     로그 설정
app/ffutil.py     ffmpeg 프로세스 관리(-progress 파싱, SIGINT 정상 종료), decklink 목록, ffprobe
app/recorder.py   채널(장치)별 preview / record ffmpeg
app/media.py      저장소 레이아웃, 브라우저 동작, proxy 생성
app/tsindex.py    TS 의 GOP 위치 탐색 (브라우저 디코딩용, 파일 앞/끝과 작은 읽기만)
app/encoder.py    subclip 큐/워커
app/sysmon.py     psutil + NVML 상태 수집
app/session.py    클라이언트 IP 별 편집 세션 (sessions.json)
web/              index.html, style.css, *.js (vendor: hls.js 1.5.20, mediamtx reader.js, tsdec)
wasm/             tsdec.c + build.sh: TS 디코더 wasm 빌드 (출력 web/vendor/tsdec/)
web/ts*.js        TS 브라우저 디코딩: tsworker(디먹스+wasm 디코딩) · tsdecode(워커 풀) · tsplayer(<video> 대역) · yuv(WebGL)
```
