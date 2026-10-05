/* Sends queued records to the backend and posts heartbeats. */
#include "uploader.h"
#include "alerts.h"
#include "queue.h"
#include "util.h"

#include <cjson/cJSON.h>
#include <curl/curl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static pthread_t up_thr, hb_thr;
static app_ctx *A;

static char last_curl_err[100];

/* ok: 1 sent, 0 failed (will retry), 2 dead-lettered */
static void health_note(int ok, long status, const char *err)
{
    (void)status;
    gw_health *h = &A->health;
    pthread_mutex_lock(&h->mu);
    int64_t w = util_wall_ms();
    if (ok == 1) { h->sent++; h->last_ok_wall = w; h->fail_streak = 0; }
    else {
        if (ok == 2) h->dead++; else { h->retries++; h->fail_streak++; }
        h->last_fail_wall = w;
        snprintf(h->last_error, sizeof h->last_error, "%s", err ? err : "");
    }
    pthread_mutex_unlock(&h->mu);
}

static void add_time(cJSON *o, const char *k, int64_t ms)
{
    if (!ms) return;
    char ts[64];
    util_iso8601(ms, ts, sizeof ts);
    cJSON_AddStringToObject(o, k, ts);
}

cJSON *gateway_health_json(void)
{
    const gateway_cfg *g = A->cfg;
    gw_health *h = &A->health;
    int64_t now = util_wall_ms();
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "app_version", PC_VERSION);
    add_time(j, "started_at", h->start_wall);
    cJSON_AddNumberToObject(j, "uptime_s", (double)((now - h->start_wall) / 1000));
    cJSON_AddNumberToObject(j, "heartbeat_s", g->heartbeat_s);
    cJSON_AddBoolToObject(j, "ntp_synced", util_clock_synced());
    pthread_mutex_lock(&h->mu);
    cJSON *u = cJSON_AddObjectToObject(j, "uploads");
    cJSON_AddNumberToObject(u, "sent", (double)h->sent);
    cJSON_AddNumberToObject(u, "retries", (double)h->retries);
    cJSON_AddNumberToObject(u, "rejected", (double)h->dead);
    cJSON_AddNumberToObject(u, "fail_streak", h->fail_streak);
    add_time(u, "last_ok_at", h->last_ok_wall);
    add_time(u, "last_fail_at", h->last_fail_wall);
    if (h->last_fail_wall) cJSON_AddStringToObject(u, "last_error", h->last_error);
    add_time(u, "last_heartbeat_ok_at", h->hb_ok_wall);
    pthread_mutex_unlock(&h->mu);
    queue_info qi;
    queue_info_get(g->queue_dir, &qi);
    cJSON *q = cJSON_AddObjectToObject(j, "queue");
    cJSON_AddNumberToObject(q, "records", qi.records);
    cJSON_AddNumberToObject(q, "pops", qi.pops);
    cJSON_AddNumberToObject(q, "alerts", qi.alerts);
    cJSON_AddNumberToObject(q, "bytes", (double)qi.bytes);
    cJSON_AddNumberToObject(q, "max_bytes", (double)((uint64_t)g->queue_max_mb << 20));
    cJSON_AddNumberToObject(q, "dead", qi.dead);
    add_time(q, "oldest_at", qi.oldest_ms);
    if (qi.oldest_ms) cJSON_AddNumberToObject(q, "oldest_age_s", (double)((now - qi.oldest_ms) / 1000));
    cJSON *d = cJSON_AddObjectToObject(j, "disk");
    cJSON_AddNumberToObject(d, "free_bytes", (double)qi.disk_free);
    cJSON_AddNumberToObject(d, "total_bytes", (double)qi.disk_total);
    int ncam = 0, nconn = 0;
    pthread_mutex_lock(&A->cams_mu);
    for (int i = 0; i < g->n_cameras; i++) {
        if (!g->cameras[i].active) continue;
        ncam++;
        pthread_mutex_lock(&A->status[i].mu);
        nconn += A->status[i].connected;
        pthread_mutex_unlock(&A->status[i].mu);
    }
    pthread_mutex_unlock(&A->cams_mu);
    cJSON_AddNumberToObject(j, "cameras", ncam);
    cJSON_AddNumberToObject(j, "cameras_connected", nconn);
    return j;
}

