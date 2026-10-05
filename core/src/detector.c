/* Creative change detection: state machine, rotation profile, library. */
#include "popcore.h"
#include <math.h>
#include <string.h>

/* ---------------- rotation profile ---------------- */

void pc_profile_triggers(const pc_profile *p, pc_triggers *t)
{
    memset(t, 0, sizeof *t);
    int S = p ? p->slot_ms : 0, N = p ? p->slot_count : 0;
    if (S > 0) {
        t->min_slot_ms = S / 2;
        t->boundary_tol_ms = (int)(S * 0.15);
        t->frozen_ms = (N == 1) ? 0 : 2 * S;      /* takeover: no frozen check */
        t->blank_slot_min_ms = S / 2;
        t->blank_slot_max_ms = S + S / 2;
        t->expected_per_hour = 3600000 / S;
        if (N > 0) t->loop_ms = N * S;
    } else {
        t->min_slot_ms = 0;          /* caller falls back to cfg.min_slot_ms */
        t->boundary_tol_ms = 0;
        t->frozen_ms = 0;            /* caller falls back to default         */
        t->blank_slot_min_ms = 3000;
        t->blank_slot_max_ms = 30000;
    }
}

/* ---------------- creative library ---------------- */

void pc_library_init(pc_library *lib)
{
    memset(lib, 0, sizeof *lib);
    lib->next_id = 1;
    lib->corr_min = 0.88f;
    lib->chroma_tol = 10.f;
    lib->tint_tol = 30.f;
    lib->adapt = 0.2f;
}

static int fp_is_flat(const pc_features *f) { return f->std_y < 6.f; }

static float mean_of(const float *v, int n)
{
    double s = 0;
    for (int i = 0; i < n; i++) s += v[i];
    return (float)(s / n);
}

/* Pearson correlation: insensitive to brightness (offset) and contrast (gain). */
static float correlation(const float *a, const float *b, int n)
{
    double ma = 0, mb = 0;
    for (int i = 0; i < n; i++) { ma += a[i]; mb += b[i]; }
    ma /= n;
    mb /= n;
    double sab = 0, saa = 0, sbb = 0;
    for (int i = 0; i < n; i++) {
        double da = a[i] - ma, db = b[i] - mb;
        sab += da * db;
        saa += da * da;
        sbb += db * db;
    }
    if (saa < 1e-6 || sbb < 1e-6) return 0.f;
    return (float)(sab / sqrt(saa * sbb));
}

int pc_fingerprint_match(const pc_library *lib, const pc_features *a, const pc_features *b,
                         int grid_only, float *score)
{
    float mua = mean_of(a->cell_u, PC_CELLS), mva = mean_of(a->cell_v, PC_CELLS);
    float mub = mean_of(b->cell_u, PC_CELLS), mvb = mean_of(b->cell_v, PC_CELLS);
    float tint = fabsf(mua - mub) + fabsf(mva - mvb);
    int fa = fp_is_flat(a), fb = fp_is_flat(b);
    if (fa || fb) {                       /* blank / black / single colour screens */
        float d = fabsf(a->mean_y - b->mean_y) + tint;
        *score = 1.f - d / 100.f;
        return fa && fb && d < 24.f;
    }
    /* colour layout relative to the overall tint (white balance shifts cancel) */
    float rel = 0;
    for (int i = 0; i < PC_CELLS; i++)
        rel += fabsf((a->cell_u[i] - mua) - (b->cell_u[i] - mub)) +
               fabsf((a->cell_v[i] - mva) - (b->cell_v[i] - mvb));
    rel /= 2.f * PC_CELLS;
    float corr = correlation(a->thumb, b->thumb, PC_THUMB);
    float need = lib->corr_min - (grid_only ? 0.08f : 0.f);   /* video: averaged, looser */
    *score = corr - rel / 100.f;
    return corr >= need && rel <= lib->chroma_tol * (grid_only ? 1.5f : 1.f) && tint <= lib->tint_tol;
}

