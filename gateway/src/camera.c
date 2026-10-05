/* One worker per camera:
 *   stream thread   RTSP -> ROI warp -> detector + fault monitor
 *   snapshot thread on each new play, burst-capture full-res JPEGs over
 *                   VAPIX, keep the best, queue the PoP record; PTZ checks */
#include "camera.h"
#include "alerts.h"
#include "jpeg.h"
#include "queue.h"
#include "stream.h"
#include "util.h"
#include "vapix.h"

#include <cjson/cJSON.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define MAX_CAMS 1024
#define JOBQ 4
#define IMGMAP 512

typedef struct {
    pc_event   ev;
    int64_t    settled_mono;
    int64_t    start_wall, settled_wall;
    pc_profile profile;
} snap_job;

typedef struct {
    app_ctx    *app;
    int         idx;
    camera_cfg *cfg;
    cam_status *st;
    pthread_t   stream_thr, snap_thr;
    pthread_mutex_t mu;
    pthread_cond_t  cv;
    snap_job    jobs[JOBQ];
    int         jh, jn;
    struct { int id; int64_t last; } imgmap[IMGMAP];
    float       pan0, tilt0, zoom0;
    int         ptz_baseline, ptz_fail, ptz_unsupported;
} camera;

static camera *cams[MAX_CAMS];
static int ncams;

static int64_t mono_to_wall(int64_t mono)
{
    return mono + (util_wall_ms() - util_mono_ms());
}

/* Straightened ROI of the current analysis frame as a small JPEG, for alerts. */
static void alert_roi_jpeg(const camera_cfg *c, const pc_frame *f, uint8_t **jpg, size_t *len)
{
    *jpg = NULL;
    *len = 0;
    if (!f) return;
    pc_warp w;
    int ow = 480, oh = 240;
    if (pc_warp_init(&w, f->width, f->height, c->roi, c->inset_pct, ow, oh) != 0) return;
    uint8_t *rgb = malloc((size_t)ow * oh * 3);
    if (rgb) {
        pc_warp_apply_rgb(&w, f, rgb);
        jpeg_encode_rgb(rgb, ow, oh, 85, jpg, len);
        free(rgb);
    }
    pc_warp_free(&w);
}

static void status_fault(camera *cm, const char *name, int raised)
{
    pthread_mutex_lock(&cm->st->mu);
    if (!strcmp(name, "stream_lost")) cm->st->stream_lost = raised;
    else if (!strcmp(name, "camera_moved")) cm->st->camera_moved = raised;
    else
        for (int k = 0; k < PC_FAULT_COUNT; k++)
            if (!strcmp(name, pc_fault_name(k))) cm->st->fault_active[k] = raised;
    pthread_mutex_unlock(&cm->st->mu);
}

/* Board aspect from the ROI corners, for warp output sizes. */
static int roi_height_for(const camera_cfg *c, int fw, int fh, int ow)
{
    float tw = hypotf((c->roi[1].x - c->roi[0].x) * fw, (c->roi[1].y - c->roi[0].y) * fh);
    float bw = hypotf((c->roi[2].x - c->roi[3].x) * fw, (c->roi[2].y - c->roi[3].y) * fh);
    float lh = hypotf((c->roi[3].x - c->roi[0].x) * fw, (c->roi[3].y - c->roi[0].y) * fh);
    float rh = hypotf((c->roi[2].x - c->roi[1].x) * fw, (c->roi[2].y - c->roi[1].y) * fh);
    float w = (tw + bw) / 2, h = (lh + rh) / 2;
    int oh = w > 1 ? (int)lroundf(ow * h / w) : ow / 2;
    if (oh < ow / 8) oh = ow / 8;
    if (oh > ow * 2) oh = ow * 2;
    return oh & ~1;
}

/* ---------------- snapshot thread ---------------- */

static int image_due(camera *cm, int creative_id, int64_t now)
{
    if (!cm->cfg->snap.first_per_hour) return 1;
    int slot = -1;
    for (int i = 0; i < IMGMAP; i++) {
        if (cm->imgmap[i].id == creative_id) { slot = i; break; }
        if (slot < 0 && cm->imgmap[i].id == 0) slot = i;
    }
    if (slot < 0) slot = creative_id % IMGMAP;
    if (cm->imgmap[slot].id == creative_id && now - cm->imgmap[slot].last < 3600000) return 0;
    cm->imgmap[slot].id = creative_id;
    cm->imgmap[slot].last = now;
    return 1;
}

