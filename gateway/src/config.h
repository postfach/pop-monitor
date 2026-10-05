#ifndef POP_CONFIG_H
#define POP_CONFIG_H

#include "popcore.h"

#define MAX_DAYPARTS 8

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
    int  first_per_hour;    /* image policy: 0 = every play        */
} snapshot_cfg;

typedef struct {
    char site_id[64], name[64], model[32];
    char host[128], username[64], password[128];
    char rtsp_url[1024];        /* full URL; built from host if empty  */
    char vapix_url[256];        /* base URL; http://host if empty      */
    int   http_port;            /* camera web port (0 = 80, or 443 with https) */
    int   rtsp_port;            /* camera RTSP port (0 = 554)          */
    int   use_https;            /* VAPIX over HTTPS                    */
    int   camera_tls_insecure;  /* accept the camera's self-signed cert */
    char  rtsp_transport[8];    /* "tcp" (default) or "http" (RTSP tunnelled over the web port) */
    char  stream_params[160];   /* Axis media.amp query string         */
    pc_point roi[4];
    float inset_pct;
    int   analysis_fps;
    pc_profile profile;
    daypart dayparts[MAX_DAYPARTS];
    int   n_dayparts;
    char  operating_hours[24];
    int   ptz_check;            /* compare PTZ position to baseline    */
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
    char state_file[600];       /* region edits made at runtime, kept across restarts */
    camera_cfg *cameras;
    int  n_cameras;
} gateway_cfg;

int  config_load(const char *path, gateway_cfg *g, char *err, size_t errlen);
void config_free(gateway_cfg *g);
/* Profile in force now for this camera (dayparts, else default). */
pc_profile config_active_profile(const camera_cfg *c, int64_t epoch_ms);

#endif
