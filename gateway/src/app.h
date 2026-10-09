/* Shared gateway state. */
#ifndef POP_APP_H
#define POP_APP_H

#include "config.h"
#include "popcore.h"
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#define PLAY_RING 2048

typedef struct {
    pthread_mutex_t mu;
    int      connected;
    int      paused;                 /* camera moved off its preset      */
    int      operating;
    pc_state state;
    float    fps;
    float    cut_score;
    int64_t  last_frame_wall, last_play_wall;
    uint64_t plays_total;
    int64_t  play_ring[PLAY_RING];   /* wall times of recent plays       */
    int      play_ring_id[PLAY_RING];/* their creative IDs               */
    int      play_head;
    int      fault_active[PC_FAULT_COUNT];
    int      stream_lost, camera_moved;
    pc_profile profile;
    int      expected_per_hour;
    pc_point roi[4];                 /* live region (editable at runtime) */
    float    inset_pct;
    unsigned roi_version;            /* bumped on every change            */
    int      low_coverage;           /* low_coverage fault active          */
    int64_t  started_wall;           /* camera (re)started                 */
    int64_t  connected_since_wall;   /* 0 while disconnected               */
    int64_t  connected_ms;           /* total connected time before connected_since */
    int      reconnects;             /* stream (re)connections after the first */
    int      expected_creatives;     /* from bookings (0 = work it out)  */
    unsigned expect_version;         /* bumped when it changes            */
    unsigned reset_version;          /* bumped to forget the site's creatives */
    int      reset_renumber;         /* ...and start IDs again at c-0001       */
    unsigned reset_applied;          /* last reset the camera thread carried out */
#define MERGEQ 64
    int      merge_from[MERGEQ], merge_into[MERGEQ];   /* creative merges waiting for the camera thread */
    int      nmerge;
    int64_t  coverage_since_wall;    /* coverage data valid from (0 = not counting) */
    float    coverage_window_pct;    /* last check over the alert window, -1 = not yet */
} cam_status;

/* Gateway-wide health counters (uploader + heartbeat). */
typedef struct {
    pthread_mutex_t mu;
    int64_t  start_wall;
    uint64_t sent, retries, dead;
    int64_t  last_ok_wall, last_fail_wall;
    int      fail_streak;            /* consecutive failed uploads          */
    char     last_error[200];
    int64_t  hb_ok_wall;             /* last heartbeat the backend accepted */
} gw_health;

typedef struct {
    gateway_cfg  *cfg;
    gw_health     health;
    volatile int  running;
    cam_status   *status;            /* MAX_CAMERAS slots, one per camera */
    pthread_mutex_t cams_mu;         /* guards the camera list (add/edit/remove) */
} app_ctx;

/* Clear a camera's status, keeping its (unlocked) mutex. Only call while
 * the camera's threads are stopped and cams_mu is held. */
static inline void status_reset(cam_status *s)
{
    pthread_mutex_t keep = s->mu;
    memset(s, 0, sizeof *s);
    s->mu = keep;
}

/* Camera connected for this share of the time since it started (mutex held). */
static inline double status_uptime(const cam_status *s, int64_t now)
{
    if (!s->started_wall || now <= s->started_wall) return 0;
    int64_t up = s->connected_ms + (s->connected_since_wall ? now - s->connected_since_wall : 0);
    return (double)up / (double)(now - s->started_wall);
}

/* copy the live region (takes the status mutex) */
static inline unsigned status_get_roi(cam_status *s, pc_point roi[4], float *inset)
{
    pthread_mutex_lock(&s->mu);
    for (int i = 0; i < 4; i++) roi[i] = s->roi[i];
    *inset = s->inset_pct;
    unsigned v = s->roi_version;
    pthread_mutex_unlock(&s->mu);
    return v;
}

/* plays in (now - window_ms, now] (call with status mutex held) */
static inline int status_plays_since(const cam_status *s, int64_t now_wall, int64_t window_ms)
{
    int n = 0;
    for (int i = 0; i < PLAY_RING; i++)
        if (s->play_ring[i] && now_wall - s->play_ring[i] <= window_ms && s->play_ring[i] <= now_wall) n++;
    return n;
}

/* Slots credited over the last window_ms: share-of-time aware (see runs.c),
 * so a booking holding several slots back to back counts in full, while a
 * missed change, hold-over or freeze does not. Status mutex held. */
static int play_ref_cmp(const void *a, const void *b)
{
    int64_t x = ((const pc_play_ref *)a)->t, y = ((const pc_play_ref *)b)->t;
    return x < y ? -1 : x > y;
}
static inline double status_slots_credited(const cam_status *s, int64_t now_wall, int64_t window_ms)
{
    int S = s->profile.slot_ms, N = s->profile.slot_count;
    if (S <= 0) return 0;
    static __thread pc_play_ref p[PLAY_RING];
    int n = 0;
    for (int i = 0; i < PLAY_RING; i++)
        if (s->play_ring[i] && s->play_ring[i] <= now_wall)
            p[n++] = (pc_play_ref){s->play_ring[i], s->play_ring_id[i]};
    qsort(p, (size_t)n, sizeof p[0], play_ref_cmp);
    return pc_runs_credited(p, n, now_wall - window_ms, now_wall, S, N, NULL, NULL, 0);
}

/* plays in the last hour for a camera (call with status mutex held) */
static inline int status_plays_last_hour(const cam_status *s, int64_t now_wall)
{
    int n = 0;
    for (int i = 0; i < PLAY_RING; i++)
        if (s->play_ring[i] && now_wall - s->play_ring[i] <= 3600000) n++;
    return n;
}

#endif
