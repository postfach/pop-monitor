#ifndef POP_CONFIG_H
#define POP_CONFIG_H

#include "popcore.h"

#define MAX_DAYPARTS 8
#define MAX_CAMERAS 256

typedef struct {
    char       from[8], to[8];
    pc_profile profile;
} daypart;

typedef struct {
    int  delay_ms;          /* after the creative settles          */
    int  burst;             /* frames to capture                   */
    int  interval_ms;       /* between burst frames                */
    int  compression;       /* VAPIX compression 0..100, lower = better */
    char resolution[24];    /* VAPIX resolution, "" = camera default (max) */
    int  roi_width;         /* straightened ROI image width        */
    int  roi_quality;       /* JPEG quality for the ROI image      */
    int  photo_every_min;   /* photos of a creative at most this often (0 = every play) */
} snapshot_cfg;

typedef struct {
    int  active;                /* slot in use (cameras can be added/removed live) */
    char site_id[64], name[64], model[32];   /* name = display name (site ID if unset) */
    char host[128], username[64], password[128];
    char rtsp_url[1024];        /* full URL; built from host if empty  */
    char vapix_url[256];        /* base URL; http://host if empty      */
    int   http_port;            /* camera web port (0 = 80, or 443 with https) */
    int   rtsp_port;            /* camera RTSP port (0 = 554)          */
    int   use_https;            /* VAPIX over HTTPS                    */
    int   camera_tls_insecure;  /* accept the camera's self-signed cert */
    char  rtsp_transport[8];    /* "tcp" (default) or "http" (RTSP tunnelled over the web port) */
    char  stream_params[160];   /* Axis media.amp query string         */
    pc_region region;           /* the screen's outline: corners, shape (curve, fold) */
    float inset_pct;
    int   board_width;          /* the screen's resolution in pixels (0 = unknown): the  */
    int   board_height;         /* straightened PoP image takes its shape, not the view's */
    int   analysis_fps;
    pc_profile profile;
    daypart dayparts[MAX_DAYPARTS];
    int   n_dayparts;
    char  operating_hours[24];
    int   ptz_check;            /* compare PTZ position to baseline    */
    int   coverage_min_pct;     /* low_coverage alert below this (0 = off) */
    int   coverage_window_min;  /* ...measured over this many minutes  */
    char  vault_screen[96];     /* screen name in Vault (bookings)      */
    int   expected_creatives;   /* distinct creatives booked (0 = auto) */
    int   setup;                /* being set up (region not final): detection alerts
                                   aren't sent to Teams/Slack; the receiver doesn't count it */
    float ptz_tolerance;        /* degrees / zoom units                */
    pc_detector_cfg det;
    pc_faults_cfg   faults;
    snapshot_cfg    snap;
    int   realtime, loop;       /* testing with files                  */
} camera_cfg;

typedef struct {
    char gateway_id[64];
    char server_url[256];
    char api_key[256];
    char hmac_key[256];
    char queue_dir[512];
    long queue_max_mb;
    int  heartbeat_s;
    int  tls_insecure;
    char teams_webhook[1024];
    char slack_webhook[1024];
    int  alert_cooldown_s;
    int  log_level;
    int  control_port;          /* live-settings API for the review page (0 = off) */
    char state_file[600];       /* legacy region edits (migrated into cameras_file) */
    char cameras_file[600];     /* camera list managed from the review page */
    char state_dir[520];        /* where the gateway keeps its own files     */
    int  cameras_from_state;    /* 1 if cameras came from cameras_file      */
    void *defaults_json;        /* cJSON: "defaults" from config.json       */
    void *cam_json[MAX_CAMERAS];/* cJSON: each camera's own settings        */
    camera_cfg *cameras;        /* MAX_CAMERAS slots; check .active         */
    int  n_cameras;             /* slots used (high-water mark)             */
} gateway_cfg;

int  config_load(const char *path, gateway_cfg *g, char *err, size_t errlen);
/* Build a camera from defaults + its own JSON object (cJSON*); validates. */
int  config_build_camera(const gateway_cfg *g, const void *obj, camera_cfg *out, char *err, size_t errlen);
/* Write the active cameras' JSON objects to cameras_file. */
int  config_save_cameras(const gateway_cfg *g);
/* Slot index of the active camera with this site ID, or -1. */
int  config_find_camera(const gateway_cfg *g, const char *site_id);
int  config_active_count(const gateway_cfg *g);
/* File holding a site's creative library (keyed by site ID). */
void config_library_path(const gateway_cfg *g, const char *site_id, char *out, size_t n);
/* A restore staged in <state>/restore-staged/ (by the receiver's Restore): put in place
 * before anything is loaded; what it replaces is kept in <state>/before-restore/. */
int  config_apply_staged_restore(const char *state_dir);
void config_free(gateway_cfg *g);
/* A camera object's region: "roi" (4 corners, required), "roi_shape"
 * ("standard", "curve", "fold"), "roi_curve" (curve: 2 handles per edge,
 * see pc_region; an earlier "roi_mid", one per edge, is converted),
 * "roi_fold" (fold: top and bottom of the fold line), "fold_pct" (share of
 * the width left of the fold). 0, or -1 without valid corners. */
int  config_parse_region(const void *obj, pc_region *r);
/* Write a region into a camera object (or status JSON) in that form. */
void config_region_to_json(void *obj, const pc_region *r);
/* Corners in 0..1 forming a convex box; a fold's faces convex too. */
int  config_region_valid(const pc_region *r, char *why, size_t n);
/* Profile in force now for this camera (dayparts, else default). */
pc_profile config_active_profile(const camera_cfg *c, int64_t epoch_ms);

#endif
