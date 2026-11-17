/* Decoder core for web/tsworker.js: libavcodec's decoders behind a few calls, built to wasm by build.sh.
 *
 *   Dec *d = dec_open("mpeg2video", lowres);      // or "ac3", "eac3", "mp2", "aac" (lowres 0)
 *   int n = dec_run(d, es, len, max);             // decode one elementary-stream chunk from scratch
 *   dec_out(d), dec_len(d), dec_w(d), dec_h(d)    // its output, valid until the next dec_run
 *
 * Video: up to max frames in display order, packed yuv420p (w*h luma, then the two chroma planes); n = frames.
 * Audio: all of it as interleaved stereo float at 48 kHz; n = samples per channel.
 * A decoder is reused across chunks (flushed in between), so nothing is allocated per chunk once warm.
 *
 * Off-air streams are damaged now and then: undecodable packets and frames are skipped (the decoder
 * conceals what it can), audio that changes layout mid-chunk (5.1 <-> 2.0 at a programme boundary) gets a
 * new resampler, and video frames of another size than the chunk's first are left out.
 */
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <emscripten/emscripten.h>
#include <libavcodec/avcodec.h>
#include <libavutil/channel_layout.h>
#include <libswresample/swresample.h>

typedef struct {
  AVCodecContext *c;
  AVCodecParserContext *p;
  AVPacket *pkt;
  AVFrame *f;
  SwrContext *swr;
  AVChannelLayout in_layout;           // what swr was set up for
  int in_fmt, in_rate;
  uint8_t *out;
  size_t cap, len;
  int count, max, w, h;
} Dec;

static int grow(Dec *d, size_t need) {
  if (d->len + need <= d->cap) return 0;
  size_t cap = d->cap ? d->cap : 1 << 20;
  while (cap < d->len + need) cap *= 2;
  uint8_t *o = realloc(d->out, cap);
  if (!o) return AVERROR(ENOMEM);
  d->out = o;
  d->cap = cap;
  return 0;
}

static int take_video(Dec *d, const AVFrame *f) {
  if (d->count >= d->max) return 0;
  if (f->format != AV_PIX_FMT_YUV420P) return AVERROR_PATCHWELCOME;
  int w = f->width, h = f->height, cw = (w + 1) >> 1, ch = (h + 1) >> 1;
  if (d->count && (w != d->w || h != d->h)) return 0;   // the packed output holds one frame size
  int r = grow(d, (size_t)w * h + 2 * (size_t)cw * ch);
  if (r < 0) return r;
  for (int i = 0; i < 3; i++) {
    int pw = i ? cw : w, ph = i ? ch : h;
    for (int y = 0; y < ph; y++, d->len += pw) memcpy(d->out + d->len, f->data[i] + (size_t)y * f->linesize[i], pw);
  }
  d->w = w;
  d->h = h;
  d->count++;
  return 0;
}

/* f NULL: what the resampler still holds */
static int take_audio(Dec *d, const AVFrame *f) {
  if (f && d->swr && (f->format != d->in_fmt || f->sample_rate != d->in_rate ||
                      av_channel_layout_compare(&f->ch_layout, &d->in_layout))) {
    int r = take_audio(d, NULL);      // input changed: drain, then a resampler for the new one
    swr_free(&d->swr);
    if (r < 0) return r;
  }
  if (!d->swr) {
    if (!f) return 0;
    AVChannelLayout stereo = AV_CHANNEL_LAYOUT_STEREO;
    int r = swr_alloc_set_opts2(&d->swr, &stereo, AV_SAMPLE_FMT_FLT, 48000, &f->ch_layout, f->format, f->sample_rate, 0, NULL);
    if (r < 0 || (r = swr_init(d->swr)) < 0) return r;
    av_channel_layout_uninit(&d->in_layout);
    av_channel_layout_copy(&d->in_layout, &f->ch_layout);
    d->in_fmt = f->format;
    d->in_rate = f->sample_rate;
  }
  int n = swr_get_out_samples(d->swr, f ? f->nb_samples : 0), r = grow(d, (size_t)n * 8);
  if (r < 0) return r;
  uint8_t *o = d->out + d->len;
  int got = swr_convert(d->swr, &o, n, f ? (const uint8_t **)f->extended_data : NULL, f ? f->nb_samples : 0);
  if (got < 0) return got;
  d->len += (size_t)got * 8;
  d->count += got;
  return 0;
}