/* Blend a fingerprint towards a new sighting so slow drift is followed. */
static void adapt_fp(pc_features *dst, const pc_features *src, float a)
{
    for (int i = 0; i < PC_CELLS; i++) {
        dst->cell_y[i] += a * (src->cell_y[i] - dst->cell_y[i]);
        dst->cell_u[i] += a * (src->cell_u[i] - dst->cell_u[i]);
        dst->cell_v[i] += a * (src->cell_v[i] - dst->cell_v[i]);
    }
    for (int i = 0; i < PC_THUMB; i++) dst->thumb[i] += a * (src->thumb[i] - dst->thumb[i]);
    dst->mean_y += a * (src->mean_y - dst->mean_y);
    dst->std_y += a * (src->std_y - dst->std_y);
}

int pc_library_match_or_add(pc_library *lib, const pc_features *fp, int64_t now)
{
    return pc_library_match_or_add_ex(lib, fp, now, 0);
}

int pc_library_match_or_add_ex(pc_library *lib, const pc_features *fp, int64_t now, int grid_only)
{
    int best = -1;
    float best_score = -1e9f;
    for (int i = 0; i < lib->count; i++) {
        float sc;
        if (pc_fingerprint_match(lib, fp, &lib->items[i].fp, grid_only, &sc) && sc > best_score) {
            best = i;
            best_score = sc;
        }
    }
    lib->changes++;
    if (best >= 0) {
        pc_creative *c = &lib->items[best];
        c->last_seen_ms = now;
        c->plays++;
        if (!grid_only) adapt_fp(&c->fp, fp, lib->adapt);
        return c->id;
    }
    int slot;
    if (lib->count < PC_LIBRARY_MAX) {
        slot = lib->count++;
    } else {             /* evict least recently seen */
        slot = 0;
        for (int i = 1; i < lib->count; i++)
            if (lib->items[i].last_seen_ms < lib->items[slot].last_seen_ms) slot = i;
    }
    pc_creative *c = &lib->items[slot];
    c->id = lib->next_id++;
    c->fp = *fp;
    c->first_seen_ms = c->last_seen_ms = now;
    c->plays = 1;
    return c->id;
}

/* ---------------- detector ---------------- */

void pc_detector_cfg_default(pc_detector_cfg *c)
{
    c->cell_delta = 12.f;
    c->cut_fraction = 0.60f;
    c->settle_fraction = 0.15f;
    c->settle_ms = 600;
    c->drift_distance = 18.f;
    c->new_hash_dist = 10;
    c->new_grid_dist = 14.f;
    c->min_slot_ms = 4000;
    c->motion_play_ms = 2000;
    c->black_mean = 24.f;
    c->blank_std = 6.f;
}

const char *pc_state_name(pc_state s)
{
    switch (s) {
    case PC_STABLE: return "stable";
    case PC_TRANSITION: return "transition";
    case PC_SETTLING: return "settling";
    }
    return "?";
}

int pc_is_blank(const pc_detector_cfg *c, const pc_features *f, int *is_black)
{
    int black = f->mean_y < c->black_mean && f->std_y < c->blank_std * 1.5f;
    int flat = f->std_y < c->blank_std;
    if (is_black) *is_black = black;
    return black || flat;
}

void pc_detector_init(pc_detector *d, const pc_detector_cfg *cfg, const pc_profile *p)
{
    memset(d, 0, sizeof *d);
    if (cfg) d->cfg = *cfg; else pc_detector_cfg_default(&d->cfg);
    pc_library_init(&d->library);
    pc_detector_set_profile(d, p);
}

void pc_detector_set_profile(pc_detector *d, const pc_profile *p)
{
    if (p) d->profile = *p; else memset(&d->profile, 0, sizeof d->profile);
    pc_profile_triggers(&d->profile, &d->trig);
}

/* Fingerprint of a video creative: the frame's stats with cell colours
 * averaged over the motion so far, which repeats from play to play. */
static void motion_fp(const pc_detector *d, const pc_features *f, pc_features *out)
{
    *out = *f;
    if (d->acc_n <= 0) return;
    for (int i = 0; i < PC_CELLS; i++) {
        out->cell_y[i] = (float)(d->acc[0][i] / d->acc_n);
        out->cell_u[i] = (float)(d->acc[1][i] / d->acc_n);
        out->cell_v[i] = (float)(d->acc[2][i] / d->acc_n);
    }
    for (int i = 0; i < PC_THUMB; i++) out->thumb[i] = (float)(d->acc_thumb[i] / d->acc_n);
}