static void capture_and_queue(camera *cm, vapix *vx, const snap_job *job)
{
    const camera_cfg *c = cm->cfg;
    const gateway_cfg *g = cm->app->cfg;
    int64_t due = job->settled_mono + c->snap.delay_ms;
    int64_t now = util_mono_ms();
    if (due > now) util_sleep_ms((int)(due - now));

    char query[160];
    snprintf(query, sizeof query, "compression=%d%s%s", c->snap.compression,
             *c->snap.resolution ? "&resolution=" : "", c->snap.resolution);

    int want_images = image_due(cm, job->ev.creative_id, util_wall_ms());
    const pc_features *ref = (job->ev.is_blank || job->ev.motion) ? NULL : &job->ev.fp;

    /* burst capture */
    int n = want_images ? c->snap.burst : 0;
    uint8_t *jpgs[8] = {0}, *rgbs[8] = {0}, *ys[8] = {0};
    size_t lens[8] = {0};
    int ws[8], hs[8], bw = 0, bh = 0, got = 0;
    int64_t walls[8];
    pc_snapshot_quality qs[8];
    for (int i = 0; i < n && cm->app->running; i++) {
        if (i) util_sleep_ms(c->snap.interval_ms);
        uint8_t *jpg, *rgb;
        size_t len;
        int w, h;
        int64_t t = util_wall_ms();
        if (vapix_jpeg(vx, query, &jpg, &len) != 0) continue;
        if (jpeg_decode_rgb(jpg, len, &rgb, &w, &h) != 0) { free(jpg); continue; }
        if (got && (w != ws[0] || h != hs[0])) { free(jpg); free(rgb); continue; }
        pc_frame f = {PC_FMT_RGB24, w, h, {rgb, NULL, NULL}, {w * 3, 0, 0}};
        if (pc_snapshot_score(&f, c->roi, c->inset_pct, ref, &qs[got], &ys[got], &bw, &bh) != 0) {
            free(jpg); free(rgb);
            continue;
        }
        jpgs[got] = jpg; lens[got] = len; rgbs[got] = rgb;
        ws[got] = w; hs[got] = h; walls[got] = t;
        got++;
    }
    /* banding needs the whole burst: static content cancels, rolling bands don't */
    if (got >= 3 && !job->ev.motion) {
        float band[8];
        pc_burst_banding(ys, got, bw, bh, band);
        for (int i = 0; i < got; i++) pc_snapshot_apply_banding(&qs[i], band[i]);
    }
    int best_idx = -1;
    for (int i = 0; i < got; i++) {
        LOGD(c->name, "burst %d: score %.2f sharp %.0f clip %.3f band %.2f hd %d %s", i, qs[i].score,
             qs[i].sharpness, qs[i].clipped, qs[i].banding, qs[i].hash_distance,
             qs[i].usable ? "ok" : qs[i].reason);
        if (best_idx < 0 || qs[i].score > qs[best_idx].score) best_idx = i;
    }
    uint8_t *best_jpg = NULL, *best_rgb = NULL;
    size_t best_len = 0;
    int best_w = 0, best_h = 0;
    int64_t best_wall = 0;
    pc_snapshot_quality best_q;
    memset(&best_q, 0, sizeof best_q);
    for (int i = 0; i < got; i++) {
        free(ys[i]);
        if (i == best_idx) {
            best_jpg = jpgs[i]; best_len = lens[i]; best_rgb = rgbs[i];
            best_w = ws[i]; best_h = hs[i]; best_wall = walls[i]; best_q = qs[i];
        } else {
            free(jpgs[i]);
            free(rgbs[i]);
        }
    }

    uint8_t *roi_jpg = NULL;
    size_t roi_len = 0;
    if (best_rgb) {
        int ow = c->snap.roi_width > 64 ? c->snap.roi_width : 1024;
        int oh = roi_height_for(c, best_w, best_h, ow);
        pc_warp w;
        if (pc_warp_init(&w, best_w, best_h, c->roi, c->inset_pct, ow, oh) == 0) {
            uint8_t *rgb = malloc((size_t)ow * oh * 3);
            pc_frame f = {PC_FMT_RGB24, best_w, best_h, {best_rgb, NULL, NULL}, {best_w * 3, 0, 0}};
            pc_warp_apply_rgb(&w, &f, rgb);
            jpeg_encode_rgb(rgb, ow, oh, c->snap.roi_quality, &roi_jpg, &roi_len);
            free(rgb);
            pc_warp_free(&w);
        }
    }

    /* metadata */
    char id[37], ts[32], hash[17], sha[65];
    util_uuid4(id);
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "event_id", id);
    cJSON_AddStringToObject(j, "type", "creative_change");
    cJSON_AddStringToObject(j, "site_id", c->site_id);
    cJSON_AddStringToObject(j, "camera", c->name);
    cJSON_AddStringToObject(j, "camera_model", c->model);
    cJSON_AddStringToObject(j, "gateway_id", g->gateway_id);
    util_iso8601(job->start_wall, ts, sizeof ts);
    cJSON_AddStringToObject(j, "play_started_at", ts);
    util_iso8601(job->settled_wall, ts, sizeof ts);
    cJSON_AddStringToObject(j, "settled_at", ts);
    if (best_wall) {
        util_iso8601(best_wall, ts, sizeof ts);
        cJSON_AddStringToObject(j, "captured_at", ts);
    }
    cJSON_AddBoolToObject(j, "ntp_synced", util_clock_synced());
    cJSON_AddNumberToObject(j, "sequence", (double)job->ev.sequence);
    char cid[16];
    snprintf(cid, sizeof cid, "c-%04d", job->ev.creative_id);
    cJSON_AddStringToObject(j, "local_creative_id", cid);
    snprintf(hash, sizeof hash, "%016llx", (unsigned long long)job->ev.fp.dhash);
    cJSON_AddStringToObject(j, "phash", hash);
    cJSON_AddNumberToObject(j, "previous_creative_ms", (double)job->ev.previous_play_ms);
    cJSON_AddNumberToObject(j, "confidence", round((double)job->ev.confidence * 100) / 100);
    cJSON_AddBoolToObject(j, "is_blank", job->ev.is_blank);
    cJSON_AddBoolToObject(j, "video_creative", job->ev.motion);
    cJSON *roi = cJSON_AddArrayToObject(j, "roi");
    for (int i = 0; i < 4; i++) {
        cJSON *pt = cJSON_CreateArray();
        cJSON_AddItemToArray(pt, cJSON_CreateNumber(round((double)c->roi[i].x * 10000) / 10000));
        cJSON_AddItemToArray(pt, cJSON_CreateNumber(round((double)c->roi[i].y * 10000) / 10000));
        cJSON_AddItemToArray(roi, pt);
    }
    cJSON *prof = cJSON_AddObjectToObject(j, "profile");
    cJSON_AddNumberToObject(prof, "slot_count", job->profile.slot_count);
    cJSON_AddNumberToObject(prof, "slot_ms", job->profile.slot_ms);
    if (!want_images) {
        cJSON_AddStringToObject(j, "images", "skipped_by_policy");
    } else if (!best_jpg) {
        cJSON_AddStringToObject(j, "images", "capture_failed");
    } else {
        cJSON *qj = cJSON_AddObjectToObject(j, "quality");
        cJSON_AddNumberToObject(qj, "sharpness", roundf(best_q.sharpness));
        cJSON_AddNumberToObject(qj, "clipped", round((double)best_q.clipped * 1000) / 1000);
        cJSON_AddNumberToObject(qj, "banding", round((double)best_q.banding * 100) / 100);
        cJSON_AddBoolToObject(qj, "usable", best_q.usable);
        if (!best_q.usable) cJSON_AddStringToObject(qj, "reason", best_q.reason);
        cJSON_AddNumberToObject(qj, "burst_index", best_idx);
        cJSON_AddNumberToObject(qj, "burst_captured", got);
        cJSON_AddNumberToObject(j, "frame_width", best_w);
        cJSON_AddNumberToObject(j, "frame_height", best_h);
        sha256_hex(best_jpg, best_len, sha);
        cJSON_AddStringToObject(j, "frame_sha256", sha);
        if (roi_jpg) {
            sha256_hex(roi_jpg, roi_len, sha);
            cJSON_AddStringToObject(j, "roi_sha256", sha);
        }
    }
    cJSON_AddStringToObject(j, "app_version", PC_VERSION);
    char *json = cJSON_PrintUnformatted(j);
    cJSON_Delete(j);

    if (queue_put(g->queue_dir, QPRIO_POP, json, best_jpg, best_len, roi_jpg, roi_len, NULL, 0) != 0)
        LOGE(c->name, "failed to queue PoP record");
    else
        LOGI(c->name, "PoP #%llu %s %s%s quality=%s (%d/%d frames)",
             (unsigned long long)job->ev.sequence, cid, job->ev.is_blank ? "[blank] " : "",
             job->ev.motion ? "[video] " : "", best_jpg ? (best_q.usable ? "ok" : best_q.reason) : "none",
             got, want_images ? c->snap.burst : 0);
    free(json);

    if (want_images && !best_jpg) {
        int64_t t = util_wall_ms();
        alerts_send(cm->app, c, "snapshot_failed", "occurred", t, t,
                    "could not fetch a JPEG from the camera for a PoP record", NULL, 0);
    } else if (best_jpg && !best_q.usable && !job->ev.is_blank) {
        int64_t t = util_wall_ms();
        alerts_send(cm->app, c, "poor_pop_image", "occurred", t, t, best_q.reason, roi_jpg, roi_len);
    }
    free(best_jpg);
    free(best_rgb);
    free(roi_jpg);
}

