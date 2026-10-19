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
static double g_glare = 0;                /* sun glare towards the top left of the board */
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
    /* k == 80, 81: near-flat creatives of similar colour that differ only in
     * their text: pink with a headline, orange-red with blocks of copy */
    if (k == 80 || k == 81) {
        if (k == 80) { *r = 225; *g = 70; *b = 100; } else { *r = 232; *g = 80; *b = 80; }
        int ink = 0;
        if (k == 80) ink = (v > 0.38 && v < 0.48 && u > 0.08 && u < 0.92 && fmod(u * 40, 1.0) < 0.6) ||
                           (v > 0.75 && v < 0.85 && u > 0.42 && u < 0.58);
        else ink = (v > 0.12 && v < 0.30 && u > 0.25 && u < 0.75 && fmod(u * 30, 1.0) < 0.5) ||
                   (v > 0.40 && v < 0.55 && u > 0.30 && u < 0.70) ||
                   (v > 0.62 && v < 0.68 && u > 0.05 && u < 0.95 && fmod(u * 60, 1.0) < 0.5) ||
                   (v > 0.80 && v < 0.88 && u > 0.35 && u < 0.65);
        if (ink) { *r -= 45; *g -= 25; *b -= 25; }   /* low-contrast copy */
        return;
    }
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
                if (g_glare > 0) {
                    double gl = g_glare * exp(-(u * u + v * v) / 0.18);
                    r += (int)gl; g += (int)gl; b += (int)gl;
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

static void test_similar_creatives_under_glare(void)
{
    /* Two near-flat creatives of similar colour, differing only in their
     * text, under the same glare. The coarse thumbnail is dominated by the
     * glare; they must still get two IDs, each repeating, through exposure
     * changes. */
    g_glare = 70;
    seg s[13];
    const double gains[] = {1.0, 0.85, 1.12, 0.9, 1.08, 0.8, 1.15, 0.95, 1.05, 0.88, 1.1, 0.92};
    const double offs[] = {0, 10, -8, 6, -10, 14, -6, 4, -4, 9, -12, 5};
    for (int i = 0; i < 12; i++) s[i] = (seg){i % 2 ? 81 : 80, 0, 8, 0, gains[i], offs[i]};
    s[12] = (seg){2, 0, 3, 0, 1, 0};
    int ev[32], ids[32];
    pc_profile prof = {2, 8000};
    int n = run_sequence(s, 13, 5, &prof, ev, ids, 32, NULL, NULL);
    g_glare = 0;
    CHECK(n == 12, "similar creatives: expected 12 plays, got %d", n);
    if (n == 12) {
        CHECK(ids[1] != ids[2], "similar creatives under glare share ID %d", ids[1]);
        for (int i = 3; i < 11; i++)
            CHECK(ids[i] == ids[i - 2], "similar creatives: play %d id %d should repeat play %d id %d",
                  i, ids[i], i - 2, ids[i - 2]);
    }
}

static void features_of(int k, double gain, double offset, pc_features *fe)
{
    static pc_warp w;
    static int init = 0;
    if (!init) { pc_warp_init(&w, FW, FH, QUAD, 3.f, 192, 96); init = 1; }
    uint8_t *rgb = malloc((size_t)FW * FH * 3), y[192 * 96], u[192 * 96], v[192 * 96];
    g_gain = gain; g_offset = offset;
    render(rgb, k, 0, 0, 0, 0);
    g_gain = 1; g_offset = 0;
    pc_frame f = frame_of(rgb);
    pc_warp_apply(&w, &f, y, u, v);
    pc_features_compute(y, u, v, 192, 96, fe);
    free(rgb);
}

static void test_library_merge(void)
{
    /* One artwork that the camera sees two ways (night: dark with a strong
     * warm cast) gets two IDs; merging them makes both looks report one ID,
     * and a different creative still gets its own. */
    static pc_library lib;
    pc_library_init(&lib);
    pc_features day, night, other;
    features_of(1, 1.0, 0, &day);
    features_of(1, 0.35, 60, &night);
    features_of(2, 1.0, 0, &other);
    int a = pc_library_match_or_add(&lib, &day, 1000);
    int b = pc_library_match_or_add(&lib, &night, 2000);
    CHECK(a != b, "test needs the two looks to start as two IDs (got %d and %d)", a, b);
    CHECK(pc_library_merge(&lib, b, a) == 0, "merge failed");
    CHECK(pc_library_merge(&lib, b, a) == -1, "merging a removed id should fail");
    CHECK(lib.count == 1, "merged library should hold 1 creative, has %d", lib.count);
    CHECK(pc_library_match_or_add(&lib, &night, 3000) == a, "night look should now report id %d", a);
    CHECK(pc_library_match_or_add(&lib, &day, 4000) == a, "day look should still report id %d", a);
    int c = pc_library_match_or_add(&lib, &other, 5000);
    CHECK(c != a && c > b, "a different creative should get a new id (got %d)", c);
    CHECK(lib.items[0].plays == 4, "plays carried over: %d", lib.items[0].plays);
}

static void test_library_split(void)
{
    /* A wrong merge (two different artworks) undone: the merged look gets its
     * old ID back, the surviving ID keeps its own looks. */
    static pc_library lib;
    pc_library_init(&lib);
    pc_features day, night, other;
    features_of(1, 1.0, 0, &day);
    features_of(1, 0.35, 60, &night);
    features_of(2, 1.0, 0, &other);
    int a = pc_library_match_or_add(&lib, &day, 1000);
    int b = pc_library_match_or_add(&lib, &night, 2000);
    int c = pc_library_match_or_add(&lib, &other, 3000);
    CHECK(pc_library_merge(&lib, b, a) == 0, "merge b failed");
    CHECK(pc_library_merge(&lib, c, a) == 0, "merge c failed");        /* the wrong one */
    CHECK(pc_library_match_or_add(&lib, &other, 4000) == a, "other now reports %d", a);
    CHECK(pc_library_split(&lib, a, c, NULL, 0, 5000) == 1, "split should return 1 look");
    CHECK(pc_library_match_or_add(&lib, &other, 6000) == c, "other should report %d again", c);
    CHECK(pc_library_match_or_add(&lib, &night, 7000) == a, "night look still %d", a);
    CHECK(pc_library_split(&lib, 99, c, NULL, 0, 8000) == -1, "unknown id");
    /* a chain: d merged into c, then c into a; splitting c (naming d, which the
     * receiver knows went into c) takes d's look too */
    pc_library_init(&lib);
    pc_features x;
    features_of(3, 1.0, 0, &x);
    a = pc_library_match_or_add(&lib, &day, 1000);
    c = pc_library_match_or_add(&lib, &other, 2000);
    int d = pc_library_match_or_add(&lib, &x, 3000);
    pc_library_merge(&lib, d, c);
    pc_library_merge(&lib, c, a);
    int also[1] = {d};
    CHECK(pc_library_split(&lib, a, c, also, 1, 4000) == 2, "both looks back");
    CHECK(pc_library_match_or_add(&lib, &x, 5000) == c, "d's look reports %d", c);
    /* untagged looks (a library saved before tagging): dropped */
    pc_library_init(&lib);
    a = pc_library_match_or_add(&lib, &day, 1000);
    c = pc_library_match_or_add(&lib, &other, 2000);
    pc_library_merge(&lib, c, a);
    lib.items[0].var_from[0] = 0;
    CHECK(pc_library_split(&lib, a, c, NULL, 0, 3000) == 0, "untagged: nothing to give back");
    int e = pc_library_match_or_add(&lib, &other, 4000);
    CHECK(e != a && e != c, "other artwork gets a new id (got %d)", e);
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

/* Share of time: a 6-slot loop of 8 s where creative 1 holds two slots back to
 * back and creative 2 holds two slots spread through the loop. */
static void test_share_of_time(void)
{
    pc_profile prof = {6, 8000};
    pc_faults fm;
    int raised[PC_FAULT_COUNT], ev[128], ids[128];
    seg loop[] = {{1, 0, 16, 0}, {2, 0, 8, 0}, {3, 0, 8, 0}, {4, 0, 8, 0}, {2, 0, 8, 0}};
    seg s[64];
    int n = 0;
    for (int l = 0; l < 8; l++) for (int i = 0; i < 5; i++) s[n++] = loop[i];
    s[n++] = (seg){1, 0, 4, 0};
    memset(raised, 0, sizeof raised);
    int nev = run_sequence(s, n, 5, &prof, ev, ids, 128, &fm, raised);
    CHECK(nev >= 38 && nev <= 41, "share of time: expected ~40 changes, got %d", nev);
    CHECK(raised[PC_FAULT_LOOP_CHANGED] == 0, "share of time must not raise loop_changed");
    CHECK(raised[PC_FAULT_MISSING_CREATIVE] == 0, "share of time must not raise missing_creative");
    CHECK(raised[PC_FAULT_FROZEN] == 0, "share of time must not raise frozen");

    /* slots credited over loops 2-8 should be the full 7 x 6 = 42 */
    pc_play_ref p[130];
    int np = 0;
    p[np++] = (pc_play_ref){0, -1};
    for (int i = 0; i < nev; i++) p[np++] = (pc_play_ref){ev[i], ids[i]};
    double got = pc_runs_credited(p, np, 48000, 384000, 8000, 6, NULL, NULL, 0);
    CHECK(fabs(got - 42) <= 1.0, "share of time: credited %.1f slots of 42", got);

    /* hold-over: from loop 6 creative 3 stops playing and 2 stays up instead */
    n = 0;
    for (int l = 0; l < 5; l++) for (int i = 0; i < 5; i++) s[n++] = loop[i];
    seg bad[] = {{1, 0, 16, 0}, {2, 0, 16, 0}, {4, 0, 8, 0}, {2, 0, 8, 0}};
    for (int l = 0; l < 3; l++) for (int i = 0; i < 4; i++) s[n++] = bad[i];
    s[n++] = (seg){1, 0, 4, 0};
    memset(raised, 0, sizeof raised);
    nev = run_sequence(s, n, 5, &prof, ev, ids, 128, &fm, raised);
    np = 0;
    p[np++] = (pc_play_ref){0, -1};
    for (int i = 0; i < nev; i++) p[np++] = (pc_play_ref){ev[i], ids[i]};
    got = pc_runs_credited(p, np, 240000, 384000, 8000, 6, NULL, NULL, 0);
    CHECK(got <= 16 && got >= 14, "hold-over: credited %.1f of 18 slots in loops 6-8 (expected about 15)", got);
    CHECK(raised[PC_FAULT_MISSING_CREATIVE] == 1, "hold-over should raise missing_creative (%d)", raised[PC_FAULT_MISSING_CREATIVE]);

    /* with the booked number of creatives given, the same hold-over is still caught */
    pc_faults_init(&fm, NULL, NULL, &prof);
    pc_faults_set_expected_creatives(&fm, 4);
    CHECK(fm.expected_creatives == 4, "expected creatives set");
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
    /* the same creative photographed much brighter (white artwork, the photo exposed
     * differently from the video): brightness grid and hash say "different", the
     * fine detail says it is the same, so it is kept */
    {
        uint8_t *bright = malloc((size_t)FW * FH * 3);
        for (size_t i = 0; i < (size_t)FW * FH * 3; i++) {
            int x = (int)(a[i] * 1.7 + 40);
            bright[i] = (uint8_t)(x > 255 ? 255 : x);
        }
        pc_frame fbr = frame_of(bright);
        pc_snapshot_quality qb;
        pc_snapshot_score(&fbr, QUAD, 3.f, &ref, &qb, NULL, NULL, NULL);
        CHECK(qb.hash_distance > 18 || qb.grid_distance > 22.f,
              "brighter photo trips the old test (hd %d gd %.1f)", qb.hash_distance, qb.grid_distance);
        CHECK(qb.usable, "brighter photo of the same creative kept (%s, detail %.2f)", qb.reason, qb.detail);
        CHECK(qe.detail < PC_MISMATCH_DETAIL, "different creative's detail disagrees (%.2f)", qe.detail);
        free(bright);
    }
    pc_warp_free(&w);

    /* clipping is judged against what the creative usually shows: artwork with
     * a lot of white clips on camera every time, and isn't "blown out" */
    pc_snapshot_quality qw;
    memset(&qw, 0, sizeof qw);
    qw.clipped = 0.30f;
    pc_snapshot_judge(&qw, 0.f);
    CHECK(!qw.usable && strstr(qw.reason, "blown out"), "30%% clipped, usually none: blown out (%s)", qw.reason);
    pc_snapshot_judge(&qw, 0.25f);
    CHECK(qw.usable, "30%% clipped, usually 25%%: fine (%s)", qw.reason);
    qw.clipped = 0.55f;
    pc_snapshot_judge(&qw, 0.25f);
    CHECK(!qw.usable && strstr(qw.reason, "usually 25%"), "55%% clipped, usually 25%%: blown out (%s)", qw.reason);
    qw.clipped = 0.40f;
    pc_snapshot_judge(&qw, -1.f);
    CHECK(qw.usable, "40%% clipped, usual not known yet: fine (%s)", qw.reason);
    qw.clipped = 0.60f;
    pc_snapshot_judge(&qw, -1.f);
    CHECK(!qw.usable, "60%% clipped, usual not known yet: blown out");
    qw.clipped = 0.05f;
    qw.mismatch = 1;
    pc_snapshot_judge(&qw, 0.25f);
    CHECK(!qw.usable && strstr(qw.reason, "does not match"), "mismatch still caught (%s)", qw.reason);
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

static void test_regions(void)
{
    const pc_point q[4] = {{0.2f, 0.15f}, {0.85f, 0.22f}, {0.8f, 0.8f}, {0.18f, 0.86f}};
    pc_region std_r, r;
    pc_region_from_corners(&std_r, q);
    /* a curve with its midpoints on the straight edges, and a fold down the
     * screen's true middle, are the same as the plain region */
    for (int shape = PC_SHAPE_CURVE; shape <= PC_SHAPE_FOLD; shape++) {
        r = std_r;
        r.shape = shape;
        double worst = 0;
        for (int i = 0; i <= 10; i++)
            for (int j = 0; j <= 10; j++) {
                pc_point a, b;
                pc_region_point(&std_r, i / 10.0, j / 10.0, &a);
                pc_region_point(&r, i / 10.0, j / 10.0, &b);
                double d = fabs(a.x - b.x) + fabs(a.y - b.y);
                if (d > worst) worst = d;
            }
        CHECK(worst < 1e-4, "shape %d with neutral handles matches standard (off by %.6f)", shape, worst);
    }
    /* curve: corners stay put, each edge passes through its two handles */
    r = std_r;
    r.shape = PC_SHAPE_CURVE;
    r.curve[0].y -= 0.05f;             /* top bows up */
    r.curve[1].y -= 0.04f;
    r.curve[4].y += 0.04f;             /* bottom bows down */
    r.curve[5].y += 0.03f;
    pc_point p;
    pc_region_point(&r, 0, 0, &p);
    CHECK(fabs(p.x - q[0].x) < 1e-5 && fabs(p.y - q[0].y) < 1e-5, "curve keeps the top-left corner");
    pc_region_point(&r, 1. / 3, 0, &p);
    CHECK(fabs(p.x - r.curve[0].x) < 1e-5 && fabs(p.y - r.curve[0].y) < 1e-5, "top edge passes through its first handle");
    pc_region_point(&r, 2. / 3, 1, &p);
    CHECK(fabs(p.x - r.curve[5].x) < 1e-5 && fabs(p.y - r.curve[5].y) < 1e-5, "bottom edge passes through its second handle");
    float a0 = pc_region_aspect(&std_r, 1920, 1080), a1 = pc_region_aspect(&r, 1920, 1080);
    CHECK(a1 > a0 && a1 < a0 * 1.1f, "bowed edges are measured along the curve (%.3f vs %.3f)", a1, a0);
    /* the earlier one-handle curve converts to the same parabola */
    pc_region q1 = std_r;
    q1.shape = PC_SHAPE_CURVE;
    pc_point mids[4];
    for (int e = 0; e < 4; e++) {
        static const double at[4][2] = {{0.5, 0}, {1, 0.5}, {0.5, 1}, {0, 0.5}};
        pc_region_point(&std_r, at[e][0], at[e][1], &mids[e]);
    }
    mids[0].y -= 0.06f;
    pc_region_curve_from_mids(&q1, mids);
    pc_region_point(&q1, 0.5, 0, &p);
    CHECK(fabs(p.y - mids[0].y) < 2e-3, "converted curve passes near the old midpoint (%.4f vs %.4f)", p.y, mids[0].y);

    /* fold: two faces of a corner; the fold line maps to u = fold_u */
    pc_point f[4] = {{0.1f, 0.3f}, {0.9f, 0.3f}, {0.9f, 0.7f}, {0.1f, 0.7f}};
    pc_region_from_corners(&r, f);
    r.shape = PC_SHAPE_FOLD;
    r.fold[0] = (pc_point){0.4f, 0.2f};
    r.fold[1] = (pc_point){0.4f, 0.8f};
    r.fold_u = 0.3f;
    pc_region_point(&r, 0.3, 0, &p);
    CHECK(fabs(p.x - 0.4f) < 1e-5 && fabs(p.y - 0.2f) < 1e-5, "fold top at u = 0.3");
    pc_region_point(&r, 0.3, 1, &p);
    CHECK(fabs(p.y - 0.8f) < 1e-5, "fold bottom at u = 0.3");
    pc_region_point(&r, 1, 1, &p);
    CHECK(fabs(p.x - 0.9f) < 1e-5 && fabs(p.y - 0.7f) < 1e-5, "fold keeps the bottom-right corner");
    pc_warp w;
    CHECK(pc_warp_init_region(&w, 640, 360, &r, 0.f, 96, 48) == 0, "fold warp builds");
    /* output column at u = 0.3 samples the fold line */
    int col = (int)(0.3 * 96);
    double sx = w.lut[((size_t)24 * 96 + col) * 2] / 65536.0 / 639.0;
    CHECK(fabs(sx - 0.4) < 0.02, "fold column samples the fold line (x %.3f)", sx);
    pc_warp_free(&w);
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
    test_similar_creatives_under_glare();
    test_library_merge();
    test_library_split();
    test_faults();
    test_share_of_time();
    test_snapshot_score();
    test_regions();
    if (failures) { printf("%d check(s) failed\n", failures); return 1; }
    printf("all popcore tests passed\n");
    return 0;
}
