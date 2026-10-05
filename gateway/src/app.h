/* Shared gateway state. */
#ifndef POP_APP_H
#define POP_APP_H

#include "config.h"
#include "popcore.h"
#include <pthread.h>
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
    int      play_head;
    int      fault_active[PC_FAULT_COUNT];
    int      stream_lost, camera_moved;
    pc_profile profile;
    int      expected_per_hour;
    pc_point roi[4];                 /* live region (editable at runtime) */
    float    inset_pct;
    unsigned roi_version;            /* bumped on every change            */
} cam_status;

typedef struct {
    gateway_cfg  *cfg;
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

/* plays in the last hour for a camera (call with status mutex held) */
static inline int status_plays_last_hour(const cam_status *s, int64_t now_wall)
{
    int n = 0;
    for (int i = 0; i < PLAY_RING; i++)
        if (s->play_ring[i] && now_wall - s->play_ring[i] <= 3600000) n++;
    return n;
}

#endif