static void ptz_check(camera *cm, vapix *vx)
{
    const camera_cfg *c = cm->cfg;
    if (!c->ptz_check || cm->ptz_unsupported) return;
    float p, t, z;
    if (vapix_ptz_position(vx, &p, &t, &z) != 0) {
        if (!cm->ptz_baseline && ++cm->ptz_fail >= 3) {
            cm->ptz_unsupported = 1;
            LOGI(c->name, "no PTZ position available: position check disabled (fixed camera?)");
        }
        return;
    }
    cm->ptz_fail = 0;
    if (!cm->ptz_baseline) {
        cm->pan0 = p; cm->tilt0 = t; cm->zoom0 = z;
        cm->ptz_baseline = 1;
        LOGI(c->name, "PTZ baseline pan=%.2f tilt=%.2f zoom=%.0f", p, t, z);
        return;
    }
    float dz = fabsf(z - cm->zoom0) / (cm->zoom0 > 1 ? cm->zoom0 : 1);
    int moved = fabsf(p - cm->pan0) > c->ptz_tolerance || fabsf(t - cm->tilt0) > c->ptz_tolerance || dz > 0.02f;
    pthread_mutex_lock(&cm->st->mu);
    int was = cm->st->camera_moved;
    cm->st->paused = moved;
    pthread_mutex_unlock(&cm->st->mu);
    if (moved != was) {
        char d[160];
        snprintf(d, sizeof d, "PTZ at pan=%.2f tilt=%.2f zoom=%.0f, preset was pan=%.2f tilt=%.2f zoom=%.0f%s",
                 p, t, z, cm->pan0, cm->tilt0, cm->zoom0, moved ? "; detection paused" : "");
        int64_t now = util_wall_ms();
        status_fault(cm, "camera_moved", moved);
        alerts_send(cm->app, c, "camera_moved", moved ? "raised" : "cleared", now, now, d, NULL, 0);
    }
}

