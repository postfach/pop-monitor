/* Unit tests for popcore using synthetic billboard frames. */
#include "popcore.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures = 0;
#define CHECK(cond, ...) do { if (!(cond)) { failures++; printf("FAIL %s:%d: ", __FILE__, __LINE__); \
    printf(__VA_ARGS__); printf("\n"); } } while (0)

#define FW 640
#define FH 360
static const pc_point QUAD[4] = {{0.16f, 0.17f}, {0.88f, 0.25f}, {0.86f, 0.83f}, {0.17f, 0.89f}};

static uint32_t rng = 12345;
static double g_gain = 1, g_offset = 0;   /* simulated camera exposure */
static int rnd(void) { rng = rng * 1103515245u + 12345u; return (int)((rng >> 16) & 0x7fff); }

/* Creative pattern at board coordinate (u,v) in [0,1]. */
static void pattern(int k, double u, double v, double t, int *r, int *g, int *b)
{
    if (k < 0) { *r = *g = *b = 4; return; }               /* black      */
    if (k == 99) { *r = 20; *g = 40; *b = 200; return; }   /* no-signal blue */
    uint32_t s = (uint32_t)(k * 2654435761u);
    int bx = (int)(u * 6), by = (int)(v * 3);
    uint32_t h = s ^ (uint32_t)(bx * 73856093) ^ (uint32_t)(by * 19349663);
    h = h * 2246822519u;
    *r = 40 + (h >> 8) % 200;
    *g = 40 + (h >> 16) % 200;
    *b = 40 + (h >> 24) % 200;
    /* texture so dHash and sharpness have something to see */
    double stripe = sin((u * (10 + k % 7) + v * (4 + k % 3)) * 6.283);
    int d = (int)(30 * stripe);
    *r += d; *g += d; *b += d;
    /* k == 62: video whose left half is identical to creative 1, so the cut
     * from it into creative 1 changes only about half the board */
    if (k == 62 && u < 0.5) { pattern(1, u, v, t, r, g, b); return; }
    if (k == 62) {
        int m = (int)(70 * sin(t * 4.0 + bx * 1.3 + by * 2.1));
        *r = 120 + m; *g = 90 - m; *b = 160 + m / 2;
        return;
    }
    /* k == 60: full-motion video creative, every zone keeps changing */
    if (k >= 60 && k < 70) {
        int m = (int)(70 * sin(t * 4.0 + bx * 1.3 + by * 2.1));
        *r += m; *g -= m; *b += m / 2;
    }
    /* k == 50: animated creative, a bright patch moving across */
    if (k == 50) {
        double px = fmod(t * 0.2, 1.0);
        if (fabs(u - px) < 0.06 && fabs(v - 0.5) < 0.12) { *r = 255; *g = 255; *b = 255; }
    }
}

static void invert3(const double m[9], double o[9])
{
    double a = m[0], b = m[1], c = m[2], d = m[3], e = m[4], f = m[5], g = m[6], h = m[7], i = m[8];
    double A = e * i - f * h, B = -(d * i - f * g), C = d * h - e * g;
    double det = a * A + b * B + c * C;
    o[0] = A / det; o[1] = -(b * i - c * h) / det; o[2] = (b * f - c * e) / det;
    o[3] = B / det; o[4] = (a * i - c * g) / det;  o[5] = -(a * f - c * d) / det;
    o[6] = C / det; o[7] = -(a * h - b * g) / det; o[8] = (a * e - b * d) / det;
}

static double HINV[9];

/* Render a full frame: dim surroundings, board in perspective.
 * mix: crossfade weight towards k2 (0 = only k). */
