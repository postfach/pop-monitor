/* Video input via FFmpeg: RTSP from the camera, or a file for replay/testing. */
#ifndef POP_STREAM_H
#define POP_STREAM_H

#include "popcore.h"

typedef struct pop_stream pop_stream;

typedef struct {
    int realtime;     /* files only: pace frames by their timestamps     */
    int loop;         /* files only: restart at end of file              */
    int timeout_s;    /* network read/open timeout (default 10)          */
    const char *rtsp_transport;  /* "tcp" (default) or "http" tunnelling  */
    volatile int *abort;         /* set to non-zero to abort blocking I/O */
} pop_stream_opts;

pop_stream *pop_stream_open(const char *url, const pop_stream_opts *o, char *err, size_t errlen);
/* 1 = frame decoded (valid until next call), 0 = end of stream, -1 = error */
int  pop_stream_read(pop_stream *s, pc_frame *out, int64_t *pts_ms);
void pop_stream_close(pop_stream *s);

#endif