static void *snapshot_thread(void *arg)
{
    camera *cm = arg;
    vapix vx;
    if (vapix_init(&vx, cm->cfg->vapix_url, cm->cfg->username, cm->cfg->password,
                   cm->cfg->camera_tls_insecure || cm->app->cfg->tls_insecure) != 0) {
        LOGE(cm->cfg->name, "curl init failed");
        return NULL;
    }
    int64_t next_ptz = util_mono_ms() + 2000;
    while (cm->app->running) {
        snap_job job;
        int have = 0;
        pthread_mutex_lock(&cm->mu);
        if (cm->jn == 0) {
            struct timespec ts;
            clock_gettime(CLOCK_REALTIME, &ts);
            ts.tv_sec += 1;
            pthread_cond_timedwait(&cm->cv, &cm->mu, &ts);
        }
        if (cm->jn > 0) {
            job = cm->jobs[cm->jh];
            cm->jh = (cm->jh + 1) % JOBQ;
            cm->jn--;
            have = 1;
        }
        pthread_mutex_unlock(&cm->mu);
        if (have) capture_and_queue(cm, &vx, &job);
        if (util_mono_ms() >= next_ptz) {
            ptz_check(cm, &vx);
            next_ptz = util_mono_ms() + 60000;
        }
    }
    vapix_free(&vx);
    return NULL;
}