static size_t sink(void *p, size_t s, size_t n, void *u) { (void)p; (void)u; return s * n; }

/* Returns HTTP status, or -1 on transport failure. */
static long post_record(CURL *c, const char *stem)
{
    const gateway_cfg *g = A->cfg;
    char jp[1200], fp[1200], rp[1200];
    queue_path(g->queue_dir, stem, ".json", jp, sizeof jp);
    queue_path(g->queue_dir, stem, ".frame.jpg", fp, sizeof fp);
    queue_path(g->queue_dir, stem, ".roi.jpg", rp, sizeof rp);
    size_t jl;
    char *json = util_read_file(jp, &jl);
    if (!json) return 0;                       /* vanished: treat as done */

    cJSON *meta = cJSON_Parse(json);
    if (!meta) { free(json); return 422; }     /* corrupt: dead-letter    */
    const cJSON *type = cJSON_GetObjectItem(meta, "type");
    const cJSON *eid = cJSON_GetObjectItem(meta, "event_id");
    const char *endpoint = (cJSON_IsString(type) && !strcmp(type->valuestring, "fault"))
                         ? "/v1/alerts" : "/v1/pop";
    char url[400], idem[300], auth[400], sig[160];
    snprintf(url, sizeof url, "%s%s", g->server_url, endpoint);
    snprintf(idem, sizeof idem, "Idempotency-Key: %s", cJSON_IsString(eid) ? eid->valuestring : stem);

    curl_easy_reset(c);
    struct curl_slist *h = NULL;
    snprintf(auth, sizeof auth, "Authorization: Bearer %s", g->api_key);
    h = curl_slist_append(h, auth);
    h = curl_slist_append(h, idem);
    if (*g->hmac_key) {
        char mac[65];
        hmac_sha256_hex(g->hmac_key, json, jl, mac);
        snprintf(sig, sizeof sig, "X-PoP-Signature: sha256=%s", mac);
        h = curl_slist_append(h, sig);
    }
    curl_mime *mime = curl_mime_init(c);
    curl_mimepart *part = curl_mime_addpart(mime);
    curl_mime_name(part, "metadata");
    curl_mime_data(part, json, jl);
    curl_mime_type(part, "application/json");
    if (access(fp, R_OK) == 0) {
        part = curl_mime_addpart(mime);
        curl_mime_name(part, "frame");
        curl_mime_filedata(part, fp);
        curl_mime_type(part, "image/jpeg");
    }
    if (access(rp, R_OK) == 0) {
        part = curl_mime_addpart(mime);
        curl_mime_name(part, "roi");
        curl_mime_filedata(part, rp);
        curl_mime_type(part, "image/jpeg");
    }
    curl_easy_setopt(c, CURLOPT_URL, url);
    curl_easy_setopt(c, CURLOPT_HTTPHEADER, h);
    curl_easy_setopt(c, CURLOPT_MIMEPOST, mime);
    curl_easy_setopt(c, CURLOPT_TIMEOUT, 60L);
    curl_easy_setopt(c, CURLOPT_CONNECTTIMEOUT, 10L);
    curl_easy_setopt(c, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(c, CURLOPT_WRITEFUNCTION, sink);
    if (g->tls_insecure) {
        curl_easy_setopt(c, CURLOPT_SSL_VERIFYPEER, 0L);
        curl_easy_setopt(c, CURLOPT_SSL_VERIFYHOST, 0L);
    }
    long status = -1;
    CURLcode rc = curl_easy_perform(c);
    if (rc == CURLE_OK) curl_easy_getinfo(c, CURLINFO_RESPONSE_CODE, &status);
    else {
        LOGW("upload", "%s: %s", url, curl_easy_strerror(rc));
        snprintf(last_curl_err, sizeof last_curl_err, "%s", curl_easy_strerror(rc));
    }
    curl_mime_free(mime);
    curl_slist_free_all(h);
    cJSON_Delete(meta);
    free(json);
    return status;
}

static void *upload_thread(void *arg)
{
    (void)arg;
    const gateway_cfg *g = A->cfg;
    CURL *c = curl_easy_init();
    int backoff_ms = 0;
    int64_t last_cap = 0;
    while (A->running) {
        if (util_mono_ms() - last_cap > 60000) {
            queue_enforce_cap(g->queue_dir, (uint64_t)g->queue_max_mb << 20);
            last_cap = util_mono_ms();
        }
        char stem[256];
        if (!queue_next(g->queue_dir, stem, sizeof stem)) {
            queue_wait(2000);
            continue;
        }
        long st = post_record(c, stem);
        if (st == 0 || (st >= 200 && st < 300) || st == 409) {   /* 409 = already have it */
            queue_remove(g->queue_dir, stem);
            backoff_ms = 0;
            health_note(1, st, NULL);
            LOGD("upload", "sent %s (HTTP %ld)", stem, st);
            continue;
        }
        if (st >= 400 && st < 500 && st != 401 && st != 403 && st != 408 && st != 429) {
            LOGE("upload", "server rejected %s (HTTP %ld): moved to dead/", stem, st);
            queue_dead_letter(g->queue_dir, stem);
            char e[80];
            snprintf(e, sizeof e, "server rejected a record (HTTP %ld); moved to dead/", st);
            health_note(2, st, e);
            continue;
        }
        /* server down, auth problem or network: keep the record, back off */
        backoff_ms = backoff_ms ? backoff_ms * 2 : 5000;
        if (backoff_ms > 600000) backoff_ms = 600000;
        LOGW("upload", "upload failed (HTTP %ld); retrying in %d s", st, backoff_ms / 1000);
        {
            char e[160];
            if (st < 0) snprintf(e, sizeof e, "cannot reach the server (%s)", last_curl_err);
            else snprintf(e, sizeof e, "server returned HTTP %ld", st);
            health_note(0, st, e);
        }
        for (int waited = 0; waited < backoff_ms && A->running; waited += 500) util_sleep_ms(500);
    }
    curl_easy_cleanup(c);
    return NULL;
}

static char *heartbeat_json(void)
{
    const gateway_cfg *g = A->cfg;
    int64_t now = util_wall_ms();
    char ts[64];
    util_iso8601(now, ts, sizeof ts);
    int qcount;
    uint64_t qbytes;
    queue_stats(g->queue_dir, &qcount, &qbytes);

    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "type", "heartbeat");
    cJSON_AddStringToObject(j, "gateway_id", g->gateway_id);
    cJSON_AddStringToObject(j, "app_version", PC_VERSION);
    cJSON_AddStringToObject(j, "sent_at", ts);
    cJSON_AddBoolToObject(j, "ntp_synced", util_clock_synced());
    cJSON *q = cJSON_AddObjectToObject(j, "queue");
    cJSON_AddNumberToObject(q, "records", qcount);
    cJSON_AddNumberToObject(q, "bytes", (double)qbytes);
    cJSON_AddItemToObject(j, "health", gateway_health_json());
    cJSON *cams = cJSON_AddArrayToObject(j, "cameras");
    pthread_mutex_lock(&A->cams_mu);
    for (int i = 0; i < g->n_cameras; i++) {
        if (!g->cameras[i].active) continue;
        cam_status *s = &A->status[i];
        cJSON *c = cJSON_CreateObject();
        pthread_mutex_lock(&s->mu);
        cJSON_AddStringToObject(c, "site_id", g->cameras[i].site_id);
        cJSON_AddStringToObject(c, "site_name", g->cameras[i].name);
        cJSON_AddStringToObject(c, "camera", g->cameras[i].name);
        cJSON_AddBoolToObject(c, "connected", s->connected);
        cJSON_AddBoolToObject(c, "operating", s->operating);
        cJSON_AddBoolToObject(c, "paused", s->paused);
        cJSON_AddStringToObject(c, "state", pc_state_name(s->state));
        cJSON_AddNumberToObject(c, "fps", s->fps);
        if (s->last_frame_wall) { util_iso8601(s->last_frame_wall, ts, sizeof ts); cJSON_AddStringToObject(c, "last_frame_at", ts); }
        if (s->last_play_wall) { util_iso8601(s->last_play_wall, ts, sizeof ts); cJSON_AddStringToObject(c, "last_play_at", ts); }
        int ph = status_plays_last_hour(s, now);
        cJSON_AddNumberToObject(c, "plays_last_hour", ph);
        if (s->expected_per_hour > 0) {
            cJSON_AddNumberToObject(c, "expected_per_hour", s->expected_per_hour);
            cJSON_AddNumberToObject(c, "coverage", (double)ph / s->expected_per_hour);
        }
        if (s->coverage_since_wall)
            cJSON_AddNumberToObject(c, "coverage_data_min", (int)((now - s->coverage_since_wall) / 60000));
        if (s->coverage_window_pct >= 0 && s->coverage_since_wall &&
            now - s->coverage_since_wall >= (int64_t)g->cameras[i].coverage_window_min * 60000)
            cJSON_AddNumberToObject(c, "coverage_window_pct", (int)(s->coverage_window_pct + 0.5f));
        cJSON_AddNumberToObject(c, "uptime_pct", (int)(status_uptime(s, now) * 1000 + 0.5) / 10.0);
        cJSON_AddNumberToObject(c, "reconnects", s->reconnects);
        cJSON_AddNumberToObject(c, "slot_count", s->profile.slot_count);
        cJSON_AddNumberToObject(c, "slot_ms", s->profile.slot_ms);
        cJSON *f = cJSON_AddArrayToObject(c, "active_faults");
        for (int k = 0; k < PC_FAULT_COUNT; k++)
            if (s->fault_active[k]) cJSON_AddItemToArray(f, cJSON_CreateString(pc_fault_name(k)));
        if (s->stream_lost) cJSON_AddItemToArray(f, cJSON_CreateString("stream_lost"));
        if (s->camera_moved) cJSON_AddItemToArray(f, cJSON_CreateString("camera_moved"));
        if (s->low_coverage) cJSON_AddItemToArray(f, cJSON_CreateString("low_coverage"));
        pthread_mutex_unlock(&s->mu);
        cJSON_AddItemToArray(cams, c);
    }
    pthread_mutex_unlock(&A->cams_mu);
    char *out = cJSON_PrintUnformatted(j);
    cJSON_Delete(j);
    return out;
}