static void render(uint8_t *rgb, int k, int k2, double mix, double t, double blur)
{
    for (int y = 0; y < FH; y++)
        for (int x = 0; x < FW; x++) {
            uint8_t *p = rgb + ((size_t)y * FW + x) * 3;
            double d = HINV[6] * x + HINV[7] * y + HINV[8];
            double u = (HINV[0] * x + HINV[1] * y + HINV[2]) / d;
            double v = (HINV[3] * x + HINV[4] * y + HINV[5]) / d;
            int r = 60, g = 70, b = 60;                      /* street */
            if (u >= 0 && u <= 1 && v >= 0 && v <= 1) {
                pattern(k, u, v, t, &r, &g, &b);
                if (mix > 0) {
                    int r2, g2, b2;
                    pattern(k2, u, v, t, &r2, &g2, &b2);
                    r = (int)(r * (1 - mix) + r2 * mix);
                    g = (int)(g * (1 - mix) + g2 * mix);
                    b = (int)(b * (1 - mix) + b2 * mix);
                }
            }
            r = (int)(r * g_gain + g_offset); g = (int)(g * g_gain + g_offset); b = (int)(b * g_gain + g_offset * 0.8);
            int n = rnd() % 7 - 3;                            /* sensor noise */
            p[0] = (uint8_t)fmin(255, fmax(0, r + n));
            p[1] = (uint8_t)fmin(255, fmax(0, g + n));
            p[2] = (uint8_t)fmin(255, fmax(0, b + n));
        }
    if (blur > 0) {   /* cheap box blur, radius = blur */
        int R = (int)blur;
        uint8_t *tmp = malloc((size_t)FW * FH * 3);
        memcpy(tmp, rgb, (size_t)FW * FH * 3);
        for (int y = R; y < FH - R; y++)
            for (int x = R; x < FW - R; x++)
                for (int c = 0; c < 3; c++) {
                    int s = 0;
                    for (int dy = -R; dy <= R; dy++)
                        for (int dx = -R; dx <= R; dx++) s += tmp[((size_t)(y + dy) * FW + x + dx) * 3 + c];
                    rgb[((size_t)y * FW + x) * 3 + c] = (uint8_t)(s / ((2 * R + 1) * (2 * R + 1)));
                }
        free(tmp);
    }
}

static pc_frame frame_of(uint8_t *rgb)
{
    pc_frame f = {PC_FMT_RGB24, FW, FH, {rgb, 0, 0}, {FW * 3, 0, 0}};
    return f;
}

/* ---- tests ---- */

static void test_warp(void)
{
    pc_warp w;
    pc_point id[4] = {{0, 0}, {1, 0}, {1, 1}, {0, 1}};
    CHECK(pc_warp_init(&w, 641, 361, id, 0, 64, 36) == 0, "warp init");
    /* identity: output pixel centre (i+0.5)/64 maps to that fraction of 640 */
    double x = w.lut[(10 * 64 + 20) * 2] / 65536.0, y = w.lut[(10 * 64 + 20) * 2 + 1] / 65536.0;
    CHECK(fabs(x - 20.5 / 64 * 640) < 0.01 && fabs(y - 10.5 / 36 * 360) < 0.01,
          "identity mapping got %.2f,%.2f", x, y);
    pc_warp_free(&w);

    /* perspective: board corners land on output corners */
    CHECK(pc_warp_init(&w, FW, FH, QUAD, 0, 100, 50) == 0, "warp init quad");
    double cx = w.lut[0] / 65536.0, cy = w.lut[1] / 65536.0;
    CHECK(fabs(cx - QUAD[0].x * (FW - 1)) < 4 && fabs(cy - QUAD[0].y * (FH - 1)) < 4,
          "top-left corner %.1f,%.1f", cx, cy);
    pc_warp_free(&w);
}

typedef struct { int k, k2; double dur, fade, gain, offset; } seg;   /* gain 0 = 1 */

static int run_sequence(const seg *s, int nseg, int fps, const pc_profile *prof,
                        int *events, int *ids, int maxev, pc_faults *fm, int *faults_raised)
{
    pc_detector d;
    pc_detector_init(&d, NULL, prof);
    if (fm) pc_faults_init(fm, NULL, NULL, prof);
    pc_warp w;
    pc_warp_init(&w, FW, FH, QUAD, 3.f, 192, 96);
    uint8_t *rgb = malloc((size_t)FW * FH * 3), y[192 * 96], u[192 * 96], v[192 * 96];
    double t = 0;
    int nev = 0;
    for (int i = 0; i < nseg; i++) {
        int frames = (int)(s[i].dur * fps);
        g_gain = s[i].gain > 0 ? s[i].gain : 1;
        g_offset = s[i].offset;
        for (int j = 0; j < frames; j++, t += 1.0 / fps) {
            double local = (double)j / fps;
            if (s[i].fade > 0 && local < s[i].fade)   /* k2 = previous creative */
                render(rgb, s[i].k2, s[i].k, local / s[i].fade, t, 0);
            else
                render(rgb, s[i].k, 0, 0, t, 0);
            pc_frame f = frame_of(rgb);
            pc_warp_apply(&w, &f, y, u, v);
            pc_features fe;
            pc_features_compute(y, u, v, 192, 96, &fe);
            pc_event ev;
            int64_t now = (int64_t)(t * 1000);
            int fired = pc_detector_push(&d, &fe, now, &ev);
            if (fired && nev < maxev) { events[nev] = (int)ev.change_start_ms; ids[nev] = ev.creative_id; nev++; }
            if (fm) {
                pc_faults_push(fm, &fe, fired ? &ev : NULL, now);
                pc_fault_event fe2;
                while (pc_faults_pop(fm, &fe2))
                    if (fe2.raised) faults_raised[fe2.type]++;
            }
        }
    }
    free(rgb);
    pc_warp_free(&w);
    g_gain = 1; g_offset = 0;
    return nev;
}

