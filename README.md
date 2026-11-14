# instantclip

DeckLink 로 녹화 중인(또는 녹화가 끝난) 클립을 원격 웹브라우저에서 보면서 mark in / mark out 을 찍어
subclip 을 만드는 시스템.

```
SDI 1080i59.94 2ch ─ DeckLink ─ ffmpeg (1 process / 채널)
                                 ├─ bwdif_cuda → hires 1080p59.94 4:2:0 MPEG-2 35 Mbps CBR (CPU)  <record>/<이름>.ts
                                 ├─ 480p29.97 H.264 4 Mbps HLS(EVENT)                    <proxy>/<이름>/index.m3u8
                                 ├─ 썸네일 160x90, 2초 간격                               <thumbnail>/<이름>/NNNNNN.jpg
                                 └─ live 640x360 H.264 + Opus → mediamtx RTSP → WebRTC(WHEP) → 녹화제어 모니터
브라우저 편집기 ─ proxy HLS(hls.js) 재생 → mark in/out → Enter
                                         → 인코더 큐 → hires 에서 frame-accurate 재인코딩 → <subclip>/
```

- 편집은 저해상도 proxy 로, subclip 은 고해상도 hires 로 만든다. 두 파일은 같은 ffmpeg 의 같은 필터
  그래프에서 나오므로 시간축이 일치한다 (`-ss` 입력 seek + 재인코딩 = 프레임 정확).
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

UI 의 subclip 코덱 설정은 바꾸는 즉시 이 파일에 저장된다. 나머지는 파일을 고친 뒤 재시작.

| 섹션 | 내용 |
|---|---|
| `http.port` | 웹 UI 포트 (기본 8090) |
| `paths.record / proxy / subclip / thumbnail` | 녹화, proxy, subclip, 썸네일 경로 |
| `capture` | DeckLink `format_code`(Hi59 = 1080i59.94), 오디오 채널, 디인터레이스 필터, 녹화 파일 이름 규칙 (`{prefix}` + strftime), 자동 종료 `auto_stop_sec`(7200 = 2시간), 연장 단위 `extend_sec`(600), `device_nodes` 장치 이름 → `/dev/blackmagic/ioN` (다른 프로세스 사용 감지용, 이 시스템이 캡처할 때마다 자동으로 고쳐짐) |
| `record_hires` | 고해상도 녹화 코덱. 기본 `mpeg2video` 35M CBR (minrate=maxrate=bitrate), GOP 30, B 2, `threads`, `extra_args`(intra_vlc, non_linear_quant, qmin/qmax). `h264_nvenc`/`libx264` 로 바꿀 수도 있음 |
| `record_proxy` | 저해상도 녹화 코덱 (854x480, 29.97, 4M, HLS segment 길이) |
| `live` | WebRTC 모니터용 인코딩 |
| `editor` | 화살표 키 seek 시간(초): `seek_lr` ←/→ (기본 10), `seek_ud` ↓/↑ (기본 60). 버튼 표시도 이 값을 따름 |
| `thumbnail` | 썸네일 간격/크기 |
| `subclip` | subclip 코덱 (h264_nvenc / hevc_nvenc / libx264, preset, rc, bitrate, GOP, faststart, 오디오, gain), `name_template`, 동시 인코딩 수 `workers`(UI 에서 변경, 즉시 적용) |
| `mediamtx` | RTSP 주소, WebRTC 주소/포트 (`webrtc_url` 이 비어 있으면 `브라우저 host:webrtc_port`) |
| `log` | 로그 경로/레벨/회전 |

subclip 이름: `name_template` = `{src}_[{n:03d}]_{start:.2f}_{end:.2f}_{dur:.2f}`
→ `소스클립이름_[N]_시작초_종료초_길이.mp4` (예 `REC_20261113_090000_[001]_125.40_190.20_64.80.mp4`).
N 은 소스 클립별 카운터로 `state.json` 에 저장된다. 카운터가 없는 소스는 subclip 폴더의 기존 번호(괄호 없는 예전 이름 포함) 최댓값 + 1 에서 시작.
편집제어의 RESET 으로 그 소스의 카운터를 [001] 부터 다시 시작한다 (카운터는 모든 사용자가 공유하며, 누가 번호를 쓰거나 리셋하면 모든 화면의 다음 번호가 1초 안에 갱신된다). 같은 이름의 파일이 이미 있으면 덮어쓰지 않고 `-2`, `-3` … 을 붙인다.

MPEG-2 는 NVENC 가 지원하지 않아 CPU 로 인코딩한다 (실측: 1080p59.94 35 Mbps 실시간 60 fps, drop 0).
1080p59.94 는 MPEG-2 Main@High 의 표준 샘플레이트 한도를 넘지만 ffmpeg/NVDEC 등 일반 디코더는 문제없이 재생한다.

## 화면

```
┌ 녹화제어 ────┐┌ 편집제어 ───────────────┐┌ Subclip 코덱 ┐
│              ││                          ││              │
├╌╌╌(드래그)╌╌╌┤│                          │├ 시스템 상태 ─┤
│ 녹화 클립    │├ 미디어 브라우저: Subclip ┤│  + 인코더 큐 │
└──────────────┘└──────────────────────────┘└──────────────┘
```

1. **녹화제어** — DeckLink 선택, 접두어, REC/STOP, live 모니터(400px, WebRTC), dBFS 레벨미터.
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

### 단축키 (플레이어에 포커스가 있을 때만 — 플레이어 오른쪽 위 초록 "단축키 사용 가능", 포커스가 없으면 빨간 "단축키 꺼짐")

| 키 | 동작 | 키 | 동작 |
|---|---|---|---|
| `I` | mark in | `O` | mark out |
| `Ctrl+I` | mark in 으로 이동 후 정지 | `Ctrl+O` | mark out 으로 이동 후 정지 |
| `Enter` | subclip 인코더 큐 등록 | `Space` / `K` | 재생/일시정지 |
| `←` `→` | ∓`editor.seek_lr` 초 (기본 10) | `↓` `↑` | ∓`editor.seek_ud` 초 (기본 60) |
| `J` `L` | -0.5 / +0.5 초 | `,` `.` | -1 / +1 프레임 |

키는 `KeyboardEvent.code` 로 판별하므로 한글 입력 상태에서도 동작한다.
mark in / mark out 프레임 모두 subclip 에 포함된다 (duration = out − in + 1 프레임).
녹화 중인 소스의 subclip 은 대기(WAITING) 중에 인코딩 슬롯을 차지하지 않는다.

## 로그

`logs/instantclip.log` (앱 이벤트), `logs/ffmpeg.log` (모든 ffmpeg 명령줄과 stderr). 10 MB × 10 회전.

## 구조

```
app/main.py       FastAPI: REST, /ws 상태 push(1초), 정적 파일, /media/proxy, /media/thumb
app/config.py     config.json 로드/기본값/원자적 저장
app/logger.py     로그 설정
app/ffutil.py     ffmpeg 프로세스 관리(-progress 파싱, SIGINT 정상 종료), decklink 목록, ffprobe
app/recorder.py   채널(장치)별 preview / record ffmpeg
app/media.py      저장소 레이아웃, 브라우저 동작, proxy 생성
app/encoder.py    subclip 큐/워커
app/sysmon.py     psutil + NVML 상태 수집
web/              index.html, style.css, *.js (vendor: hls.js 1.5.20, mediamtx reader.js)
```
