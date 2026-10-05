/*
 * popcore — portable billboard Proof-of-Play detection core.
 *
 * Pure C99, no camera or OS dependencies. The same code runs in the
 * gateway service (frames from RTSP / VAPIX) and inside an on-camera
 * ACAP (frames from VDO).
 *
 * Pipeline per analysis frame:
 *   pc_warp_apply()      quadrilateral ROI  -> small flat YUV image
 *   pc_features_compute() grid colour signature, dHash, brightness stats
 *   pc_detector_push()   change state machine -> NEW_PLAY events
 *   pc_faults_push()     black / blank / frozen / loop / partial faults
 *
 * Snapshot quality:
 *   pc_snapshot_score()  sharpness, clipping, banding, match to creative
 */
#ifndef POPCORE_H
#define POPCORE_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_VERSION "0.1.0"

/* ------------------------------------------------------------------ */
/* Frames                                                              */
/* ------------------------------------------------------------------ */

typedef enum {
    PC_FMT_GRAY = 0,   /* plane0 = Y                                  */
    PC_FMT_NV12,       /* plane0 = Y, plane1 = interleaved UV (2x2)   */
    PC_FMT_YUV420P,    /* plane0 = Y, plane1 = U, plane2 = V (2x2)    */
    PC_FMT_RGB24       /* plane0 = packed RGB                          */
} pc_format;

typedef struct {
    pc_format      fmt;
    int            width, height;
    const uint8_t *plane[3];
    int            stride[3];
} pc_frame;

typedef struct { float x, y; } pc_point;

/* ------------------------------------------------------------------ */
/* ROI warp                                                            */
/* ------------------------------------------------------------------ */

/* Corners are NORMALISED (0..1) in source-image coordinates, ordered
 * top-left, top-right, bottom-right, bottom-left, so one ROI works at
 * any stream resolution. */
typedef struct {
    int       src_w, src_h;
    int       out_w, out_h;
    int32_t  *lut;      /* out_w*out_h*2 fixed-point 16.16 source coords */
    double    h[9];     /* homography: output pixel -> source pixel      */
} pc_warp;

int  pc_warp_init(pc_warp *w, int src_w, int src_h, const pc_point corners[4],
                  float inset_pct, int out_w, int out_h);
void pc_warp_free(pc_warp *w);
/* Output planes are out_w*out_h bytes each (full-resolution chroma). */
void pc_warp_apply(const pc_warp *w, const pc_frame *f,
                   uint8_t *out_y, uint8_t *out_u, uint8_t *out_v);
/* Packed RGB output (out_w*out_h*3), for the straightened ROI image. */
void pc_warp_apply_rgb(const pc_warp *w, const pc_frame *f, uint8_t *out_rgb);

/* ------------------------------------------------------------------ */
/* Features                                                            */
/* ------------------------------------------------------------------ */

#define PC_GRID_X 8
#define PC_GRID_Y 4
#define PC_CELLS  (PC_GRID_X * PC_GRID_Y)
#define PC_THUMB_W 16              /* luma thumbnail used to recognise creatives */
#define PC_THUMB_H 8
#define PC_THUMB  (PC_THUMB_W * PC_THUMB_H)
#define PC_DETAIL_W 32             /* fine luma detail used to tell creatives apart */
#define PC_DETAIL_H 16
#define PC_DETAIL (PC_DETAIL_W * PC_DETAIL_H)

typedef struct {
    float    cell_y[PC_CELLS], cell_u[PC_CELLS], cell_v[PC_CELLS];
    float    thumb[PC_THUMB];     /* 16x8 luma means                          */
    /* 32x16 luma with the low frequencies (glare, vignetting, exposure)
     * removed, scaled to a fixed contrast: the creative's own detail
     * (text, edges, layout). has_detail = 0 for fingerprints saved before
     * this existed; they gain it the next time they are seen. */
    int8_t   detail[PC_DETAIL];
    int      has_detail;
    uint64_t dhash;
    float    mean_y, std_y;
    float    sharpness;   /* variance of Laplacian on Y */
    float    clipped;     /* fraction of Y >= 250       */
} pc_features;

void pc_features_compute(const uint8_t *y, const uint8_t *u, const uint8_t *v,
                         int w, int h, pc_features *out);