static void test_detector_rotation(void)
{
    /* 2 loops of 4 creatives x 10 s; creative 3 enters with a 0.8 s crossfade;
     * creative 50 is animated (moving patch) and must count once. */
    seg loop[] = {{1, 0, 10, 0}, {2, 0, 10, 0}, {3, 2, 10, 0.8}, {50, 0, 10, 0},
                  {1, 0, 10, 0}, {2, 0, 10, 0}, {3, 2, 10, 0.8}, {50, 0, 10, 0}, {1, 0, 4, 0}};
    int ev[64], ids[64];
    pc_profile prof = {4, 10000};
    int n = run_sequence(loop, 9, 5, &prof, ev, ids, 64, NULL, NULL);
    /* first creative is the starting reference (no event), then 8 changes */
    CHECK(n == 8, "expected 8 plays, got %d", n);
    if (n == 8) {
        CHECK(ids[0] != ids[1] && ids[1] != ids[2], "distinct ids %d %d %d", ids[0], ids[1], ids[2]);
        CHECK(ids[0] == ids[4] && ids[1] == ids[5] && ids[2] == ids[6], "repeat plays get the same id");
        for (int i = 0; i < n; i++) {
            int expect = 10000 * (i + 1);
            CHECK(abs(ev[i] - expect) <= 1500, "play %d at %d ms, expected ~%d", i, ev[i], expect);
        }
    }

    /* same rotation, no profile: animated creative must still count once */
    n = run_sequence(loop, 9, 5, NULL, ev, ids, 64, NULL, NULL);
    CHECK(n == 8, "no profile: expected 8 plays, got %d", n);
}

static void test_video_creative(void)
{
    seg s[] = {{1, 0, 10, 0}, {60, 0, 10, 0}, {2, 0, 10, 0}, {3, 0, 4, 0}};
    int ev[16], ids[16];
    pc_profile prof = {3, 10000};
    int n = run_sequence(s, 4, 5, &prof, ev, ids, 16, NULL, NULL);
    CHECK(n == 3, "video rotation: expected 3 plays, got %d", n);
    if (n == 3)
        CHECK(abs(ev[0] - 10000) <= 400 && abs(ev[1] - 20000) <= 400 && abs(ev[2] - 30000) <= 400,
              "video plays at %d %d %d", ev[0], ev[1], ev[2]);

    /* video into a static creative that shares half the screen: the cut is
     * small, but the static creative must still be logged at its start */
    seg s3[] = {{2, 0, 10, 0}, {62, 0, 10, 0}, {1, 0, 10, 0}, {3, 0, 10, 0}, {2, 0, 3, 0}};
    n = run_sequence(s3, 5, 5, &prof, ev, ids, 16, NULL, NULL);
    CHECK(n == 4, "video into half-matching creative: expected 4 plays, got %d", n);
    if (n == 4)
        CHECK(abs(ev[1] - 20000) <= 400 && abs(ev[2] - 30000) <= 400,
              "plays at %d %d %d %d", ev[0], ev[1], ev[2], ev[3]);

    /* back-to-back video creatives: second is logged at the inferred boundary */
    seg s2[] = {{1, 0, 10, 0}, {60, 0, 10, 0}, {61, 0, 10, 0}, {2, 0, 10, 0}, {3, 0, 4, 0}};
    n = run_sequence(s2, 5, 5, &prof, ev, ids, 16, NULL, NULL);
    CHECK(n == 4, "back-to-back video: expected 4 plays, got %d", n);
    if (n == 4)
        CHECK(abs(ev[1] - 20000) <= 400 && abs(ev[2] - 30000) <= 400,
              "back-to-back video plays at %d %d %d %d", ev[0], ev[1], ev[2], ev[3]);
}

