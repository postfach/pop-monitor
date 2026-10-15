/* Snapshot quality scoring for choosing the best frame of a burst. */
#include "popcore.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static float base_score(const pc_snapshot_quality *q)
{
    float s = logf(1.f + q->sharpness) * (1.f - 2.f * q->clipped)
            - 0.5f * (q->banding > 1.5f ? q->banding - 1.5f : 0.f)
            - 0.05f * (float)q->hash_distance;
    return q->usable ? s : s - 100.f;
}

int pc_snapshot_score(const pc_frame *f, const pc_point corners[4], float inset_pct,
                      const pc_features *ref, pc_snapshot_quality *q,
                      uint8_t **board_y, int *board_w, int *board_h)
{
    pc_region r;
    pc_region_from_corners(&r, corners);
    return pc_snapshot_score_region(f, &r, inset_pct, ref, q, board_y, board_w, board_h);
}

int pc_snapshot_score_region(const pc_frame *f, const pc_region *r, float inset_pct,
                             const pc_features *ref, pc_snapshot_quality *q,
                             uint8_t **board_y, int *board_w, int *board_h)
{
    memset(q, 0, sizeof *q);
    if (board_y) *board_y = NULL;
    float aspect = pc_region_aspect(r, f->width, f->height);
    int ow = 384;
    int oh = aspect > 0.01f ? (int)(ow / aspect) : 192;
    if (oh < 64) oh = 64;
    if (oh > 384) oh = 384;

    pc_warp w;
    if (pc_warp_init_region(&w, f->width, f->height, r, inset_pct, ow, oh) != 0) return -1;
    size_t n = (size_t)ow * oh;
    uint8_t *buf = malloc(n * 3);
    if (!buf) { pc_warp_free(&w); return -1; }
    pc_warp_apply(&w, f, buf, buf + n, buf + 2 * n);
    pc_warp_free(&w);

    pc_features fe;
    pc_features_compute(buf, buf + n, buf + 2 * n, ow, oh, &fe);
    q->sharpness = fe.sharpness;
    q->clipped = fe.clipped;
    if (ref) {
        q->hash_distance = pc_hamming64(fe.dhash, ref->dhash);
        q->grid_distance = pc_features_grid_distance(&fe, ref);
    }
    if (board_y) {
        uint8_t *y = realloc(buf, n);     /* keep only the luma plane */
        *board_y = y ? y : buf;
        if (board_w) *board_w = ow;
        if (board_h) *board_h = oh;
    } else {
        free(buf);
    }

    q->mismatch = ref && fe.std_y >= 6.f && (q->hash_distance > 18 || q->grid_distance > 22.f);
    pc_snapshot_judge(q, 0.f);
    return 0;
}

void pc_snapshot_judge(pc_snapshot_quality *q, float typical)
{
    int known = typical >= 0.f;
    if (!known) typical = PC_CLIP_UNKNOWN_MAX - PC_CLIP_EXCESS;
    if (typical > 0.85f) typical = 0.85f;
    q->clip_typical = known ? typical : -1.f;
    q->usable = 1;
    q->reason[0] = 0;
    if (q->clipped > PC_CLIP_EXCESS && q->clipped - typical > PC_CLIP_EXCESS) {
        q->usable = 0;
        if (known && typical > 0.005f)
            snprintf(q->reason, sizeof q->reason, "%.0f%% of board blown out (usually %.0f%%)",
                     q->clipped * 100, typical * 100);
        else
            snprintf(q->reason, sizeof q->reason, "%.0f%% of board blown out", q->clipped * 100);
    } else if (q->mismatch) {
        q->usable = 0;
        snprintf(q->reason, sizeof q->reason, "frame does not match the settled creative");
    }
    q->score = base_score(q);
}

static int cmp_u8(const void *a, const void *b) { return *(const uint8_t *)a - *(const uint8_t *)b; }

void pc_burst_banding(uint8_t *const ys[], int n, int w, int h, float *out)
{
    for (int i = 0; i < n; i++) out[i] = 0.f;
    if (n < 3 || n > 16 || w < 3 || h < 3) return;
    size_t np = (size_t)w * h;
    uint8_t *med = malloc(np);
    double *rows = malloc(sizeof(double) * (size_t)h), *cols = malloc(sizeof(double) * (size_t)w);
    if (!med || !rows || !cols) { free(med); free(rows); free(cols); return; }
    uint8_t v[16];
    for (size_t p = 0; p < np; p++) {
        for (int i = 0; i < n; i++) v[i] = ys[i][p];
        qsort(v, (size_t)n, 1, cmp_u8);
        med[p] = v[n / 2];
    }
    for (int i = 0; i < n; i++) {
        memset(rows, 0, sizeof(double) * (size_t)h);
        memset(cols, 0, sizeof(double) * (size_t)w);
        for (int r = 0; r < h; r++)
            for (int c = 0; c < w; c++) {
                double d = (double)ys[i][(size_t)r * w + c] - med[(size_t)r * w + c];
                rows[r] += d;
                cols[c] += d;
            }
        /* remove the overall brightness offset (exposure flicker) first */
        double mean = 0;
        for (int r = 0; r < h; r++) mean += rows[r];
        mean /= (double)w * h;
        double er = 0, ec = 0;
        for (int r = 0; r < h; r++) { double d = rows[r] / w - mean; er += d * d; }
        for (int c = 0; c < w; c++) { double d = cols[c] / h - mean; ec += d * d; }
        er /= h;
        ec /= w;
        /* the +0.5 floor keeps sensor noise from producing big ratios */
        out[i] = (float)((er + 0.5) / (ec + 0.5));
    }
    free(med);
    free(rows);
    free(cols);
}

void pc_snapshot_apply_banding(pc_snapshot_quality *q, float banding)
{
    q->banding = banding;
    if (q->usable && banding > 3.0f) {
        q->usable = 0;
        snprintf(q->reason, sizeof q->reason, "LED banding (ratio %.1f)", banding);
    }
    q->score = base_score(q);
}
