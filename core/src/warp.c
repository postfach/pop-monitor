/* ROI perspective warp: quadrilateral in the source frame -> flat image. */
#include "popcore.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

/* Solve A x = b for 8x8 with partial pivoting. Returns 0 on success. */
static int solve8(double A[8][8], double b[8], double x[8])
{
    for (int c = 0; c < 8; c++) {
        int p = c;
        for (int r = c + 1; r < 8; r++)
            if (fabs(A[r][c]) > fabs(A[p][c])) p = r;
        if (fabs(A[p][c]) < 1e-12) return -1;
        if (p != c) {
            for (int k = 0; k < 8; k++) { double t = A[c][k]; A[c][k] = A[p][k]; A[p][k] = t; }
            double t = b[c]; b[c] = b[p]; b[p] = t;
        }
        for (int r = c + 1; r < 8; r++) {
            double f = A[r][c] / A[c][c];
            for (int k = c; k < 8; k++) A[r][k] -= f * A[c][k];
            b[r] -= f * b[c];
        }
    }
    for (int r = 7; r >= 0; r--) {
        double s = b[r];
        for (int k = r + 1; k < 8; k++) s -= A[r][k] * x[k];
        x[r] = s / A[r][r];
    }
    return 0;
}

/* Homography mapping (u,v) in the unit square to the quad (pixels). */
static int unit_square_to_quad(const double qx[4], const double qy[4], double h[9])
{
    static const double ux[4] = {0, 1, 1, 0}, uy[4] = {0, 0, 1, 1};
    double A[8][8], b[8], x[8];
    for (int i = 0; i < 4; i++) {
        double u = ux[i], v = uy[i], X = qx[i], Y = qy[i];
        double r0[8] = {u, v, 1, 0, 0, 0, -u * X, -v * X};
        double r1[8] = {0, 0, 0, u, v, 1, -u * Y, -v * Y};
        memcpy(A[2 * i], r0, sizeof r0);
        memcpy(A[2 * i + 1], r1, sizeof r1);
        b[2 * i] = X;
        b[2 * i + 1] = Y;
    }
    if (solve8(A, b, x) != 0) return -1;
    for (int i = 0; i < 8; i++) h[i] = x[i];
    h[8] = 1.0;
    return 0;
}

int pc_warp_init(pc_warp *w, int src_w, int src_h, const pc_point corners[4],
                 float inset_pct, int out_w, int out_h)
{
    memset(w, 0, sizeof *w);
    if (src_w <= 1 || src_h <= 1 || out_w <= 0 || out_h <= 0) return -1;
    double qx[4], qy[4];
    for (int i = 0; i < 4; i++) {
        qx[i] = corners[i].x * (src_w - 1);
        qy[i] = corners[i].y * (src_h - 1);
    }
    if (unit_square_to_quad(qx, qy, w->h) != 0) return -1;

    w->lut = malloc((size_t)out_w * out_h * 2 * sizeof(int32_t));
    if (!w->lut) return -1;
    w->src_w = src_w; w->src_h = src_h; w->out_w = out_w; w->out_h = out_h;

    double in = inset_pct / 100.0;
    if (in < 0) in = 0;
    if (in > 0.4) in = 0.4;
    const double *h = w->h;
    for (int j = 0; j < out_h; j++) {
        double v = in + (1 - 2 * in) * (j + 0.5) / out_h;
        for (int i = 0; i < out_w; i++) {
            double u = in + (1 - 2 * in) * (i + 0.5) / out_w;
            double d = h[6] * u + h[7] * v + h[8];
            double X = (h[0] * u + h[1] * v + h[2]) / d;
            double Y = (h[3] * u + h[4] * v + h[5]) / d;
            if (X < 0) X = 0;
            if (Y < 0) Y = 0;
            if (X > src_w - 1.001) X = src_w - 1.001;
            if (Y > src_h - 1.001) Y = src_h - 1.001;
            size_t k = ((size_t)j * out_w + i) * 2;
            w->lut[k] = (int32_t)(X * 65536.0);
            w->lut[k + 1] = (int32_t)(Y * 65536.0);
        }
    }
    return 0;
}

void pc_warp_free(pc_warp *w)
{
    free(w->lut);
    w->lut = NULL;
}