static void test_creative_ids_under_exposure_drift(void)
{
    /* 6 creatives x 3 loops; every play has different exposure, contrast
     * and a slight cast, as a real camera does through the day. */
    seg s[19];
    const double gains[] = {1.0, 0.75, 1.25, 0.85, 1.15, 0.7, 1.3, 0.9, 1.1, 0.8, 1.2, 0.95,
                            1.05, 0.72, 1.28, 0.88, 1.12, 0.78};
    const double offs[] = {0, 18, -15, 10, -20, 25, -10, 5, -5, 15, -18, 8, -8, 22, -12, 12, -22, 3};
    for (int i = 0; i < 18; i++) { s[i] = (seg){1 + i % 6, 0, 8, 0, gains[i], offs[i]}; }
    s[18] = (seg){1, 0, 3, 0, 1, 0};
    int ev[32], ids[32];
    pc_profile prof = {6, 8000};
    int n = run_sequence(s, 19, 5, &prof, ev, ids, 32, NULL, NULL);
    CHECK(n == 18, "expected 18 plays, got %d", n);
    int distinct[64] = {0}, nd = 0;
    for (int i = 0; i < n; i++) {
        int seen = 0;
        for (int j = 0; j < nd; j++) if (distinct[j] == ids[i]) seen = 1;
        if (!seen) distinct[nd++] = ids[i];
    }
    CHECK(nd == 6, "6 creatives under exposure drift should give 6 IDs, got %d", nd);
    for (int i = 6; i < n; i++)
        CHECK(ids[i] == ids[i - 6], "play %d id %d should repeat play %d id %d", i, ids[i], i - 6, ids[i - 6]);
}

static void test_faults(void)
{
    pc_faults fm;
    int raised[PC_FAULT_COUNT];
    int ev[64], ids[64];
    pc_profile prof = {3, 8000};

    /* black screen for 15 s */
    memset(raised, 0, sizeof raised);
    seg s1[] = {{1, 0, 8, 0}, {2, 0, 8, 0}, {-1, 0, 15, 0}, {3, 0, 8, 0}};
    run_sequence(s1, 4, 5, &prof, ev, ids, 64, &fm, raised);
    CHECK(raised[PC_FAULT_BLACK] == 1, "black raised %d", raised[PC_FAULT_BLACK]);

    /* frozen: one creative stuck for 30 s with an 8 s slot profile */
    memset(raised, 0, sizeof raised);
    seg s2[] = {{1, 0, 8, 0}, {2, 0, 30, 0}};
    run_sequence(s2, 2, 5, &prof, ev, ids, 64, &fm, raised);
    CHECK(raised[PC_FAULT_FROZEN] == 1, "frozen raised %d", raised[PC_FAULT_FROZEN]);

    /* takeover (N=1): long single creative is not frozen */
    memset(raised, 0, sizeof raised);
    pc_profile take = {1, 8000};
    run_sequence(s2, 2, 5, &take, ev, ids, 64, &fm, raised);
    CHECK(raised[PC_FAULT_FROZEN] == 0, "takeover should not raise frozen");

    /* blank slot: creative 2 replaced by black for one slot, repeated loops */
    memset(raised, 0, sizeof raised);
    seg s3[] = {{1, 0, 8, 0}, {-1, 0, 8, 0}, {3, 0, 8, 0}, {1, 0, 8, 0}, {-1, 0, 8, 0}, {3, 0, 8, 0},
                {1, 0, 8, 0}, {-1, 0, 8, 0}, {3, 0, 8, 0}, {1, 0, 8, 0}, {-1, 0, 8, 0}, {3, 0, 8, 0}};
    run_sequence(s3, 12, 5, &prof, ev, ids, 64, &fm, raised);
    CHECK(raised[PC_FAULT_BLANK_SLOT] >= 3, "blank slots %d", raised[PC_FAULT_BLANK_SLOT]);
    CHECK(raised[PC_FAULT_MISSING_CREATIVE] == 1, "missing creative raised %d", raised[PC_FAULT_MISSING_CREATIVE]);
    CHECK(raised[PC_FAULT_BLACK] == 0, "8 s black slot is below black hold time");

    /* no-signal blue screen */
    memset(raised, 0, sizeof raised);
    seg s4[] = {{1, 0, 8, 0}, {99, 0, 15, 0}};
    run_sequence(s4, 2, 5, &prof, ev, ids, 64, &fm, raised);
    CHECK(raised[PC_FAULT_BLANK] == 1, "blank raised %d", raised[PC_FAULT_BLANK]);

    /* healthy rotation raises nothing */
    memset(raised, 0, sizeof raised);
    seg s5[] = {{1, 0, 8, 0}, {2, 0, 8, 0}, {3, 0, 8, 0}, {1, 0, 8, 0}, {2, 0, 8, 0}, {3, 0, 8, 0},
                {1, 0, 8, 0}, {2, 0, 8, 0}, {3, 0, 8, 0}, {1, 0, 8, 0}};
    run_sequence(s5, 10, 5, &prof, ev, ids, 64, &fm, raised);
    int any = 0;
    for (int i = 0; i < PC_FAULT_COUNT; i++) any += raised[i];
    CHECK(any == 0, "healthy rotation raised %d faults", any);
}

