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

static void hmap(const double h[9], double u, double v, double *x, double *y)
{
    double d = h[6] * u + h[7] * v + h[8];
    *x = (h[0] * u + h[1] * v + h[2]) / d;
    *y = (h[3] * u + h[4] * v + h[5]) / d;
}

static int quad_h(pc_point a, pc_point b, pc_point c, pc_point d, double h[9])
{
    double qx[4] = {a.x, b.x, c.x, d.x}, qy[4] = {a.y, b.y, c.y, d.y};
    return unit_square_to_quad(qx, qy, h);
}

/* Where each curve handle sits on the flat screen: top and bottom at
 * u = 1/3, 2/3; right and left at v = 1/3, 2/3 (see pc_region). */
static const double CURVE_AT[8][2] = {{1. / 3, 0}, {2. / 3, 0}, {1, 1. / 3}, {1, 2. / 3},
                                      {1. / 3, 1}, {2. / 3, 1}, {0, 1. / 3}, {0, 2. / 3}};

/* The mapping, prepared once: homographies and the curve's bends. */
typedef struct {
    int    shape;
    double h[9], hl[9], hr[9];
    double bend[8][2];      /* curve: each handle's offset from the straight edge */
    double fu;
} rmap;

static int rmap_init(rmap *m, const pc_region *r)
{
    memset(m, 0, sizeof *m);
    m->shape = r->shape;
    if (quad_h(r->c[0], r->c[1], r->c[2], r->c[3], m->h) != 0) return -1;
    if (r->shape == PC_SHAPE_CURVE) {
        for (int k = 0; k < 8; k++) {
            double x, y;
            hmap(m->h, CURVE_AT[k][0], CURVE_AT[k][1], &x, &y);
            m->bend[k][0] = r->curve[k].x - x;
            m->bend[k][1] = r->curve[k].y - y;
        }
    } else if (r->shape == PC_SHAPE_FOLD) {
        m->fu = r->fold_u < 0.05f ? 0.05 : r->fold_u > 0.95f ? 0.95 : r->fold_u;
        if (quad_h(r->c[0], r->fold[0], r->fold[1], r->c[3], m->hl) != 0) return -1;
        if (quad_h(r->fold[0], r->c[1], r->c[2], r->fold[1], m->hr) != 0) return -1;
    }
    return 0;
}

/* Weights of the two handles of an edge at s (0..1 along it): the cubic
 * through 0 at both corners and the handles' bends at s = 1/3 and 2/3. */
static void thirds(double s, double *w1, double *w2)
{
    *w1 = 13.5 * s * (s - 2. / 3) * (s - 1);
    *w2 = -13.5 * s * (s - 1. / 3) * (s - 1);
}

static void rmap_point(const rmap *m, double u, double v, double *x, double *y)
{
    if (m->shape == PC_SHAPE_FOLD) {
        if (u < m->fu) hmap(m->hl, u / m->fu, v, x, y);
        else hmap(m->hr, (u - m->fu) / (1 - m->fu), v, x, y);
        return;
    }
    hmap(m->h, u, v, x, y);
    if (m->shape == PC_SHAPE_CURVE) {
        /* each edge bends as a smooth curve through its two handles; the bend
         * fades linearly towards the opposite edge (a Coons-style blend) */
        double a1, a2, b1, b2;
        thirds(u, &a1, &a2);
        thirds(v, &b1, &b2);
        double wt[8] = {a1 * (1 - v), a2 * (1 - v), b1 * u, b2 * u, a1 * v, a2 * v, b1 * (1 - u), b2 * (1 - u)};
        for (int k = 0; k < 8; k++) {
            *x += wt[k] * m->bend[k][0];
            *y += wt[k] * m->bend[k][1];
        }
    }
}

void pc_region_from_corners(pc_region *r, const pc_point corners[4])
{
    memset(r, 0, sizeof *r);
    memcpy(r->c, corners, sizeof r->c);
    r->fold_u = 0.5f;
    double h[9], x, y;
    int ok = quad_h(corners[0], corners[1], corners[2], corners[3], h) == 0;
    for (int k = 0; k < 8; k++) {
        if (ok) {
            hmap(h, CURVE_AT[k][0], CURVE_AT[k][1], &x, &y);
        } else {                         /* degenerate: along the straight edges */
            double u = CURVE_AT[k][0], v = CURVE_AT[k][1];
            x = (1 - u) * (1 - v) * corners[0].x + u * (1 - v) * corners[1].x + u * v * corners[2].x + (1 - u) * v * corners[3].x;
            y = (1 - u) * (1 - v) * corners[0].y + u * (1 - v) * corners[1].y + u * v * corners[2].y + (1 - u) * v * corners[3].y;
        }
        r->curve[k].x = (float)x;
        r->curve[k].y = (float)y;
    }
    for (int k = 0; k < 2; k++) {        /* fold down the middle */
        double v = k;
        if (ok) hmap(h, 0.5, v, &x, &y);
        else {
            x = (corners[k ? 3 : 0].x + corners[k ? 2 : 1].x) / 2;
            y = (corners[k ? 3 : 0].y + corners[k ? 2 : 1].y) / 2;
        }
        r->fold[k].x = (float)x;
        r->fold[k].y = (float)y;
    }
}

