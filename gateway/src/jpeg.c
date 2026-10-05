#include "jpeg.h"

#include <stdlib.h>
#include <string.h>
#include <turbojpeg.h>

int jpeg_decode_rgb(const uint8_t *jpg, size_t len, uint8_t **rgb, int *w, int *h)
{
    tjhandle t = tjInitDecompress();
    if (!t) return -1;
    int sub, cs;
    if (tjDecompressHeader3(t, jpg, (unsigned long)len, w, h, &sub, &cs) != 0) {
        tjDestroy(t);
        return -1;
    }
    *rgb = malloc((size_t)*w * *h * 3);
    if (!*rgb) { tjDestroy(t); return -1; }
    int rc = tjDecompress2(t, jpg, (unsigned long)len, *rgb, *w, 0, *h, TJPF_RGB, TJFLAG_ACCURATEDCT);
    tjDestroy(t);
    if (rc != 0) { free(*rgb); *rgb = NULL; return -1; }
    return 0;
}

int jpeg_encode_rgb(const uint8_t *rgb, int w, int h, int quality, uint8_t **out, size_t *len)
{
    tjhandle t = tjInitCompress();
    if (!t) return -1;
    unsigned char *buf = NULL;
    unsigned long n = 0;
    int rc = tjCompress2(t, rgb, w, 0, h, TJPF_RGB, &buf, &n, TJSAMP_444, quality, TJFLAG_ACCURATEDCT);
    tjDestroy(t);
    if (rc != 0) { if (buf) tjFree(buf); return -1; }
    /* copy into malloc memory so callers can free() it uniformly */
    *out = malloc(n);
    if (!*out) { tjFree(buf); return -1; }
    memcpy(*out, buf, n);
    *len = n;
    tjFree(buf);
    return 0;
}