static void test_snapshot_score(void)
{
    uint8_t *a = malloc((size_t)FW * FH * 3), *b = malloc((size_t)FW * FH * 3);
    render(a, 7, 0, 0, 0, 0);
    render(b, 7, 0, 0, 0, 2);
    pc_frame fa = frame_of(a), fb = frame_of(b);
    pc_snapshot_quality qa, qb;
    pc_snapshot_score(&fa, QUAD, 3.f, NULL, &qa, NULL, NULL, NULL);
    pc_snapshot_score(&fb, QUAD, 3.f, NULL, &qb, NULL, NULL, NULL);
    CHECK(qa.score > qb.score, "sharp %.2f should beat blurred %.2f", qa.score, qb.score);
    CHECK(qa.usable, "sharp frame usable: %s", qa.reason);

    /* banding: a burst of 3 frames of the same creative, one with rolling
     * horizontal bands. Static headline-style content must not trigger it. */
    uint8_t *burst[3], *ys[3];
    int bw = 0, bh = 0;
    pc_snapshot_quality qs[3];
    for (int i = 0; i < 3; i++) {
        burst[i] = malloc((size_t)FW * FH * 3);
        render(burst[i], 7, 0, 0, 0, 0);
    }
    for (int y = 0; y < FH; y++)
        if ((y / 6) % 2 == 0)
            for (int x = 0; x < FW; x++)
                for (int c = 0; c < 3; c++) {
                    uint8_t *p = &burst[1][((size_t)y * FW + x) * 3 + c];
                    *p = (uint8_t)(*p * 3 / 4);
                }
    for (int i = 0; i < 3; i++) {
        pc_frame f = frame_of(burst[i]);
        pc_snapshot_score(&f, QUAD, 3.f, NULL, &qs[i], &ys[i], &bw, &bh);
    }
    float band[3];
    pc_burst_banding(ys, 3, bw, bh, band);
    for (int i = 0; i < 3; i++) pc_snapshot_apply_banding(&qs[i], band[i]);
    CHECK(qs[0].usable && qs[2].usable, "clean burst frames usable (banding %.2f, %.2f)", band[0], band[2]);
    CHECK(!qs[1].usable, "banded frame rejected (banding %.2f)", band[1]);
    for (int i = 0; i < 3; i++) { free(ys[i]); free(burst[i]); }

    /* mismatch: frame of a different creative than the reference */
    pc_warp w;
    pc_warp_init(&w, FW, FH, QUAD, 3.f, 192, 96);
    uint8_t y[192 * 96], u[192 * 96], v[192 * 96];
    render(a, 7, 0, 0, 0, 0);
    pc_warp_apply(&w, &fa, y, u, v);
    pc_features ref;
    pc_features_compute(y, u, v, 192, 96, &ref);
    render(b, 8, 0, 0, 0, 0);
    pc_snapshot_quality qd, qe;
    pc_snapshot_score(&fa, QUAD, 3.f, &ref, &qd, NULL, NULL, NULL);
    pc_snapshot_score(&fb, QUAD, 3.f, &ref, &qe, NULL, NULL, NULL);
    CHECK(qd.usable, "matching frame usable (%s, hd %d gd %.1f)", qd.reason, qd.hash_distance, qd.grid_distance);
    CHECK(!qe.usable, "different creative rejected (hd %d gd %.1f)", qe.hash_distance, qe.grid_distance);
    pc_warp_free(&w);
    free(a);
    free(b);
}

static void test_profile(void)
{
    pc_profile p = {6, 10000};
    pc_triggers t;
    pc_profile_triggers(&p, &t);
    CHECK(t.min_slot_ms == 5000 && t.boundary_tol_ms == 1500 && t.frozen_ms == 20000, "derived triggers");
    CHECK(t.blank_slot_min_ms == 5000 && t.blank_slot_max_ms == 15000, "blank slot range");
    CHECK(t.loop_ms == 60000 && t.expected_per_hour == 360, "loop and plays per hour");
}

int main(void)
{
    pc_warp w;
    pc_warp_init(&w, FW, FH, QUAD, 0, 1000, 1000);
    /* render() needs pixel -> board (u,v): invert unit->pixel homography */
    double hs[9];
    memcpy(hs, w.h, sizeof hs);
    invert3(hs, HINV);
    pc_warp_free(&w);

    test_profile();
    test_warp();
    test_detector_rotation();
    test_video_creative();
    test_creative_ids_under_exposure_drift();
    test_faults();
    test_snapshot_score();
    if (failures) { printf("%d check(s) failed\n", failures); return 1; }
    printf("all popcore tests passed\n");
    return 0;
}