/* Fraction (0..1) of grid cells whose colour moved more than cell_delta. */
float pc_features_changed_cells(const pc_features *a, const pc_features *b,
                                float cell_delta);
/* Mean absolute colour distance across cells (0..255 scale). */
float pc_features_grid_distance(const pc_features *a, const pc_features *b);
int   pc_hamming64(uint64_t a, uint64_t b);

/* ------------------------------------------------------------------ */
/* Rotation profile                                                    */
/* ------------------------------------------------------------------ */

typedef struct {
    int slot_count;   /* N; 0 = unknown, 1 = takeover   */
    int slot_ms;      /* S; 0 = unknown                 */
} pc_profile;

/* Triggers derived from the profile (see spec "Rotation profile"). */
typedef struct {
    int min_slot_ms;        /* ignore changes faster than this        */
    int boundary_tol_ms;    /* +- window around each expected boundary */
    int frozen_ms;          /* no change for this long -> frozen       */
    int blank_slot_min_ms;  /* blank interval counted as a blank slot  */
    int blank_slot_max_ms;
    int loop_ms;            /* N * S, 0 if unknown                     */
    int expected_per_hour;  /* 3600 / S, 0 if unknown                  */
} pc_triggers;

void pc_profile_triggers(const pc_profile *p, pc_triggers *t);

/* ------------------------------------------------------------------ */
/* Creative fingerprint library                                        */
/* ------------------------------------------------------------------ */

#define PC_LIBRARY_MAX 256

typedef struct {
    int         id;            /* local creative id, 1..             */
    pc_features fp;            /* adapts slowly as the creative is seen again */
    int64_t     first_seen_ms, last_seen_ms;
    int         plays;
} pc_creative;

/* Creatives are recognised by the SHAPE of their brightness pattern
 * (correlation of exposure-normalised 16x8 thumbnails), their fine detail
 * with glare and vignetting removed (32x16), and their colour layout
 * relative to the overall tint, so exposure, contrast and white balance
 * drift between plays don't create new IDs, while two different creatives
 * of similar colour under the same lighting don't share one. */
typedef struct {
    pc_creative items[PC_LIBRARY_MAX];
    int         count;
    int         next_id;
    float       corr_min;      /* thumbnail correlation for a match (0.88)     */
    float       chroma_tol;    /* relative colour layout difference (10)       */
    float       tint_tol;      /* overall colour cast difference (30)          */
    float       adapt;         /* how fast a fingerprint follows drift (0.2)   */
    float       detail_min;    /* fine-detail correlation for a match (0.35)   */
    unsigned    changes;       /* bumped on every add/update (for saving)      */
} pc_library;

/* Similarity of two fingerprints; returns 1 if they look like the same
 * creative. *score (higher = closer) is set either way. */
/* Fine-detail correlation of two fingerprints (1 if either has none). */
float pc_fingerprint_detail(const pc_features *a, const pc_features *b);
int  pc_fingerprint_match(const pc_library *lib, const pc_features *a, const pc_features *b,
                          int grid_only, float *score);
void pc_library_init(pc_library *lib);
/* Returns the matching or newly-added creative id. */
int  pc_library_match_or_add(pc_library *lib, const pc_features *fp, int64_t now_ms);
/* grid_only: match on colour layout alone (video creatives, whose dHash
 * varies from frame to frame). */
int  pc_library_match_or_add_ex(pc_library *lib, const pc_features *fp, int64_t now_ms,
                                int grid_only);

/* ------------------------------------------------------------------ */
/* Change detector                                                     */
/* ------------------------------------------------------------------ */

typedef enum { PC_STABLE = 0, PC_TRANSITION, PC_SETTLING } pc_state;

typedef struct {
    float cell_delta;          /* per-cell colour change, levels (12)        */
    float cut_fraction;        /* share of cells for a cut (0.60)            */
    float settle_fraction;     /* below this share the image is still (0.15) */
    int   settle_ms;           /* stillness needed before a play counts (600)*/
    float drift_distance;      /* grid distance from reference that also
                                  starts a transition (crossfades) (18)      */
    int   new_hash_dist;       /* dHash bits => different creative (10)      */
    float new_grid_dist;       /* or grid distance => different (14)         */
    int   min_slot_ms;         /* fallback when no profile (4000)            */
    int   motion_play_ms;      /* continuous motion this long = a video
                                  creative is playing; log it (2000)         */
    float black_mean;          /* blank/black classification (24)            */
    float blank_std;           /* (6)                                        */
} pc_detector_cfg;

