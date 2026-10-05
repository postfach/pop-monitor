/* Shared gateway state. */
#ifndef POP_APP_H
#define POP_APP_H

#include "config.h"
#include "popcore.h"
#include <pthread.h>
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
} cam_status;

typedef struct {
    gateway_cfg  *cfg;
    volatile int  running;
    cam_status   *status;            /* one per camera                   */
} app_ctx;

/* plays in the last hour for a camera (call with status mutex held) */
static inline int status_plays_last_hour(const cam_status *s, int64_t now_wall)
{
    int n = 0;
    for (int i = 0; i < PLAY_RING; i++)
        if (s->play_ring[i] && now_wall - s->play_ring[i] <= 3600000) n++;
    return n;
}

#endif