/* Bilinear sample of an 8-bit plane with pixel step `step` (1 or 2). */
static inline int bil(const uint8_t *p, int stride, int step, int maxx, int maxy,
                      int32_t fx, int32_t fy)
{
    int x0 = fx >> 16, y0 = fy >> 16;
    int ax = (fx >> 8) & 0xff, ay = (fy >> 8) & 0xff;
    int x1 = x0 < maxx ? x0 + 1 : x0, y1 = y0 < maxy ? y0 + 1 : y0;
    const uint8_t *r0 = p + (size_t)y0 * stride, *r1 = p + (size_t)y1 * stride;
    int a = r0[x0 * step], b = r0[x1 * step], c = r1[x0 * step], d = r1[x1 * step];
    int top = a * (256 - ax) + b * ax;
    int bot = c * (256 - ax) + d * ax;
    return (top * (256 - ay) + bot * ay + (1 << 15)) >> 16;
}

static inline uint8_t clamp8(int v) { return (uint8_t)(v < 0 ? 0 : v > 255 ? 255 : v); }

static void sample_yuv(const pc_frame *f, int32_t fx, int32_t fy, int *Y, int *U, int *V)
{
    int W = f->width, H = f->height;
    switch (f->fmt) {
    case PC_FMT_GRAY:
        *Y = bil(f->plane[0], f->stride[0], 1, W - 1, H - 1, fx, fy);
        *U = *V = 128;
        break;
    case PC_FMT_NV12: {
        *Y = bil(f->plane[0], f->stride[0], 1, W - 1, H - 1, fx, fy);
        int32_t cx = fx / 2, cy = fy / 2;
        int cw = (W + 1) / 2 - 1, ch = (H + 1) / 2 - 1;
        *U = bil(f->plane[1], f->stride[1], 2, cw, ch, cx, cy);
        *V = bil(f->plane[1] + 1, f->stride[1], 2, cw, ch, cx, cy);
        break;
    }
    case PC_FMT_YUV420P: {
        *Y = bil(f->plane[0], f->stride[0], 1, W - 1, H - 1, fx, fy);
        int32_t cx = fx / 2, cy = fy / 2;
        int cw = (W + 1) / 2 - 1, ch = (H + 1) / 2 - 1;
        *U = bil(f->plane[1], f->stride[1], 1, cw, ch, cx, cy);
        *V = bil(f->plane[2], f->stride[2], 1, cw, ch, cx, cy);
        break;
    }
    case PC_FMT_RGB24: {
        int r = bil(f->plane[0], f->stride[0], 3, W - 1, H - 1, fx, fy);
        int g = bil(f->plane[0] + 1, f->stride[0], 3, W - 1, H - 1, fx, fy);
        int b = bil(f->plane[0] + 2, f->stride[0], 3, W - 1, H - 1, fx, fy);
        /* BT.601 full range */
        *Y = (77 * r + 150 * g + 29 * b + 128) >> 8;
        *U = ((-43 * r - 85 * g + 128 * b + 128) >> 8) + 128;
        *V = ((128 * r - 107 * g - 21 * b + 128) >> 8) + 128;
        break;
    }
    }
}

void pc_warp_apply(const pc_warp *w, const pc_frame *f,
                   uint8_t *oy, uint8_t *ou, uint8_t *ov)
{
    size_t n = (size_t)w->out_w * w->out_h;
    for (size_t k = 0; k < n; k++) {
        int Y, U, V;
        sample_yuv(f, w->lut[2 * k], w->lut[2 * k + 1], &Y, &U, &V);
        oy[k] = clamp8(Y);
        if (ou) ou[k] = clamp8(U);
        if (ov) ov[k] = clamp8(V);
    }
}

void pc_warp_apply_rgb(const pc_warp *w, const pc_frame *f, uint8_t *rgb)
{
    size_t n = (size_t)w->out_w * w->out_h;
    for (size_t k = 0; k < n; k++) {
        int32_t fx = w->lut[2 * k], fy = w->lut[2 * k + 1];
        if (f->fmt == PC_FMT_RGB24) {
            int W = f->width, H = f->height;
            for (int c = 0; c < 3; c++)
                rgb[3 * k + c] = clamp8(bil(f->plane[0] + c, f->stride[0], 3, W - 1, H - 1, fx, fy));
            continue;
        }
        int Y, U, V;
        sample_yuv(f, fx, fy, &Y, &U, &V);
        int d = U - 128, e = V - 128;
        rgb[3 * k]     = clamp8(Y + ((359 * e + 128) >> 8));
        rgb[3 * k + 1] = clamp8(Y - ((88 * d + 183 * e + 128) >> 8));
        rgb[3 * k + 2] = clamp8(Y + ((454 * d + 128) >> 8));
    }
}