void pc_detector_cfg_default(pc_detector_cfg *c);

typedef enum {
    PC_EV_NONE = 0,
    PC_EV_NEW_PLAY,            /* a new creative has settled on screen */
    PC_EV_SAME_CREATIVE        /* change settled back to same creative */
} pc_event_type;

typedef struct {
    pc_event_type type;
    int64_t  change_start_ms;     /* when the transition began (play start) */
    int64_t  settled_ms;          /* when it was judged stable              */
    int64_t  previous_play_ms;    /* duration of the previous creative      */
    int      creative_id;         /* library id                             */
    int      is_blank;            /* black or uniform screen                */
    int      motion;              /* logged during motion (video creative)  */
    float    confidence;          /* 0..1                                   */
    uint64_t sequence;            /* plays counted since start              */
    pc_features fp;               /* fingerprint of the new creative        */
} pc_event;

typedef struct {
    pc_detector_cfg cfg;
    pc_profile      profile;
    pc_triggers     trig;
    pc_state        state;
    int             have_prev, have_ref;
    pc_features     prev, ref;
    int64_t         transition_start_ms, settle_start_ms;
    int64_t         last_play_ms;        /* change_start of current play */
    int64_t         last_frame_ms;
    int             motion_emitted;      /* this transition already logged */
    int64_t         last_big_cut_ms;     /* most recent cut-sized change   */
    int64_t         last_motion_ms;      /* most recent frame with motion  */
    double          acc[3][PC_CELLS];    /* cell colour sums during motion */
    double          acc_thumb[PC_THUMB];
    float           acc_detail[PC_DETAIL];
    int             acc_n;
    int             frames_in_play, motion_frames_in_play; /* video creative? */
    uint64_t        sequence;
    float           last_cut_score;      /* exposed for UI / tuning      */
    float           last_ref_distance;
    pc_library      library;
} pc_detector;

void pc_detector_init(pc_detector *d, const pc_detector_cfg *cfg, const pc_profile *p);
void pc_detector_set_profile(pc_detector *d, const pc_profile *p);
/* Feed one analysed frame. Returns 1 and fills *ev when an event fires. */
int  pc_detector_push(pc_detector *d, const pc_features *f, int64_t now_ms, pc_event *ev);
int  pc_is_blank(const pc_detector_cfg *c, const pc_features *f, int *is_black);
const char *pc_state_name(pc_state s);

/* ------------------------------------------------------------------ */
/* Fault monitor                                                       */
/* ------------------------------------------------------------------ */

typedef enum {
    PC_FAULT_BLACK = 0,
    PC_FAULT_BLANK,
    PC_FAULT_FROZEN,
    PC_FAULT_BLANK_SLOT,     /* one-shot */
    PC_FAULT_LOOP_CHANGED,
    PC_FAULT_MISSING_CREATIVE,
    PC_FAULT_PARTIAL,
    PC_FAULT_BLURRED,        /* sharpness collapse: moved / fogged / defocused */
    PC_FAULT_COUNT
} pc_fault_type;

typedef struct {
    int black_ms, blank_ms, partial_ms;  /* persistence before raising   */
    int frozen_default_ms;               /* used when no profile is set  */
    float blur_ratio;                    /* play sharpness vs first sighting */
    int blur_window;                     /* judge over the last N plays (6)  */
} pc_faults_cfg;

void pc_faults_cfg_default(pc_faults_cfg *c);

typedef struct {
    pc_fault_type type;
    int      raised;          /* 1 raised, 0 cleared                  */
    int      one_shot;        /* blank slot: no clear will follow      */
    int64_t  since_ms, at_ms;
    char     detail[160];
} pc_fault_event;

#define PC_FAULT_QUEUE 16
#define PC_PLAY_HISTORY 256