static int drain(Dec *d) {
  int r;
  int bad = 0;
  while ((r = avcodec_receive_frame(d->c, d->f)) >= 0 || (r != AVERROR(EAGAIN) && r != AVERROR_EOF &&
                                                          r != AVERROR(ENOMEM) && ++bad < 1000)) {
    if (r < 0) continue;                // a frame it could not decode: on to the next
    r = d->c->codec_type == AVMEDIA_TYPE_VIDEO ? take_video(d, d->f) : take_audio(d, d->f);
    av_frame_unref(d->f);
    if (r < 0) return r;
  }
  return r == AVERROR(EAGAIN) || r == AVERROR_EOF ? 0 : r;
}

/* one packet, or the end of the stream (data NULL); a damaged packet is skipped like ffmpeg does, whatever
   the decoder calls it (the AC-3 decoder has its own codes, e.g. AC3_PARSE_ERROR_SYNC -0x1030c0a) */
static int send(Dec *d, uint8_t *data, int size) {
  int r;
  d->pkt->data = data;
  d->pkt->size = size;
  while ((r = avcodec_send_packet(d->c, data ? d->pkt : NULL)) == AVERROR(EAGAIN))
    if ((r = drain(d)) < 0) return r;
  if (r == AVERROR(ENOMEM)) return r;
  return drain(d);
}

EMSCRIPTEN_KEEPALIVE Dec *dec_open(const char *name, int lowres) {
  av_log_set_level(AV_LOG_QUIET);   // damaged pictures at a cut GOP are expected; JS reports failures
  const AVCodec *codec = avcodec_find_decoder_by_name(name);
  if (!codec) return NULL;
  Dec *d = calloc(1, sizeof *d);
  d->c = avcodec_alloc_context3(codec);
  d->c->lowres = codec->type == AVMEDIA_TYPE_VIDEO ? lowres : 0;
  d->c->thread_count = 1;
  d->pkt = av_packet_alloc();
  d->f = av_frame_alloc();
  if (!d->c || !d->pkt || !d->f || avcodec_open2(d->c, codec, NULL) < 0) {
    avcodec_free_context(&d->c);
    av_packet_free(&d->pkt);
    av_frame_free(&d->f);
    free(d);
    return NULL;
  }
  return d;
}

EMSCRIPTEN_KEEPALIVE int dec_run(Dec *d, uint8_t *in, int len, int max) {
  avcodec_flush_buffers(d->c);
  if (d->p) av_parser_close(d->p);
  d->p = av_parser_init(d->c->codec_id);
  swr_free(&d->swr);
  d->len = 0;
  d->count = 0;
  d->max = max > 0 ? max : INT_MAX;
  int video = d->c->codec_type == AVMEDIA_TYPE_VIDEO, r;
  uint8_t *data;
  int size;
  while (len > 0) {
    int used = av_parser_parse2(d->p, d->c, &data, &size, in, len, AV_NOPTS_VALUE, AV_NOPTS_VALUE, 0);
    in += used;
    len -= used;
    if (size && (r = send(d, data, size)) < 0) return r;
    if (video && d->count >= d->max) return d->count;
  }
  av_parser_parse2(d->p, d->c, &data, &size, NULL, 0, AV_NOPTS_VALUE, AV_NOPTS_VALUE, 0);
  if (size && (r = send(d, data, size)) < 0) return r;
  if ((r = send(d, NULL, 0)) < 0) return r;
  if (!video && d->swr && (r = take_audio(d, NULL)) < 0) return r;   // samples the resampler holds back
  return d->count;
}

EMSCRIPTEN_KEEPALIVE uint8_t *dec_out(Dec *d) { return d->out; }
EMSCRIPTEN_KEEPALIVE int dec_len(Dec *d) { return (int)d->len; }
EMSCRIPTEN_KEEPALIVE int dec_w(Dec *d) { return d->w; }
EMSCRIPTEN_KEEPALIVE int dec_h(Dec *d) { return d->h; }

EMSCRIPTEN_KEEPALIVE void dec_close(Dec *d) {
  if (!d) return;
  av_parser_close(d->p);
  swr_free(&d->swr);
  av_channel_layout_uninit(&d->in_layout);
  avcodec_free_context(&d->c);
  av_packet_free(&d->pkt);
  av_frame_free(&d->f);
  free(d->out);
  free(d);
}