void pc_region_curve_from_mids(pc_region *r, const pc_point mid[4])
{
    rmap m;
    if (quad_h(r->c[0], r->c[1], r->c[2], r->c[3], m.h) != 0) return;
    static const double at[4][2] = {{0.5, 0}, {1, 0.5}, {0.5, 1}, {0, 0.5}};
    for (int e = 0; e < 4; e++) {
        double x, y;
        hmap(m.h, at[e][0], at[e][1], &x, &y);
        double bx = mid[e].x - x, by = mid[e].y - y;   /* a parabola's bend at 1/3 and 2/3 is 8/9 of it */
        for (int k = 0; k < 2; k++) {
            int i = 2 * e + k;
            double px, py;
            hmap(m.h, CURVE_AT[i][0], CURVE_AT[i][1], &px, &py);
            r->curve[i].x = (float)(px + bx * 8 / 9);
            r->curve[i].y = (float)(py + by * 8 / 9);
        }
    }
}

int pc_region_point(const pc_region *r, double u, double v, pc_point *out)
{
    rmap m;
    if (rmap_init(&m, r) != 0) return -1;
    double x, y;
    rmap_point(&m, u, v, &x, &y);
    out->x = (float)x;
    out->y = (float)y;
    return 0;
}

float pc_region_aspect(const pc_region *r, int fw, int fh)
{
    rmap m;
    if (rmap_init(&m, r) != 0) return 2.f;
    const int N = 16;
    double len[4] = {0};               /* top, bottom, left, right */
    for (int k = 0; k < N; k++) {
        double a = (double)k / N, b = (double)(k + 1) / N, x0, y0, x1, y1;
        double seg[4][4] = {{a, 0, b, 0}, {a, 1, b, 1}, {0, a, 0, b}, {1, a, 1, b}};
        for (int e = 0; e < 4; e++) {
            rmap_point(&m, seg[e][0], seg[e][1], &x0, &y0);
            rmap_point(&m, seg[e][2], seg[e][3], &x1, &y1);
            len[e] += hypot((x1 - x0) * fw, (y1 - y0) * fh);
        }
    }
    double w = (len[0] + len[1]) / 2, h = (len[2] + len[3]) / 2;
    return h > 1 ? (float)(w / h) : 2.f;
}

int pc_warp_init(pc_warp *w, int src_w, int src_h, const pc_point corners[4],
                 float inset_pct, int out_w, int out_h)
{
    pc_region r;
    pc_region_from_corners(&r, corners);
    return pc_warp_init_region(w, src_w, src_h, &r, inset_pct, out_w, out_h);
}

int pc_warp_init_region(pc_warp *w, int src_w, int src_h, const pc_region *r,
                        float inset_pct, int out_w, int out_h)
{
    memset(w, 0, sizeof *w);
    if (src_w <= 1 || src_h <= 1 || out_w <= 0 || out_h <= 0) return -1;
    rmap m;
    if (rmap_init(&m, r) != 0) return -1;
    /* the corners' homography, unit square -> source pixels */
    for (int k = 0; k < 9; k++)
        w->h[k] = m.h[k] * (k < 3 ? src_w - 1 : k < 6 ? src_h - 1 : 1);

    w->lut = malloc((size_t)out_w * out_h * 2 * sizeof(int32_t));
    if (!w->lut) return -1;
    w->src_w = src_w; w->src_h = src_h; w->out_w = out_w; w->out_h = out_h;

    double in = inset_pct / 100.0;
    if (in < 0) in = 0;
    if (in > 0.4) in = 0.4;
    for (int j = 0; j < out_h; j++) {
        double v = in + (1 - 2 * in) * (j + 0.5) / out_h;
        for (int i = 0; i < out_w; i++) {
            double u = in + (1 - 2 * in) * (i + 0.5) / out_w;
            double X, Y;
            rmap_point(&m, u, v, &X, &Y);
            X *= src_w - 1;
            Y *= src_h - 1;
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
    *Y = 0; *U = *V = 128;
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