static void acc_reset(pc_detector *d)
{
    memset(d->acc, 0, sizeof d->acc);
    memset(d->acc_thumb, 0, sizeof d->acc_thumb);
    d->acc_n = 0;
}

static void ema_ref(pc_features *ref, const pc_features *f, float a)
{
    for (int i = 0; i < PC_CELLS; i++) {
        ref->cell_y[i] += a * (f->cell_y[i] - ref->cell_y[i]);
        ref->cell_u[i] += a * (f->cell_u[i] - ref->cell_u[i]);
        ref->cell_v[i] += a * (f->cell_v[i] - ref->cell_v[i]);
    }
}

int pc_detector_push(pc_detector *d, const pc_features *f, int64_t now, pc_event *ev)
{
    const pc_detector_cfg *c = &d->cfg;
    int fired = 0;
    d->last_frame_ms = now;

    if (!d->have_prev) {
        d->prev = *f;
        d->ref = *f;
        d->have_prev = d->have_ref = 1;
        d->last_play_ms = now;
        d->state = PC_STABLE;
        pc_library_match_or_add(&d->library, f, now);
        return 0;
    }

    float cut = pc_features_changed_cells(f, &d->prev, c->cell_delta);
    float refd = pc_features_grid_distance(f, &d->ref);
    d->last_cut_score = cut;
    d->last_ref_distance = refd;
    if (cut >= c->cut_fraction) d->last_big_cut_ms = now;
    if (d->state != PC_STABLE) {
        for (int i = 0; i < PC_CELLS; i++) {
            d->acc[0][i] += f->cell_y[i];
            d->acc[1][i] += f->cell_u[i];
            d->acc[2][i] += f->cell_v[i];
        }
        for (int i = 0; i < PC_THUMB; i++) d->acc_thumb[i] += f->thumb[i];
        d->acc_n++;
    }
    d->frames_in_play++;
    if (cut >= c->settle_fraction) {
        d->motion_frames_in_play++;
        d->last_motion_ms = now;
    }

    switch (d->state) {
    case PC_STABLE:
        if (cut >= c->cut_fraction || refd >= c->drift_distance) {
            d->state = PC_TRANSITION;
            d->transition_start_ms = now;
            d->motion_emitted = 0;
            acc_reset(d);
        } else if (cut < c->settle_fraction && refd < c->drift_distance * 0.5f) {
            ema_ref(&d->ref, f, 0.05f);   /* follow slow exposure drift */
        }
        break;

    case PC_TRANSITION:
        if (d->motion_emitted && d->profile.slot_ms > 0 &&
            now - d->last_play_ms >= d->profile.slot_ms + d->trig.boundary_tol_ms) {
            /* Motion continues past the slot boundary: back-to-back video
             * creatives. Log the next one at the inferred boundary. */
            memset(ev, 0, sizeof *ev);
            ev->type = PC_EV_NEW_PLAY;
            ev->change_start_ms = d->last_play_ms + d->profile.slot_ms;
            ev->settled_ms = now;
            ev->previous_play_ms = d->profile.slot_ms;
            ev->motion = 1;
            ev->confidence = 0.3f;
            ev->sequence = ++d->sequence;
            motion_fp(d, f, &ev->fp);
            ev->creative_id = pc_library_match_or_add_ex(&d->library, &ev->fp, now, 1);
            acc_reset(d);
            d->last_play_ms = ev->change_start_ms;
            d->ref = *f;
            d->frames_in_play = d->motion_frames_in_play = 0;
            fired = 1;
        } else if (cut < c->settle_fraction) {
            d->state = PC_SETTLING;
            d->settle_start_ms = now;
        } else if (!d->motion_emitted && c->motion_play_ms > 0 &&
                   now - d->transition_start_ms >= c->motion_play_ms) {
            /* Never settles: a full-motion video creative. Log it once. */
            int64_t since = d->transition_start_ms - d->last_play_ms;
            int min_slot = d->trig.min_slot_ms > 0 ? d->trig.min_slot_ms : c->min_slot_ms;
            d->motion_emitted = 1;
            if (since >= min_slot) {
                memset(ev, 0, sizeof *ev);
                ev->type = PC_EV_NEW_PLAY;
                ev->change_start_ms = d->transition_start_ms;
                ev->settled_ms = now;
                ev->previous_play_ms = since;
                ev->motion = 1;
                ev->confidence = 0.5f;
                ev->sequence = ++d->sequence;
                motion_fp(d, f, &ev->fp);
                ev->creative_id = pc_library_match_or_add_ex(&d->library, &ev->fp, now, 1);
                d->last_play_ms = d->transition_start_ms;
                d->ref = *f;
                d->frames_in_play = d->motion_frames_in_play = 0;
                fired = 1;
            }
        }
        break;

    case PC_SETTLING:
        if (cut >= c->settle_fraction) {
            d->state = PC_TRANSITION;
            break;
        }
        if (now - d->settle_start_ms < c->settle_ms) break;

        {
            int fb, rb;
            int f_blank = pc_is_blank(c, f, &fb);
            int r_blank = pc_is_blank(c, &d->ref, &rb);
            int hd = pc_hamming64(f->dhash, d->ref.dhash);
            float gd = pc_features_grid_distance(f, &d->ref);
            int different, strong;
            if (f_blank && r_blank) {
                different = gd >= c->new_grid_dist;
                strong = gd >= 2 * c->new_grid_dist;
            } else if (f_blank != r_blank) {
                different = strong = 1;      /* content <-> blank is always a change */
            } else {
                different = hd >= c->new_hash_dist || gd >= c->new_grid_dist;
                strong = hd >= 2 * c->new_hash_dist || gd >= 2 * c->new_grid_dist;
            }

            /* After a video creative, the play starts at the last hard cut,
             * or, when the cut was too small to count as one (the next
             * creative shares part of the screen), when the motion stopped. */
            int64_t start = d->transition_start_ms;
            if (d->motion_emitted)
                start = d->last_big_cut_ms > d->last_play_ms ? d->last_big_cut_ms
                      : d->last_motion_ms > d->last_play_ms ? d->last_motion_ms
                      : d->transition_start_ms;
            int64_t since = start - d->last_play_ms;
            int min_slot = d->trig.min_slot_ms > 0 ? d->trig.min_slot_ms : c->min_slot_ms;
            int accept = different && since >= min_slot;
            /* loop-aware: early changes must be strong to count */
            int early = d->profile.slot_ms > 0
                      ? since < d->profile.slot_ms - d->trig.boundary_tol_ms
                      : since < 2 * min_slot;
            if (accept && d->profile.slot_ms > 0 && early && !strong)
                accept = 0;
            /* Inside a video creative (mostly motion since the play began)
             * a brief still moment is not a new play, however different. */
            int video = d->frames_in_play > 0 &&
                        d->motion_frames_in_play * 10 > d->frames_in_play * 3;
            if (accept && video && early)
                accept = 0;

            if (accept) {
                memset(ev, 0, sizeof *ev);
                ev->type = PC_EV_NEW_PLAY;
                ev->change_start_ms = start;
                ev->settled_ms = now;
                ev->previous_play_ms = since;
                ev->is_blank = f_blank;
                float conf = (f_blank || r_blank) ? gd / (2.f * c->new_grid_dist)
                           : fmaxf((float)hd / (2.f * c->new_hash_dist), gd / (2.f * c->new_grid_dist));
                ev->confidence = conf < 0.3f ? 0.3f : conf > 1.f ? 1.f : conf;
                ev->sequence = ++d->sequence;
                ev->fp = *f;
                ev->creative_id = pc_library_match_or_add(&d->library, f, now);
                d->last_play_ms = start;
                d->frames_in_play = d->motion_frames_in_play = 0;
                fired = 1;
            }
            /* Either way the settled image is the new reference: an
             * animation inside one creative updates the reference silently. */
            d->ref = *f;
            d->state = PC_STABLE;
        }
        break;
    }

    d->prev = *f;
    return fired;
}