typedef struct {
    pc_faults_cfg   cfg;
    pc_detector_cfg dcfg;
    pc_triggers     trig;
    pc_profile      profile;
    int             active[PC_FAULT_COUNT];
    int64_t         cond_since[PC_FAULT_COUNT];   /* condition first true */
    int64_t         raised_at[PC_FAULT_COUNT];
    int64_t         blank_since;                   /* current blank run    */
    int64_t         cell_dark_since[PC_CELLS];
    int             cell_dark_plays[PC_CELLS];  /* plays seen while dark */
    int64_t         last_play_ms, start_ms, last_loop_check_ms;
    int             loop_bad_checks;
    int             expected_creatives;             /* 0 = work it out from the loop */
    int             cur_run_slots;                  /* usual run of the creative on screen */
    struct { int64_t t; int id; int blank; } plays[PC_PLAY_HISTORY];
    int             play_head, play_count;
    struct { int id; float sharp; } sharp_ref[64];
    int             sharp_ref_n;
    uint8_t         blur_hist[32];
    int             blur_head, blur_n;
    int             operating;                      /* inside operating hours */
    pc_fault_event  q[PC_FAULT_QUEUE];
    int             qn;
} pc_faults;

void pc_faults_init(pc_faults *m, const pc_faults_cfg *cfg,
                    const pc_detector_cfg *dcfg, const pc_profile *p);
void pc_faults_set_profile(pc_faults *m, const pc_profile *p);
void pc_faults_set_operating(pc_faults *m, int operating, int64_t now_ms);
/* Distinct creatives the loop should hold (e.g. booked campaigns); 0 = auto. */
void pc_faults_set_expected_creatives(pc_faults *m, int n);
/* Call for every frame; pass the detector event if one fired, else NULL.
 * Returns number of queued fault events (read with pc_faults_pop). */
int  pc_faults_push(pc_faults *m, const pc_features *f, const pc_event *ev,
                    int64_t now_ms);
int  pc_faults_pop(pc_faults *m, pc_fault_event *out);
const char *pc_fault_name(pc_fault_type t);

/* ------------------------------------------------------------------ */
/* Share-of-time accounting (runs.c)                                   */
/* ------------------------------------------------------------------ */

typedef struct { int64_t t; int id; } pc_play_ref;   /* a play: start time, creative */
#define PC_RUNS_MAX_IDS 256

/* Usual run length (whole slots) of each creative in plays p[0..n) (oldest
 * first): the most common rounded duration of its completed plays. Fills
 * ids/typ, returns how many creatives. */
int    pc_runs_typical(const pc_play_ref *p, int n, int slot_ms, int slot_count,
                       int *ids, int *typ, int max_ids);
/* Slots credited over [from, to): each play earns its duration in whole slots,
 * capped at its creative's usual run length; plays crossing the window edges
 * earn the overlapping share. The last play runs until `to`. Optionally adds
 * each creative's share to slots_by_id[j] for ids_in[j]. */
double pc_runs_credited(const pc_play_ref *p, int n, int64_t from, int64_t to,
                        int slot_ms, int slot_count, double *slots_by_id, const int *ids_in, int nids_in);

/* ------------------------------------------------------------------ */
/* Snapshot quality scoring                                            */
/* ------------------------------------------------------------------ */

typedef struct {
    float sharpness;      /* Laplacian variance of the board              */
    float clipped;        /* fraction of blown highlights                 */
    float banding;        /* LED refresh banding, from pc_burst_banding() */
    int   hash_distance;  /* to the settled creative fingerprint          */
    float grid_distance;
    float score;          /* higher is better                             */
    int   usable;         /* passes all hard checks                       */
    char  reason[96];     /* why not usable                               */
} pc_snapshot_quality;

/* Warps the full-resolution frame (384 px wide at the board's aspect) and
 * scores sharpness, clipping and match against the creative fingerprint
 * `ref` (NULL to skip, e.g. video creatives). If board_y is non-NULL it
 * receives the malloc'd warped luma (board_w x board_h) for burst checks. */
int pc_snapshot_score(const pc_frame *f, const pc_point corners[4], float inset_pct,
                      const pc_features *ref, pc_snapshot_quality *q,
                      uint8_t **board_y, int *board_w, int *board_h);

/* LED refresh banding across a burst of the same static creative: each
 * frame's difference from the burst median, row energy vs column energy.
 * Static content cancels out, rolling bands don't. Needs n >= 3. */
void pc_burst_banding(uint8_t *const ys[], int n, int w, int h, float *banding_out);
/* Fold a banding ratio into the quality verdict and score. */
void pc_snapshot_apply_banding(pc_snapshot_quality *q, float banding);

#ifdef __cplusplus
}
#endif
#endif /* POPCORE_H */