/* ---------------- stream thread ---------------- */

static void push_job(camera *cm, const pc_event *ev, int64_t now_mono, pc_profile prof)
{
    pthread_mutex_lock(&cm->mu);
    if (cm->jn == JOBQ) {
        LOGW(cm->cfg->name, "snapshot queue full; dropping oldest pending capture");
        cm->jh = (cm->jh + 1) % JOBQ;
        cm->jn--;
    }
    snap_job *j = &cm->jobs[(cm->jh + cm->jn) % JOBQ];
    j->ev = *ev;
    j->settled_mono = now_mono;
    j->start_wall = mono_to_wall(ev->change_start_ms);
    j->settled_wall = mono_to_wall(ev->settled_ms);
    j->profile = prof;
    cm->jn++;
    pthread_cond_signal(&cm->cv);
    pthread_mutex_unlock(&cm->mu);
}

static void *stream_thread(void *arg)
{
    camera *cm = arg;
    camera_cfg *c = cm->cfg;
    cam_status *st = cm->st;

    pc_profile prof = config_active_profile(c, util_wall_ms());
    pc_detector det;
    pc_faults fm;
    pc_detector_init(&det, &c->det, &prof);
    pc_faults_init(&fm, &c->faults, &c->det, &prof);

    pc_warp warp = {0};
    int ow = 192, oh = 96;
    uint8_t *py = NULL, *pu = NULL, *pv = NULL;
    int64_t lost_since = 0, last_housekeep = 0, last_analysed = 0;
    int backoff = 2000, frames_in_window = 0;
    int64_t fps_window_start = util_mono_ms();

    while (cm->app->running) {
        char err[256] = "";
        pop_stream_opts so = {c->realtime, c->loop, 15, c->rtsp_transport};
        pop_stream *s = pop_stream_open(c->rtsp_url, &so, err, sizeof err);
        if (!s) {
            int64_t now = util_mono_ms();
            if (!lost_since) lost_since = now;
            pthread_mutex_lock(&st->mu);
            st->connected = 0;
            int lost = st->stream_lost;
            pthread_mutex_unlock(&st->mu);
            LOGW(c->name, "cannot open stream: %s (retry in %d s)", err, backoff / 1000);
            if (!lost && now - lost_since > 60000) {
                int64_t w = util_wall_ms();
                status_fault(cm, "stream_lost", 1);
                alerts_send(cm->app, c, "stream_lost", "raised", mono_to_wall(lost_since), w,
                            "no video from the camera for over 60 s", NULL, 0);
            }
            for (int t = 0; t < backoff && cm->app->running; t += 250) util_sleep_ms(250);
            backoff = backoff < 30000 ? backoff * 2 : 30000;
            continue;
        }
        LOGI(c->name, "stream connected");
        backoff = 2000;
        pthread_mutex_lock(&st->mu);
        st->connected = 1;
        int was_lost = st->stream_lost;
        pthread_mutex_unlock(&st->mu);
        if (was_lost) {
            int64_t w = util_wall_ms();
            status_fault(cm, "stream_lost", 0);
            alerts_send(cm->app, c, "stream_lost", "cleared", mono_to_wall(lost_since), w, "video restored", NULL, 0);
        }
        lost_since = 0;

        pc_frame f;
        int64_t pts;
        int rc;
        while (cm->app->running && (rc = pop_stream_read(s, &f, &pts)) == 1) {
            int64_t now = util_mono_ms();

            /* housekeeping every 30 s: operating hours, daypart profile */
            if (now - last_housekeep > 30000 || !last_housekeep) {
                int64_t w = util_wall_ms();
                int on = util_in_time_range(c->operating_hours, w);
                pc_faults_set_operating(&fm, on, now);
                pc_profile p = config_active_profile(c, w);
                if (p.slot_count != prof.slot_count || p.slot_ms != prof.slot_ms) {
                    LOGI(c->name, "rotation profile now %d x %d ms", p.slot_count, p.slot_ms);
                    prof = p;
                    pc_detector_set_profile(&det, &prof);
                    pc_faults_set_profile(&fm, &prof);
                }
                pc_triggers tr;
                pc_profile_triggers(&prof, &tr);
                pthread_mutex_lock(&st->mu);
                st->operating = on;
                st->profile = prof;
                st->expected_per_hour = tr.expected_per_hour;
                pthread_mutex_unlock(&st->mu);
                last_housekeep = now;
            }

            if (now - last_analysed < 1000 / c->analysis_fps - 5) continue;
            last_analysed = now;

            pthread_mutex_lock(&st->mu);
            int paused = st->paused;
            st->last_frame_wall = util_wall_ms();
            pthread_mutex_unlock(&st->mu);
            if (paused) continue;

            if (!warp.lut || warp.src_w != f.width || warp.src_h != f.height) {
                pc_warp_free(&warp);
                oh = roi_height_for(c, f.width, f.height, ow);
                if (pc_warp_init(&warp, f.width, f.height, c->roi, c->inset_pct, ow, oh) != 0) {
                    LOGE(c->name, "invalid ROI");
                    break;
                }
                free(py); free(pu); free(pv);
                py = malloc((size_t)ow * oh); pu = malloc((size_t)ow * oh); pv = malloc((size_t)ow * oh);
                LOGI(c->name, "analysing %dx%d stream, board %dx%d", f.width, f.height, ow, oh);
            }
            pc_warp_apply(&warp, &f, py, pu, pv);
            pc_features fe;
            pc_features_compute(py, pu, pv, ow, oh, &fe);
            pc_event ev;
            int fired = pc_detector_push(&det, &fe, now, &ev);
            if (fired) {
                push_job(cm, &ev, now, prof);
                int64_t w = mono_to_wall(ev.change_start_ms);
                pthread_mutex_lock(&st->mu);
                st->last_play_wall = w;
                st->plays_total++;
                st->play_ring[st->play_head] = w;
                st->play_head = (st->play_head + 1) % PLAY_RING;
                pthread_mutex_unlock(&st->mu);
            }
            pc_faults_push(&fm, &fe, fired ? &ev : NULL, now);
            pc_fault_event fev;
            while (pc_faults_pop(&fm, &fev)) {
                const char *name = pc_fault_name(fev.type);
                const char *state = fev.one_shot ? "occurred" : (fev.raised ? "raised" : "cleared");
                if (!fev.one_shot) status_fault(cm, name, fev.raised);
                uint8_t *jpg = NULL;
                size_t jl = 0;
                if (fev.raised) alert_roi_jpeg(c, &f, &jpg, &jl);
                alerts_send(cm->app, c, name, state, mono_to_wall(fev.since_ms), mono_to_wall(fev.at_ms),
                            fev.detail, jpg, jl);
                free(jpg);
            }

            frames_in_window++;
            pthread_mutex_lock(&st->mu);
            st->state = det.state;
            st->cut_score = det.last_cut_score;
            if (now - fps_window_start >= 5000) {
                st->fps = frames_in_window * 1000.f / (float)(now - fps_window_start);
                frames_in_window = 0;
                fps_window_start = now;
            }
            pthread_mutex_unlock(&st->mu);
        }
        pop_stream_close(s);
        if (!cm->app->running) break;
        LOGW(c->name, "stream ended or failed; reconnecting");
        pthread_mutex_lock(&st->mu);
        st->connected = 0;
        pthread_mutex_unlock(&st->mu);
        if (!lost_since) lost_since = util_mono_ms();
        util_sleep_ms(1000);
    }
    pc_warp_free(&warp);
    free(py); free(pu); free(pv);
    pthread_mutex_lock(&cm->mu);
    pthread_cond_signal(&cm->cv);
    pthread_mutex_unlock(&cm->mu);
    return NULL;
}

int camera_start(app_ctx *app, int idx)
{
    if (ncams >= MAX_CAMS) return -1;
    camera *cm = calloc(1, sizeof *cm);
    cm->app = app;
    cm->idx = idx;
    cm->cfg = &app->cfg->cameras[idx];
    cm->st = &app->status[idx];
    pthread_mutex_init(&cm->mu, NULL);
    pthread_cond_init(&cm->cv, NULL);
    if (pthread_create(&cm->snap_thr, NULL, snapshot_thread, cm) != 0) return -1;
    if (pthread_create(&cm->stream_thr, NULL, stream_thread, cm) != 0) return -1;
    cams[ncams++] = cm;
    return 0;
}

void camera_join_all(void)
{
    for (int i = 0; i < ncams; i++) {
        pthread_join(cams[i]->stream_thr, NULL);
        pthread_join(cams[i]->snap_thr, NULL);
        free(cams[i]);
    }
    ncams = 0;
}
