#include "stream.h"
#include "util.h"

#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/imgutils.h>
#include <libswscale/swscale.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct pop_stream {
    AVFormatContext *fmt;
    AVCodecContext  *dec;
    AVPacket        *pkt;
    AVFrame         *frm;
    struct SwsContext *sws;
    uint8_t         *conv[4];
    int              conv_ls[4];
    int              conv_w, conv_h;
    int              vidx;
    int              is_file;
    pop_stream_opts  o;
    int64_t          deadline_mono;    /* interrupt callback deadline   */
    int64_t          start_mono;       /* realtime pacing               */
    int64_t          loop_offset_ms, last_pts_ms;
};

static int interrupt_cb(void *opaque)
{
    pop_stream *s = opaque;
    return s->deadline_mono && util_mono_ms() > s->deadline_mono;
}

static void arm(pop_stream *s) { s->deadline_mono = util_mono_ms() + 1000LL * s->o.timeout_s; }

pop_stream *pop_stream_open(const char *url, const pop_stream_opts *o, char *err, size_t errlen)
{
    pop_stream *s = calloc(1, sizeof *s);
    if (!s) return NULL;
    if (o) s->o = *o;
    if (s->o.timeout_s <= 0) s->o.timeout_s = 10;
    s->is_file = strstr(url, "://") == NULL || strncmp(url, "file:", 5) == 0;

    s->fmt = avformat_alloc_context();
    s->fmt->interrupt_callback.callback = interrupt_cb;
    s->fmt->interrupt_callback.opaque = s;
    AVDictionary *opt = NULL;
    if (!s->is_file) {
        av_dict_set(&opt, "rtsp_transport", "tcp", 0);
        char us[32];
        snprintf(us, sizeof us, "%lld", (long long)s->o.timeout_s * 1000000LL);
        av_dict_set(&opt, "timeout", us, 0);
    }
    arm(s);
    int rc = avformat_open_input(&s->fmt, url, NULL, &opt);
    av_dict_free(&opt);
    if (rc < 0) {
        av_strerror(rc, err, errlen);
        free(s);   /* avformat_open_input frees fmt on failure */
        return NULL;
    }
    arm(s);
    if ((rc = avformat_find_stream_info(s->fmt, NULL)) < 0) goto fail;
    const AVCodec *codec = NULL;
    s->vidx = av_find_best_stream(s->fmt, AVMEDIA_TYPE_VIDEO, -1, -1, &codec, 0);
    if (s->vidx < 0 || !codec) { rc = AVERROR_STREAM_NOT_FOUND; goto fail; }
    s->dec = avcodec_alloc_context3(codec);
    avcodec_parameters_to_context(s->dec, s->fmt->streams[s->vidx]->codecpar);
    s->dec->thread_count = 1;   /* many cameras per gateway: one thread each */
    if ((rc = avcodec_open2(s->dec, codec, NULL)) < 0) goto fail;
    s->pkt = av_packet_alloc();
    s->frm = av_frame_alloc();
    s->start_mono = util_mono_ms();
    return s;
fail:
    av_strerror(rc, err, errlen);
    pop_stream_close(s);
    return NULL;
}

static int to_pc_frame(pop_stream *s, pc_frame *out)
{
    AVFrame *f = s->frm;
    memset(out, 0, sizeof *out);
    out->width = f->width;
    out->height = f->height;
    if (f->format == AV_PIX_FMT_YUV420P || f->format == AV_PIX_FMT_YUVJ420P) {
        out->fmt = PC_FMT_YUV420P;
        for (int i = 0; i < 3; i++) { out->plane[i] = f->data[i]; out->stride[i] = f->linesize[i]; }
        return 0;
    }
    if (f->format == AV_PIX_FMT_NV12) {
        out->fmt = PC_FMT_NV12;
        for (int i = 0; i < 2; i++) { out->plane[i] = f->data[i]; out->stride[i] = f->linesize[i]; }
        return 0;
    }
    /* anything else: convert to YUV420P */
    if (!s->sws || s->conv_w != f->width || s->conv_h != f->height) {
        sws_freeContext(s->sws);
        if (s->conv[0]) av_freep(&s->conv[0]);
        s->sws = sws_getContext(f->width, f->height, f->format, f->width, f->height,
                                AV_PIX_FMT_YUV420P, SWS_BILINEAR, NULL, NULL, NULL);
        if (!s->sws) return -1;
        if (av_image_alloc(s->conv, s->conv_ls, f->width, f->height, AV_PIX_FMT_YUV420P, 32) < 0)
            return -1;
        s->conv_w = f->width;
        s->conv_h = f->height;
    }
    sws_scale(s->sws, (const uint8_t *const *)f->data, f->linesize, 0, f->height, s->conv, s->conv_ls);
    out->fmt = PC_FMT_YUV420P;
    for (int i = 0; i < 3; i++) { out->plane[i] = s->conv[i]; out->stride[i] = s->conv_ls[i]; }
    return 0;
}

int pop_stream_read(pop_stream *s, pc_frame *out, int64_t *pts_ms)
{
    for (;;) {
        int rc = avcodec_receive_frame(s->dec, s->frm);
        if (rc == 0) {
            AVRational tb = s->fmt->streams[s->vidx]->time_base;
            int64_t pts = s->frm->best_effort_timestamp;
            int64_t ms = pts == AV_NOPTS_VALUE ? s->last_pts_ms + 40
                       : av_rescale_q(pts, tb, (AVRational){1, 1000});
            ms += s->loop_offset_ms;
            s->last_pts_ms = ms;
            if (pts_ms) *pts_ms = ms;
            if (s->is_file && s->o.realtime) {
                int64_t due = s->start_mono + ms;
                int64_t now = util_mono_ms();
                if (due > now) util_sleep_ms((int)(due - now));
            }
            return to_pc_frame(s, out) == 0 ? 1 : -1;
        }
        if (rc != AVERROR(EAGAIN)) {
            if (rc == AVERROR_EOF) return 0;
            return -1;
        }
        arm(s);
        rc = av_read_frame(s->fmt, s->pkt);
        if (rc == AVERROR_EOF && s->is_file && s->o.loop) {
            s->loop_offset_ms = s->last_pts_ms + 40;
            av_seek_frame(s->fmt, s->vidx, 0, AVSEEK_FLAG_BACKWARD);
            avcodec_flush_buffers(s->dec);
            continue;
        }
        if (rc < 0) {
            if (rc == AVERROR_EOF) {             /* drain decoder */
                avcodec_send_packet(s->dec, NULL);
                continue;
            }
            return -1;
        }
        if (s->pkt->stream_index == s->vidx) avcodec_send_packet(s->dec, s->pkt);
        av_packet_unref(s->pkt);
    }
}

void pop_stream_close(pop_stream *s)
{
    if (!s) return;
    if (s->sws) sws_freeContext(s->sws);
    if (s->conv[0]) av_freep(&s->conv[0]);
    av_frame_free(&s->frm);
    av_packet_free(&s->pkt);
    avcodec_free_context(&s->dec);
    if (s->fmt) avformat_close_input(&s->fmt);
    free(s);
}
