/* Per-frame features of the straightened board image. */
#include "popcore.h"
#include <math.h>
#include <string.h>

int pc_hamming64(uint64_t a, uint64_t b)
{
    uint64_t x = a ^ b;
    int n = 0;
    while (x) { x &= x - 1; n++; }
    return n;
}

/* Box-average a plane into gw x gh cells. */
static void box_means(const uint8_t *p, int w, int h, int gw, int gh, float *out)
{
    double sum[128];   /* gw*gh <= 128 (8x4 grid, 9x8 hash) */
    int cnt[128];
    if (gw * gh > 128) return;
    memset(sum, 0, sizeof(double) * gw * gh);
    memset(cnt, 0, sizeof(int) * gw * gh);
    for (int y = 0; y < h; y++) {
        int cy = y * gh / h;
        const uint8_t *row = p + (size_t)y * w;
        for (int x = 0; x < w; x++) {
            int c = cy * gw + x * gw / w;
            sum[c] += row[x];
            cnt[c]++;
        }
    }
    for (int i = 0; i < gw * gh; i++) out[i] = cnt[i] ? (float)(sum[i] / cnt[i]) : 0.f;
}

void pc_features_compute(const uint8_t *y, const uint8_t *u, const uint8_t *v,
                         int w, int h, pc_features *f)
{
    memset(f, 0, sizeof *f);
    box_means(y, w, h, PC_GRID_X, PC_GRID_Y, f->cell_y);
    if (u) box_means(u, w, h, PC_GRID_X, PC_GRID_Y, f->cell_u);
    else for (int i = 0; i < PC_CELLS; i++) f->cell_u[i] = 128.f;
    if (v) box_means(v, w, h, PC_GRID_X, PC_GRID_Y, f->cell_v);
    else for (int i = 0; i < PC_CELLS; i++) f->cell_v[i] = 128.f;

    /* dHash: 9x8 thumbnail, compare horizontal neighbours -> 64 bits */
    float th[9 * 8];
    box_means(y, w, h, 9, 8, th);
    uint64_t hash = 0;
    for (int r = 0; r < 8; r++)
        for (int c = 0; c < 8; c++) {
            hash <<= 1;
            if (th[r * 9 + c] > th[r * 9 + c + 1]) hash |= 1;
        }
    f->dhash = hash;

    /* global stats */
    double s = 0, s2 = 0;
    size_t n = (size_t)w * h, clip = 0;
    for (size_t i = 0; i < n; i++) {
        s += y[i];
        s2 += (double)y[i] * y[i];
        if (y[i] >= 250) clip++;
    }
    f->mean_y = (float)(s / n);
    double var = s2 / n - (s / n) * (s / n);
    f->std_y = (float)sqrt(var > 0 ? var : 0);
    f->clipped = (float)clip / (float)n;

    /* sharpness: variance of 4-neighbour Laplacian */
    double ls = 0, ls2 = 0;
    size_t ln = 0;
    for (int r = 1; r < h - 1; r++)
        for (int c = 1; c < w - 1; c++) {
            const uint8_t *p = y + (size_t)r * w + c;
            int L = 4 * p[0] - p[-1] - p[1] - p[-w] - p[w];
            ls += L;
            ls2 += (double)L * L;
            ln++;
        }
    if (ln) {
        double m = ls / ln;
        f->sharpness = (float)(ls2 / ln - m * m);
    }
}

static inline float cell_dist(const pc_features *a, const pc_features *b, int i)
{
    /* luma weighted fully, chroma at half weight */
    return fabsf(a->cell_y[i] - b->cell_y[i]) +
           0.5f * (fabsf(a->cell_u[i] - b->cell_u[i]) + fabsf(a->cell_v[i] - b->cell_v[i]));
}

float pc_features_changed_cells(const pc_features *a, const pc_features *b, float delta)
{
    int n = 0;
    for (int i = 0; i < PC_CELLS; i++)
        if (cell_dist(a, b, i) > delta) n++;
    return (float)n / PC_CELLS;
}

float pc_features_grid_distance(const pc_features *a, const pc_features *b)
{
    float s = 0;
    for (int i = 0; i < PC_CELLS; i++) s += cell_dist(a, b, i);
    return s / PC_CELLS;
}