static void *heartbeat_thread(void *arg)
{
    (void)arg;
    const gateway_cfg *g = A->cfg;
    char url[400];
    snprintf(url, sizeof url, "%s/v1/heartbeat", g->server_url);
    int64_t next = util_mono_ms() + 5000;      /* first one shortly after start */
    while (A->running) {
        if (util_mono_ms() >= next) {
            char *body = heartbeat_json();
            long st = http_post_json(url, body, g->api_key, g->tls_insecure, 20);
            if (st < 200 || st >= 300) LOGW("heartbeat", "heartbeat failed (HTTP %ld)", st);
            else {
                pthread_mutex_lock(&A->health.mu);
                A->health.hb_ok_wall = util_wall_ms();
                pthread_mutex_unlock(&A->health.mu);
            }
            free(body);
            next = util_mono_ms() + (int64_t)g->heartbeat_s * 1000;
        }
        util_sleep_ms(500);
    }
    return NULL;
}

int uploader_start(app_ctx *app)
{
    A = app;
    if (pthread_create(&up_thr, NULL, upload_thread, NULL) != 0) return -1;
    if (pthread_create(&hb_thr, NULL, heartbeat_thread, NULL) != 0) return -1;
    return 0;
}

void uploader_join(void)
{
    queue_wake();
    pthread_join(up_thr, NULL);
    pthread_join(hb_thr, NULL);
}
